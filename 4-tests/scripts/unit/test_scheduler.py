"""Scheduler unit tests - LPT, EFT, capacity-aware, concurrency penalty, ETA."""

import pytest

from app.services.scheduler import (
    RTFTracker, ServerProfile, TaskScheduler, DEFAULT_RTF,
)


def _make_server(sid: str, concurrency: int = 4, rtf: float = 0.3, running: int = 0, penalty: float = 0.1) -> ServerProfile:
    return ServerProfile(server_id=sid, host="10.0.0.1", port=10095, max_concurrency=concurrency, rtf_baseline=rtf, penalty_factor=penalty, status="ONLINE", running_tasks=running)


@pytest.mark.unit
class TestRTFTracker:
    def test_record_and_p90(self):
        """T-M2-15: RTF rolling window p90."""
        tracker = RTFTracker(window_size=50)
        for v in [0.2, 0.25, 0.3, 0.28, 0.22, 0.35, 0.40, 0.18, 0.32, 0.27]:
            tracker.record("s1", v)
        p90 = tracker.get_p90("s1")
        assert 0.35 <= p90 <= 0.40

    def test_window_overflow(self):
        tracker = RTFTracker(window_size=5)
        for i in range(10):
            tracker.record("s1", 0.1 * (i + 1))
        assert tracker.get_window_size("s1") == 5

    def test_empty_returns_default(self):
        tracker = RTFTracker()
        assert tracker.get_p90("unknown") == DEFAULT_RTF

    def test_clear_server(self):
        tracker = RTFTracker()
        tracker.record("s1", 0.3)
        tracker.clear("s1")
        assert tracker.get_window_size("s1") == 0


@pytest.mark.unit
class TestSchedulerLPT:
    def test_lpt_sorts_longest_first(self):
        """T-M2-10: LPT sorts tasks by duration descending."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "short", "audio_duration_sec": 600},
            {"task_id": "long", "audio_duration_sec": 3600},
            {"task_id": "medium", "audio_duration_sec": 1800},
        ]
        servers = [_make_server("s1", concurrency=1)]
        decisions = sched.schedule_batch(tasks, servers)
        order = [d.task_id for d in decisions]
        assert order[0] == "long"
        assert order[1] == "medium"
        assert order[2] == "short"

    def test_earliest_finish_time_assignment(self):
        """T-M2-11: Tasks assigned to earliest available slot."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "t1", "audio_duration_sec": 600},
            {"task_id": "t2", "audio_duration_sec": 300},
            {"task_id": "t3", "audio_duration_sec": 100},
        ]
        servers = [_make_server("s1", concurrency=2), _make_server("s2", concurrency=2)]
        decisions = sched.schedule_batch(tasks, servers)
        assert len(decisions) == 3
        server_ids = {d.server_id for d in decisions}
        assert len(server_ids) >= 1

    def test_offline_server_excluded(self):
        """T-M2-13: OFFLINE servers not included in scheduling."""
        sched = TaskScheduler()
        tasks = [{"task_id": "t1", "audio_duration_sec": 600}]
        servers = [
            _make_server("online-1"),
            ServerProfile(server_id="offline-1", host="10.0.0.2", port=10095, max_concurrency=4, status="OFFLINE"),
        ]
        decisions = sched.schedule_batch(tasks, servers)
        assert all(d.server_id == "online-1" for d in decisions)

    def test_no_online_servers_returns_empty(self):
        sched = TaskScheduler()
        tasks = [{"task_id": "t1", "audio_duration_sec": 600}]
        servers = [ServerProfile(server_id="off", host="x", port=1, max_concurrency=4, status="OFFLINE")]
        decisions = sched.schedule_batch(tasks, servers)
        assert decisions == []


@pytest.mark.unit
class TestCapacityAwareScheduling:
    """Tests for capacity-aware batch scheduling with heterogeneous servers."""

    def test_faster_server_gets_bigger_task(self):
        """Fastest server (lowest RTF) should be assigned the longest task."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "huge", "audio_duration_sec": 7200},
            {"task_id": "small", "audio_duration_sec": 60},
        ]
        fast = _make_server("fast", concurrency=1, rtf=0.1)
        slow = _make_server("slow", concurrency=1, rtf=0.5)
        decisions = sched.schedule_batch(tasks, [fast, slow])
        decision_map = {d.task_id: d.server_id for d in decisions}
        assert decision_map["huge"] == "fast"

    def test_balanced_distribution_three_servers(self):
        """With 3 heterogeneous servers, batch should distribute across all of them."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "t1", "audio_duration_sec": 5400},
            {"task_id": "t2", "audio_duration_sec": 2700},
            {"task_id": "t3", "audio_duration_sec": 1200},
            {"task_id": "t4", "audio_duration_sec": 600},
            {"task_id": "t5", "audio_duration_sec": 300},
            {"task_id": "t6", "audio_duration_sec": 120},
            {"task_id": "t7", "audio_duration_sec": 60},
            {"task_id": "t8", "audio_duration_sec": 30},
        ]
        fast = _make_server("fast", concurrency=4, rtf=0.1)
        medium = _make_server("medium", concurrency=4, rtf=0.25)
        slow = _make_server("slow", concurrency=4, rtf=0.5)
        decisions = sched.schedule_batch(tasks, [fast, medium, slow])
        assert len(decisions) == 8

        server_tasks = {}
        for d in decisions:
            server_tasks.setdefault(d.server_id, []).append(d.task_id)
        assert len(server_tasks) == 3, "All 3 servers should receive tasks"

        longest_task = next(d for d in decisions if d.task_id == "t1")
        assert longest_task.server_id == "fast", "Longest task should go to fastest server"

    def test_running_tasks_reduce_available_slots(self):
        """Servers with running tasks should have fewer slots available."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "t1", "audio_duration_sec": 600},
            {"task_id": "t2", "audio_duration_sec": 600},
        ]
        full = _make_server("full", concurrency=2, running=2)
        free = _make_server("free", concurrency=2, running=0)
        decisions = sched.schedule_batch(tasks, [full, free])
        assert all(d.server_id == "free" for d in decisions)

    def test_all_slots_occupied_returns_empty(self):
        """If all server slots are occupied, return empty schedule."""
        sched = TaskScheduler()
        tasks = [{"task_id": "t1", "audio_duration_sec": 600}]
        full1 = _make_server("s1", concurrency=2, running=2)
        full2 = _make_server("s2", concurrency=2, running=2)
        decisions = sched.schedule_batch(tasks, [full1, full2])
        assert decisions == []

    def test_simulated_full_batch_scenario(self):
        """Simulate the real full E2E scenario: 8 files across 3 servers.

        Verify that the 178.9MB long audio (GuruMorningTeaching ~5400s audio)
        goes to the fastest server, not the slowest.
        """
        sched = TaskScheduler()
        tasks = [
            {"task_id": "m4a-36MB", "audio_duration_sec": 1500},
            {"task_id": "mp3-178MB", "audio_duration_sec": 5400},
            {"task_id": "mp4-412MB", "audio_duration_sec": 420},
            {"task_id": "mp4-6MB", "audio_duration_sec": 45},
            {"task_id": "wav-3MB", "audio_duration_sec": 120},
            {"task_id": "mp4-20MB", "audio_duration_sec": 180},
            {"task_id": "wav-5MB", "audio_duration_sec": 180},
            {"task_id": "mp4-9MB", "audio_duration_sec": 90},
        ]
        s95 = _make_server("funasr-10095", concurrency=4, rtf=0.15)
        s96 = _make_server("funasr-10096", concurrency=4, rtf=0.20)
        s97 = _make_server("funasr-10097", concurrency=4, rtf=0.35)
        decisions = sched.schedule_batch(tasks, [s95, s96, s97])
        assert len(decisions) == 8

        decision_map = {d.task_id: d for d in decisions}
        longest = decision_map["mp3-178MB"]
        assert longest.server_id == "funasr-10095", (
            "The longest audio must go to the fastest server (10095, RTF=0.15)"
        )

        server_finish_times = {}
        for d in decisions:
            cur = server_finish_times.get(d.server_id, 0)
            server_finish_times[d.server_id] = max(cur, d.estimated_finish)

        makespan = max(server_finish_times.values())
        naive_makespan = 5400 * 0.35 + 5.0
        assert makespan < naive_makespan, (
            f"Capacity-aware makespan ({makespan:.0f}s) should be "
            f"much less than naive single-slowest ({naive_makespan:.0f}s)"
        )


@pytest.mark.unit
class TestQuotaAllocation:
    """Tests for speed-proportional quota allocation (makespan-priority, no min guarantee)."""

    def test_proportional_allocation_heterogeneous_servers(self):
        """With 8 tasks across 3 heterogeneous servers, fast server should dominate."""
        sched = TaskScheduler()
        tasks = [{"task_id": f"t{i}", "audio_duration_sec": 100 * (i + 1)} for i in range(8)]
        fast = _make_server("fast", concurrency=4, rtf=0.124)
        medium = _make_server("medium", concurrency=4, rtf=0.656)
        slow = _make_server("slow", concurrency=4, rtf=0.737)
        decisions = sched.schedule_batch(tasks, [fast, medium, slow])

        server_counts = {}
        for d in decisions:
            server_counts[d.server_id] = server_counts.get(d.server_id, 0) + 1
        assert server_counts.get("fast", 0) >= 4, "Fast server (5x faster) should get at least 4 of 8 tasks"
        assert server_counts["fast"] > server_counts.get("medium", 0)
        assert server_counts["fast"] > server_counts.get("slow", 0)

    def test_faster_server_gets_more_tasks(self):
        """Faster server should get proportionally more tasks."""
        sched = TaskScheduler()
        tasks = [{"task_id": f"t{i}", "audio_duration_sec": 300} for i in range(12)]
        fast = _make_server("fast", concurrency=8, rtf=0.1)
        slow = _make_server("slow", concurrency=8, rtf=0.5)
        decisions = sched.schedule_batch(tasks, [fast, slow])

        server_counts = {}
        for d in decisions:
            server_counts[d.server_id] = server_counts.get(d.server_id, 0) + 1
        assert server_counts["fast"] > server_counts["slow"]

    def test_single_task_single_server(self):
        """1 task + 1 server: no quota issue."""
        sched = TaskScheduler()
        tasks = [{"task_id": "t1", "audio_duration_sec": 600}]
        decisions = sched.schedule_batch(tasks, [_make_server("s1")])
        assert len(decisions) == 1
        assert decisions[0].server_id == "s1"

    def test_more_servers_than_tasks(self):
        """When tasks < servers, prefer fastest server — don't force spread."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "t1", "audio_duration_sec": 600},
            {"task_id": "t2", "audio_duration_sec": 300},
        ]
        s1 = _make_server("s1", concurrency=4, rtf=0.1)
        s2 = _make_server("s2", concurrency=4, rtf=0.3)
        s3 = _make_server("s3", concurrency=4, rtf=0.5)
        decisions = sched.schedule_batch(tasks, [s1, s2, s3])
        assert all(d.server_id == "s1" for d in decisions), (
            "Both tasks should go to fastest server when tasks < servers"
        )

    def test_quota_respects_available_slots(self):
        """Quota should not exceed server's free slot count."""
        sched = TaskScheduler()
        tasks = [{"task_id": f"t{i}", "audio_duration_sec": 300} for i in range(5)]
        fast = _make_server("fast", concurrency=4, rtf=0.1, running=3)
        slow = _make_server("slow", concurrency=4, rtf=0.5, running=0)
        decisions = sched.schedule_batch(tasks, [fast, slow])
        fast_count = sum(1 for d in decisions if d.server_id == "fast")
        slow_count = sum(1 for d in decisions if d.server_id == "slow")
        assert fast_count <= 1, "Fast server only has 1 free slot"
        assert slow_count <= 4, "Slow server has 4 free slots"
        assert len(decisions) == 5

    def test_select_dispatchable_now_limits_to_current_free_slots(self):
        """Only first-wave tasks should be started immediately."""
        sched = TaskScheduler()
        tasks = [{"task_id": f"t{i}", "audio_duration_sec": 300 + i * 10} for i in range(8)]
        fast = _make_server("fast", concurrency=2, rtf=0.1)
        medium = _make_server("medium", concurrency=1, rtf=0.2)
        slow = _make_server("slow", concurrency=1, rtf=0.5)

        decisions = sched.schedule_batch(tasks, [fast, medium, slow])
        immediate = sched.select_dispatchable_now(decisions)

        assert len(decisions) == 8
        assert len(immediate) == 4
        assert all(d.estimated_start == 0.0 for d in immediate)
        assert all(d.task_id in {decision.task_id for decision in decisions} for d in immediate)

    def test_real_world_8_task_fast_server_dominates(self):
        """Reproduce the quant-course 8-task scenario: fast server should dominate."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "ep4", "audio_duration_sec": 1548},
            {"task_id": "ep6", "audio_duration_sec": 1065},
            {"task_id": "ep5", "audio_duration_sec": 871},
            {"task_id": "ep7", "audio_duration_sec": 613},
            {"task_id": "ep3", "audio_duration_sec": 516},
            {"task_id": "ep2", "audio_duration_sec": 274},
            {"task_id": "ep8", "audio_duration_sec": 194},
            {"task_id": "ep1", "audio_duration_sec": 24},
        ]
        s95 = _make_server("funasr-10095", concurrency=4, rtf=0.124)
        s97 = _make_server("funasr-10097", concurrency=4, rtf=0.656)
        s96 = _make_server("funasr-10096", concurrency=4, rtf=0.737)
        decisions = sched.schedule_batch(tasks, [s95, s97, s96])

        server_counts = {}
        for d in decisions:
            server_counts[d.server_id] = server_counts.get(d.server_id, 0) + 1

        assert server_counts["funasr-10095"] >= server_counts.get("funasr-10097", 0)
        assert server_counts["funasr-10095"] >= server_counts.get("funasr-10096", 0)

        longest = next(d for d in decisions if d.task_id == "ep4")
        assert longest.server_id == "funasr-10095", "Longest task should still go to fastest server"

    def test_small_batch_no_forced_spread(self):
        """Regression: 2 tasks + 3 servers must NOT force tasks to slow nodes."""
        sched = TaskScheduler()
        tasks = [
            {"task_id": "t1", "audio_duration_sec": 600},
            {"task_id": "t2", "audio_duration_sec": 300},
        ]
        fast = _make_server("fast", concurrency=4, rtf=0.1)
        medium = _make_server("medium", concurrency=4, rtf=0.3)
        slow = _make_server("slow", concurrency=4, rtf=0.5)
        decisions = sched.schedule_batch(tasks, [fast, medium, slow])

        fast_makespan = max(d.estimated_finish for d in decisions if d.server_id == "fast")
        forced_spread_makespan = 300 * 0.3 + 5.0
        assert fast_makespan < forced_spread_makespan, (
            f"Small batch makespan ({fast_makespan:.0f}s) must be better than "
            f"forced-spread ({forced_spread_makespan:.0f}s)"
        )

    def test_allocate_quotas_proportional(self):
        """Quotas should be strictly proportional to speed, fast server gets most."""
        sched = TaskScheduler()
        servers = [
            _make_server("fast", concurrency=4, rtf=0.124),
            _make_server("medium", concurrency=4, rtf=0.656),
            _make_server("slow", concurrency=4, rtf=0.737),
        ]
        quotas = sched._allocate_quotas(8, servers)
        assert sum(quotas.values()) == 8
        assert quotas["fast"] > quotas["slow"]
        assert quotas["fast"] >= quotas["medium"]

    def test_backfill_small_batch_all_to_fast(self):
        """Backfill scenario: 3 remaining tasks with extreme speed diff → all to fast."""
        sched = TaskScheduler()
        fast = _make_server("fast", concurrency=4, rtf=0.1)
        medium = _make_server("medium", concurrency=4, rtf=0.8)
        slow = _make_server("slow", concurrency=4, rtf=0.95)
        quotas = sched._allocate_quotas(3, [fast, medium, slow])
        assert sum(quotas.values()) == 3
        assert quotas["fast"] >= 2, (
            f"Fast server (10x faster) should get at least 2 of 3, got {quotas['fast']}"
        )

    def test_backfill_single_task_to_fastest(self):
        """Backfill: 1 remaining task always goes to the server with best EFT."""
        sched = TaskScheduler()
        tasks = [{"task_id": "remaining", "audio_duration_sec": 120}]
        fast = _make_server("fast", concurrency=4, rtf=0.1)
        slow = _make_server("slow", concurrency=4, rtf=0.5)
        decisions = sched.schedule_batch(tasks, [fast, slow])
        assert decisions[0].server_id == "fast"

    def test_extreme_speed_diff_slow_gets_zero_quota(self):
        """With extreme speed difference, slowest server may get 0 quota."""
        sched = TaskScheduler()
        fast = _make_server("fast", concurrency=4, rtf=0.05)
        slow = _make_server("slow", concurrency=4, rtf=2.0)
        quotas = sched._allocate_quotas(4, [fast, slow])
        assert sum(quotas.values()) == 4
        assert quotas["fast"] >= 3, "20:1 speed ratio → fast should get almost all"

    def test_quota_sum_always_equals_effective_count(self):
        """Quota sum must always equal min(task_count, total_free)."""
        sched = TaskScheduler()
        servers = [
            _make_server("s1", concurrency=4, rtf=0.1),
            _make_server("s2", concurrency=4, rtf=0.3),
            _make_server("s3", concurrency=4, rtf=0.7),
        ]
        for task_count in [1, 3, 5, 8, 12, 15, 25]:
            quotas = sched._allocate_quotas(task_count, servers)
            expected = min(task_count, 12)
            assert sum(quotas.values()) == expected, (
                f"task_count={task_count}: sum={sum(quotas.values())} != {expected}"
            )


@pytest.mark.unit
class TestConcurrencyPenalty:
    def test_penalty_increases_eta(self):
        """T-M2-12: running_tasks=8 produces higher ETA than running_tasks=2."""
        sched = TaskScheduler()
        light = _make_server("s1", running=2, penalty=0.1)
        heavy = _make_server("s2", running=8, penalty=0.1)
        eta_light = sched.estimate_processing_time(600, light)
        eta_heavy = sched.estimate_processing_time(600, heavy)
        assert eta_heavy > eta_light

    def test_effective_rtf_includes_penalty(self):
        sched = TaskScheduler()
        srv = _make_server("s1", running=4, penalty=0.1, rtf=0.3)
        rtf = sched.get_effective_rtf(srv)
        expected = 0.3 * (1.0 + 0.1 * 4)
        assert abs(rtf - expected) < 0.001


@pytest.mark.unit
class TestETACalculation:
    def test_eta_formula(self):
        """T-M2-14: ETA = queue_time + asr_time + overhead."""
        sched = TaskScheduler()
        srv = _make_server("s1", running=0)
        eta = sched.calculate_task_eta(audio_duration_sec=600, server=srv, queue_position=0)
        expected = int(600 * sched.get_effective_rtf(srv) + 5.0)
        assert eta == expected

    def test_eta_with_queue_position(self):
        sched = TaskScheduler()
        srv = _make_server("s1", concurrency=2, running=2)
        eta_front = sched.calculate_task_eta(600, srv, queue_position=0, avg_queue_task_duration=100)
        eta_back = sched.calculate_task_eta(600, srv, queue_position=5, avg_queue_task_duration=100)
        assert eta_back > eta_front


@pytest.mark.unit
class TestCalibration:
    def test_calibration_records_rtf(self):
        sched = TaskScheduler()
        sched.calibrate_after_completion("s1", audio_duration_sec=600, actual_duration_sec=180)
        assert sched.rtf_tracker.get_window_size("s1") == 1

    def test_calibration_penalty_increase_on_large_deviation(self):
        """T-M2-16: deviation > 30% triggers penalty_factor increase."""
        sched = TaskScheduler()
        result = sched.calibrate_after_completion(
            "s1", audio_duration_sec=600, actual_duration_sec=300,
            predicted_duration_sec=180, current_penalty_factor=0.1,
        )
        assert result["deviation"] is not None
        assert result["deviation"] > 1.3
        assert result["new_penalty_factor"] > 0.1

    def test_calibration_no_change_within_threshold(self):
        sched = TaskScheduler()
        result = sched.calibrate_after_completion(
            "s1", audio_duration_sec=600, actual_duration_sec=185,
            predicted_duration_sec=180, current_penalty_factor=0.1,
        )
        assert result["new_penalty_factor"] == 0.1


@pytest.mark.unit
class TestCapacityComparison:
    def test_compare_server_capacity(self):
        sched = TaskScheduler()
        servers = [
            _make_server("fast", rtf=0.1),
            _make_server("medium", rtf=0.25),
            _make_server("slow", rtf=0.5),
        ]
        comparison = sched.compare_server_capacity(servers)
        assert len(comparison) == 3
        assert comparison[0]["server_id"] == "fast"
        assert comparison[0]["relative_speed"] == 1.0
        assert comparison[1]["relative_speed"] == pytest.approx(0.4, abs=0.01)
        assert comparison[2]["relative_speed"] == pytest.approx(0.2, abs=0.01)

    def test_compare_empty(self):
        sched = TaskScheduler()
        assert sched.compare_server_capacity([]) == []


@pytest.mark.unit
class TestSlotQueues:
    """Tests for build_slot_queues grouping and ordering."""

    def test_groups_by_slot_key(self):
        from app.services.scheduler import ScheduleDecision
        sched = TaskScheduler()
        decisions = [
            ScheduleDecision("t1", "s1", 0, 0.0, 10.0, 10.0, 1, 60.0),
            ScheduleDecision("t2", "s1", 1, 0.0, 12.0, 12.0, 1, 80.0),
            ScheduleDecision("t3", "s1", 0, 10.0, 8.0, 18.0, 2, 50.0),
            ScheduleDecision("t4", "s2", 0, 0.0, 15.0, 15.0, 1, 100.0),
        ]
        queues = sched.build_slot_queues(decisions)
        assert len(queues) == 3
        assert "s1:0" in queues
        assert "s1:1" in queues
        assert "s2:0" in queues
        assert len(queues["s1:0"].decisions) == 2
        assert len(queues["s1:1"].decisions) == 1
        assert queues["s1:0"].decisions[0].task_id == "t1"
        assert queues["s1:0"].decisions[1].task_id == "t3"

    def test_ordered_by_estimated_start(self):
        from app.services.scheduler import ScheduleDecision
        sched = TaskScheduler()
        decisions = [
            ScheduleDecision("late", "s1", 0, 20.0, 5.0, 25.0, 3, 30.0),
            ScheduleDecision("early", "s1", 0, 0.0, 10.0, 10.0, 1, 60.0),
            ScheduleDecision("mid", "s1", 0, 10.0, 8.0, 18.0, 2, 50.0),
        ]
        queues = sched.build_slot_queues(decisions)
        q = queues["s1:0"]
        assert [d.task_id for d in q.decisions] == ["early", "mid", "late"]

    def test_empty_decisions(self):
        sched = TaskScheduler()
        queues = sched.build_slot_queues([])
        assert queues == {}

    def test_audio_duration_preserved(self):
        from app.services.scheduler import ScheduleDecision
        sched = TaskScheduler()
        decisions = [
            ScheduleDecision("t1", "s1", 0, 0.0, 10.0, 10.0, 1, 120.5),
        ]
        queues = sched.build_slot_queues(decisions)
        assert queues["s1:0"].decisions[0].audio_duration_sec == 120.5

    def test_schedule_batch_populates_audio_duration(self):
        sched = TaskScheduler()
        tasks = [
            {"task_id": "t1", "audio_duration_sec": 60.0},
            {"task_id": "t2", "audio_duration_sec": 120.0},
        ]
        servers = [_make_server("s1", rtf=0.2, concurrency=4)]
        decisions = sched.schedule_batch(tasks, servers)
        dur_map = {d.task_id: d.audio_duration_sec for d in decisions}
        assert dur_map["t1"] == 60.0
        assert dur_map["t2"] == 120.0
