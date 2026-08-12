"""CRUD for workspace-defined sandbox tools (custom tool descriptors).

The only writer of the `sandbox_tools` table, and therefore the one place three
create-time invariants are enforced, because a bad row is a bad tool the model
will call for the rest of the workspace's life:

- The slug `name` cannot shadow a built-in tool. `build_registry` composes the
  custom family LAST and `dict.update`s it over the others, so a custom `name`
  equal to `run_python` would silently *replace* the real one. The reserved set
  is computed from the live registry rather than hardcoded, so it cannot drift.
- Every `{{placeholder}}` in `argv` must reference a property the `input_schema`
  declares, so a typo cannot become a silently-empty substitution at call time.
- `egress_hosts` are bare hostnames, not URLs: the sandbox allowlist matches on
  host, and a `https://…/path` entry would match nothing and quietly grant no
  egress the author thought they wrote.

Isolation mirrors mcp.py: `_load` filters on `workspace_id` so a foreign id 404s
rather than leaks, and the audit row is flushed-before-written so `resource_id`
is non-null.
"""
from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Dict, List, Set

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import SandboxTool
from ..schemas import SandboxToolOut, SandboxToolRequest, SandboxToolUpdate
from ..services.audit import record_audit
from ..services.llm_tools import ToolContext, registry_families
from ..services.sandbox.policy import ALWAYS_DENIED_CIDRS

#: policy.py's hard-denied ranges, as networks, so an egress host that is a bare
#: IP literal inside one can be refused at authoring. `egress_rules` re-denies
#: these CIDRs regardless, but allow-vs-deny precedence is the provider's; the
#: application keeps its own floor rather than depending on that ordering.
_DENIED_NETS = tuple(ipaddress.ip_network(cidr) for cidr in ALWAYS_DENIED_CIDRS)

router = APIRouter(prefix="/api/sandbox-tools", tags=["sandbox-tools"])

# A slug the model calls. Underscore allowed so names read naturally
# (fetch_url, run_report); it must not carry a separator or anything the model
# API rejects in a tool name.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,58}[a-z0-9])?$")

#: A `{{name}}` reference inside an argv token.
_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")


def _reserved_names(db: Session, actor: Actor) -> Set[str]:
    """Every tool name that already exists, so a custom slug cannot shadow one.

    Computed from `registry_families` minus the custom family itself: that is the
    exact set `build_registry` would let a colliding custom name overwrite, so
    deriving it here means the check cannot fall out of step with the registry.
    """
    context = ToolContext(
        workspace_id=actor.workspace_id, user_id=actor.user_id, conversation_id=""
    )
    reserved: Set[str] = set()
    for family, tools in registry_families(db, context):
        if family == "sandbox_tools":
            continue
        reserved.update(tools.keys())
    return reserved


def _tool_out(tool: SandboxTool) -> SandboxToolOut:
    def _loads(raw: str, fallback: Any) -> Any:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return fallback
        return value

    schema = _loads(tool.input_schema_json, {"type": "object", "properties": {}})
    argv = _loads(tool.argv_json, [])
    hosts = _loads(tool.egress_hosts_json, [])
    return SandboxToolOut(
        id=tool.id,
        name=tool.name,
        description=tool.description,
        input_schema=schema if isinstance(schema, dict) else {},
        argv=[str(token) for token in argv] if isinstance(argv, list) else [],
        egress_hosts=[str(host) for host in hosts] if isinstance(hosts, list) else [],
        approval="always" if tool.approval == "always" else "inherit",
        enabled=tool.enabled,
        created_at=tool.created_at,
    )


def _load(db: Session, actor: Actor, tool_id: str) -> SandboxTool:
    tool = db.scalar(
        select(SandboxTool).where(
            SandboxTool.id == tool_id,
            SandboxTool.workspace_id == actor.workspace_id,
        )
    )
    if tool is None:
        raise HTTPException(status_code=404, detail="Sandbox tool not found")
    return tool


def _validate_name(name: str, reserved: Set[str]) -> None:
    if not NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="Name must be lowercase letters, digits, dashes, and underscores",
        )
    if name in reserved:
        raise HTTPException(
            status_code=422, detail="Name collides with a built-in tool"
        )


def _validate_schema(schema: Dict[str, Any]) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise HTTPException(
            status_code=422, detail='input_schema must be a JSON object with "type": "object"'
        )


def _declared_params(schema: Dict[str, Any]) -> Set[str]:
    properties = schema.get("properties")
    return set(properties.keys()) if isinstance(properties, dict) else set()


def _validate_argv(argv: List[str], schema: Dict[str, Any]) -> None:
    if not argv or not all(isinstance(token, str) for token in argv):
        raise HTTPException(
            status_code=422, detail="argv must be a non-empty list of strings"
        )
    declared = _declared_params(schema)
    for token in argv:
        for name in _PLACEHOLDER_RE.findall(token):
            if name not in declared:
                raise HTTPException(
                    status_code=422,
                    detail=f"argv references undeclared parameter {{{{{name}}}}}",
                )


def _validate_hosts(hosts: List[str]) -> None:
    for host in hosts:
        if not isinstance(host, str) or not host.strip():
            raise HTTPException(status_code=422, detail="egress_hosts must be hostnames")
        if any(bad in host for bad in ("/", ":")) or host.split() != [host]:
            raise HTTPException(
                status_code=422,
                detail="egress_hosts are bare hostnames — no scheme, port, path, or spaces",
            )
        # A bare IP literal in a hard-denied range must never enter an allow list,
        # even though egress_rules re-denies those CIDRs: this is the app's own
        # floor, not one that depends on the provider's allow-vs-deny precedence.
        # A hostname is left to the network-layer CIDR denials (it cannot name a
        # CIDR, and a name resolving into a denied range is blocked there).
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if any(address in net for net in _DENIED_NETS):
            raise HTTPException(
                status_code=422,
                detail="egress_hosts may not name a loopback, private, or cloud-metadata address",
            )


@router.get("", response_model=List[SandboxToolOut])
def list_tools(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SandboxToolOut]:
    tools = list(
        db.scalars(
            select(SandboxTool)
            .where(SandboxTool.workspace_id == actor.workspace_id)
            .order_by(SandboxTool.created_at.asc())
        )
    )
    return [_tool_out(tool) for tool in tools]


@router.post("", response_model=SandboxToolOut, status_code=201)
def create_tool(
    payload: SandboxToolRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SandboxToolOut:
    _validate_name(payload.name, _reserved_names(db, actor))
    _validate_schema(payload.input_schema)
    _validate_argv(payload.argv, payload.input_schema)
    _validate_hosts(payload.egress_hosts)
    duplicate = db.scalar(
        select(SandboxTool).where(
            SandboxTool.workspace_id == actor.workspace_id,
            SandboxTool.name == payload.name,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="That tool name is taken")

    tool = SandboxTool(
        workspace_id=actor.workspace_id,
        name=payload.name,
        description=payload.description,
        input_schema_json=json.dumps(payload.input_schema),
        argv_json=json.dumps(payload.argv),
        egress_hosts_json=json.dumps(payload.egress_hosts),
        approval=payload.approval,
        enabled=payload.enabled,
        created_by=actor.user_id,
    )
    db.add(tool)
    # The id is assigned on insert and the audit row references it; without this
    # flush record_audit stores a NULL resource_id and the commit fails the NOT
    # NULL constraint.
    db.flush()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_tool.created",
        resource_type="sandbox_tool",
        resource_id=tool.id,
        detail={"name": tool.name, "approval": tool.approval},
    )
    db.commit()
    return _tool_out(tool)


@router.patch("/{tool_id}", response_model=SandboxToolOut)
def update_tool(
    tool_id: str,
    payload: SandboxToolUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SandboxToolOut:
    tool = _load(db, actor, tool_id)
    current = _tool_out(tool)

    if payload.name is not None and payload.name != tool.name:
        _validate_name(payload.name, _reserved_names(db, actor))
        duplicate = db.scalar(
            select(SandboxTool).where(
                SandboxTool.workspace_id == actor.workspace_id,
                SandboxTool.name == payload.name,
                SandboxTool.id != tool.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(status_code=409, detail="That tool name is taken")
        tool.name = payload.name
    # argv and schema are validated against each other as the row will end up,
    # so an argv edit is checked against the schema it will actually run under —
    # whether or not this same PATCH also changed that schema.
    if payload.input_schema is not None or payload.argv is not None:
        new_schema = (
            payload.input_schema if payload.input_schema is not None else current.input_schema
        )
        new_argv = payload.argv if payload.argv is not None else current.argv
        _validate_schema(new_schema)
        _validate_argv(new_argv, new_schema)
        tool.input_schema_json = json.dumps(new_schema)
        tool.argv_json = json.dumps(new_argv)
    if payload.egress_hosts is not None:
        _validate_hosts(payload.egress_hosts)
        tool.egress_hosts_json = json.dumps(payload.egress_hosts)
    if payload.description is not None:
        tool.description = payload.description
    if payload.approval is not None:
        tool.approval = payload.approval
    if payload.enabled is not None:
        tool.enabled = payload.enabled

    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_tool.updated",
        resource_type="sandbox_tool",
        resource_id=tool.id,
        detail={"name": tool.name, "enabled": tool.enabled},
    )
    db.commit()
    return _tool_out(tool)


@router.delete("/{tool_id}", status_code=204)
def delete_tool(
    tool_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    tool = _load(db, actor, tool_id)
    name = tool.name
    db.delete(tool)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_tool.deleted",
        resource_type="sandbox_tool",
        resource_id=tool_id,
        detail={"name": name},
    )
    db.commit()
