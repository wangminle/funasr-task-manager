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

import bisect
import math
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.observability.logging import get_logger

logger = get_logger(__name__)

DEFAULT_RTF = 0.3
DEFAULT_OVERHEAD = 5.0
DEFAULT_SEGMENT_OVERHEAD = 8.0
RTF_WINDOW_SIZE = 50
CALIBRATION_THRESHOLD = 0.3
PENALTY_INCREASE_RATE = 0.2
PENALTY_DECREASE_RATE = 0.1
CONSECUTIVE_FAST_THRESHOLD = 10
IMMEDIATE_START_TOLERANCE = 1e-9

ETA_CAL_HISTORY_SIZE = 2
ETA_CAL_CHANGE_THRESHOLD = 0.05
ETA_CAL_STEP = 0.1
ETA_CAL_MIN_FACTOR = 0.5
ETA_CAL_MAX_FACTOR = 3.0


class ETACalibrationTracker:
    """Per-server ETA calibration using recent (predicted, actual) history.

    After accumulating HISTORY_SIZE records, computes the average
    actual/predicted ratio.  When the ratio diverges from 1.0 by more
    than CHANGE_THRESHOLD, the factor is updated to the nearest 0.1 step
    and clamped to [MIN_FACTOR, MAX_FACTOR].
    """

    def __init__(
        self,
        history_size: int = ETA_CAL_HISTORY_SIZE,
        change_threshold: float = ETA_CAL_CHANGE_THRESHOLD,
    ):
        self._history_size = history_size
        self._change_threshold = change_threshold
        self._history: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self._factors: dict[tuple[str, str], float] = {}

    @staticmethod
    def _key(server_id: str, work_kind: str = "task") -> tuple[str, str]:
        return (server_id, work_kind or "task")

    def record(
        self,
        server_id: str,
        predicted: float,
        actual: float,
        *,
        work_kind: str = "task",
    ) -> None:
        if predicted <= 0 or actual <= 0:
            return
        key = self._key(server_id, work_kind)
        window = self._history.get(key)
        if window is None:
            window = deque(maxlen=self._history_size)
            self._history[key] = window
        window.append((predicted, actual))
        if len(window) >= self._history_size:
            self._recalc(server_id, work_kind)

    def get_factor(self, server_id: str, work_kind: str = "task") -> float:
        return self._factors.get(self._key(server_id, work_kind), 1.0)

    def _recalc(self, server_id: str, work_kind: str = "task") -> None:
        key = self._key(server_id, work_kind)
        window = self._history.get(key)
        if not window or len(window) < self._history_size:
            return
        ratios = [actual / predicted for predicted, actual in window]
        avg_ratio = statistics.mean(ratios)
        if abs(avg_ratio - 1.0) > self._change_threshold:
            stepped = round(avg_ratio / ETA_CAL_STEP) * ETA_CAL_STEP
            stepped = round(stepped, 1)
            clamped = max(ETA_CAL_MIN_FACTOR, min(ETA_CAL_MAX_FACTOR, stepped))
            old = self._factors.get(key, 1.0)
            self._factors[key] = clamped
            if old != clamped:
                logger.info(
                    "eta_calibration_factor_updated",
                    server_id=server_id,
                    work_kind=work_kind,
                    old_factor=f"{old:.1f}",
                    new_factor=f"{clamped:.1f}",
                    avg_ratio=f"{avg_ratio:.3f}",
                )

    def clear(self, server_id: str | None = None) -> None:
        if server_id:
            for key in list(self._history):
                if key[0] == server_id:
                    self._history.pop(key, None)
            for key in list(self._factors):
                if key[0] == server_id:
                    self._factors.pop(key, None)
        else:
            self._history.clear()
            self._factors.clear()


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
    kind: str = "task"
    parent_task_id: str | None = None


@dataclass
class SlotQueue:
    """Ordered task queue for a single virtual slot on a server."""
    server_id: str
    slot_index: int
    decisions: list[ScheduleDecision] = field(default_factory=list)


class PlanPool:
    """Persistent work pool that survives across dispatch cycles.

    Each server has a sorted list of ScheduleDecisions ordered by
    estimated_finish (ascending). Head = next to dispatch, tail = steal
    candidate. Supports incremental sorted merge and budget-based pop.

    Uses list (not deque) to allow bisect-based sorted insertion.
    Queue sizes are O(tasks_per_server) so list performance is fine.
    """

    def __init__(self) -> None:
        self._queues: dict[str, list[ScheduleDecision]] = {}
        self._task_index: dict[str, str] = {}

    @property
    def task_ids(self) -> set[str]:
        return set(self._task_index.keys())

    @property
    def server_ids(self) -> frozenset[str]:
        return frozenset(
            sid for sid, q in self._queues.items() if q
        )

    def __len__(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def __bool__(self) -> bool:
        return any(self._queues.values())

    def contains(self, task_id: str) -> bool:
        return task_id in self._task_index

    def server_queue_len(self, server_id: str) -> int:
        q = self._queues.get(server_id)
        return len(q) if q else 0

    def merge(self, decisions: list[ScheduleDecision]) -> int:
        """Merge new decisions into the pool, maintaining EFT sort order.

        Items already in the pool (by task_id) are skipped.
        New items are inserted at the correct sorted position using bisect.
        Returns the count of newly added items.
        """
        added = 0
        for d in decisions:
            if d.task_id in self._task_index:
                continue
            q = self._queues.get(d.server_id)
            if q is None:
                q = []
                self._queues[d.server_id] = q
            eft_keys = [x.estimated_finish for x in q]
            idx = bisect.bisect_right(eft_keys, d.estimated_finish)
            q.insert(idx, d)
            self._task_index[d.task_id] = d.server_id
            added += 1
        return added

    def replace(self, decisions: list[ScheduleDecision]) -> None:
        """Full replace: clear all queues and rebuild from decisions."""
        self.clear()
        self.merge(decisions)

    def pop_dispatchable(self, server_id: str, budget: int) -> list[ScheduleDecision]:
        """Pop up to `budget` items that are eligible for immediate dispatch."""
        q = self._queues.get(server_id)
        if not q or budget <= 0:
            return []

        selected_indices: list[int] = []
        for idx, item in enumerate(q):
            if item.estimated_start <= IMMEDIATE_START_TOLERANCE:
                selected_indices.append(idx)
                if len(selected_indices) >= budget:
                    break

        if not selected_indices:
            selected_indices = list(range(min(budget, len(q))))

        result = [q[idx] for idx in selected_indices]
        for idx in reversed(selected_indices):
            del q[idx]
        for item in result:
            self._task_index.pop(item.task_id, None)
        if not q:
            del self._queues[server_id]
        return result

    def remove(self, task_id: str) -> ScheduleDecision | None:
        """Remove a specific task from the pool by task_id."""
        sid = self._task_index.pop(task_id, None)
        if sid is None:
            return None
        q = self._queues.get(sid)
        if q is None:
            return None
        for i, d in enumerate(q):
            if d.task_id == task_id:
                del q[i]
                if not q:
                    del self._queues[sid]
                return d
        return None

    def peek_tail(self, server_id: str) -> ScheduleDecision | None:
        """Look at the last (lowest-priority) item in a server queue."""
        q = self._queues.get(server_id)
        return q[-1] if q else None

    def steal_tail(self, server_id: str) -> ScheduleDecision | None:
        """Pop the last item from a server queue (for work stealing)."""
        q = self._queues.get(server_id)
        if not q:
            return None
        item = q.pop()
        self._task_index.pop(item.task_id, None)
        if not q:
            del self._queues[server_id]
        return item

    def iter_server_tails(
        self, exclude_server: str, max_per_server: int = 3,
    ) -> list[tuple[str, ScheduleDecision, float]]:
        """Yield (server_id, decision, remaining_sec) from tails of other servers.

        Used by work-steal to scan candidates across all servers.
        Returns items from the tail (lowest priority) of each server queue.
        """
        result: list[tuple[str, ScheduleDecision, float]] = []
        for sid, q in self._queues.items():
            if sid == exclude_server or not q:
                continue
            remaining = sum(d.estimated_duration for d in q)
            for d in reversed(q[-max_per_server:]):
                result.append((sid, d, remaining))
        return result

    def get_queue_snapshot(self, server_id: str) -> tuple[ScheduleDecision, ...]:
        """Return a read-only snapshot of a server's queue."""
        q = self._queues.get(server_id)
        return tuple(q) if q else ()

    def server_tail_finish(self) -> dict[str, float]:
        """Return the estimated_finish of the last item per server queue.

        Used as backlog offset when merging incremental scheduling results
        so that new items don't leapfrog existing backlog.
        """
        return {
            sid: q[-1].estimated_finish
            for sid, q in self._queues.items() if q
        }

    def server_remaining_sec(self) -> dict[str, float]:
        """Total estimated duration per server queue."""
        return {
            sid: sum(d.estimated_duration for d in q)
            for sid, q in self._queues.items() if q
        }

    def clear(self) -> None:
        self._queues.clear()
        self._task_index.clear()


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
    segment_fixed_overhead_sec: float = DEFAULT_SEGMENT_OVERHEAD


class RTFTracker:
    """Rolling window RTF statistics per server."""

    def __init__(self, window_size: int = RTF_WINDOW_SIZE):
        self._window_size = window_size
        self._data: dict[tuple[str, str], deque[float]] = {}
        self._consecutive_fast: dict[tuple[str, str], int] = defaultdict(int)

    @staticmethod
    def _key(server_id: str, work_kind: str = "task") -> tuple[str, str]:
        return (server_id, work_kind or "task")

    def _get_window(self, server_id: str, work_kind: str = "task") -> deque[float]:
        key = self._key(server_id, work_kind)
        window = self._data.get(key)
        if window is None:
            window = deque(maxlen=self._window_size)
            self._data[key] = window
        return window

    def record(self, server_id: str, actual_rtf: float, work_kind: str = "task") -> None:
        self._get_window(server_id, work_kind).append(actual_rtf)

    def get_p90(
        self,
        server_id: str,
        default: float = DEFAULT_RTF,
        work_kind: str = "task",
    ) -> float:
        window = self._data.get(self._key(server_id, work_kind))
        if not window or len(window) < 3:
            return default
        sorted_vals = sorted(window)
        idx = int(math.ceil(0.9 * len(sorted_vals))) - 1
        return sorted_vals[max(idx, 0)]

    def get_mean(
        self,
        server_id: str,
        default: float = DEFAULT_RTF,
        work_kind: str = "task",
    ) -> float:
        window = self._data.get(self._key(server_id, work_kind))
        if not window:
            return default
        return statistics.mean(window)

    def get_window_size(self, server_id: str, work_kind: str = "task") -> int:
        return len(self._data.get(self._key(server_id, work_kind), []))

    def clear(self, server_id: str | None = None) -> None:
        if server_id:
            for key in list(self._data):
                if key[0] == server_id:
                    self._data.pop(key, None)
            for key in list(self._consecutive_fast):
                if key[0] == server_id:
                    self._consecutive_fast.pop(key, None)
        else:
            self._data.clear()
            self._consecutive_fast.clear()


class TaskScheduler:
    """LPT + Earliest-Finish-Time capacity-aware scheduler."""

    def __init__(self):
        self.rtf_tracker = RTFTracker()
        self.eta_tracker = ETACalibrationTracker()

    def get_effective_rtf(self, server: ServerProfile, work_kind: str = "task") -> float:
        """Get effective RTF for a server, applying concurrency penalty."""
        base_rtf = self.rtf_tracker.get_p90(
            server.server_id,
            default=server.rtf_baseline or DEFAULT_RTF,
            work_kind=work_kind,
        )
        penalty = 1.0 + server.penalty_factor * server.running_tasks
        return base_rtf * penalty

    def get_base_rtf(self, server: ServerProfile, work_kind: str = "task") -> float:
        """Get base RTF for a server WITHOUT concurrency penalty (for pure capacity comparison)."""
        return self.rtf_tracker.get_p90(
            server.server_id,
            default=server.rtf_baseline or DEFAULT_RTF,
            work_kind=work_kind,
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
        overhead: float | None = None,
        *,
        work_kind: str = "task",
    ) -> float:
        """Estimate total processing time for a task on a server.

        Applies the per-server ETA calibration factor when available,
        so estimates converge toward observed reality over time.
        """
        rtf = self.get_effective_rtf(server, work_kind=work_kind)
        if overhead is None:
            overhead = (
                server.segment_fixed_overhead_sec
                if work_kind == "segment"
                else DEFAULT_OVERHEAD
            )
        raw = audio_duration_sec * rtf + overhead
        factor = self.eta_tracker.get_factor(server.server_id, work_kind=work_kind)
        return raw * factor

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

        Quotas use **total** server capacity (max_concurrency), not current
        free slots.  This is a structural backlog distribution — decoupled
        from instantaneous slot availability — so that a replan never
        collapses all tasks onto the one server that happens to have a free
        slot at that instant.

        Quotas are purely proportional to server speed with no minimum guarantee.
        Slow servers may receive 0 tasks when their speed share rounds down.
        Rounding remainders go to the fastest server first.
        """
        if not servers or task_count <= 0:
            return {}

        speeds = {}
        for s in servers:
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
            occupied = min(srv.running_tasks, srv.max_concurrency)
            free = max(srv.max_concurrency - srv.running_tasks, 0)

            if occupied > 0:
                base_rtf = self.get_base_rtf(srv)
                avg_task_dur = 180.0
                est_per_slot = avg_task_dur * base_rtf + DEFAULT_OVERHEAD
                for i in range(occupied):
                    slots.append(ServerSlot(
                        server_id=srv.server_id,
                        slot_index=i,
                        earliest_free=est_per_slot * (occupied - i) / occupied,
                    ))

            for i in range(free):
                slots.append(ServerSlot(
                    server_id=srv.server_id,
                    slot_index=occupied + i,
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
            work_kind = t.get("kind", "task")
            best_time = float("inf")
            best_server_id = online_servers[0].server_id
            for srv in online_servers:
                est = self.estimate_processing_time(dur, srv, work_kind=work_kind)
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
                key=lambda s: (
                    s.earliest_free
                    + self.estimate_processing_time(
                        dur,
                        server_map[s.server_id],
                        work_kind=task_info.get("kind", "task"),
                    )
                ),
            )
            srv = server_map[best_slot.server_id]
            est_duration = self.estimate_processing_time(
                dur,
                srv,
                work_kind=task_info.get("kind", "task"),
            )

            decision = ScheduleDecision(
                task_id=task_info["task_id"],
                server_id=best_slot.server_id,
                slot_index=best_slot.slot_index,
                estimated_start=best_slot.earliest_free,
                estimated_duration=est_duration,
                estimated_finish=best_slot.earliest_free + est_duration,
                queue_position=pos + 1,
                audio_duration_sec=dur,
                kind=task_info.get("kind", "task"),
                parent_task_id=task_info.get("parent_task_id"),
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
        *,
        kind: str = "task",
        parent_task_id: str | None = None,
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
            est = self.estimate_processing_time(audio_duration_sec, srv, work_kind=kind)
            finish = est
            if finish < best_finish:
                best_finish = finish
                best_server = srv

        if best_server is None:
            best_server = min(online_servers, key=lambda s: s.running_tasks / max(s.max_concurrency, 1))
            best_finish = self.estimate_processing_time(audio_duration_sec, best_server, work_kind=kind)

        return ScheduleDecision(
            task_id=task_id,
            server_id=best_server.server_id,
            slot_index=best_server.running_tasks,
            estimated_start=0.0,
            estimated_duration=best_finish,
            estimated_finish=best_finish,
            kind=kind,
            parent_task_id=parent_task_id,
        )

    def calibrate_after_completion(
        self,
        server_id: str,
        audio_duration_sec: float,
        actual_duration_sec: float,
        predicted_duration_sec: float | None = None,
        current_penalty_factor: float = 0.1,
        work_kind: str = "task",
    ) -> dict:
        """Called after a task completes. Updates RTF stats and returns calibration info.

        Returns dict with: new_rtf_p90, deviation, penalty_adjustment, new_penalty_factor
        """
        work_kind = work_kind or "task"
        actual_rtf = actual_duration_sec / audio_duration_sec if audio_duration_sec > 0 else DEFAULT_RTF
        self.rtf_tracker.record(server_id, actual_rtf, work_kind=work_kind)

        new_p90 = self.rtf_tracker.get_p90(server_id, work_kind=work_kind)
        result = {
            "server_id": server_id,
            "work_kind": work_kind,
            "actual_rtf": actual_rtf,
            "new_rtf_p90": new_p90,
            "deviation": None,
            "penalty_adjustment": 0.0,
            "new_penalty_factor": current_penalty_factor,
        }

        if predicted_duration_sec and predicted_duration_sec > 0:
            current_factor = self.eta_tracker.get_factor(server_id, work_kind=work_kind)
            raw_predicted = predicted_duration_sec / current_factor if current_factor != 1.0 else predicted_duration_sec
            self.eta_tracker.record(server_id, raw_predicted, actual_duration_sec, work_kind=work_kind)
            result["eta_calibration_factor"] = self.eta_tracker.get_factor(server_id, work_kind=work_kind)

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
                    work_kind=work_kind,
                    deviation=f"{deviation:.2f}",
                    new_penalty=f"{new_pf:.3f}",
                )
            elif deviation < (1.0 - CALIBRATION_THRESHOLD):
                tracker = self.rtf_tracker
                fast_key = (server_id, work_kind)
                tracker._consecutive_fast[fast_key] += 1
                if tracker._consecutive_fast[fast_key] >= CONSECUTIVE_FAST_THRESHOLD:
                    adjustment = -current_penalty_factor * PENALTY_DECREASE_RATE
                    new_pf = max(0.01, current_penalty_factor + adjustment)
                    result["penalty_adjustment"] = adjustment
                    result["new_penalty_factor"] = new_pf
                    tracker._consecutive_fast[fast_key] = 0
                    logger.info(
                        "eta_calibration_penalty_decrease",
                        server_id=server_id,
                        work_kind=work_kind,
                        new_penalty=f"{new_pf:.3f}",
                    )

        logger.info(
            "task_completion_calibrated",
            server_id=server_id,
            work_kind=work_kind,
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
