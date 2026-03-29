"""Fix callback_outbox schema: align migration with ORM model.

Revision ID: 002_fix_outbox
Revises: 001_initial
Create Date: 2026-03-28

Changes:
- Rename ``id`` → ``outbox_id`` (String(26) primary key)
- Add FK on ``event_id`` → ``task_events.event_id``
- Drop ``signature`` and ``updated_at`` columns
- Add ``sent_at`` column (DateTime with timezone)

Migration strategy for SQLite (batch_alter_table recreate="always"):
  SQLite recreates the table and only copies columns that exist in BOTH
  old and new schemas (matched by name). Since ``outbox_id`` is a brand-new
  column, we must first add it as NULLABLE, backfill existing rows with
  generated ULID values, then recreate with the final NOT NULL + PK schema.
"""

from alembic import op
import sqlalchemy as sa

revision = "002_fix_outbox"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        _upgrade_sqlite(conn)
    else:
        _upgrade_generic(conn)


def _upgrade_sqlite(conn) -> None:
    # Step 1: Add outbox_id as NULLABLE so existing rows survive
    with op.batch_alter_table("callback_outbox", recreate="auto") as batch_op:
        batch_op.add_column(sa.Column("outbox_id", sa.String(26), nullable=True))

    # Step 2: Backfill outbox_id for any existing rows
    rows = conn.execute(sa.text(
        "SELECT rowid FROM callback_outbox WHERE outbox_id IS NULL"
    )).fetchall()
    if rows:
        from ulid import ULID
        for (rowid,) in rows:
            new_id = str(ULID())
            conn.execute(sa.text(
                "UPDATE callback_outbox SET outbox_id = :uid WHERE rowid = :rid"
            ), {"uid": new_id, "rid": rowid})

    # Step 3: Recreate with final schema (outbox_id NOT NULL + PK)
    with op.batch_alter_table("callback_outbox", recreate="always") as batch_op:
        batch_op.alter_column("outbox_id", nullable=False)
        batch_op.add_column(sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))

        batch_op.drop_column("id")
        batch_op.drop_column("signature")
        batch_op.drop_column("updated_at")

        batch_op.create_primary_key("pk_callback_outbox", ["outbox_id"])
        batch_op.create_foreign_key(
            "fk_callback_outbox_event_id",
            "task_events",
            ["event_id"],
            ["event_id"],
        )


def _upgrade_generic(conn) -> None:
    # PostgreSQL / MySQL: standard ALTER TABLE works fine
    op.add_column("callback_outbox", sa.Column("outbox_id", sa.String(26), nullable=True))
    op.add_column("callback_outbox", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))

    # Backfill outbox_id for existing rows
    rows = conn.execute(sa.text(
        "SELECT id FROM callback_outbox WHERE outbox_id IS NULL"
    )).fetchall()
    if rows:
        from ulid import ULID
        for (old_id,) in rows:
            new_id = str(ULID())
            conn.execute(sa.text(
                "UPDATE callback_outbox SET outbox_id = :uid WHERE id = :oid"
            ), {"uid": new_id, "oid": old_id})

    op.alter_column("callback_outbox", "outbox_id", nullable=False)
    op.drop_column("callback_outbox", "id")
    op.drop_column("callback_outbox", "signature")
    op.drop_column("callback_outbox", "updated_at")
    op.create_primary_key("pk_callback_outbox", "callback_outbox", ["outbox_id"])
    op.create_foreign_key(
        "fk_callback_outbox_event_id",
        "callback_outbox",
        "task_events",
        ["event_id"],
        ["event_id"],
    )


def downgrade() -> None:
    conn = op.get_bind()
    dialect = conn.dialect.name

    if dialect == "sqlite":
        _downgrade_sqlite()
    else:
        _downgrade_generic()


def _downgrade_sqlite() -> None:
    with op.batch_alter_table("callback_outbox", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_callback_outbox_event_id", type_="foreignkey")
        batch_op.drop_constraint("pk_callback_outbox", type_="primary")

        batch_op.drop_column("sent_at")
        batch_op.drop_column("outbox_id")

        batch_op.add_column(sa.Column("id", sa.Integer, primary_key=True, autoincrement=True))
        batch_op.add_column(sa.Column("signature", sa.String(128)))
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(timezone=True),
                       server_default=sa.func.now()),
        )


def _downgrade_generic() -> None:
    op.drop_constraint("fk_callback_outbox_event_id", "callback_outbox", type_="foreignkey")
    op.drop_constraint("pk_callback_outbox", "callback_outbox", type_="primary")

    op.add_column("callback_outbox", sa.Column("id", sa.Integer, autoincrement=True))
    op.add_column("callback_outbox", sa.Column("signature", sa.String(128)))
    op.add_column("callback_outbox",
                  sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

    op.create_primary_key("pk_callback_outbox_id", "callback_outbox", ["id"])
    op.drop_column("callback_outbox", "sent_at")
    op.drop_column("callback_outbox", "outbox_id")
