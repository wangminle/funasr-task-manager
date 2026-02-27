"""Scheduler unit tests - LPT, EFT, concurrency penalty, ETA."""

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
