"""Initial schema: files, tasks, task_events, server_instances, callback_outbox.

Revision ID: 001_initial
Revises:
Create Date: 2026-02-27
"""

from alembic import op
import sqlalchemy as sa


revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("file_id", sa.String(26), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("original_name", sa.Text, nullable=False),
        sa.Column("media_type", sa.String(32)),
        sa.Column("mime", sa.String(128)),
        sa.Column("duration_sec", sa.Float),
        sa.Column("codec", sa.String(64)),
        sa.Column("sample_rate", sa.Integer),
        sa.Column("channels", sa.SmallInteger),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("checksum_sha256", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False, server_default="UPLOADED"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "server_instances",
        sa.Column("server_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128)),
        sa.Column("host", sa.String(256), nullable=False),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("protocol_version", sa.String(32), nullable=False),
        sa.Column("server_type", sa.String(32)),
        sa.Column("supported_modes", sa.String(128)),
        sa.Column("max_concurrency", sa.Integer, nullable=False, server_default="4"),
        sa.Column("rtf_baseline", sa.Float),
        sa.Column("penalty_factor", sa.Float, server_default="0.1"),
        sa.Column("labels_json", sa.Text),
        sa.Column("status", sa.String(16), nullable=False, server_default="ONLINE"),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "tasks",
        sa.Column("task_id", sa.String(26), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("file_id", sa.String(26), sa.ForeignKey("files.file_id"), nullable=False),
        sa.Column("task_group_id", sa.String(26), index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("progress", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("eta_seconds", sa.Integer),
        sa.Column("assigned_server_id", sa.String(64), sa.ForeignKey("server_instances.server_id")),
        sa.Column("external_vendor", sa.String(32)),
        sa.Column("external_task_id", sa.Text),
        sa.Column("language", sa.String(16), server_default="zh"),
        sa.Column("options_json", sa.Text),
        sa.Column("result_path", sa.Text),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text),
        sa.Column("retry_count", sa.SmallInteger, server_default="0"),
        sa.Column("callback_url", sa.Text),
        sa.Column("callback_secret", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "task_events",
        sa.Column("event_id", sa.String(26), primary_key=True),
        sa.Column("task_id", sa.String(26), sa.ForeignKey("tasks.task_id"), nullable=False, index=True),
        sa.Column("from_status", sa.String(16), nullable=False),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("payload_json", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "callback_outbox",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(26), sa.ForeignKey("tasks.task_id"), nullable=False, index=True),
        sa.Column("event_id", sa.String(26), nullable=False),
        sa.Column("callback_url", sa.Text, nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False),
        sa.Column("signature", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("retry_count", sa.SmallInteger, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("callback_outbox")
    op.drop_table("task_events")
    op.drop_table("tasks")
    op.drop_table("server_instances")
    op.drop_table("files")
