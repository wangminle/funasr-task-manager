"""Task scheduler: LPT + Earliest-Finish-Time + capacity-aware scheduling.

Scheduling algorithm:
1. Estimate processing time per task: p(i,s) = duration(i) * rtf(s) + overhead
   where rtf(s) uses per-server RTF baseline (from benchmark/history) for
   capacity-aware assignment — faster servers handle bigger files.
2. Allocate per-server task quotas proportional to throughput speed
   (max_concurrency / base_rtf). This uses per-slot RTF from production
   history (P90) or benchmark with representative samples, scaled by slot
   count — more accurate than throughput_rtf which is measured with very
   short benchmark samples.
3. Expand each server's FREE slots into virtual machines (respecting running_tasks)
4. Sort tasks by estimated duration descending (LPT: Longest Processing Time First)
5. Assign each task to the eligible slot with earliest expected finish time,
   respecting per-server quota limits.
6. Online correction: update RTF rolling statistics after each task completes
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_RTF = 0.3
DEFAULT_OVERHEAD = 5.0
RTF_WINDOW_SIZE = 50
CALIBRATION_THRESHOLD = 0.3
PENALTY_INCREASE_RATE = 0.2
PENALTY_DECREASE_RATE = 0.1
CONSECUTIVE_FAST_THRESHOLD = 10
IMMEDIATE_START_TOLERANCE = 1e-9


@dataclass
class ServerSlot:
    server_id: str
    slot_index: int
    earliest_free: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.server_id}:{self.slot_index}"


@dataclass
class ScheduleDecision:
    task_id: str
    server_id: str
    slot_index: int
    estimated_start: float
    estimated_duration: float
    estimated_finish: float
    queue_position: int = 0
    audio_duration_sec: float = 0.0


@dataclass
class SlotQueue:
    """Ordered task queue for a single virtual slot on a server."""
    server_id: str
    slot_index: int
    decisions: list[ScheduleDecision] = field(default_factory=list)


@dataclass
class ServerProfile:
    server_id: str
    host: str
    port: int
    max_concurrency: int
    rtf_baseline: float | None = None
    throughput_rtf: float | None = None
    penalty_factor: float = 0.1
    status: str = "ONLINE"
    running_tasks: int = 0


class RTFTracker:
    """Rolling window RTF statistics per server."""

    def __init__(self, window_size: int = RTF_WINDOW_SIZE):
        self._window_size = window_size
        self._data: dict[str, deque[float]] = {}
        self._consecutive_fast: dict[str, int] = defaultdict(int)

    def _get_window(self, server_id: str) -> deque[float]:
        window = self._data.get(server_id)
        if window is None:
            window = deque(maxlen=self._window_size)
            self._data[server_id] = window
        return window

    def record(self, server_id: str, actual_rtf: float) -> None:
        self._get_window(server_id).append(actual_rtf)

    def get_p90(self, server_id: str, default: float = DEFAULT_RTF) -> float:
        window = self._data.get(server_id)
        if not window or len(window) < 3:
            return default
        sorted_vals = sorted(window)
        idx = int(math.ceil(0.9 * len(sorted_vals))) - 1
        return sorted_vals[max(idx, 0)]

    def get_mean(self, server_id: str, default: float = DEFAULT_RTF) -> float:
        window = self._data.get(server_id)
        if not window:
            return default
        return statistics.mean(window)

    def get_window_size(self, server_id: str) -> int:
        return len(self._data.get(server_id, []))

    def clear(self, server_id: str | None = None) -> None:
        if server_id:
            self._data.pop(server_id, None)
            self._consecutive_fast.pop(server_id, None)
        else:
            self._data.clear()
            self._consecutive_fast.clear()


class TaskScheduler:
    """LPT + Earliest-Finish-Time capacity-aware scheduler."""

    def __init__(self):
        self.rtf_tracker = RTFTracker()

    def get_effective_rtf(self, server: ServerProfile) -> float:
        """Get effective RTF for a server, applying concurrency penalty."""
        base_rtf = self.rtf_tracker.get_p90(
            server.server_id,
            default=server.rtf_baseline or DEFAULT_RTF,
        )
        penalty = 1.0 + server.penalty_factor * server.running_tasks
        return base_rtf * penalty

    def get_base_rtf(self, server: ServerProfile) -> float:
        """Get base RTF for a server WITHOUT concurrency penalty (for pure capacity comparison)."""
        return self.rtf_tracker.get_p90(
            server.server_id,
            default=server.rtf_baseline or DEFAULT_RTF,
        )

    def get_throughput_speed(self, server: ServerProfile) -> float:
        """Get throughput-based speed for quota allocation.

        Formula: max_concurrency / base_rtf

        Uses per-slot RTF (from production P90 or rtf_baseline benchmark)
        scaled by slot count. This is more representative than 1/throughput_rtf
        because throughput_rtf is measured with very short samples (~6s) whose
        RTF characteristics differ significantly from real workloads (3-30 min).
        """
        base_rtf = self.get_base_rtf(server)
        slots = max(server.max_concurrency, 1)
        return slots / max(base_rtf, 0.01)

    def estimate_processing_time(
        self,
        audio_duration_sec: float,
        server: ServerProfile,
        overhead: float = DEFAULT_OVERHEAD,
    ) -> float:
        """Estimate total processing time for a task on a server."""
        rtf = self.get_effective_rtf(server)
        return audio_duration_sec * rtf + overhead

    def _allocate_quotas(
        self,
        task_count: int,
        servers: list[ServerProfile],
    ) -> dict[str, int]:
        """Allocate per-server task quotas proportional to throughput speed.

        Speed = max_concurrency / base_rtf, so servers with more slots AND
        faster per-slot processing get proportionally more tasks. Global soft
        quota distributes the ENTIRE batch by this speed ratio so the slot
        queue shape reflects true throughput proportions.

        Quotas are purely proportional to server speed with no minimum guarantee.
        Slow servers may receive 0 tasks when their speed share rounds down.
        Rounding remainders go to the fastest server first.
        """
        if not servers or task_count <= 0:
            return {}

        servers_with_slots = [s for s in servers
                              if max(s.max_concurrency - s.running_tasks, 0) > 0]
        if not servers_with_slots:
            return {}

        speeds = {}
        for s in servers_with_slots:
            speeds[s.server_id] = self.get_throughput_speed(s)

        total_speed = sum(speeds.values())
        quotas: dict[str, int] = {}
        for sid, spd in speeds.items():
            raw = task_count * spd / total_speed
            quotas[sid] = round(raw)

        allocated = sum(quotas.values())
        diff = task_count - allocated
        if diff > 0:
            fastest = max(speeds, key=speeds.get)
            quotas[fastest] += diff
        elif diff < 0:
            for sid in sorted(speeds, key=speeds.get):
                take = min(-diff, quotas[sid])
                quotas[sid] -= take
                diff += take
                if diff >= 0:
                    break

        logger.info("quota_allocation",
                     task_count=task_count,
                     quotas={sid: q for sid, q in quotas.items()},
                     speeds={sid: f"{spd:.1f}" for sid, spd in speeds.items()})

        return quotas

    def schedule_batch(
        self,
        tasks: list[dict],
        servers: list[ServerProfile],
    ) -> list[ScheduleDecision]:
        """Schedule a batch of tasks using LPT + Earliest-Finish-Time with quota balancing.

        Two-phase approach:
        1. Allocate per-server quotas strictly proportional to speed (no minimum guarantee)
        2. Assign tasks via LPT + EFT within quota constraints

        tasks: list of dicts with keys: task_id, audio_duration_sec
        servers: list of ServerProfile (only ONLINE servers with running_tasks set)

        Returns list of ScheduleDecision.
        """
        online_servers = [s for s in servers if s.status == "ONLINE"]
        if not online_servers:
            logger.warning("no_online_servers_for_scheduling")
            return []

        slots: list[ServerSlot] = []
        for srv in online_servers:
            free = max(srv.max_concurrency - srv.running_tasks, 0)
            for i in range(free):
                slots.append(ServerSlot(
                    server_id=srv.server_id,
                    slot_index=srv.running_tasks + i,
                ))

        if not slots:
            logger.warning("no_free_slots_for_scheduling",
                           servers=[f"{s.server_id}({s.running_tasks}/{s.max_concurrency})" for s in online_servers])
            return []

        server_map = {s.server_id: s for s in online_servers}

        quotas = self._allocate_quotas(len(tasks), online_servers)
        assigned_count: dict[str, int] = {s.server_id: 0 for s in online_servers}

        task_estimates = []
        for t in tasks:
            dur = t.get("audio_duration_sec", 0) or 0
            best_time = float("inf")
            best_server_id = online_servers[0].server_id
            for srv in online_servers:
                est = self.estimate_processing_time(dur, srv)
                if est < best_time:
                    best_time = est
                    best_server_id = srv.server_id
            task_estimates.append({
                **t,
                "estimated_duration": best_time,
                "preferred_server": best_server_id,
            })

        task_estimates.sort(key=lambda x: x["estimated_duration"], reverse=True)

        decisions: list[ScheduleDecision] = []
        for pos, task_info in enumerate(task_estimates):
            dur = task_info.get("audio_duration_sec", 0) or 0

            eligible_slots = [s for s in slots
                              if assigned_count.get(s.server_id, 0) < quotas.get(s.server_id, 0)]
            if not eligible_slots:
                eligible_slots = slots

            best_slot = min(
                eligible_slots,
                key=lambda s: s.earliest_free + self.estimate_processing_time(dur, server_map[s.server_id]),
            )
            srv = server_map[best_slot.server_id]
            est_duration = self.estimate_processing_time(dur, srv)

            decision = ScheduleDecision(
                task_id=task_info["task_id"],
                server_id=best_slot.server_id,
                slot_index=best_slot.slot_index,
                estimated_start=best_slot.earliest_free,
                estimated_duration=est_duration,
                estimated_finish=best_slot.earliest_free + est_duration,
                queue_position=pos + 1,
                audio_duration_sec=dur,
            )
            decisions.append(decision)
            best_slot.earliest_free += est_duration
            assigned_count[best_slot.server_id] = assigned_count.get(best_slot.server_id, 0) + 1

        self._log_batch_plan(decisions, server_map, task_estimates)
        return decisions

    def select_dispatchable_now(
        self,
        decisions: list[ScheduleDecision],
    ) -> list[ScheduleDecision]:
        """Return only the first-wave tasks whose planned start time is immediate."""
        return [
            decision for decision in decisions
            if decision.estimated_start <= IMMEDIATE_START_TOLERANCE
        ]

    def build_slot_queues(
        self,
        decisions: list[ScheduleDecision],
    ) -> dict[str, SlotQueue]:
        """Group decisions into per-slot ordered queues for incremental dispatch.

        Each key is "server_id:slot_index". Decisions within a queue are
        ordered by estimated_start (ascending), so popping from the head
        gives the next task to dispatch when that slot becomes free.
        """
        queues: dict[str, SlotQueue] = {}
        for d in sorted(decisions, key=lambda x: x.estimated_start):
            key = f"{d.server_id}:{d.slot_index}"
            if key not in queues:
                queues[key] = SlotQueue(
                    server_id=d.server_id,
                    slot_index=d.slot_index,
                )
            queues[key].decisions.append(d)
        return queues

    def _log_batch_plan(
        self,
        decisions: list[ScheduleDecision],
        server_map: dict[str, ServerProfile],
        task_estimates: list[dict],
    ) -> None:
        """Log the batch scheduling plan for diagnostics."""
        if not decisions:
            return
        server_loads: dict[str, list[str]] = defaultdict(list)
        server_finish: dict[str, float] = {}
        for d in decisions:
            dur_info = ""
            for te in task_estimates:
                if te["task_id"] == d.task_id:
                    dur_info = f"dur={te.get('audio_duration_sec', 0):.0f}s"
                    break
            server_loads[d.server_id].append(f"{d.task_id[:8]}({dur_info})")
            server_finish[d.server_id] = max(server_finish.get(d.server_id, 0), d.estimated_finish)

        for sid, tasks in server_loads.items():
            srv = server_map.get(sid)
            rtf = self.get_base_rtf(srv) if srv else DEFAULT_RTF
            logger.info(
                "batch_schedule_plan",
                server_id=sid,
                rtf=f"{rtf:.3f}",
                task_count=len(tasks),
                est_finish=f"{server_finish.get(sid, 0):.1f}s",
                tasks=tasks,
            )

        makespan = max(server_finish.values()) if server_finish else 0
        logger.info("batch_schedule_summary",
                     total_tasks=len(decisions),
                     servers_used=len(server_loads),
                     estimated_makespan=f"{makespan:.1f}s")

    def assign_single_task(
        self,
        task_id: str,
        audio_duration_sec: float,
        servers: list[ServerProfile],
    ) -> ScheduleDecision | None:
        """Assign a single task to the best available server."""
        online_servers = [s for s in servers if s.status == "ONLINE"]
        if not online_servers:
            return None

        best_server = None
        best_finish = float("inf")

        for srv in online_servers:
            if srv.running_tasks >= srv.max_concurrency:
                continue
            est = self.estimate_processing_time(audio_duration_sec, srv)
            finish = est
            if finish < best_finish:
                best_finish = finish
                best_server = srv

        if best_server is None:
            best_server = min(online_servers, key=lambda s: s.running_tasks / max(s.max_concurrency, 1))
            best_finish = self.estimate_processing_time(audio_duration_sec, best_server)

        return ScheduleDecision(
            task_id=task_id,
            server_id=best_server.server_id,
            slot_index=best_server.running_tasks,
            estimated_start=0.0,
            estimated_duration=best_finish,
            estimated_finish=best_finish,
        )

    def calibrate_after_completion(
        self,
        server_id: str,
        audio_duration_sec: float,
        actual_duration_sec: float,
        predicted_duration_sec: float | None = None,
        current_penalty_factor: float = 0.1,
    ) -> dict:
        """Called after a task completes. Updates RTF stats and returns calibration info.

        Returns dict with: new_rtf_p90, deviation, penalty_adjustment, new_penalty_factor
        """
        actual_rtf = actual_duration_sec / audio_duration_sec if audio_duration_sec > 0 else DEFAULT_RTF
        self.rtf_tracker.record(server_id, actual_rtf)

        new_p90 = self.rtf_tracker.get_p90(server_id)
        result = {
            "server_id": server_id,
            "actual_rtf": actual_rtf,
            "new_rtf_p90": new_p90,
            "deviation": None,
            "penalty_adjustment": 0.0,
            "new_penalty_factor": current_penalty_factor,
        }

        if predicted_duration_sec and predicted_duration_sec > 0:
            deviation = actual_duration_sec / predicted_duration_sec
            result["deviation"] = deviation

            if deviation > (1.0 + CALIBRATION_THRESHOLD):
                adjustment = current_penalty_factor * PENALTY_INCREASE_RATE
                new_pf = current_penalty_factor + adjustment
                result["penalty_adjustment"] = adjustment
                result["new_penalty_factor"] = new_pf
                logger.warning(
                    "eta_calibration_penalty_increase",
                    server_id=server_id,
                    deviation=f"{deviation:.2f}",
                    new_penalty=f"{new_pf:.3f}",
                )
            elif deviation < (1.0 - CALIBRATION_THRESHOLD):
                tracker = self.rtf_tracker
                tracker._consecutive_fast[server_id] += 1
                if tracker._consecutive_fast[server_id] >= CONSECUTIVE_FAST_THRESHOLD:
                    adjustment = -current_penalty_factor * PENALTY_DECREASE_RATE
                    new_pf = max(0.01, current_penalty_factor + adjustment)
                    result["penalty_adjustment"] = adjustment
                    result["new_penalty_factor"] = new_pf
                    tracker._consecutive_fast[server_id] = 0
                    logger.info(
                        "eta_calibration_penalty_decrease",
                        server_id=server_id,
                        new_penalty=f"{new_pf:.3f}",
                    )

        logger.info(
            "task_completion_calibrated",
            server_id=server_id,
            actual_rtf=f"{actual_rtf:.3f}",
            new_p90=f"{new_p90:.3f}",
        )
        return result

    def calculate_task_eta(
        self,
        audio_duration_sec: float,
        server: ServerProfile,
        queue_position: int = 0,
        avg_queue_task_duration: float = 0.0,
        overhead: float = DEFAULT_OVERHEAD,
    ) -> int:
        """Calculate ETA in seconds for a task.

        eta = queue_time + asr_time + overhead
        where:
          queue_time = queue_position * avg_queue_task_duration / available_slots
          asr_time = audio_duration * rtf_p90(server)
        """
        available_slots = max(server.max_concurrency - server.running_tasks, 1)
        queue_time = (queue_position * avg_queue_task_duration) / available_slots if queue_position > 0 else 0
        asr_time = audio_duration_sec * self.get_effective_rtf(server)
        eta = queue_time + asr_time + overhead
        return max(int(eta), 1)

    def compare_server_capacity(self, servers: list[ServerProfile]) -> list[dict]:
        """Return per-server capacity comparison (for diagnostics/UI).

        Uses throughput_rtf for speed comparison when available, falls back to rtf_baseline.
        Each entry: server_id, rtf (single-thread), throughput_rtf, relative_speed, acceleration_ratio.
        """
        if not servers:
            return []
        entries = []
        for srv in servers:
            single_rtf = self.get_base_rtf(srv)
            tp_speed = self.get_throughput_speed(srv)
            entries.append({
                "server_id": srv.server_id,
                "rtf": single_rtf,
                "throughput_rtf": srv.throughput_rtf,
                "tp_speed": tp_speed,
                "server": srv,
            })

        max_speed = max(e["tp_speed"] for e in entries)
        result = []
        for e in entries:
            ratio = e["tp_speed"] / max_speed if max_speed > 0 else 0
            result.append({
                "server_id": e["server_id"],
                "rtf": round(e["rtf"], 4),
                "relative_speed": round(ratio, 3),
                "acceleration_ratio": round(e["tp_speed"], 2),
            })
        result.sort(key=lambda x: x["relative_speed"], reverse=True)
        return result


scheduler = TaskScheduler()
