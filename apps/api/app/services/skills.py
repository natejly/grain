"""The mechanics behind shared skills: visibility, versioning, and injection.

`api/skills.py` owns the HTTP shape (idempotency, audit, the sharing gate);
this module owns the parts that must be identical everywhere they happen — how a
skill is deserialized, when an edit becomes a new version, and how a skill's body
is spliced into a turn. Keeping the last one here is the load-bearing choice:
`agent_loop.resolve_directives` gains three lines and the single instruction path
stays unforked, so a turn resumed after an approval or budget park re-injects the
same body the sender chose.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Run, Skill, SkillVersion
from ..schemas import SkillArg

#: A skill placeholder: `{{ name }}`, with any surrounding whitespace. A closed
#: literal-token replacement, not an expression language — the same philosophy
#: the workflow `{{ input.x }}` grammar keeps, one notch simpler.
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

#: Ceiling on a single string arg's length. A skill body is already bounded at
#: authoring; this bounds what an invocation can splice into it per turn.
MAX_ARG_CHARS = 4000


def serialize_args(args: List[SkillArg]) -> str:
    """The canonical JSON for a declared arg list. Order is preserved because a
    form renders the fields in the order they were authored, and the hash below
    must be stable for identical input."""
    return json.dumps([arg.model_dump() for arg in args], separators=(",", ":"))


def parse_args(raw: str) -> List[SkillArg]:
    """`args_json` → the API's arg list. A row that does not parse degrades to no
    args rather than making the skill unreadable — a corrupt declaration must
    never turn a skill into a failed request."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    out: List[SkillArg] = []
    for item in parsed:
        if isinstance(item, dict):
            try:
                out.append(SkillArg.model_validate(item))
            except ValueError:
                continue
    return out


def content_hash(*, title: str, description: str, body: str, args_json: str) -> str:
    """The identity of a skill's *content*. `shared` is deliberately excluded:
    toggling visibility is metadata, not an edit, and must not spend a version."""
    digest = hashlib.sha256()
    digest.update("\x00".join([title, description, body, args_json]).encode("utf-8"))
    return digest.hexdigest()


def resolve_visible(
    db: Session, *, workspace_id: str, user_id: str, skill_id: str
) -> Optional[Skill]:
    """The one visibility rule, in one place: a skill is visible when it is the
    caller's own or shared, and always within their workspace. Returns None so a
    foreign or invisible id becomes a 404 at the call site, never a leak."""
    skill = db.scalar(
        select(Skill).where(
            Skill.id == skill_id, Skill.workspace_id == workspace_id
        )
    )
    if skill is None:
        return None
    if skill.shared or skill.created_by == user_id:
        return skill
    return None


def list_visible(db: Session, *, workspace_id: str, user_id: str) -> List[Skill]:
    rows = db.scalars(
        select(Skill)
        .where(
            Skill.workspace_id == workspace_id,
            (Skill.shared.is_(True)) | (Skill.created_by == user_id),
        )
        .order_by(Skill.created_at.asc(), Skill.id.asc())
    )
    return list(rows)


def list_versions(db: Session, *, skill: Skill) -> List[SkillVersion]:
    rows = db.scalars(
        select(SkillVersion)
        .where(
            SkillVersion.workspace_id == skill.workspace_id,
            SkillVersion.skill_id == skill.id,
        )
        .order_by(SkillVersion.version.desc())
    )
    return list(rows)


def create_skill(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    title: str,
    description: str,
    body: str,
    args: List[SkillArg],
    shared: bool,
) -> Skill:
    """Write the skill and its version-1 snapshot together, so a skill always has
    a retained history from the moment it exists."""
    args_json = serialize_args(args)
    digest = content_hash(
        title=title, description=description, body=body, args_json=args_json
    )
    skill = Skill(
        workspace_id=workspace_id,
        name=name,
        title=title,
        description=description,
        body=body,
        args_json=args_json,
        shared=shared,
        version=1,
        content_hash=digest,
        created_by=user_id,
    )
    db.add(skill)
    db.flush()
    db.add(_snapshot(skill, user_id=user_id))
    db.flush()
    return skill


def update_skill(
    db: Session,
    *,
    skill: Skill,
    user_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    body: Optional[str] = None,
    args: Optional[List[SkillArg]] = None,
    shared: Optional[bool] = None,
) -> bool:
    """Apply a partial edit. Returns True when a new content version was written.

    Content and visibility are separate facts: a change to title/description/body/
    args that alters the `content_hash` bumps the version and appends a snapshot,
    while flipping `shared` alone is metadata and spends no version.
    """
    new_title = skill.title if title is None else title
    new_description = skill.description if description is None else description
    new_body = skill.body if body is None else body
    new_args_json = skill.args_json if args is None else serialize_args(args)
    digest = content_hash(
        title=new_title,
        description=new_description,
        body=new_body,
        args_json=new_args_json,
    )

    versioned = digest != skill.content_hash
    if versioned:
        skill.title = new_title
        skill.description = new_description
        skill.body = new_body
        skill.args_json = new_args_json
        skill.content_hash = digest
        skill.version += 1
        db.add(_snapshot(skill, user_id=user_id))
    if shared is not None:
        skill.shared = shared
    db.flush()
    return versioned


def restore_version(
    db: Session, *, skill: Skill, snapshot: SkillVersion, user_id: str
) -> Skill:
    """Apply a past snapshot as a *new* version. History is append-only: a restore
    never mutates or removes the intervening versions, it adds one that happens to
    carry old content — so the trail still reads forward."""
    skill.title = snapshot.title
    skill.description = snapshot.description
    skill.body = snapshot.body
    skill.args_json = snapshot.args_json
    skill.content_hash = snapshot.content_hash or content_hash(
        title=snapshot.title,
        description=snapshot.description,
        body=snapshot.body,
        args_json=snapshot.args_json,
    )
    skill.version += 1
    db.add(_snapshot(skill, user_id=user_id))
    db.flush()
    return skill


def _snapshot(skill: Skill, *, user_id: str) -> SkillVersion:
    return SkillVersion(
        workspace_id=skill.workspace_id,
        skill_id=skill.id,
        version=skill.version,
        title=skill.title,
        description=skill.description,
        body=skill.body,
        args_json=skill.args_json,
        content_hash=skill.content_hash,
        created_by=user_id,
    )


def validate_args(skill: Skill, provided: Dict[str, Any]) -> str:
    """Coerce and check the caller's args against the skill's declaration.

    Enforces the same three things the workflow input check does — required
    present, coercible to the declared type, member of `choices` — and returns a
    compact JSON object to store on the run. Refuses a bad request with 422 so
    the failure lands at send time, not silently inside the turn.
    """
    resolved: Dict[str, Any] = {}
    for arg in parse_args(skill.args_json):
        if arg.name in provided and provided[arg.name] is not None:
            value = _coerce(arg, provided[arg.name])
        elif arg.default is not None:
            value = arg.default
        elif arg.required:
            raise HTTPException(
                status_code=422, detail=f"Skill argument '{arg.name}' is required"
            )
        else:
            continue
        if arg.choices and value not in arg.choices:
            raise HTTPException(
                status_code=422,
                detail=f"Skill argument '{arg.name}' must be one of {arg.choices}",
            )
        resolved[arg.name] = value
    return json.dumps(resolved, separators=(",", ":"))


def _coerce(arg: SkillArg, value: Any) -> Any:
    try:
        if arg.type == "integer":
            return int(value)
        if arg.type == "number":
            return float(value)
        if arg.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes", "on"}
            return bool(value)
        # A string arg is an uncapped channel into the turn's instructions and the
        # DB; bound it like every other free-text input the app accepts.
        return str(value)[:MAX_ARG_CHARS]
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"Skill argument '{arg.name}' is not a valid {arg.type}",
        ) from None


def render_for_run(db: Session, run: Run) -> str:
    """The skill body to splice into this turn's instructions, or "" for none.

    A deleted or cross-workspace skill degrades to no injection, mirroring the
    missing-agent fallback in `resolve_directives` — a run must survive its
    skill's deletion, and the historical record on the run is enough. The body's
    `{{ name }}` placeholders are filled from the run's stored args (or the arg's
    default), and the result is wrapped so the model reads it as an instruction it
    was asked to follow, not as user data.
    """
    if not run.skill_id:
        return ""
    snapshot = _pinned_snapshot(db, run)
    if snapshot is None or not snapshot.body.strip():
        return ""

    values: Dict[str, Any] = {}
    if run.skill_args_json:
        try:
            parsed = json.loads(run.skill_args_json)
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            values = parsed
    defaults = {arg.name: arg.default for arg in parse_args(snapshot.args_json)}

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values and values[key] is not None:
            return str(values[key])
        fallback = defaults.get(key)
        return "" if fallback is None else str(fallback)

    filled = _PLACEHOLDER.sub(_sub, snapshot.body)
    return f"You have been asked to follow this skill for this turn:\n\n{filled}"


def _pinned_snapshot(db: Session, run: Run) -> Optional[Any]:
    """The exact skill version this run invoked.

    Pinned at send time, so a skill edited or version-restored while the run is
    parked (on an approval or the spend ceiling) resumes with the body its author
    saw — the same way the args are pinned. Reading the live skill instead would
    silently swap the instructions of a turn already in flight. A run written
    before versions were pinned (`skill_version == 0`) falls back to the live
    skill; a deleted skill takes its versions with it, so the lookup finds nothing
    and the turn degrades to no injection.
    """
    if run.skill_version:
        return db.scalar(
            select(SkillVersion).where(
                SkillVersion.workspace_id == run.workspace_id,
                SkillVersion.skill_id == run.skill_id,
                SkillVersion.version == run.skill_version,
            )
        )
    return db.scalar(
        select(Skill).where(
            Skill.id == run.skill_id, Skill.workspace_id == run.workspace_id
        )
    )
