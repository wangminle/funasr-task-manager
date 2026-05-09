"""Fix task_events.from_status nullable, server_instances.status default, and updated_at nullable.

Revision ID: 005_fix_nullable_defaults
Revises: 004_task_segments
Create Date: 2026-05-09

Changes:
- H2: task_events.from_status → nullable=True (first event has no prior status)
- H3: server_instances.status server_default → "OFFLINE" (unprobed servers should be offline)
- L19: files/server_instances/tasks updated_at → nullable=False (align with ORM)
"""

from alembic import op
import sqlalchemy as sa

revision = "005_fix_nullable_defaults"
down_revision = "004_task_segments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    _tables_with_updated_at = ["files", "server_instances", "tasks"]

    if dialect == "sqlite":
        with op.batch_alter_table("task_events", recreate="auto") as batch_op:
            batch_op.alter_column("from_status", existing_type=sa.String(16), nullable=True)
        with op.batch_alter_table("server_instances", recreate="auto") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=sa.String(16),
                server_default="OFFLINE",
            )
        for tbl in _tables_with_updated_at:
            with op.batch_alter_table(tbl, recreate="auto") as batch_op:
                batch_op.alter_column(
                    "updated_at",
                    existing_type=sa.DateTime(timezone=True),
                    nullable=False,
                    existing_server_default=sa.func.now(),
                )
    else:
        op.alter_column(
            "task_events", "from_status",
            existing_type=sa.String(16),
            nullable=True,
        )
        op.alter_column(
            "server_instances", "status",
            existing_type=sa.String(16),
            server_default="OFFLINE",
        )
        for tbl in _tables_with_updated_at:
            op.execute(sa.text(f"UPDATE {tbl} SET updated_at = created_at WHERE updated_at IS NULL"))
            op.alter_column(
                tbl, "updated_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("task_events", recreate="auto") as batch_op:
            batch_op.alter_column("from_status", existing_type=sa.String(16), nullable=False)
        with op.batch_alter_table("server_instances", recreate="auto") as batch_op:
            batch_op.alter_column(
                "status",
                existing_type=sa.String(16),
                server_default="ONLINE",
            )
    else:
        op.alter_column(
            "task_events", "from_status",
            existing_type=sa.String(16),
            nullable=False,
        )
        op.alter_column(
            "server_instances", "status",
            existing_type=sa.String(16),
            server_default="ONLINE",
        )
