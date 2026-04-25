"""Create task_segments table for VAD parallel transcription.

Revision ID: 004_task_segments
Revises: 003_throughput_rtf
Create Date: 2026-04-24

Changes:
- Create ``task_segments`` table with segment-level tracking fields
- Add index on (task_id) for parent-task lookups
- Add foreign keys to tasks and server_instances
"""

from alembic import op
import sqlalchemy as sa

revision = "004_task_segments"
down_revision = "003_throughput_rtf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_segments",
        sa.Column("segment_id", sa.String(26), primary_key=True),
        sa.Column(
            "task_id", sa.String(26),
            sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("segment_index", sa.SmallInteger(), nullable=False),
        sa.Column("source_start_ms", sa.Integer(), nullable=False),
        sa.Column("source_end_ms", sa.Integer(), nullable=False),
        sa.Column("keep_start_ms", sa.Integer(), nullable=False),
        sa.Column("keep_end_ms", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column(
            "assigned_server_id", sa.String(64),
            sa.ForeignKey("server_instances.server_id"),
            nullable=True,
        ),
        sa.Column("retry_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("raw_result_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_task_segments_status", "task_segments", ["status"])
    op.create_index(
        "ix_task_segments_task_index",
        "task_segments",
        ["task_id", "segment_index"],
        unique=True,
    )
    op.create_index(
        "ix_task_segments_assigned_server",
        "task_segments",
        ["assigned_server_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_segments_assigned_server", table_name="task_segments")
    op.drop_index("ix_task_segments_task_index", table_name="task_segments")
    op.drop_index("ix_task_segments_status", table_name="task_segments")
    op.drop_table("task_segments")
