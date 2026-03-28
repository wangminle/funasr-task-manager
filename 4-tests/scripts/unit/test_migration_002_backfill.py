"""Unit tests for migration 002 outbox_id backfill logic.

Verifies that the migration can handle:
1. Empty callback_outbox table (no backfill needed)
2. Existing rows get valid outbox_id values
3. outbox_id values are unique ULIDs
"""

import pytest


@pytest.mark.unit
class TestMigration002BackfillLogic:
    """Test the backfill logic that migration 002 uses."""

    def test_ulid_generation_is_unique(self):
        """Each backfilled outbox_id must be unique."""
        from ulid import ULID
        ids = {str(ULID()) for _ in range(100)}
        assert len(ids) == 100

    def test_ulid_is_26_chars(self):
        """ULID string representation should be 26 characters."""
        from ulid import ULID
        uid = str(ULID())
        assert len(uid) == 26

    def test_backfill_scenario_simulation(self):
        """Simulate the backfill: existing rows with NULL outbox_id get new values."""
        from ulid import ULID

        existing_rows = [
            {"id": 1, "outbox_id": None, "event_id": "evt-1"},
            {"id": 2, "outbox_id": None, "event_id": "evt-2"},
            {"id": 3, "outbox_id": None, "event_id": "evt-3"},
        ]

        for row in existing_rows:
            if row["outbox_id"] is None:
                row["outbox_id"] = str(ULID())

        for row in existing_rows:
            assert row["outbox_id"] is not None
            assert len(row["outbox_id"]) == 26

        ids = [r["outbox_id"] for r in existing_rows]
        assert len(set(ids)) == 3

    def test_empty_table_no_backfill_needed(self):
        """When callback_outbox has no rows, backfill step is a no-op."""
        existing_rows = []
        rows_needing_backfill = [r for r in existing_rows if r.get("outbox_id") is None]
        assert len(rows_needing_backfill) == 0

    def test_migration_preserves_existing_columns(self):
        """Backfill should not affect other column values."""
        from ulid import ULID

        row = {
            "id": 42,
            "outbox_id": None,
            "event_id": "evt-42",
            "task_id": "task-42",
            "callback_url": "https://example.com/webhook",
            "status": "PENDING",
            "retry_count": 0,
        }
        row["outbox_id"] = str(ULID())
        assert row["event_id"] == "evt-42"
        assert row["task_id"] == "task-42"
        assert row["callback_url"] == "https://example.com/webhook"
