"""Add enabled field to server_instances.

Revision ID: 006_add_server_enabled
Revises: 005_fix_nullable_defaults
Create Date: 2026-05-10

Changes:
- Add enabled (boolean, default True) to server_instances table.
  Allows disabling a server from scheduling without heartbeat overriding.
"""

from alembic import op
import sqlalchemy as sa

revision = "006_add_server_enabled"
down_revision = "005_fix_nullable_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("server_instances", recreate="auto") as batch_op:
            batch_op.add_column(
                sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
            )
    else:
        op.add_column(
            "server_instances",
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        )


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("server_instances", recreate="auto") as batch_op:
            batch_op.drop_column("enabled")
    else:
        op.drop_column("server_instances", "enabled")
