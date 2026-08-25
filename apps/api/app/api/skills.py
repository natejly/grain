"""Skills a person authors: a named instruction block, invoked per turn.

A skill is the reusable half of what an agent's system prompt does — text a user
writes once and splices into a single turn from the composer, rather than a
standing voice. These routes make it a workspace resource: list, author, edit
(as a new version), retire, and roll back. What a skill *does* to a turn lives in
`services/agent_loop.resolve_directives` (the body is appended to the turn's
instructions) and `api/chat.send_message` (the invocation is validated and stored
on the run), not here.

Two invariants this router owns:

- Visibility is own-or-shared, always same-workspace. A member may author a
  private skill freely; a foreign or invisible id is a 404, never a leak.
- Sharing is gated. Flipping `shared` True is owner/admin-only — a member sending
  it is refused 403 rather than silently downgraded, so the ceiling is legible.
  Editing and deleting are author-or-owner.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Skill, SkillVersion
from ..schemas import (
    SkillCreate,
    SkillImportRequest,
    SkillOut,
    SkillUpdate,
    SkillVersionOut,
)
from ..services import skill_markdown
from ..services import skills as skills_service
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["skills"])


def _can_share(actor: Actor) -> bool:
    """Who may make a skill visible to the rest of the workspace."""
    return actor.role in {"owner", "admin"}


def _can_edit(actor: Actor, skill: Skill) -> bool:
    """Who may change or retire a skill: the author, or the workspace owner."""
    return skill.created_by == actor.user_id or actor.role == "owner"


def _out(skill: Skill, actor: Actor) -> SkillOut:
    return SkillOut(
        id=skill.id,
        name=skill.name,
        title=skill.title,
        description=skill.description,
        body=skill.body,
        args=skills_service.parse_args(skill.args_json),
        shared=bool(skill.shared),
        version=skill.version,
        can_share=_can_share(actor),
        can_edit=_can_edit(actor, skill),
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


def _load_visible(db: Session, actor: Actor, skill_id: str) -> Skill:
    """A skill the caller may see, or a 404. The workspace filter is what makes a
    foreign id indistinguishable from a missing one."""
    skill = skills_service.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        skill_id=skill_id,
    )
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


def _require_editable(actor: Actor, skill: Skill) -> None:
    if not _can_edit(actor, skill):
        raise HTTPException(
            status_code=403, detail="Only the author or an owner may change this skill"
        )


@router.get("/skills", response_model=List[SkillOut])
def list_skills(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SkillOut]:
    rows = skills_service.list_visible(
        db, workspace_id=actor.workspace_id, user_id=actor.user_id
    )
    return [_out(row, actor) for row in rows]


@router.post("/skills", response_model=SkillOut, status_code=201)
def create_skill(
    payload: SkillCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SkillOut:
    if payload.shared and not _can_share(actor):
        raise HTTPException(
            status_code=403, detail="Sharing a skill requires an owner or admin"
        )
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="skill.create", key=key
    )
    if replay:
        skill = db.scalar(
            select(Skill).where(
                Skill.id == replay.resource_id,
                Skill.workspace_id == actor.workspace_id,
            )
        )
        if skill is None:
            raise replayed_resource_gone()
        return _out(skill, actor)

    # The (workspace_id, name) uniqueness is a real constraint; without this
    # pre-check a duplicate slug surfaces as an uncaught IntegrityError 500 rather
    # than the conflict it is. The idempotency replay above already handles a
    # retried identical create; this is a genuinely different skill reusing a name.
    existing = db.scalar(
        select(Skill).where(
            Skill.workspace_id == actor.workspace_id, Skill.name == payload.name
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"A skill named “{payload.name}” already exists"
        )

    skill = skills_service.create_skill(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        name=payload.name,
        title=payload.title.strip(),
        description=payload.description.strip(),
        body=payload.body,
        args=payload.args,
        shared=payload.shared,
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="skill.create",
        key=key,
        resource_id=skill.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="skill.created",
        resource_type="skill",
        resource_id=skill.id,
        detail={"name": skill.name, "shared": bool(skill.shared)},
    )
    db.commit()
    return _out(skill, actor)


@router.get("/skills/{skill_id}", response_model=SkillOut)
def get_skill(
    skill_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SkillOut:
    return _out(_load_visible(db, actor, skill_id), actor)


@router.patch("/skills/{skill_id}", response_model=SkillOut)
def update_skill(
    skill_id: str,
    payload: SkillUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SkillOut:
    skill = _load_visible(db, actor, skill_id)
    _require_editable(actor, skill)
    if payload.shared is not None and not _can_share(actor):
        raise HTTPException(
            status_code=403, detail="Sharing a skill requires an owner or admin"
        )

    versioned = skills_service.update_skill(
        db,
        skill=skill,
        user_id=actor.user_id,
        title=payload.title.strip() if payload.title is not None else None,
        description=(
            payload.description.strip() if payload.description is not None else None
        ),
        body=payload.body,
        args=payload.args,
        shared=payload.shared,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="skill.updated",
        resource_type="skill",
        resource_id=skill.id,
        detail={"version": skill.version, "versioned": versioned, "shared": bool(skill.shared)},
    )
    db.commit()
    return _out(skill, actor)


@router.delete("/skills/{skill_id}", status_code=204)
def delete_skill(
    skill_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    skill = _load_visible(db, actor, skill_id)
    _require_editable(actor, skill)
    name = skill.name
    # Versions are retained only as long as the skill is; cascade them by hand
    # because SQLite does not enforce the FK on its own.
    db.execute(delete(SkillVersion).where(SkillVersion.skill_id == skill.id))
    db.delete(skill)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="skill.deleted",
        resource_type="skill",
        resource_id=skill_id,
        detail={"name": name},
    )
    db.commit()


@router.get("/skills/{skill_id}/versions", response_model=List[SkillVersionOut])
def list_skill_versions(
    skill_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SkillVersionOut]:
    skill = _load_visible(db, actor, skill_id)
    versions = skills_service.list_versions(db, skill=skill)
    return [SkillVersionOut.model_validate(row) for row in versions]


@router.post(
    "/skills/{skill_id}/versions/{version_id}/restore", response_model=SkillOut
)
def restore_skill_version(
    skill_id: str,
    version_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SkillOut:
    skill = _load_visible(db, actor, skill_id)
    _require_editable(actor, skill)
    snapshot = db.scalar(
        select(SkillVersion).where(
            SkillVersion.id == version_id,
            SkillVersion.skill_id == skill.id,
            SkillVersion.workspace_id == actor.workspace_id,
        )
    )
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Skill version not found")
    skills_service.restore_version(db, skill=skill, snapshot=snapshot, user_id=actor.user_id)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="skill.restored",
        resource_type="skill",
        resource_id=skill.id,
        detail={"restored_from": snapshot.version, "version": skill.version},
    )
    db.commit()
    return _out(skill, actor)


@router.post("/skills/import", response_model=SkillOut, status_code=201)
def import_skill(
    payload: SkillImportRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SkillOut:
    """Create a skill from a SKILL.md file — the open agent-skills format.

    Deliberately a thin translation onto the same service call `POST /skills`
    makes: the parser owns format errors (422, its messages are user-ready),
    this route owns the same name-conflict 409 the create route answers, and
    what lands is inert by default — private, no args — exactly like any other
    newly authored skill. Grain's declared args have no SKILL.md
    representation, so an import starts with none; `{{ placeholders }}` in the
    body stay literal until args are declared in the editor.
    """
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="skill.import", key=key
    )
    if replay:
        skill = db.scalar(
            select(Skill).where(
                Skill.id == replay.resource_id,
                Skill.workspace_id == actor.workspace_id,
            )
        )
        if skill is None:
            raise replayed_resource_gone()
        return _out(skill, actor)
    try:
        parsed = skill_markdown.parse_skill_markdown(payload.markdown)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    existing = db.scalar(
        select(Skill).where(
            Skill.workspace_id == actor.workspace_id, Skill.name == parsed.name
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A skill named “{parsed.name}” already exists. Edit it, or "
                "rename the file's frontmatter `name` to import alongside it."
            ),
        )
    skill = skills_service.create_skill(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        name=parsed.name,
        title=parsed.title,
        description=parsed.description,
        body=parsed.body,
        args=[],
        shared=False,
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="skill.import",
        key=key,
        resource_id=skill.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="skill.imported",
        resource_type="skill",
        resource_id=skill.id,
        detail={"name": skill.name},
    )
    db.commit()
    return _out(skill, actor)


@router.get("/skills/{skill_id}/export")
def export_skill(
    skill_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> PlainTextResponse:
    """The skill as a SKILL.md file, portable to any agent-skills consumer.

    Declared args are the one thing the format cannot carry; the body's
    `{{ placeholders }}` export literally, which the round trip back into
    Grain also preserves.
    """
    skill = _load_visible(db, actor, skill_id)
    # SKILL.md frontmatter values are single-line; a natively authored skill's
    # title/description may legally hold newlines (the create schema bounds
    # only length), and the renderer refuses them. Collapsing whitespace here
    # is lossy in exactly the way the format demands — the alternative is a
    # 500 on a skill the API itself accepted.
    rendered = skill_markdown.render_skill_markdown(
        name=skill.name,
        title=" ".join(skill.title.split()),
        description=" ".join(skill.description.split()),
        body=skill.body,
    )
    return PlainTextResponse(
        rendered,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="SKILL.md"'},
    )
