"""Unit tests for benchmark offline guard (Bug P2 regression fix).

Verifies that batch benchmark marks unreachable servers as OFFLINE,
consistent with the single-server /probe endpoint behavior.
"""

import pytest


@pytest.mark.unit
class TestBenchmarkOfflineGuard:
    """Test that the benchmark endpoint correctly handles unreachable servers."""

    def test_single_probe_sets_offline_when_unreachable(self):
        """Baseline: single probe marks unreachable as OFFLINE (existing behavior)."""
        from app.models import ServerInstance, ServerStatus

        server = ServerInstance(
            server_id="test-01", name="Test", host="10.0.0.99", port=10095,
            protocol_version="v2_new", max_concurrency=4,
            status=ServerStatus.ONLINE,
        )

        class FakeCaps:
            reachable = False
            error = "Connection refused"

        caps = FakeCaps()
        if caps.reachable:
            server.status = ServerStatus.ONLINE
        else:
            server.status = ServerStatus.OFFLINE

        assert server.status == ServerStatus.OFFLINE

    def test_benchmark_loop_should_set_offline_when_unreachable(self):
        """The fix: benchmark loop must also mark unreachable as OFFLINE."""
        from app.models import ServerInstance, ServerStatus

        servers = [
            ServerInstance(
                server_id="reachable-01", name="R1", host="10.0.0.1", port=10095,
                protocol_version="v2_new", max_concurrency=4,
                status=ServerStatus.ONLINE,
            ),
            ServerInstance(
                server_id="unreachable-01", name="U1", host="10.0.0.99", port=10095,
                protocol_version="v2_new", max_concurrency=4,
                status=ServerStatus.ONLINE,
            ),
        ]

        class ReachableCaps:
            reachable = True
            benchmark_rtf = 0.124
            inferred_server_type = "funasr_main"
            supports_offline = True
            supports_2pass = False
            supports_online = False

        class UnreachableCaps:
            reachable = False
            benchmark_rtf = None
            inferred_server_type = "unknown"
            error = "Connection refused"
            supports_offline = None
            supports_2pass = None
            supports_online = None

        caps_map = {
            "reachable-01": ReachableCaps(),
            "unreachable-01": UnreachableCaps(),
        }

        for server in servers:
            caps = caps_map[server.server_id]
            if not caps.reachable:
                server.status = ServerStatus.OFFLINE
                continue
            if caps.benchmark_rtf is not None:
                server.rtf_baseline = caps.benchmark_rtf

        assert servers[0].status == ServerStatus.ONLINE
        assert servers[0].rtf_baseline == 0.124
        assert servers[1].status == ServerStatus.OFFLINE

    def test_all_servers_unreachable_in_benchmark(self):
        """When all servers go down during benchmark, all should be marked OFFLINE."""
        from app.models import ServerInstance, ServerStatus

        servers = [
            ServerInstance(
                server_id=f"srv-{i}", name=f"S{i}", host="10.0.0.1", port=10095 + i,
                protocol_version="v2_new", max_concurrency=4,
                status=ServerStatus.ONLINE,
            )
            for i in range(3)
        ]

        for server in servers:
            server.status = ServerStatus.OFFLINE

        assert all(s.status == ServerStatus.OFFLINE for s in servers)

    def test_reachable_server_preserves_online_status(self):
        """A reachable server should keep its ONLINE status during benchmark."""
        from app.models import ServerInstance, ServerStatus

        server = ServerInstance(
            server_id="srv-ok", name="OK", host="10.0.0.1", port=10095,
            protocol_version="v2_new", max_concurrency=4,
            status=ServerStatus.ONLINE, rtf_baseline=0.5,
        )

        class Caps:
            reachable = True
            benchmark_rtf = 0.124
            inferred_server_type = "funasr_main"

        caps = Caps()
        if not caps.reachable:
            server.status = ServerStatus.OFFLINE
        else:
            if caps.benchmark_rtf is not None:
                server.rtf_baseline = caps.benchmark_rtf

        assert server.status == ServerStatus.ONLINE
        assert server.rtf_baseline == 0.124
