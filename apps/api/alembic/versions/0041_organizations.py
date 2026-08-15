"""Put an organization above every workspace, and a policy tier above every scope.

Until now the workspace was the top of the authority ladder: its owner could set
any policy, and there was no sentence in the schema that meant "the organization
forbids this even though the workspace owner wants it". Three tables and one
column add that sentence. `services.agent_loop.evaluate_policy` is where it is
enforced — as a `max` over verdict strictness applied after everything that can
loosen — and `org_tool_policies` is what it reads.

**The backfill is the whole risk of this migration**, because a workspace with no
organization is a row nothing can govern, and one left behind is a permanent hole
in a control whose entire value is that it has none. So:

- every existing workspace gets its own organization, named after it. Not one
  shared organization: grouping strangers' workspaces under a common posture
  would be inventing an authority relationship the data never contained, and
  whoever administered it would silently govern people who never agreed to it.
- every existing workspace *owner* becomes an admin of that organization. This
  is the only assignment that changes nobody's effective power on the day it
  runs: before the migration a workspace owner was the highest authority over
  their workspace, and after it they still are — the difference is that the
  authority is now expressible, delegable, and separable from workspace
  ownership. Promoting nobody would instead freeze every existing organization
  as unconfigurable forever, and promoting members as well would hand governance
  to people who had none.
- both allow-lists are left "", meaning *unbounded*. A migration must not start
  refusing models or harnesses that worked yesterday; an organization that has
  never been configured constrains nothing, which is exactly the `allow` identity
  element the policy clamp is built around.

`workspaces.organization_id` ends NOT NULL, via a SQLite batch rebuild, because
nullable-plus-a-convention is the version of this that quietly grows an orphan the
first time somebody inserts a workspace outside the ORM. Guarded like 0024–0040:
0001 runs `create_all` from live model metadata, so a database migrated from empty
already has all of this and every block below is a no-op for it.

Revision ID: 0041_organizations
Revises: 0040_personal_scope
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "0041_organizations"
down_revision = "0040_personal_scope"
branch_labels = None
depends_on = None

_NEW_TABLES = ("organizations", "org_memberships", "org_tool_policies")


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table in _NEW_TABLES:
        Base.metadata.tables[table].create(bind=bind, checkfirst=True)

    if "organization_id" in _columns("workspaces"):
        return

    # Added bare and nullable: SQLite cannot ALTER in a constraint, and the
    # column has nothing to point at until the loop below runs anyway. The
    # foreign key and the NOT NULL both arrive together in the batch rebuild at
    # the end, once every row has a real organization to satisfy them.
    op.add_column("workspaces", sa.Column("organization_id", sa.String(36), nullable=True))

    # Ids are minted in Python rather than by a SQL function because neither
    # SQLite nor Postgres offers the same one, and a migration that only runs on
    # the development database is not a migration.
    # Only `id` and `name` are read. Copying the workspace's own `created_at`
    # onto its organization would read better and would couple this migration to
    # a column it does not need — `tests/test_personal_scope_migration.py` builds
    # a `workspaces` table with just the columns under test, and a migration that
    # breaks on a narrower table is a migration that assumes more than it uses.
    # The organization was in fact created now, by this migration, so now is also
    # the honest timestamp.
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # A workspace always gets an organization; promoting its owners is
    # conditional on there being a `memberships` table to read them from.
    # `tests/test_personal_scope_migration.py` constructs a legacy database
    # holding only the tables its own subject touches, and the invariant this
    # migration must not break is "no workspace without an organization" — not
    # "every organization has an admin", which an empty membership table cannot
    # satisfy and which an org with no admin already fails safe on.
    has_memberships = "memberships" in sa.inspect(bind).get_table_names()
    workspaces = bind.execute(sa.text("SELECT id, name FROM workspaces")).fetchall()
    for workspace_id, name in workspaces:
        org_id = str(uuid.uuid4())
        bind.execute(
            sa.text(
                "INSERT INTO organizations "
                "(id, name, allowed_harnesses_json, allowed_models_json, "
                " created_at, updated_at) "
                "VALUES (:id, :name, '', '', :created, :created)"
            ),
            {"id": org_id, "name": (name or "Organization")[:160], "created": created_at},
        )
        bind.execute(
            sa.text("UPDATE workspaces SET organization_id = :org WHERE id = :ws"),
            {"org": org_id, "ws": workspace_id},
        )
        owners = (
            bind.execute(
                sa.text(
                    "SELECT user_id FROM memberships "
                    "WHERE workspace_id = :ws AND role = 'owner'"
                ),
                {"ws": workspace_id},
            ).fetchall()
            if has_memberships
            else []
        )
        for (user_id,) in owners:
            bind.execute(
                sa.text(
                    "INSERT INTO org_memberships "
                    "(id, organization_id, user_id, role, created_at) "
                    "VALUES (:id, :org, :user, 'admin', :created)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "org": org_id,
                    "user": user_id,
                    "created": created_at,
                },
            )

    # Batch mode because SQLite can alter neither a column's nullability nor its
    # constraints in place. It rebuilds `workspaces`, which is the table most
    # others carry a foreign key to; that is safe here because SQLite resolves
    # those references by table name and the replacement keeps the name.
    #
    # The constraint is named, unlike the anonymous one `create_all` produces on
    # a database built from empty — batch mode requires a name to emit. The two
    # paths therefore differ in that one label and in nothing that constrains a
    # row, which is the same cosmetic divergence 0040's rebuilds accepted.
    with op.batch_alter_table("workspaces") as batch:
        batch.alter_column("organization_id", existing_type=sa.String(36), nullable=False)
        batch.create_foreign_key(
            "fk_workspaces_organization_id", "organizations", ["organization_id"], ["id"]
        )
    op.create_index("ix_workspaces_organization_id", "workspaces", ["organization_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if "organization_id" in _columns("workspaces"):
        indexes = {
            index["name"] for index in sa.inspect(bind).get_indexes("workspaces")
        }
        if "ix_workspaces_organization_id" in indexes:
            op.drop_index("ix_workspaces_organization_id", table_name="workspaces")
        with op.batch_alter_table("workspaces") as batch:
            batch.drop_column("organization_id")
    for table in reversed(_NEW_TABLES):
        Base.metadata.tables[table].drop(bind=bind, checkfirst=True)
