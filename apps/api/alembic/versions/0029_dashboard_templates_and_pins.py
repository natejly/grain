"""Dashboard templates, per-user pins, and the link from a bound dashboard back.

Three changes, one subsystem — the analytics dashboards, which until now had
tables and routes and no way for anything in the product to produce a row.

`dashboard_templates` is a dashboard definition with no dataset. It stores the
shape it requires (`required_columns_json`: column names and types) alongside a
spec written against those declared names. The contract is stored rather than
inferred from whatever dataset the template was first drawn over, because an
inferred contract accepts anything that parses and defers the mismatch to the
morning somebody opens the chart.

`dashboard_pins` is per *user*, not per workspace, and that is the whole reason
it is a separate table rather than a flag on `dashboards`. A workspace's
dashboards are shared; which of them you keep on your home screen, and where, is
yours. The unique constraint is (user_id, dashboard_id) so pinning is idempotent
— a second pin moves the tile instead of adding a duplicate — and the grid
geometry rides on the pin because two people looking at the same dashboards must
be able to arrange them differently.

`dashboards.template_id` and `dashboards.bindings_json` record where a bound
dashboard came from. Both are needed: the stored spec names the *dataset's*
columns, so without the binding map a spec grouping by "territory" cannot be
traced back to a template that declared "region". `bindings_json` defaults to
"{}", meaning "authored directly, bound to nothing".

Revision ID: 0029_dashboard_templates_and_pins
Revises: 0028_version_authors
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_dashboard_templates_and_pins"
down_revision = "0028_version_authors"
branch_labels = None
depends_on = None


def _add(table: str, column: sa.Column) -> None:
    """Idempotent add, matching 0024/0025: these databases have been through
    `create_all` as well as alembic, so a column can already be there and the
    migration still has to run to completion."""
    inspector = sa.inspect(op.get_bind())
    if column.name not in {existing["name"] for existing in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "dashboard_templates" not in tables:
        op.create_table(
            "dashboard_templates",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "created_by",
                sa.String(length=36),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.String(length=500), nullable=False, server_default=""),
            sa.Column("required_columns_json", sa.Text(), nullable=False),
            sa.Column("spec_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        op.create_index(
            "ix_dashboard_templates_workspace_id", "dashboard_templates", ["workspace_id"]
        )

    if "dashboard_pins" not in tables:
        op.create_table(
            "dashboard_pins",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column(
                "dashboard_id",
                sa.String(length=36),
                sa.ForeignKey("dashboards.id"),
                nullable=False,
            ),
            sa.Column("grid_x", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("grid_y", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("grid_w", sa.Integer(), nullable=False, server_default="6"),
            sa.Column("grid_h", sa.Integer(), nullable=False, server_default="4"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("user_id", "dashboard_id"),
        )
        op.create_index("ix_dashboard_pins_workspace_id", "dashboard_pins", ["workspace_id"])
        op.create_index("ix_dashboard_pins_user_id", "dashboard_pins", ["user_id"])
        op.create_index(
            "ix_dashboard_pins_dashboard_id", "dashboard_pins", ["dashboard_id"]
        )
        op.create_index(
            "ix_dashboard_pins_workspace_user",
            "dashboard_pins",
            ["workspace_id", "user_id"],
        )

    # No ForeignKey on template_id: SQLite cannot add a constrained column to an
    # existing table without rewriting it, and a batch rewrite of `dashboards`
    # to gain a constraint the application already enforces (every lookup is
    # workspace-scoped, and deleting a template clears the column) is a worse
    # trade than doing without.
    _add("dashboards", sa.Column("template_id", sa.String(length=36), nullable=True))
    _add(
        "dashboards",
        sa.Column("bindings_json", sa.Text(), nullable=False, server_default="{}"),
    )
    inspector = sa.inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("dashboards")}
    if "ix_dashboards_template_id" not in indexes:
        op.create_index("ix_dashboards_template_id", "dashboards", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_dashboards_template_id", table_name="dashboards")
    op.drop_column("dashboards", "bindings_json")
    op.drop_column("dashboards", "template_id")
    op.drop_table("dashboard_pins")
    op.drop_table("dashboard_templates")
