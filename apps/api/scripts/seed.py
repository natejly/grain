from __future__ import annotations

from sqlalchemy import select

from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID, seed_dev_workspace
from app.database import Base, SessionLocal, engine
from app.models import Source, new_id
from app.services.ingestion import ingest_source, object_path

DEMO_TEXT = """# Atlas product brief

Atlas is a workspace initiative focused on making research evidence easier to
inspect and reuse. Priya Shah owns the pilot, which begins on September 15.

The pilot has three goals:

1. Reduce the time required to find source material.
2. Keep citations connected to their original passages.
3. Require a human decision before an agent makes an external request.

The primary risk is stale content. Every answer should therefore expose its
supporting passages and source update time.
"""


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_dev_workspace(db)
        existing = db.scalar(
            select(Source).where(
                Source.workspace_id == DEV_SEED_WORKSPACE_ID,
                Source.filename == "atlas-product-brief.md",
                Source.deleted_at.is_(None),
            )
        )
        if existing:
            print("Demo source already exists:", existing.id)
            return
        source_id = new_id()
        path = object_path(DEV_SEED_WORKSPACE_ID, source_id, "atlas-product-brief.md")
        path.write_text(DEMO_TEXT, encoding="utf-8")
        source = Source(
            id=source_id,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            created_by=DEV_SEED_USER_ID,
            filename="atlas-product-brief.md",
            media_type="text/markdown",
            object_key=str(path),
            byte_size=len(DEMO_TEXT.encode()),
            status="queued",
        )
        db.add(source)
        db.commit()
        ingest_source(source.id, DEV_SEED_USER_ID)
        print("Seeded demo source:", source.id)
    finally:
        db.close()


if __name__ == "__main__":
    main()
