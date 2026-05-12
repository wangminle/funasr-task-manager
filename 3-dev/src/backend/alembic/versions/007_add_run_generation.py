"""Add run_generation to tasks and task_segments.

Revision ID: 007_add_run_generation
Revises: 006_add_server_enabled
Create Date: 2026-05-12

Changes:
- Add run_generation (smallint, default 0) to tasks table.
  Incremented on each dispatch so late-arriving results from a prior
  run can be detected and discarded.
- Add run_generation (smallint, default 0) to task_segments table.
  Same purpose for segment-level dispatches.
"""

from alembic import op
import sqlalchemy as sa

revision = "007_add_run_generation"
down_revision = "006_add_server_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("tasks", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column("run_generation", sa.SmallInteger(), nullable=False, server_default="0"),
            )
        with op.batch_alter_table("task_segments", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column("run_generation", sa.SmallInteger(), nullable=False, server_default="0"),
            )
    else:
        op.add_column(
            "tasks",
            sa.Column("run_generation", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        )
        op.add_column(
            "task_segments",
            sa.Column("run_generation", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("task_segments", recreate="auto") as batch_op:
            batch_op.drop_column("run_generation")
        with op.batch_alter_table("tasks", recreate="auto") as batch_op:
            batch_op.drop_column("run_generation")
    else:
        op.drop_column("task_segments", "run_generation")
        op.drop_column("tasks", "run_generation")
