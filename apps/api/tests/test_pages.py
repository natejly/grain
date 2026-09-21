"""Publishing a thread as a page: the gates, the freeze, and the drift sweep.

A page's whole value is that its evidence is pinned, so these tests are about
the three ways the pin could be a lie: publishing something the publisher
should not be able to reach, freezing our own retrieval prefix instead of the
author's words, and a sweep that either misses drift or invents it.
"""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from app.clock import utcnow
from app.database import SessionLocal
from app.models import (
    Chunk,
    Conversation,
    Message,
    Notification,
    Page,
    PageCitation,
    Source,
    Workspace,
    new_id,
)
from app.services import pages
from app.services.embeddings import content_fingerprint
from app.services.retrieval import indexed_text


def _workspace(db) -> str:
    workspace_id = new_id()
    db.add(Workspace(id=workspace_id, name="Pages"))
    db.flush()
    return workspace_id


def _chunk(db, workspace_id: str, *, text: str, prefix: str = "", filename="doc.md"):
    source = Source(
        workspace_id=workspace_id,
        created_by="",
        filename=filename,
        media_type="text/markdown",
        object_key="/x/" + filename,
        byte_size=len(text),
        status="ready",
    )
    db.add(source)
    db.flush()
    chunk = Chunk(
        workspace_id=workspace_id,
        source_id=source.id,
        ordinal=0,
        content=text,
        char_start=0,
        char_end=len(text),
        token_count=len(text.split()),
        context_prefix=prefix,
    )
    db.add(chunk)
    db.flush()
    return source, chunk


def _citation(source, chunk) -> dict:
    return {
        "chunk_id": chunk.id,
        "source_id": source.id,
        "filename": source.filename,
        "ordinal": chunk.ordinal,
        "excerpt": chunk.content[:200],
        "score": 1.0,
    }


def _thread(db, workspace_id: str, *, user_id: str, **fields) -> Conversation:
    conversation = Conversation(
        workspace_id=workspace_id, created_by=user_id, title="Thread", **fields
    )
    db.add(conversation)
    db.flush()
    return conversation


def _answer(db, conversation, *, content: str, citations: list[dict]) -> Message:
    message = Message(
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        run_id="",
        role="assistant",
        content=content,
        citations_json=json.dumps(citations),
    )
    db.add(message)
    db.flush()
    return message


def test_a_colleague_cannot_publish_a_personal_thread_and_its_creator_can():
    """THE PERSONAL-THREAD DOCTRINE, inherited whole from `resolve_visible` —
    there is no second rule here that could disagree with it."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        mine = new_id()
        theirs = new_id()
        conversation = _thread(db, workspace_id, user_id=mine, shared=False)
        db.commit()
        try:
            pages.publish(
                db,
                workspace_id=workspace_id,
                user_id=theirs,
                conversation_id=conversation.id,
                title="Nope",
            )
            raise AssertionError("a colleague published a personal thread")
        except pages.PageError as exc:
            assert exc.status_code == 404
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=mine,
            conversation_id=conversation.id,
            title="Mine",
        )
        db.commit()
        assert page.published_by == mine
    finally:
        db.close()


def test_a_subject_thread_and_a_temporary_chat_both_409():
    """The same two refusals `share_links._resolve_resource` already makes."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        subject = _thread(
            db, workspace_id, user_id=user_id, subject_id=new_id(), shared=True
        )
        temporary = _thread(db, workspace_id, user_id=user_id, incognito=True)
        db.commit()
        for conversation, fragment in (
            (subject, "subject"),
            (temporary, "temporary"),
        ):
            try:
                pages.publish(
                    db,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    conversation_id=conversation.id,
                    title="t",
                )
                raise AssertionError(f"{fragment} thread published")
            except pages.PageError as exc:
                assert exc.status_code == 409
                assert fragment in str(exc).lower()
    finally:
        db.close()


def test_markers_renumber_page_wide_across_two_answers():
    """A page is ONE document: two answers each numbered [1][2] must publish as
    [1][2][3][4], or the same marker means two things on one page."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        sources = [
            _chunk(db, workspace_id, text=f"Passage {index}.", filename=f"{index}.md")
            for index in range(4)
        ]
        _answer(
            db,
            conversation,
            content="First [1] and second [2].",
            citations=[_citation(*sources[0]), _citation(*sources[1])],
        )
        _answer(
            db,
            conversation,
            content="Third [1] and fourth [2].",
            citations=[_citation(*sources[2]), _citation(*sources[3])],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Renumbered",
        )
        db.commit()
        assert "First [1] and second [2]." in page.body_md
        assert "Third [3] and fourth [4]." in page.body_md
        rows = list(
            db.scalars(
                select(PageCitation)
                .where(PageCitation.page_id == page.id)
                .order_by(PageCitation.marker)
            )
        )
        assert [row.marker for row in rows] == [1, 2, 3, 4]
        for row, (_source, chunk) in zip(rows, sources, strict=True):
            assert row.frozen_excerpt == chunk.content
    finally:
        db.close()


def test_interval_notation_in_prose_is_never_renumbered():
    """The validator deliberately reads any bracketed number as a citation, so
    the splice only touches markers IN RANGE for that message's own list."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        first = _chunk(db, workspace_id, text="A.", filename="a.md")
        second = _chunk(db, workspace_id, text="B.", filename="b.md")
        _answer(
            db,
            conversation,
            content="Scores land in the range [0, 100] and so does this [1].",
            citations=[_citation(*first)],
        )
        _answer(
            db,
            conversation,
            content="And the second answer says so [1].",
            citations=[_citation(*second)],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Prose",
        )
        db.commit()
        # [0, 100] is out of range for a one-citation message: untouched.
        assert "[0, 100]" in page.body_md
        assert "and so does this [1]." in page.body_md
        assert "second answer says so [2]." in page.body_md
    finally:
        db.close()


def test_the_excerpt_is_the_authors_words_and_the_hash_covers_the_indexed_text():
    """Two texts, deliberately different. The excerpt is `chunk.content`; the
    hash covers `indexed_text`, which is what a re-embed compares."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        source, chunk = _chunk(
            db,
            workspace_id,
            text="The retention window is ninety days.",
            prefix="From the retention policy, section two.",
        )
        _answer(
            db,
            conversation,
            content="Ninety days [1].",
            citations=[_citation(source, chunk)],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Freeze",
        )
        db.commit()
        row = db.scalar(select(PageCitation).where(PageCitation.page_id == page.id))
        assert row is not None
        assert row.frozen_excerpt == chunk.content
        assert "section two" not in row.frozen_excerpt
        assert row.content_hash == content_fingerprint(indexed_text(chunk))
    finally:
        db.close()


def test_editing_only_the_context_prefix_is_reported_as_drift():
    """The prefix changes what the passage RETRIEVES as, so a page that
    ignored it would under-report drift."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        source, chunk = _chunk(
            db, workspace_id, text="Ninety days.", prefix="Section two."
        )
        _answer(
            db,
            conversation,
            content="Ninety days [1].",
            citations=[_citation(source, chunk)],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Prefix",
        )
        db.commit()
        chunk.context_prefix = "Section three."
        db.commit()
        frozen, changed, missing = pages.revalidate(db, page=page)
        db.commit()
        assert (frozen, changed, missing) == (0, 1, 0)
        assert page.status == "drifted"
        assert page.drift_count == 1
    finally:
        db.close()


def test_revalidate_reports_changed_missing_and_frozen_separately():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        untouched = _chunk(db, workspace_id, text="Untouched.", filename="u.md")
        mutated = _chunk(db, workspace_id, text="Mutated.", filename="m.md")
        deleted = _chunk(db, workspace_id, text="Deleted.", filename="d.md")
        _answer(
            db,
            conversation,
            content="A [1] B [2] C [3].",
            citations=[
                _citation(*untouched),
                _citation(*mutated),
                _citation(*deleted),
            ],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Verdicts",
        )
        db.commit()
        mutated[1].content = "Mutated, and then amended."
        db.delete(deleted[1])
        db.commit()
        frozen, changed, missing = pages.revalidate(db, page=page)
        db.commit()
        assert (frozen, changed, missing) == (1, 1, 1)
        assert page.drift_count == 2
        assert page.status == "drifted"
        verdicts = {
            row.marker: row.status
            for row in db.scalars(
                select(PageCitation).where(PageCitation.page_id == page.id)
            )
        }
        assert verdicts == {1: "frozen", 2: "changed", 3: "missing"}
    finally:
        db.close()


def test_the_sweep_is_edge_triggered_and_notifies_the_publisher():
    """Two sweeps over one drifted page write exactly ONE notification, and it
    is addressed to the publisher rather than broadcast."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        source, chunk = _chunk(db, workspace_id, text="Before.")
        _answer(
            db,
            conversation,
            content="Before [1].",
            citations=[_citation(source, chunk)],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Edge",
        )
        db.commit()
        chunk.content = "After."
        db.commit()

        # Membership, not equality: the sweep is workspace-global by design
        # and the suite shares one database. The EDGE is about this page.
        first = pages.sweep(db)
        db.commit()
        second = pages.sweep(db)
        db.commit()
        assert first.count(page.id) == 1
        assert page.id not in second

        alerts = list(
            db.scalars(
                select(Notification).where(
                    Notification.workspace_id == workspace_id,
                    Notification.kind == "page_drift",
                )
            )
        )
        assert len(alerts) == 1
        assert alerts[0].target_user_id == user_id
        assert alerts[0].page_id == page.id
    finally:
        db.close()


def test_a_drift_notice_reaches_its_publisher_through_the_inbox():
    """A notification nobody can read is dead weight. This is the reader.

    The notices set uses the ('' , member) pair mentions use rather than the ''
    pin automation uses, because a page's drift is addressed to its PUBLISHER —
    the one person who can decide to republish or withdraw — while a shared
    watch speaks to the room.
    """
    db = SessionLocal()
    try:
        from app.services.inbox_feed import waiting_for

        workspace_id = _workspace(db)
        publisher = new_id()
        colleague = new_id()
        conversation = _thread(db, workspace_id, user_id=publisher)
        source, chunk = _chunk(db, workspace_id, text="Before.")
        _answer(
            db,
            conversation,
            content="Before [1].",
            citations=[_citation(source, chunk)],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=publisher,
            conversation_id=conversation.id,
            title="Reachable",
        )
        db.commit()
        chunk.content = "After."
        db.commit()
        pages.sweep(db)
        db.commit()

        mine = waiting_for(db, workspace_id=workspace_id, user_id=publisher)
        assert [row.page_id for row in mine.notices] == [page.id]
        assert [row.kind for row in mine.notices] == ["page_drift"]

        theirs = waiting_for(db, workspace_id=workspace_id, user_id=colleague)
        assert page.id not in [row.page_id for row in theirs.notices]
    finally:
        db.close()


def test_the_sweep_skips_a_page_checked_within_the_interval():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        page = Page(
            workspace_id=workspace_id,
            conversation_id="",
            title="Fresh",
            body_md="",
            published_by="",
            drift_checked_at=utcnow(),
        )
        db.add(page)
        db.commit()
        assert page.id not in pages.sweep(db)
    finally:
        db.close()


def test_a_second_overlapping_sweep_does_not_notify_the_publisher_twice():
    """The edge is CLAIMED, `watches.claim`'s idiom exactly.

    The tick is an ordinary HTTP endpoint driven by an external scheduler, so
    two ticks overlap whenever one runs longer than the interval. Without a
    claim both read the page as `published`, both compute the same crossing,
    and the publisher gets the same notice twice for one edge — the edge
    trigger defeated by the thing meant to make it exact.
    """
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        source, chunk = _chunk(db, workspace_id, text="The window is 30 days.")
        _answer(
            db,
            conversation,
            content="Thirty days [1].",
            citations=[_citation(source, chunk)],
        )
        db.commit()
        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Retention",
        )
        db.commit()
        chunk.content = "The window is 90 days."
        db.commit()

        assert page.id in pages.sweep(db)
        db.commit()
        # A second sweep in the same interval loses the claim and does nothing,
        # even though nothing has re-stamped the page in between.
        assert page.id not in pages.sweep(db)
        db.commit()
        notices = list(
            db.scalars(
                select(Notification).where(
                    Notification.workspace_id == workspace_id,
                    Notification.kind == "page_drift",
                )
            )
        )
        assert len(notices) == 1
    finally:
        db.close()


def test_a_page_past_the_freeze_ceiling_keeps_every_markers_row():
    """The ceiling bounds stored passages, not the page's honesty.

    Markers are assigned page-wide BEFORE any cap could apply and the body is
    rewritten with all of them, so slicing the row list left markers 201..N
    asserted in the prose with nothing behind them — a page claiming
    provenance it does not hold, which is the exact failure the frozen-evidence
    design exists to prevent. Past the cap a citation freezes as `missing`, the
    status `_frozen_citation` already uses for a vanished chunk.
    """
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        user_id = new_id()
        conversation = _thread(db, workspace_id, user_id=user_id)
        over = pages.MAX_PAGE_CITATIONS + 5
        made = [
            _chunk(db, workspace_id, text=f"Passage {index}.", filename=f"{index}.md")
            for index in range(over)
        ]
        # Ten citations per answer, so the cap falls mid-message rather than
        # conveniently on a message boundary.
        for start in range(0, over, 10):
            batch = made[start : start + 10]
            body = " ".join(f"Claim [{index + 1}]." for index in range(len(batch)))
            _answer(
                db,
                conversation,
                content=body,
                citations=[_citation(*pair) for pair in batch],
            )
        db.commit()

        page = pages.publish(
            db,
            workspace_id=workspace_id,
            user_id=user_id,
            conversation_id=conversation.id,
            title="Long",
        )
        db.commit()
        rows = list(
            db.scalars(
                select(PageCitation)
                .where(PageCitation.page_id == page.id)
                .order_by(PageCitation.marker)
            )
        )
        assert [row.marker for row in rows] == list(range(1, over + 1))
        markers = {int(found) for found in re.findall(r"\[(\d+)\]", page.body_md)}
        assert markers <= {row.marker for row in rows}
        assert max(markers) > pages.MAX_PAGE_CITATIONS
        over_cap = [row for row in rows if row.marker > pages.MAX_PAGE_CITATIONS]
        assert all(row.status == "missing" for row in over_cap)
        assert all(row.frozen_excerpt == "" for row in over_cap)
        # And the ones inside the ceiling still hold the author's words.
        assert rows[0].status == "frozen"
        assert rows[0].frozen_excerpt == made[0][1].content
    finally:
        db.close()
