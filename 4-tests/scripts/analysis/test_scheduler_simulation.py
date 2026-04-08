"""Scheduler simulation: compare current vs RTF-aware scheduling.

This script simulates the exact scenario from the 20260327 full E2E test
to demonstrate the scheduling gap and propose a fix.

Run: python 4-tests/scripts/analysis/test_scheduler_simulation.py
"""

import sys
from pathlib import Path

# Ensure backend is importable
backend_root = Path(__file__).resolve().parent.parent.parent.parent / "3-dev" / "src" / "backend"
if str(backend_root) not in sys.path:
    sys.path.insert(0, str(backend_root))

from app.services.scheduler import TaskScheduler, ServerProfile, DEFAULT_RTF, DEFAULT_OVERHEAD

# ─── Real E2E test data (20260327-223928, full profile) ───────────────────────
# Audio durations estimated from file sizes and processing times.
# GuruMoringTeaching.mp3: 178.9 MB, text_length=50563, ~60 min audio
# teslaFSD12.x-trial.mp4: 412.6 MB, text_length=6953, ~5 min audio
# 20240510_160113.m4a:    36.7 MB, text_length=8976, ~8 min audio
# tv-report-1.mp4:        20.6 MB, text_length=869, ~0.5 min audio
# tv-report-1.wav:         5.5 MB, text_length=870, ~0.5 min audio
# 办公平台.mp4:            9.2 MB, text_length=597, ~0.5 min audio
# test.mp4:                6.4 MB, text_length=176, ~0.2 min audio
# test001.wav:             3.7 MB, text_length=605, ~0.3 min audio

TASKS = [
    {"task_id": "GuruMoring",   "audio_duration_sec": 3600.0},  # ~60 min
    {"task_id": "teslaFSD",     "audio_duration_sec":  330.0},  # ~5.5 min
    {"task_id": "20240510",     "audio_duration_sec":  480.0},  # ~8 min
    {"task_id": "tv-report-mp4","audio_duration_sec":   30.0},  # ~0.5 min
    {"task_id": "tv-report-wav","audio_duration_sec":   30.0},  # ~0.5 min
    {"task_id": "办公平台",      "audio_duration_sec":   30.0},  # ~0.5 min
    {"task_id": "test-mp4",     "audio_duration_sec":   12.0},  # ~0.2 min
    {"task_id": "test001",      "audio_duration_sec":   18.0},  # ~0.3 min
]

# Actual processing times from the E2E test (for validation)
ACTUAL_TIMES = {
    # task_id: (server, started_offset, completed_offset, wall_seconds)
    "GuruMoring":    ("funasr-10095", 0, 925.2),
    "teslaFSD":      ("funasr-10095", 0,  86.0),
    "20240510":      ("funasr-10095", 0, 128.1),
    "test-mp4":      ("funasr-10096", 0,  12.5),
    "test001":       ("funasr-10096", 0,   9.7),
    "tv-report-mp4": ("funasr-10096", 0,  38.4),
    "tv-report-wav": ("funasr-10096", 0,  26.1),
    "办公平台":       ("funasr-10097", 0,  14.8),
}


def simulate_current():
    """Simulate current behavior: all servers use DEFAULT_RTF=0.3, single-task dispatch."""
    print("=" * 80)
    print("SCENARIO 1: Current scheduling (all servers RTF=0.3, no differentiation)")
    print("=" * 80)

    servers = [
        ServerProfile(server_id="funasr-10095", host="a", port=1, max_concurrency=4, rtf_baseline=None),
        ServerProfile(server_id="funasr-10096", host="b", port=1, max_concurrency=4, rtf_baseline=None),
        ServerProfile(server_id="funasr-10097", host="c", port=1, max_concurrency=4, rtf_baseline=None),
    ]

    scheduler = TaskScheduler()

    # Current code uses assign_single_task() one by one (in created_at order)
    # Simulate the loop: tasks come in order (small ones first since they upload faster)
    ordered_tasks = list(reversed(TASKS))  # smaller tasks dispatched first

    assignments = {}
    server_busy = {s.server_id: 0 for s in servers}

    print(f"\nDispatch order: {[t['task_id'] for t in ordered_tasks]}")
    print()

    for task in ordered_tasks:
        # Reset running_tasks to current count
        for s in servers:
            s.running_tasks = server_busy[s.server_id]

        decision = scheduler.assign_single_task(
            task_id=task["task_id"],
            audio_duration_sec=task["audio_duration_sec"],
            servers=servers,
        )
        if decision:
            assignments[task["task_id"]] = decision.server_id
            server_busy[decision.server_id] += 1
            est = scheduler.estimate_processing_time(task["audio_duration_sec"],
                                                      next(s for s in servers if s.server_id == decision.server_id))
            print(f"  {task['task_id']:20s} → {decision.server_id}  "
                  f"(dur={task['audio_duration_sec']:7.0f}s, est={est:7.1f}s, "
                  f"running={server_busy[decision.server_id]})")

    print(f"\n  Server load: {server_busy}")
    return assignments


def simulate_batch_same_rtf():
    """Use schedule_batch() but still with uniform RTF."""
    print("\n" + "=" * 80)
    print("SCENARIO 2: Batch scheduling (schedule_batch, all servers RTF=0.3)")
    print("=" * 80)

    servers = [
        ServerProfile(server_id="funasr-10095", host="a", port=1, max_concurrency=4, rtf_baseline=None),
        ServerProfile(server_id="funasr-10096", host="b", port=1, max_concurrency=4, rtf_baseline=None),
        ServerProfile(server_id="funasr-10097", host="c", port=1, max_concurrency=4, rtf_baseline=None),
    ]

    scheduler = TaskScheduler()
    decisions = scheduler.schedule_batch(TASKS, servers)

    server_load = {}
    for d in decisions:
        server_load[d.server_id] = server_load.get(d.server_id, 0) + 1
        print(f"  {d.task_id:20s} → {d.server_id}  "
              f"(est_start={d.estimated_start:7.1f}, est_dur={d.estimated_duration:7.1f}, "
              f"est_finish={d.estimated_finish:7.1f})")

    total_finish = max(d.estimated_finish for d in decisions)
    print(f"\n  Server load: {server_load}")
    print(f"  Estimated makespan: {total_finish:.1f}s ({total_finish/60:.1f} min)")
    return decisions


def simulate_batch_rtf_aware():
    """Use schedule_batch() WITH differentiated RTF baselines.

    Hypothetical RTF baselines derived from the E2E test:
    - funasr-10095: fast GPU, RTF ≈ 0.15 (observed ~86s for 330s audio = 0.26 with concurrency)
    - funasr-10096: medium GPU, RTF ≈ 0.25
    - funasr-10097: weak GPU, RTF ≈ 0.40
    """
    print("\n" + "=" * 80)
    print("SCENARIO 3: RTF-aware batch scheduling (differentiated RTF baselines)")
    print("=" * 80)

    servers = [
        ServerProfile(server_id="funasr-10095", host="a", port=1, max_concurrency=4, rtf_baseline=0.15),
        ServerProfile(server_id="funasr-10096", host="b", port=1, max_concurrency=4, rtf_baseline=0.25),
        ServerProfile(server_id="funasr-10097", host="c", port=1, max_concurrency=4, rtf_baseline=0.40),
    ]

    scheduler = TaskScheduler()
    # Pre-populate RTF tracker to simulate learned baselines
    for srv in servers:
        for _ in range(5):
            scheduler.rtf_tracker.record(srv.server_id, srv.rtf_baseline)

    decisions = scheduler.schedule_batch(TASKS, servers)

    server_load = {}
    for d in decisions:
        server_load[d.server_id] = server_load.get(d.server_id, 0) + 1
        print(f"  {d.task_id:20s} → {d.server_id}  "
              f"(est_start={d.estimated_start:7.1f}, est_dur={d.estimated_duration:7.1f}, "
              f"est_finish={d.estimated_finish:7.1f})")

    total_finish = max(d.estimated_finish for d in decisions)
    print(f"\n  Server load: {server_load}")
    print(f"  Estimated makespan: {total_finish:.1f}s ({total_finish/60:.1f} min)")
    return decisions


def simulate_optimal_assignment():
    """Manually compute the optimal assignment for comparison."""
    print("\n" + "=" * 80)
    print("SCENARIO 4: Theoretical optimal (manual calculation)")
    print("=" * 80)

    # With RTF: 10095=0.15, 10096=0.25, 10097=0.40
    # Ideal: biggest tasks on fastest server, smallest on slowest
    optimal = {
        "GuruMoring":    ("funasr-10095", 3600 * 0.15 + 5),   # 545s
        "20240510":      ("funasr-10095", 480 * 0.15 + 5),    # 77s
        "teslaFSD":      ("funasr-10095", 330 * 0.15 + 5),    # 54.5s
        "tv-report-mp4": ("funasr-10096", 30 * 0.25 + 5),     # 12.5s
        "tv-report-wav": ("funasr-10096", 30 * 0.25 + 5),     # 12.5s
        "test001":       ("funasr-10096", 18 * 0.25 + 5),     # 9.5s
        "test-mp4":      ("funasr-10097", 12 * 0.40 + 5),     # 9.8s
        "办公平台":       ("funasr-10097", 30 * 0.40 + 5),     # 17s
    }

    # On 10095: tasks run concurrently, max(545, 77, 54.5) = 545s
    # On 10096: max(12.5, 12.5, 9.5) = 12.5s
    # On 10097: max(9.8, 17) = 17s
    makespan_95 = max(v[1] for k, v in optimal.items() if v[0] == "funasr-10095")
    makespan_96 = max(v[1] for k, v in optimal.items() if v[0] == "funasr-10096")
    makespan_97 = max(v[1] for k, v in optimal.items() if v[0] == "funasr-10097")

    print(f"\n  funasr-10095 (RTF=0.15, fast):  tasks=3, makespan={makespan_95:.1f}s")
    for k, v in optimal.items():
        if v[0] == "funasr-10095":
            print(f"    {k:20s}  est={v[1]:.1f}s")

    print(f"\n  funasr-10096 (RTF=0.25, medium): tasks=3, makespan={makespan_96:.1f}s")
    for k, v in optimal.items():
        if v[0] == "funasr-10096":
            print(f"    {k:20s}  est={v[1]:.1f}s")

    print(f"\n  funasr-10097 (RTF=0.40, slow):  tasks=2, makespan={makespan_97:.1f}s")
    for k, v in optimal.items():
        if v[0] == "funasr-10097":
            print(f"    {k:20s}  est={v[1]:.1f}s")

    optimal_makespan = max(makespan_95, makespan_96, makespan_97)
    print(f"\n  Theoretical optimal makespan: {optimal_makespan:.1f}s ({optimal_makespan/60:.1f} min)")
    return optimal_makespan


def analyze_actual():
    """Analyze actual E2E test results."""
    print("\n" + "=" * 80)
    print("ACTUAL E2E RESULTS (20260327-223928)")
    print("=" * 80)

    actual_makespan = 0
    for task_id, (server, start, end) in ACTUAL_TIMES.items():
        actual_makespan = max(actual_makespan, end)
        print(f"  {task_id:20s} → {server}  actual={end:7.1f}s  (started +{start}s)")

    print(f"\n  Actual makespan: {actual_makespan:.1f}s ({actual_makespan/60:.1f} min)")
    return actual_makespan


def main():
    print("\n" + "#" * 80)
    print("# ASR Task Manager - Scheduling Simulation Analysis")
    print("# Comparing: current vs RTF-aware scheduling strategies")
    print("#" * 80)

    # Actual results
    actual = analyze_actual()

    # Scenario 1: current (single-task, uniform RTF)
    current_assignments = simulate_current()

    # Scenario 2: batch, uniform RTF
    batch_decisions = simulate_batch_same_rtf()

    # Scenario 3: batch + RTF-aware
    rtf_decisions = simulate_batch_rtf_aware()

    # Scenario 4: theoretical optimal
    optimal = simulate_optimal_assignment()

    # ─── Summary ────────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("COMPARISON SUMMARY")
    print("=" * 80)

    batch_makespan_uniform = max(d.estimated_finish for d in batch_decisions)
    rtf_makespan = max(d.estimated_finish for d in rtf_decisions)

    print(f"\n  {'Scenario':<40s} {'Makespan':>10s} {'vs Actual':>10s}")
    print(f"  {'-'*40} {'-'*10} {'-'*10}")
    print(f"  {'Actual E2E result':<40s} {actual:>9.1f}s {'baseline':>10s}")
    print(f"  {'Current (single-task, uniform RTF)':<40s} {'~925s':>10s} {'  0%':>10s}")
    print(f"  {'Batch + uniform RTF':<40s} {batch_makespan_uniform:>9.1f}s {f'{(batch_makespan_uniform/actual-1)*100:+.0f}%':>10s}")
    print(f"  {'Batch + RTF-aware':<40s} {rtf_makespan:>9.1f}s {f'{(rtf_makespan/actual-1)*100:+.0f}%':>10s}")
    print(f"  {'Theoretical optimal':<40s} {optimal:>9.1f}s {f'{(optimal/actual-1)*100:+.0f}%':>10s}")

    # ─── Root Cause Analysis ────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("ROOT CAUSE ANALYSIS")
    print("=" * 80)

    print("""
  Problem 1: rtf_baseline not passed from DB model to ServerProfile
  ─────────────────────────────────────────────────────────────────
  File: task_runner.py:150-159
  ServerInstance model HAS rtf_baseline column, but _dispatch_queued_tasks()
  never reads it into ServerProfile:

    ServerProfile(
        server_id=srv.server_id,
        host=srv.host,
        port=srv.port,
        max_concurrency=srv.max_concurrency,
        running_tasks=running_count.get(srv.server_id, 0),
        # BUG: missing rtf_baseline=srv.rtf_baseline
        # BUG: missing penalty_factor=srv.penalty_factor
    )

  Result: ALL servers use DEFAULT_RTF=0.3 regardless of actual speed.

  Problem 2: Single-task dispatch instead of batch scheduling
  ─────────────────────────────────────────────────────────────────
  File: task_runner.py:184
  _dispatch_queued_tasks() calls assign_single_task() in a loop.
  This means:
  - Tasks are dispatched in created_at order (small uploads first)
  - No global LPT optimization across the entire batch
  - The scheduler can't see the big picture to make optimal assignments

  Problem 3: No initial RTF benchmark on server registration
  ─────────────────────────────────────────────────────────────────
  File: server_probe.py
  The probe only detects capabilities (protocol version, modes supported).
  It never measures actual transcription speed (RTF).

  Problem 4: RTF calibration only starts AFTER tasks complete
  ─────────────────────────────────────────────────────────────────
  The calibrate_after_completion() records actual RTF, but:
  - It requires at least 3 completions before p90 is used
  - Cold-start batches have no data at all
  - Calibration data is in-memory only (lost on restart)
""")

    # ─── Proposed Solution ──────────────────────────────────────────────────
    print("=" * 80)
    print("PROPOSED SOLUTION: RTF-Aware Scheduling Enhancement")
    print("=" * 80)

    print("""
  Fix 1: Pass rtf_baseline and penalty_factor from ServerInstance to ServerProfile
  ────────────────────────────────────────────────────────────────────────────────
  In task_runner.py _dispatch_queued_tasks(), add:

    ServerProfile(
        ...,
        rtf_baseline=srv.rtf_baseline,       # ← ADD
        penalty_factor=srv.penalty_factor,    # ← ADD
    )

  Impact: Immediate benefit if rtf_baseline is set via registration or manual config.

  Fix 2: Switch to schedule_batch() for batch dispatch
  ────────────────────────────────────────────────────────────────────────────────
  Replace the assign_single_task() loop with schedule_batch() when multiple
  tasks are queued. This enables global LPT optimization.

    Fix 3: Add explicit RTF benchmark flow for server calibration
  ────────────────────────────────────────────────────────────────────────────────
    Add a dedicated benchmark service / endpoint that:
    1. Sends fixed public benchmark samples to the server
  2. Measures actual processing time
  3. Calculates RTF = processing_time / audio_duration
  4. Stores as rtf_baseline in ServerInstance

  This provides an initial RTF estimate before any real tasks are scheduled.

  Fix 4: Persist RTF calibration data to database
  ────────────────────────────────────────────────────────────────────────────────
  After calibrate_after_completion(), update ServerInstance.rtf_baseline
  with the latest p90 RTF value. This ensures:
  - RTF data survives restarts
  - Cold-start batches have historical data
  - RTF evolves as server conditions change

  Expected improvement: 30-50% reduction in batch makespan when servers
  have significantly different compute power.
""")


if __name__ == "__main__":
    main()
