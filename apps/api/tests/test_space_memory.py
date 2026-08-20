"""The space axis of memory: learned in a space, recalled in that space.

Mirrors test_personal_scope.py's shape for the second scope axis. The pairs
worth proving together, because they are the answer to "does a space's
correction destroy the workspace's fact":

* supersession is within an (owner, space) cell — correcting a claim inside a
  space retires the space's row and leaves the global row standing;
* shadowing is across shelves at read time — inside the space the space's
  value is served and the global one dropped, outside the space the global
  one is untouched.

And the axes compose: a *personal* memory learned in a space's personal
thread is invisible to a roommate even inside the same space.
"""
from __future__ import annotations

import os
from typing import List, Optional

from conftest import create_identity

from app.config import get_settings
from app.database import SessionLocal
from app.models import Conversation, Membership, MemoryItem, Space, User
from app.services.memory import (
    apply_extracted_memories,
    memory_space,
    recall,
    remember_memory,
)


def _tenant() -> tuple[str, str]:
    identity = create_identity(name="Rememberer", workspace_name="Space memory")
    return identity.workspace_id, identity.user_id


def _space(workspace_id: str, name: str = "Falconry") -> str:
    db = SessionLocal()
    try:
        space = Space(workspace_id=workspace_id, name=name)
        db.add(space)
        db.commit()
        return space.id
    finally:
        db.close()


def _conversation(
    workspace_id: str, user_id: str, space_id: str = "", *, shared: bool = True
) -> str:
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=workspace_id,
            created_by=user_id,
            space_id=space_id,
            shared=shared,
        )
        db.add(conversation)
        db.commit()
        return conversation.id
    finally:
        db.close()


def _extract(
    workspace_id: str,
    conversation_id: Optional[str],
    facts: List[dict],
    *,
    owner_id: str = "",
    space_id: str = "",
) -> None:
    db = SessionLocal()
    try:
        apply_extracted_memories(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            run_id="",
            extracted=facts,
            message_ids=[],
            settings=get_settings(),
            owner_id=owner_id,
            space_id=space_id,
        )
        db.commit()
    finally:
        db.close()


def _recalled(
    workspace_id: str, conversation_id: str, query: str, viewer_id: str = ""
) -> List[str]:
    db = SessionLocal()
    try:
        context = recall(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            query=query,
            viewer_id=viewer_id,
        )
        return [item.content for item in context.items]
    finally:
        db.close()


def _fact(content: str, key: str) -> dict:
    return {"content": content, "kind": "fact", "normalized_key": key}


# --------------------------------------------------------------------------
# Stamping


def test_the_shelf_comes_from_the_conversation_never_a_field() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id)
    in_space = _conversation(workspace_id, user_id, space_id)
    outside = _conversation(workspace_id, user_id)
    db = SessionLocal()
    try:
        assert memory_space(db, in_space) == space_id
        assert memory_space(db, outside) == ""
        assert memory_space(db, None) == ""
        assert memory_space(db, "gone") == ""
    finally:
        db.close()


def test_remember_in_a_space_thread_lands_on_the_space_shelf() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id)
    conversation_id = _conversation(workspace_id, user_id, space_id)
    db = SessionLocal()
    try:
        result = remember_memory(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            user_id=user_id,
            content="The kestrel cage code is 4417.",
        )
        db.commit()
        assert result.item.space_id == space_id
    finally:
        db.close()


# --------------------------------------------------------------------------
# Recall scope


def test_a_space_turn_recalls_its_shelf_plus_the_global_one() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id)
    _extract(
        workspace_id, None, [_fact("Deploys happen on Tuesdays.", "deploy|day")]
    )
    _extract(
        workspace_id,
        None,
        [_fact("The kestrel hunts at dawn.", "kestrel|habit")],
        space_id=space_id,
    )
    in_space = _conversation(workspace_id, user_id, space_id)
    outside = _conversation(workspace_id, user_id)

    inside_view = _recalled(workspace_id, in_space, "kestrel deploys", user_id)
    assert "The kestrel hunts at dawn." in inside_view
    assert "Deploys happen on Tuesdays." in inside_view

    outside_view = _recalled(workspace_id, outside, "kestrel deploys", user_id)
    assert "The kestrel hunts at dawn." not in outside_view
    assert "Deploys happen on Tuesdays." in outside_view


def test_one_space_never_recalls_anothers_shelf() -> None:
    workspace_id, user_id = _tenant()
    space_a = _space(workspace_id, "Falconry")
    space_b = _space(workspace_id, "Astronomy")
    _extract(
        workspace_id,
        None,
        [_fact("The kestrel hunts at dawn.", "kestrel|habit")],
        space_id=space_a,
    )
    in_b = _conversation(workspace_id, user_id, space_b)
    assert "The kestrel hunts at dawn." not in _recalled(
        workspace_id, in_b, "kestrel", user_id
    )


# --------------------------------------------------------------------------
# Supersession stays inside a shelf; shadowing crosses shelves at read time


def test_a_space_correction_leaves_the_global_fact_standing() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id)
    _extract(workspace_id, None, [_fact("Deploys happen on Tuesdays.", "deploy|day")])
    _extract(
        workspace_id,
        None,
        [_fact("In this project, deploys happen on Fridays.", "deploy|day")],
        space_id=space_id,
    )
    db = SessionLocal()
    try:
        by_shelf = {
            row.space_id: row.status
            for row in db.query(MemoryItem).filter_by(workspace_id=workspace_id)
            if row.normalized_key == "deploy|day"
        }
        assert by_shelf == {"": "active", space_id: "active"}
    finally:
        db.close()


def test_inside_the_space_its_value_shadows_the_global_one() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id)
    _extract(workspace_id, None, [_fact("Deploys happen on Tuesdays.", "deploy|day")])
    _extract(
        workspace_id,
        None,
        [_fact("In this project, deploys happen on Fridays.", "deploy|day")],
        space_id=space_id,
    )
    in_space = _conversation(workspace_id, user_id, space_id)
    outside = _conversation(workspace_id, user_id)

    inside_view = _recalled(workspace_id, in_space, "deploys", user_id)
    assert "In this project, deploys happen on Fridays." in inside_view
    assert "Deploys happen on Tuesdays." not in inside_view

    outside_view = _recalled(workspace_id, outside, "deploys", user_id)
    assert outside_view == ["Deploys happen on Tuesdays."]


# --------------------------------------------------------------------------
# The two axes compose


def test_a_personal_space_memory_stays_invisible_to_a_roommate() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id)
    db = SessionLocal()
    try:
        roommate = User(email=f"{os.urandom(6).hex()}@example.com", name="Roommate")
        db.add(roommate)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=roommate.id))
        db.commit()
        roommate_id = roommate.id
    finally:
        db.close()

    personal_thread = _conversation(workspace_id, user_id, space_id, shared=False)
    _extract(
        workspace_id,
        personal_thread,
        [_fact("My private kestrel note.", "kestrel|note")],
        owner_id=user_id,
        space_id=space_id,
    )
    shared_thread = _conversation(workspace_id, roommate_id, space_id)
    assert "My private kestrel note." not in _recalled(
        workspace_id, shared_thread, "kestrel", roommate_id
    )
    my_thread = _conversation(workspace_id, user_id, space_id)
    assert "My private kestrel note." in _recalled(
        workspace_id, my_thread, "kestrel", user_id
    )


# --------------------------------------------------------------------------
# The admin surface still sees every shelf


def test_the_memory_api_lists_space_rows_with_their_space_id() -> None:
    from conftest import TEST_BASE_URL, authenticate, create_identity
    from fastapi.testclient import TestClient

    from app.main import app

    identity = create_identity(name="Admin", workspace_name="Memory admin")
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    space_id = _space(identity.workspace_id)
    _extract(
        identity.workspace_id,
        None,
        [_fact("Space shelf row.", "shelf|row")],
        space_id=space_id,
    )
    rows = client.get("/api/memory").json()
    assert [(row["content"], row["space_id"]) for row in rows] == [
        ("Space shelf row.", space_id)
    ]
