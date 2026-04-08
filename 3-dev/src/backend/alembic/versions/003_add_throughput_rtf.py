"""Add throughput_rtf and benchmark_concurrency to server_instances.

Revision ID: 003_throughput_rtf
Revises: 002_fix_outbox
Create Date: 2026-04-08

Changes:
- Add ``throughput_rtf`` (Float, nullable) — concurrent throughput RTF
- Add ``benchmark_concurrency`` (SmallInteger, nullable) — concurrency level used
"""

from alembic import op
import sqlalchemy as sa

revision = "003_throughput_rtf"
down_revision = "002_fix_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("server_instances", recreate="auto") as batch_op:
            batch_op.add_column(sa.Column("throughput_rtf", sa.Float(), nullable=True))
            batch_op.add_column(sa.Column("benchmark_concurrency", sa.SmallInteger(), nullable=True))
    else:
        op.add_column("server_instances", sa.Column("throughput_rtf", sa.Float(), nullable=True))
        op.add_column("server_instances", sa.Column("benchmark_concurrency", sa.SmallInteger(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        with op.batch_alter_table("server_instances", recreate="auto") as batch_op:
            batch_op.drop_column("benchmark_concurrency")
            batch_op.drop_column("throughput_rtf")
    else:
        op.drop_column("server_instances", "benchmark_concurrency")
        op.drop_column("server_instances", "throughput_rtf")
