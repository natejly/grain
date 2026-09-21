"""Watches: the at-most-once claim, the silence rule, and the owner rule.

The owner rule is the one that would be invisible if it broke. A watch whose
`shared` flag is read backwards publishes one person's reading of a personal
file to everyone's memory shelf, and nothing in the product would say so — the
facts would simply start appearing in other people's turns. Hence the explicit
test, and hence `owner_id=""` being written from exactly one expression.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    Chunk,
    DeliverableManifest,
    Document,
    GraphEntity,
    GraphProjection,
    ManifestFile,
    MemoryItem,
    Notification,
    Source,
    Space,
    Watch,
    WatchObservation,
    Workspace,
    new_id,
)
from app.services import spaces, watches


def _workspace(db) -> str:
    workspace_id = new_id()
    db.add(Workspace(id=workspace_id, name="Watches"))
    db.flush()
    return workspace_id


def _source(db, workspace_id: str, *, filename="brief.md", space_id="") -> Source:
    source = Source(
        workspace_id=workspace_id,
        created_by="",
        filename=filename,
        media_type="text/markdown",
        object_key="/x/" + filename,
        byte_size=10,
        status="ready",
        space_id=space_id,
    )
    db.add(source)
    db.flush()
    return source


def _chunk(db, source: Source, *, ordinal: int, text: str) -> Chunk:
    chunk = Chunk(
        workspace_id=source.workspace_id,
        source_id=source.id,
        ordinal=ordinal,
        content=text,
        char_start=0,
        char_end=len(text),
        token_count=len(text.split()),
    )
    db.add(chunk)
    db.flush()
    return chunk


def _watch(db, workspace_id: str, source: Source, **fields) -> Watch:
    watch = Watch(
        workspace_id=workspace_id,
        created_by=fields.pop("created_by", new_id()),
        name="Release notes",
        target_kind="source",
        target_id=source.id,
        space_id=fields.pop("space_id", ""),
        schedule_cron="0 9 * * *",
        schedule_timezone="UTC",
        enabled=True,
        extraction_schema_json=json.dumps(["owner"]),
        **fields,
    )
    db.add(watch)
    db.flush()
    return watch


def test_two_ticks_in_one_minute_fire_a_watch_once():
    """The conditional UPDATE is the whole of the at-most-once guarantee."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source = _source(db, workspace_id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = _watch(db, workspace_id, source)
        db.commit()
        moment = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
        # Membership, not equality: the suite shares one database and the
        # ticker is workspace-global by design, so other tests' watches may be
        # due in the same minute. The claim is about THIS watch.
        first = watches.dispatch_due(db, moment=moment)
        second = watches.dispatch_due(db, moment=moment)
        assert first.count(watch.id) == 1
        assert watch.id not in second
    finally:
        db.close()


def test_a_tick_delayed_past_the_minute_boundary_still_fires_once():
    """CATCHUP covers the just-missed minute, and nothing older."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source = _source(db, workspace_id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = _watch(db, workspace_id, source)
        db.commit()
        nine = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
        late = nine + watches.CATCHUP
        assert watches.dispatch_due(db, moment=late).count(watch.id) == 1
        assert watch.id not in watches.dispatch_due(db, moment=late)
    finally:
        db.close()


def test_a_day_of_downtime_fires_nothing_older_than_catchup():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source = _source(db, workspace_id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = _watch(db, workspace_id, source)
        db.commit()
        nine = utcnow().replace(hour=9, minute=0, second=0, microsecond=0)
        a_day_late = nine + timedelta(days=1) - timedelta(minutes=30)
        assert watch.id not in watches.dispatch_due(db, moment=a_day_late)
    finally:
        db.close()


def test_an_unchanged_target_writes_an_observation_and_nothing_else():
    """NO CHANGE, NO NOISE. A watch that pings on no change gets turned off."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source = _source(db, workspace_id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = _watch(db, workspace_id, source)
        db.commit()
        settings = get_settings()

        watches.check(db, watch=watch, settings=settings)
        db.commit()
        again = watches.check(db, watch=watch, settings=settings)
        db.commit()

        assert again.changed is False
        assert again.summary == "No change."
        observations = list(
            db.scalars(
                select(WatchObservation).where(WatchObservation.watch_id == watch.id)
            )
        )
        assert len(observations) == 2
        # One notification, from the FIRST check (which saw the target appear).
        alerts = list(
            db.scalars(
                select(Notification).where(
                    Notification.workspace_id == workspace_id,
                    Notification.kind == "watch_change",
                )
            )
        )
        assert len(alerts) == 1
        documents = list(
            db.scalars(select(Document).where(Document.workspace_id == workspace_id))
        )
        assert len(documents) == 1
    finally:
        db.close()


def test_supersession_leaves_one_active_memory_per_watch_field():
    """The claim key is `watch:<id>|<field>`, so the row count is bounded by
    declared fields rather than by how often the watch fires."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        creator = new_id()
        source = _source(db, workspace_id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = _watch(db, workspace_id, source, created_by=creator)
        db.commit()
        settings = get_settings()

        watches.check(db, watch=watch, settings=settings)
        db.commit()
        _chunk(db, source, ordinal=1, text="The owner is Ravi now.")
        db.commit()
        watches.check(db, watch=watch, settings=settings)
        db.commit()

        key = watches.claim_key(watch.id, "owner")
        assert key is not None
        rows = list(
            db.scalars(
                select(MemoryItem).where(
                    MemoryItem.workspace_id == workspace_id,
                    MemoryItem.normalized_key.like(f"{key}%"),
                )
            )
        )
        active = [row for row in rows if row.status == "active"]
        superseded = [row for row in rows if row.status == "superseded"]
        assert len(active) == 1
        assert "Ravi" in active[0].content
        assert len(superseded) == 1
        assert "Maya" in superseded[0].content
    finally:
        db.close()


def test_the_owner_rule_only_a_shared_watch_writes_the_shared_owner():
    """THE OWNER RULE. `owner_id=""` is the global shelf; a personal watch must
    never reach it."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        creator = new_id()
        settings = get_settings()

        personal_source = _source(db, workspace_id, filename="personal.md")
        _chunk(db, personal_source, ordinal=0, text="The owner is Maya.")
        personal = _watch(
            db, workspace_id, personal_source, created_by=creator, shared=False
        )
        db.commit()
        watches.check(db, watch=personal, settings=settings)
        db.commit()
        row = db.scalar(
            select(MemoryItem).where(
                MemoryItem.normalized_key
                == watches.claim_key(personal.id, "owner")
            )
        )
        assert row is not None and row.owner_id == creator

        shared_source = _source(db, workspace_id, filename="shared.md")
        _chunk(db, shared_source, ordinal=0, text="The owner is Ravi.")
        shared = _watch(
            db, workspace_id, shared_source, created_by=creator, shared=True
        )
        db.commit()
        watches.check(db, watch=shared, settings=settings)
        db.commit()
        row = db.scalar(
            select(MemoryItem).where(
                MemoryItem.normalized_key == watches.claim_key(shared.id, "owner")
            )
        )
        assert row is not None and row.owner_id == ""
    finally:
        db.close()


def test_a_space_targeted_watch_writes_its_memory_into_that_space():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        creator = new_id()
        space = Space(workspace_id=workspace_id, name="Research", created_by=creator)
        db.add(space)
        db.flush()
        source = _source(db, workspace_id, space_id=space.id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = Watch(
            workspace_id=workspace_id,
            created_by=creator,
            name="Space watch",
            target_kind="space",
            target_id=space.id,
            space_id=space.id,
            schedule_cron="0 9 * * *",
            extraction_schema_json=json.dumps(["owner"]),
        )
        db.add(watch)
        db.commit()
        watches.check(db, watch=watch, settings=get_settings())
        db.commit()
        row = db.scalar(
            select(MemoryItem).where(
                MemoryItem.normalized_key == watches.claim_key(watch.id, "owner")
            )
        )
        assert row is not None
        assert row.space_id == space.id
        assert row.space_id != ""
    finally:
        db.close()


def test_the_watch_marks_the_graph_stale_rather_than_writing_entities():
    """The KG is a PROJECTION: a hand-written entity would be clobbered by the
    next `rebuild_graph`."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source = _source(db, workspace_id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = _watch(db, workspace_id, source)
        db.commit()
        watches.check(db, watch=watch, settings=get_settings())
        db.commit()
        assert (
            list(
                db.scalars(
                    select(GraphEntity).where(
                        GraphEntity.workspace_id == workspace_id
                    )
                )
            )
            == []
        )
        projection = db.scalar(
            select(GraphProjection).where(
                GraphProjection.workspace_id == workspace_id
            )
        )
        assert projection is not None and projection.status == "stale"
    finally:
        db.close()


def test_deterministic_extract_is_pure_and_is_the_scripted_path():
    text = "The release ships on Friday. The owner is Maya Chen. Nothing else."
    assert watches.deterministic_extract(text, ["owner", "release", "budget"]) == {
        "owner": "The owner is Maya Chen.",
        "release": "The release ships on Friday.",
        "budget": "",
    }
    from app.services import model as model_service

    assert model_service.extract_watch_fields(
        text, ["owner"], user_id="u", settings=get_settings()
    ) == {"owner": "The owner is Maya Chen."}


def test_the_fingerprint_is_stable_and_moves_only_when_the_target_does():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source = _source(db, workspace_id)
        chunk = _chunk(db, source, ordinal=0, text="One.")
        db.commit()
        args = dict(
            workspace_id=workspace_id, target_kind="source", target_id=source.id
        )
        first, ids = watches.fingerprint(db, **args)
        second, again = watches.fingerprint(db, **args)
        assert first == second and ids == again == [chunk.id]
        chunk.content = "Two."
        db.commit()
        third, _ids = watches.fingerprint(db, **args)
        assert third != first
    finally:
        db.close()


def test_deleting_a_space_deletes_its_watches_observations_and_manifests():
    """A space-scoped watch re-labelled `space_id = ""` would start writing to
    the GLOBAL shelf — the one direction scoping may never fail toward."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        creator = new_id()
        space = Space(workspace_id=workspace_id, name="Doomed", created_by=creator)
        db.add(space)
        db.flush()
        source = _source(db, workspace_id, space_id=space.id)
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        watch = _watch(db, workspace_id, source, space_id=space.id)
        db.add(
            WatchObservation(
                workspace_id=workspace_id,
                watch_id=watch.id,
                fingerprint="x",
                changed=True,
                summary="something",
            )
        )
        manifest = DeliverableManifest(
            workspace_id=workspace_id,
            workflow_run_id="",
            space_id=space.id,
            title="Doomed deliverable",
            created_by=creator,
        )
        db.add(manifest)
        db.flush()
        db.add(
            ManifestFile(
                workspace_id=workspace_id,
                manifest_id=manifest.id,
                ordinal=0,
                filename="out.csv",
            )
        )
        db.commit()

        teardown = spaces.delete_space(
            db, workspace_id=workspace_id, space_id=space.id
        )
        db.commit()
        assert teardown.watch_count == 1
        assert teardown.manifest_count == 1
        assert (
            list(db.scalars(select(Watch).where(Watch.workspace_id == workspace_id)))
            == []
        )
        assert (
            list(
                db.scalars(
                    select(WatchObservation).where(
                        WatchObservation.workspace_id == workspace_id
                    )
                )
            )
            == []
        )
        assert (
            list(
                db.scalars(
                    select(ManifestFile).where(
                        ManifestFile.workspace_id == workspace_id
                    )
                )
            )
            == []
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Supersession's two edges: a key that will not slugify, and a scope that moves


def _claim_rows(db, workspace_id: str, key: str) -> list[MemoryItem]:
    return list(
        db.scalars(
            select(MemoryItem).where(
                MemoryItem.workspace_id == workspace_id,
                MemoryItem.normalized_key.like(f"{key}%"),
            )
        )
    )


def test_a_field_name_that_will_not_slugify_still_supersedes():
    """"price (USD)" is an ordinary thing to type, and it used to be unbounded.

    `normalize_claim_key` only maps whitespace and `-.:/`, so a parenthesis
    made `claim_key` return None, the write fell back to a content hash, and
    every fire filed a NEW active row. An hourly watch accumulated hundreds of
    mutually contradictory live facts — with the ten-field cap's stated
    invariant ("bounded by fields, not by fires") quietly false.
    """
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source = _source(db, workspace_id)
        _chunk(db, source, ordinal=0, text="The price (USD) is $99.")
        watch = _watch(db, workspace_id, source)
        watch.extraction_schema_json = json.dumps(["price (USD)"])
        db.commit()
        settings = get_settings()

        watches.check(db, watch=watch, settings=settings)
        db.commit()
        _chunk(db, source, ordinal=1, text="The price (USD) is $149 now.")
        db.commit()
        watches.check(db, watch=watch, settings=settings)
        db.commit()

        key = watches.claim_key(watch.id, "price (USD)")
        assert key  # total, never None
        rows = _claim_rows(db, workspace_id, key)
        active = [row for row in rows if row.status == "active"]
        superseded = [row for row in rows if row.status == "superseded"]
        assert len(active) == 1, [row.content for row in rows]
        assert "149" in active[0].content
        assert len(superseded) == 1
        # Stable across edits to the watch's OTHER fields, which an index-based
        # key would not be.
        assert watches.claim_key(watch.id, "price (USD)") == key
        assert watches.claim_key(watch.id, "PRICE (usd)") == key
        assert watches.claim_key(watch.id, "% change") != key
    finally:
        db.close()


def test_the_boundary_refuses_a_field_name_it_could_only_store_as_a_digest(client):
    """Told while the person still holds the form, not a month later."""
    identity = client.get("/api/bootstrap").json()["identity"]
    db = SessionLocal()
    try:
        source = _source(
            db, identity["workspace_id"], filename="prices-" + new_id() + ".md"
        )
        db.commit()
        source_id = source.id
    finally:
        db.close()
    response = client.post(
        "/api/watches",
        headers={"Idempotency-Key": "watch-field-" + new_id()},
        json={
            "name": "Pricing",
            "target_kind": "source",
            "target_id": source_id,
            "schedule_cron": "0 9 * * *",
            "extraction_fields": ["price (USD)"],
        },
    )
    assert response.status_code == 422, response.text
    assert "price (USD)" in response.json()["detail"]


def test_flipping_shared_retires_the_claims_it_is_leaving_behind(client):
    """The flag decides the SCOPE, and supersession matches scope exactly.

    A shared watch writes to the workspace shelf. Flipped to personal, its next
    fire writes to its creator's — and the workspace row it wrote last week is
    unreachable to every future supersession: still `active`, still recalled in
    every member's turns, with no watch claiming it and nothing able to correct
    it. So the flip retires what it leaves.
    """
    identity = client.get("/api/bootstrap").json()["identity"]
    workspace_id = identity["workspace_id"]
    db = SessionLocal()
    try:
        source = _source(db, workspace_id, filename="pricing-" + new_id() + ".md")
        _chunk(db, source, ordinal=0, text="The owner is Maya.")
        db.commit()
        created = client.post(
            "/api/watches",
            headers={"Idempotency-Key": "watch-shared-" + new_id()},
            json={
                "name": "Owner",
                "target_kind": "source",
                "target_id": source.id,
                "schedule_cron": "0 9 * * *",
                "shared": True,
                "extraction_fields": ["owner"],
            },
        )
        assert created.status_code == 201, created.text
        watch = db.scalar(select(Watch).where(Watch.id == created.json()["id"]))
        assert watch is not None

        watches.check(db, watch=watch, settings=get_settings())
        db.commit()
        key = watches.claim_key(watch.id, "owner")
        shared_rows = [
            row for row in _claim_rows(db, workspace_id, key) if row.owner_id == ""
        ]
        assert [row.status for row in shared_rows] == ["active"]

        patched = client.patch(f"/api/watches/{watch.id}", json={"shared": False})
        assert patched.status_code == 200, patched.text
        db.expire_all()

        _chunk(db, source, ordinal=1, text="The owner is Ravi now.")
        db.commit()
        watches.check(db, watch=db.scalar(select(Watch).where(Watch.id == watch.id)),
                      settings=get_settings())
        db.commit()

        rows = _claim_rows(db, workspace_id, key)
        active = [row for row in rows if row.status == "active"]
        assert len(active) == 1, [(row.owner_id, row.status, row.content) for row in rows]
        assert active[0].owner_id == identity["user_id"]
        assert "Ravi" in active[0].content
        # The workspace-shelf row is retired rather than left to contradict it.
        assert all(row.status == "superseded" for row in rows if row.owner_id == "")
    finally:
        db.close()
