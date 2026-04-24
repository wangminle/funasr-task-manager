"""FunASR server benchmark service — single-thread RTF + concurrent throughput RTF.

Two-metric benchmark:
  1. single_rtf: use tv-report-1.wav (longer WAV) for accurate per-file processing speed
  2. throughput_rtf: use test.mp4 (shorter) ×N for fast gradient concurrency test (1→2→4→8)
"""

from __future__ import annotations

import asyncio
import json
import ssl
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.adapters.websocket_compat import connect_websocket
from app.config import PROJECT_ROOT
from app.observability.logging import get_logger
from app.services.metadata import extract_metadata
from app.services.upload import estimate_duration_from_size

logger = get_logger(__name__)

BENCHMARK_SAMPLE_DIR = PROJECT_ROOT / "3-dev" / "benchmark" / "samples"
SINGLE_RTF_SAMPLE = "tv-report-1.wav"
THROUGHPUT_RTF_SAMPLE = "test.mp4"
BENCHMARK_SAMPLE_FILES = (SINGLE_RTF_SAMPLE, THROUGHPUT_RTF_SAMPLE)
BENCHMARK_CHUNK_SIZE = 64 * 1024
CONCURRENCY_GRADIENT = (1, 2, 4, 8)
BENCHMARK_REPEATS = 2

# Degradation detection thresholds
THROUGHPUT_MIN_IMPROVEMENT = 0.10  # throughput_rtf must improve >=10% per concurrency doubling
PER_FILE_MAX_DEGRADATION = 2.0    # per_file_rtf must stay below 2× single_rtf

ProgressCallback = Callable[[dict], Awaitable[None]] | None


async def _emit_progress(callback: ProgressCallback, event: dict) -> None:
    """Safely invoke progress callback. Never raises — failures are silently logged."""
    if callback is None:
        return
    try:
        await callback(event)
    except Exception:
        logger.debug("progress_callback_error", event_type=event.get("type"))


@dataclass
class BenchmarkSample:
    name: str
    path: Path
    payload: bytes
    sample_rate: int
    wav_format: str
    duration_sec: float


@dataclass
class ConnectionTiming:
    """Per-connection timing breakdown (V2)."""
    connect_ms: float
    upload_ms: float
    first_response_ms: float
    post_upload_wait_ms: float
    total_ms: float


@dataclass
class ConcurrencyGradient:
    concurrency: int
    per_file_rtf: float
    throughput_rtf: float
    wall_clock_sec: float
    total_audio_sec: float
    # V2 timing breakdown
    avg_connect_ms: float = 0.0
    avg_upload_ms: float = 0.0
    upload_spread_ms: float = 0.0
    avg_post_upload_wait_ms: float = 0.0
    max_post_upload_wait_ms: float = 0.0
    concurrent_post_upload_ms: float = 0.0
    avg_first_response_ms: float = 0.0
    server_per_file_rtf: float = 0.0
    server_throughput_rtf: float = 0.0
    ping_rtt_ms: float | None = None


@dataclass
class ServerBenchmarkResult:
    reachable: bool = False
    responsive: bool = False
    error: str | None = None
    single_rtf: float | None = None
    throughput_rtf: float | None = None
    benchmark_concurrency: int | None = None
    recommended_concurrency: int | None = None
    benchmark_audio_sec: float | None = None
    benchmark_elapsed_sec: float | None = None
    benchmark_samples: list[str] = field(default_factory=list)
    benchmark_notes: list[str] = field(default_factory=list)
    concurrency_gradient: list[ConcurrencyGradient] = field(default_factory=list)
    gradient_complete: bool = True
    # V2: Phase 1 timing breakdown
    single_timing: ConnectionTiming | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "responsive": self.responsive,
            "error": self.error,
            "single_rtf": self.single_rtf,
            "throughput_rtf": self.throughput_rtf,
            "benchmark_concurrency": self.benchmark_concurrency,
            "recommended_concurrency": self.recommended_concurrency,
            "benchmark_audio_sec": self.benchmark_audio_sec,
            "benchmark_elapsed_sec": self.benchmark_elapsed_sec,
            "benchmark_samples": self.benchmark_samples,
            "benchmark_notes": self.benchmark_notes,
            "gradient_complete": self.gradient_complete,
            "concurrency_gradient": [
                {
                    "concurrency": g.concurrency,
                    "per_file_rtf": g.per_file_rtf,
                    "throughput_rtf": g.throughput_rtf,
                    "wall_clock_sec": g.wall_clock_sec,
                    "total_audio_sec": g.total_audio_sec,
                    "avg_connect_ms": g.avg_connect_ms,
                    "avg_upload_ms": g.avg_upload_ms,
                    "upload_spread_ms": g.upload_spread_ms,
                    "avg_post_upload_wait_ms": g.avg_post_upload_wait_ms,
                    "max_post_upload_wait_ms": g.max_post_upload_wait_ms,
                    "concurrent_post_upload_ms": g.concurrent_post_upload_ms,
                    "avg_first_response_ms": g.avg_first_response_ms,
                    "server_per_file_rtf": g.server_per_file_rtf,
                    "server_throughput_rtf": g.server_throughput_rtf,
                    "ping_rtt_ms": g.ping_rtt_ms,
                }
                for g in self.concurrency_gradient
            ],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def benchmark_server_full(
    host: str,
    port: int,
    max_concurrency: int = 8,
    *,
    use_ssl: bool = True,
    timeout: float = 900.0,
    progress_callback: ProgressCallback = None,
) -> ServerBenchmarkResult:
    """Run full benchmark: single-thread RTF + gradient concurrency throughput RTF.

    Phase 1 — single_rtf: send tv-report-1.wav (long WAV) once for accurate single-thread speed.
    Phase 2 — throughput_rtf: send test.mp4 (short) ×N concurrently for throughput.

    Gradient always tests (1, 2, 4, 8) regardless of current max_concurrency
    to prevent spiral-down. Degradation detection finds the true optimal level.
    Pass max_concurrency=1 to skip gradient (legacy single-thread mode).
    """
    samples_map = await load_benchmark_samples_by_role()
    single_sample = samples_map["single"]
    throughput_sample = samples_map["throughput"]
    result = ServerBenchmarkResult(
        benchmark_samples=[single_sample.name, throughput_sample.name],
    )

    scheme = "wss" if use_ssl else "ws"
    uri = f"{scheme}://{host}:{port}"
    ssl_ctx = _make_ssl_ctx() if use_ssl else None

    gradient_levels = list(CONCURRENCY_GRADIENT) if max_concurrency > 1 else [1]
    total_steps = BENCHMARK_REPEATS + len(gradient_levels) * BENCHMARK_REPEATS
    logger.info("benchmark_start", uri=uri, single_sample=single_sample.name,
                throughput_sample=throughput_sample.name, gradient_levels=gradient_levels,
                repeats=BENCHMARK_REPEATS)
    await _emit_progress(progress_callback, {
        "type": "benchmark_start",
        "uri": uri,
        "samples": [single_sample.name, throughput_sample.name],
        "gradient_levels": gradient_levels,
        "repeats": BENCHMARK_REPEATS,
        "total_steps": total_steps,
    })

    # Phase 1: single-thread RTF using tv-report-1.wav (repeated sampling)
    logger.info("benchmark_phase1_start", uri=uri, sample=single_sample.name,
                duration_sec=f"{single_sample.duration_sec:.2f}", repeats=BENCHMARK_REPEATS)
    await _emit_progress(progress_callback, {
        "type": "phase_start",
        "phase": 1,
        "description": "单线程 RTF 测试",
        "sample": single_sample.name,
        "duration_sec": round(single_sample.duration_sec, 2),
        "repeats": BENCHMARK_REPEATS,
    })

    single_timings: list[ConnectionTiming] = []
    for rep in range(BENCHMARK_REPEATS):
        try:
            async with asyncio.timeout(timeout):
                timing = await _benchmark_single_sample(uri, ssl_ctx, single_sample)
                result.reachable = True
                result.responsive = True
                single_timings.append(timing)
                rep_rtf = round(timing.total_ms / 1000 / single_sample.duration_sec, 4)
                logger.debug("benchmark_phase1_rep", uri=uri, rep=rep + 1,
                             rtf=f"{rep_rtf:.4f}", total_ms=f"{timing.total_ms:.1f}")
                await _emit_progress(progress_callback, {
                    "type": "phase_progress",
                    "phase": 1,
                    "rep": rep + 1,
                    "total_reps": BENCHMARK_REPEATS,
                    "rtf": rep_rtf,
                    "elapsed_ms": round(timing.total_ms, 1),
                })
        except asyncio.TimeoutError:
            if not single_timings:
                result.error = "single-thread benchmark timeout"
                return result
            break
        except ConnectionRefusedError:
            if not single_timings:
                result.error = "connection refused"
                return result
            break
        except OSError as exc:
            if not single_timings:
                result.error = f"network error: {exc}"
                return result
            break
        except Exception as exc:
            if not single_timings:
                result.error = str(exc)
                logger.warning("server_benchmark_single_error", uri=uri, error=str(exc))
                return result
            break
        if rep < BENCHMARK_REPEATS - 1:
            await asyncio.sleep(0.5)

    single_timings.sort(key=lambda t: t.total_ms)
    median_idx = (len(single_timings) - 1) // 2
    single_timing = single_timings[median_idx]
    elapsed = single_timing.total_ms / 1000
    result.single_timing = single_timing
    result.single_rtf = round(elapsed / single_sample.duration_sec, 4)
    result.benchmark_audio_sec = round(single_sample.duration_sec, 3)
    result.benchmark_elapsed_sec = round(elapsed, 3)
    result.benchmark_notes.append(
        f"[single] {single_sample.name}: {elapsed:.2f}s / {single_sample.duration_sec:.2f}s → RTF {result.single_rtf} "
        f"(connect={single_timing.connect_ms:.0f}ms upload={single_timing.upload_ms:.0f}ms "
        f"wait={single_timing.post_upload_wait_ms:.0f}ms, repeats={len(single_timings)})"
    )
    logger.info(
        "benchmark_single_complete",
        uri=uri,
        sample=single_sample.name,
        single_rtf=f"{result.single_rtf:.4f}",
        elapsed=f"{elapsed:.2f}s",
        audio=f"{single_sample.duration_sec:.2f}s",
        connect_ms=f"{single_timing.connect_ms:.1f}",
        upload_ms=f"{single_timing.upload_ms:.1f}",
        post_upload_wait_ms=f"{single_timing.post_upload_wait_ms:.1f}",
        repeats=len(single_timings),
        all_rtfs=[f"{t.total_ms / 1000 / single_sample.duration_sec:.4f}" for t in single_timings],
    )
    await _emit_progress(progress_callback, {
        "type": "phase_complete",
        "phase": 1,
        "single_rtf": result.single_rtf,
        "elapsed_sec": round(elapsed, 3),
        "audio_sec": round(single_sample.duration_sec, 2),
    })

    # Phase 2: gradient concurrency throughput using test.mp4 ×N
    # Always test full gradient (1, 2, 4, 8) to detect the true optimal
    # concurrency. Not capping by current max_concurrency prevents the
    # spiral-down where a previously lowered max blocks future discovery.
    # Pass max_concurrency=1 from legacy wrappers to skip gradient entirely.
    if max_concurrency <= 1:
        capped_gradient = [1]
    else:
        capped_gradient = list(CONCURRENCY_GRADIENT)

    logger.info("benchmark_phase2_start", uri=uri, sample=throughput_sample.name,
                gradient=capped_gradient, repeats=BENCHMARK_REPEATS)
    await _emit_progress(progress_callback, {
        "type": "phase_start",
        "phase": 2,
        "description": "并发吞吐量梯度测试",
        "sample": throughput_sample.name,
        "gradient_levels": capped_gradient,
        "repeats": BENCHMARK_REPEATS,
    })

    for grad_idx, n in enumerate(capped_gradient):
        ping_rtt: float | None = None

        logger.info("benchmark_gradient_level_start", uri=uri, concurrency=n,
                     level_index=grad_idx + 1, total_levels=len(capped_gradient))
        await _emit_progress(progress_callback, {
            "type": "gradient_start",
            "concurrency": n,
            "level_index": grad_idx + 1,
            "total_levels": len(capped_gradient),
            "repeats": BENCHMARK_REPEATS,
        })

        # Repeated sampling per concurrency level — take median wall_clock
        repeat_results: list[ConcurrentBenchmarkResult] = []
        level_failed = False
        for rep in range(BENCHMARK_REPEATS):
            try:
                async with asyncio.timeout(timeout):
                    if rep == 0:
                        try:
                            ping_rtt = await _measure_ping_rtt(uri, ssl_ctx)
                        except Exception:
                            pass

                    bench_result = await _benchmark_concurrent(
                        uri, ssl_ctx, throughput_sample, n,
                    )
                    repeat_results.append(bench_result)
            except asyncio.TimeoutError:
                if not repeat_results:
                    result.benchmark_notes.append(f"[concurrent] {throughput_sample.name}×{n}: timeout")
                    logger.warning("benchmark_concurrent_timeout", uri=uri, concurrency=n)
                    result.gradient_complete = False
                    level_failed = True
                break
            except Exception as exc:
                if not repeat_results:
                    result.benchmark_notes.append(f"[concurrent] {throughput_sample.name}×{n}: error: {exc}")
                    logger.warning("benchmark_concurrent_error", uri=uri, concurrency=n, error=str(exc))
                    result.gradient_complete = False
                    level_failed = True
                break
            if rep < BENCHMARK_REPEATS - 1:
                await asyncio.sleep(0.5)

        if level_failed:
            await _emit_progress(progress_callback, {
                "type": "gradient_error",
                "concurrency": n,
                "level_index": grad_idx + 1,
                "error": f"concurrency N={n} failed",
            })
            break

        # Select lower-median result by wall_clock (avoids pessimistic bias with even sample counts)
        repeat_results.sort(key=lambda r: r.wall_clock_sec)
        median_result = repeat_results[(len(repeat_results) - 1) // 2]

        wall_clock = median_result.wall_clock_sec
        conn_timings = median_result.timings
        concurrent_puw = median_result.concurrent_post_upload_ms
        upload_spread = median_result.upload_spread_ms

        # V1 metrics (backward compat)
        per_file_rtf = round(wall_clock / throughput_sample.duration_sec, 4)
        total_audio_concurrent = throughput_sample.duration_sec * n
        tp_rtf = round(wall_clock / total_audio_concurrent, 4)

        # V2 timing aggregation from per-connection timings
        avg_connect = sum(t.connect_ms for t in conn_timings) / len(conn_timings)
        avg_upload = sum(t.upload_ms for t in conn_timings) / len(conn_timings)
        avg_puw = sum(t.post_upload_wait_ms for t in conn_timings) / len(conn_timings)
        max_puw = max(t.post_upload_wait_ms for t in conn_timings)
        avg_fr = sum(t.first_response_ms for t in conn_timings) / len(conn_timings)

        svr_per_file_rtf = round(concurrent_puw / 1000 / throughput_sample.duration_sec, 4)
        svr_tp_rtf = round(concurrent_puw / 1000 / total_audio_concurrent, 4)

        gradient = ConcurrencyGradient(
            concurrency=n,
            per_file_rtf=per_file_rtf,
            throughput_rtf=tp_rtf,
            wall_clock_sec=round(wall_clock, 3),
            total_audio_sec=round(total_audio_concurrent, 3),
            avg_connect_ms=round(avg_connect, 1),
            avg_upload_ms=round(avg_upload, 1),
            upload_spread_ms=round(upload_spread, 1),
            avg_post_upload_wait_ms=round(avg_puw, 1),
            max_post_upload_wait_ms=round(max_puw, 1),
            concurrent_post_upload_ms=round(concurrent_puw, 1),
            avg_first_response_ms=round(avg_fr, 1),
            server_per_file_rtf=svr_per_file_rtf,
            server_throughput_rtf=svr_tp_rtf,
            ping_rtt_ms=ping_rtt,
        )
        result.concurrency_gradient.append(gradient)

        spread_note = ""
        if n > 1 and avg_upload > 0 and upload_spread > avg_upload * 0.5:
            spread_note = " ⚠ upload_spread高"

        all_wall_clocks = [f"{r.wall_clock_sec:.2f}s" for r in repeat_results]
        result.benchmark_notes.append(
            f"[concurrent] {throughput_sample.name}×{n}: wall={wall_clock:.2f}s, "
            f"per_file_rtf={per_file_rtf:.4f}, throughput_rtf={tp_rtf:.4f}, "
            f"server_tp_rtf={svr_tp_rtf:.4f} "
            f"(upload={avg_upload:.0f}ms wait={concurrent_puw:.0f}ms "
            f"spread={upload_spread:.0f}ms rtt={ping_rtt or 0:.1f}ms "
            f"repeats={len(repeat_results)}){spread_note}"
        )
        logger.info(
            "benchmark_concurrent_level",
            uri=uri,
            sample=throughput_sample.name,
            concurrency=n,
            wall_clock=f"{wall_clock:.2f}s",
            per_file_rtf=f"{per_file_rtf:.4f}",
            throughput_rtf=f"{tp_rtf:.4f}",
            server_throughput_rtf=f"{svr_tp_rtf:.4f}",
            avg_upload_ms=f"{avg_upload:.1f}",
            concurrent_post_upload_ms=f"{concurrent_puw:.1f}",
            upload_spread_ms=f"{upload_spread:.1f}",
            ping_rtt_ms=f"{ping_rtt or 0:.1f}",
            repeats=len(repeat_results),
            all_wall_clocks=all_wall_clocks,
        )
        await _emit_progress(progress_callback, {
            "type": "gradient_complete",
            "concurrency": n,
            "level_index": grad_idx + 1,
            "total_levels": len(capped_gradient),
            "per_file_rtf": per_file_rtf,
            "throughput_rtf": tp_rtf,
            "wall_clock_sec": round(wall_clock, 3),
        })

        await asyncio.sleep(1)

    # Degradation detection: find the highest concurrency where throughput
    # still improves meaningfully, rather than blindly picking the "best"
    # throughput_rtf (which might select a low N and waste server capacity).
    if result.concurrency_gradient:
        rec_n, rec_tp_rtf = _detect_optimal_concurrency(
            result.concurrency_gradient, result.single_rtf,
        )
        result.throughput_rtf = rec_tp_rtf
        result.benchmark_concurrency = rec_n
        result.recommended_concurrency = rec_n

        max_tested = result.concurrency_gradient[-1].concurrency
        if rec_n < max_tested:
            result.benchmark_notes.append(
                f"[degradation] 并发 N>{rec_n} 吞吐量退化，"
                f"推荐 max_concurrency={rec_n} "
                f"(throughput_rtf={rec_tp_rtf:.4f})"
            )
        else:
            result.benchmark_notes.append(
                f"[auto] 最优并发级别: N={rec_n} "
                f"(throughput_rtf={rec_tp_rtf:.4f})"
            )
    else:
        result.throughput_rtf = result.single_rtf
        result.benchmark_concurrency = 1
        result.recommended_concurrency = 1
        result.gradient_complete = False
        result.benchmark_notes.append(
            "[auto] 梯度测试全部失败，回退到单线程 (N=1)"
        )

    logger.info(
        "benchmark_full_complete",
        uri=uri,
        single_rtf=result.single_rtf,
        throughput_rtf=result.throughput_rtf,
        benchmark_concurrency=result.benchmark_concurrency,
        recommended_concurrency=result.recommended_concurrency,
    )
    await _emit_progress(progress_callback, {
        "type": "benchmark_complete",
        "single_rtf": result.single_rtf,
        "throughput_rtf": result.throughput_rtf,
        "recommended_concurrency": result.recommended_concurrency,
        "gradient_complete": result.gradient_complete,
    })

    return result


async def benchmark_server_full_with_ssl_fallback(
    host: str,
    port: int,
    max_concurrency: int = 8,
    timeout: float = 900.0,
    progress_callback: ProgressCallback = None,
) -> ServerBenchmarkResult:
    """Full benchmark with wss→ws fallback.

    Raises FileNotFoundError/ValueError immediately — these are local config
    errors (missing samples), NOT server connectivity issues. Callers must
    handle them separately and must NOT mark servers OFFLINE because of them.
    """
    try:
        result = await benchmark_server_full(
            host, port, max_concurrency, use_ssl=True, timeout=timeout,
            progress_callback=progress_callback,
        )
        if result.reachable:
            return result
    except (FileNotFoundError, ValueError):
        raise
    except Exception as exc:
        logger.warning("benchmark_full_wss_exception", host=host, port=port, error=str(exc))
        result = None

    err_msg = (result.error or "") if result else ""
    is_ssl_error = any(t in err_msg.lower() for t in ("ssl", "tls", "certificate"))
    is_conn_error = any(t in err_msg.lower() for t in ("refused", "timeout", "network", "websocket", "http response"))
    if is_ssl_error or is_conn_error or result is None:
        logger.info("benchmark_full_retry_plain_ws", host=host, port=port, original_error=err_msg)
        await _emit_progress(progress_callback, {
            "type": "ssl_fallback",
            "description": "WSS 连接失败，回退到 WS 重试",
            "original_error": err_msg,
        })
        try:
            return await benchmark_server_full(
                host, port, max_concurrency, use_ssl=False, timeout=timeout,
                progress_callback=progress_callback,
            )
        except (FileNotFoundError, ValueError):
            raise
        except Exception as exc:
            logger.warning("benchmark_full_ws_also_failed", host=host, port=port, error=str(exc))

    return result if result else ServerBenchmarkResult(error="benchmark failed")


# Keep legacy single-thread API for backward compat
async def benchmark_server(host: str, port: int, *, use_ssl: bool = True, timeout: float = 900.0) -> ServerBenchmarkResult:
    """Legacy single-thread benchmark (no concurrent test)."""
    return await benchmark_server_full(host, port, max_concurrency=1, use_ssl=use_ssl, timeout=timeout)


async def benchmark_server_with_ssl_fallback(host: str, port: int, timeout: float = 900.0) -> ServerBenchmarkResult:
    """Legacy single-thread benchmark with SSL fallback."""
    return await benchmark_server_full_with_ssl_fallback(host, port, max_concurrency=1, timeout=timeout)


# ---------------------------------------------------------------------------
# Sample loading
# ---------------------------------------------------------------------------

async def load_benchmark_samples() -> list[BenchmarkSample]:
    """Load all benchmark samples (for legacy / general use)."""
    samples: list[BenchmarkSample] = []
    missing: list[str] = []
    for name in BENCHMARK_SAMPLE_FILES:
        path = BENCHMARK_SAMPLE_DIR / name
        if not path.exists():
            missing.append(name)
            continue
        samples.append(await _load_benchmark_sample(path))

    if not samples:
        raise FileNotFoundError(
            f"Benchmark 样本文件缺失: {', '.join(missing)}。"
            f"请将样本放入 {BENCHMARK_SAMPLE_DIR}/ 目录。"
            f"当前查找路径: {BENCHMARK_SAMPLE_DIR}"
        )
    if missing:
        logger.warning("benchmark_samples_partial", missing=missing, found=[s.name for s in samples])

    return samples


async def load_benchmark_samples_by_role() -> dict[str, BenchmarkSample]:
    """Load samples with designated roles.

    Returns dict with keys:
      - "single": tv-report-1.wav — long WAV for accurate single-thread RTF
      - "throughput": test.mp4 — short file for fast concurrent throughput testing
    """
    single_path = BENCHMARK_SAMPLE_DIR / SINGLE_RTF_SAMPLE
    throughput_path = BENCHMARK_SAMPLE_DIR / THROUGHPUT_RTF_SAMPLE

    missing = []
    if not single_path.exists():
        missing.append(SINGLE_RTF_SAMPLE)
    if not throughput_path.exists():
        missing.append(THROUGHPUT_RTF_SAMPLE)
    if missing:
        raise FileNotFoundError(
            f"Benchmark 样本文件缺失: {', '.join(missing)}。"
            f"请将样本放入 {BENCHMARK_SAMPLE_DIR}/ 目录。"
            f"当前查找路径: {BENCHMARK_SAMPLE_DIR}"
        )

    return {
        "single": await _load_benchmark_sample(single_path),
        "throughput": await _load_benchmark_sample(throughput_path),
    }


async def _load_benchmark_sample(path: Path) -> BenchmarkSample:
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wf:
            sample_rate = wf.getframerate()
            frames = wf.getnframes()
            duration_sec = frames / sample_rate
            payload = wf.readframes(frames)
        return BenchmarkSample(
            name=path.name,
            path=path,
            payload=payload,
            sample_rate=sample_rate,
            wav_format="pcm",
            duration_sec=duration_sec,
        )

    payload = path.read_bytes()
    metadata = await extract_metadata(path)
    duration_sec = metadata.duration_sec or estimate_duration_from_size(path.stat().st_size, path.name)
    if duration_sec is None or duration_sec <= 0:
        raise ValueError(f"Unable to determine benchmark duration for {path}")

    return BenchmarkSample(
        name=path.name,
        path=path,
        payload=payload,
        sample_rate=16000,
        wav_format="others",
        duration_sec=duration_sec,
    )


# ---------------------------------------------------------------------------
# Degradation detection
# ---------------------------------------------------------------------------

def _detect_optimal_concurrency(
    gradient: list[ConcurrencyGradient],
    single_rtf: float | None,
) -> tuple[int, float]:
    """Find the highest non-degraded concurrency level via throughput analysis.

    Uses wall-clock-based throughput_rtf and per_file_rtf for degradation
    detection. Both share the same end-to-end time basis as single_rtf,
    ensuring apples-to-apples comparison.

    server_throughput_rtf / server_per_file_rtf are preserved on each gradient
    item for observability but NOT used for decision-making: the post-upload
    tail time (concurrent_post_upload_ms) does not account for server
    processing that overlaps with upload, making it systematically lower than
    real server time. Using it against the end-to-end single_rtf baseline
    would widen thresholds and over-recommend concurrency.

    A concurrency level N is accepted (and becomes the new "best") only if:
      1. throughput_rtf(N) improved >= THROUGHPUT_MIN_IMPROVEMENT over N-1
      2. per_file_rtf(N) <= single_rtf * PER_FILE_MAX_DEGRADATION

    Returns (recommended_concurrency, throughput_rtf_at_that_level).
    """
    if not gradient:
        return 1, single_rtf or 0.3

    best = gradient[0]

    for i in range(1, len(gradient)):
        current = gradient[i]
        prev = gradient[i - 1]

        if prev.throughput_rtf > 0:
            improvement = 1.0 - (current.throughput_rtf / prev.throughput_rtf)
            if improvement < THROUGHPUT_MIN_IMPROVEMENT:
                logger.info(
                    "degradation_detected_throughput",
                    concurrency=current.concurrency,
                    prev_concurrency=prev.concurrency,
                    improvement=f"{improvement:.2%}",
                    threshold=f"{THROUGHPUT_MIN_IMPROVEMENT:.0%}",
                )
                break

        if single_rtf and single_rtf > 0:
            if current.per_file_rtf > single_rtf * PER_FILE_MAX_DEGRADATION:
                logger.info(
                    "degradation_detected_per_file",
                    concurrency=current.concurrency,
                    per_file_rtf=f"{current.per_file_rtf:.4f}",
                    single_rtf=f"{single_rtf:.4f}",
                    ratio=f"{current.per_file_rtf / single_rtf:.1f}x",
                )
                break

        best = current

    return best.concurrency, best.throughput_rtf


# ---------------------------------------------------------------------------
# Internal benchmark helpers
# ---------------------------------------------------------------------------

def _make_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _measure_ping_rtt(uri: str, ssl_ctx: ssl.SSLContext | None, attempts: int = 3) -> float | None:
    """Measure WebSocket ping/pong RTT in milliseconds.

    Opens a temporary connection, sends `attempts` pings, returns the median RTT.
    Returns None if measurement fails (non-critical — benchmark continues without it).
    """
    try:
        async with connect_websocket(
            uri,
            subprotocols=["binary"],
            ping_interval=None,
            ssl=ssl_ctx,
            close_timeout=5,
            max_size=1024 * 1024,
            open_timeout=10,
        ) as ws:
            loop = asyncio.get_running_loop()
            rtts: list[float] = []
            for _ in range(attempts):
                t_start = loop.time()
                pong = await ws.ping()
                await asyncio.wait_for(pong, timeout=5.0)
                rtt = (loop.time() - t_start) * 1000
                rtts.append(rtt)
            if not rtts:
                return None
            rtts.sort()
            return round(rtts[len(rtts) // 2], 2)
    except Exception as exc:
        logger.debug("ping_rtt_measurement_failed", uri=uri, error=str(exc))
        return None


def _build_benchmark_messages(sample: BenchmarkSample) -> tuple[str, str]:
    """Build start and end JSON messages for benchmark."""
    start_msg = json.dumps({
        "mode": "offline",
        "wav_name": f"{sample.path.stem}-benchmark",
        "wav_format": sample.wav_format,
        "audio_fs": sample.sample_rate,
        "is_speaking": True,
        "itn": True,
    })
    end_msg = json.dumps({"is_speaking": False})
    return start_msg, end_msg


def _is_final_response(data: dict) -> bool:
    """Check whether a parsed JSON message indicates completion."""
    is_final = data.get("is_final")
    if isinstance(is_final, str):
        is_final = is_final.lower() in ("true", "1")
    if is_final:
        return True
    mode = str(data.get("mode", "")).lower()
    return mode == "offline" or "2pass-offline" in mode


async def _benchmark_single_sample(uri: str, ssl_ctx: ssl.SSLContext | None, sample: BenchmarkSample) -> ConnectionTiming:
    """Send one sample and return detailed timing breakdown."""
    start_msg, end_msg = _build_benchmark_messages(sample)
    loop = asyncio.get_running_loop()

    t0 = loop.time()

    async with connect_websocket(
        uri,
        subprotocols=["binary"],
        ping_interval=None,
        ssl=ssl_ctx,
        close_timeout=10,
        max_size=1024 * 1024 * 1024,
        open_timeout=10,
    ) as ws:
        await ws.send(start_msg)
        t1 = loop.time()

        for offset in range(0, len(sample.payload), BENCHMARK_CHUNK_SIZE):
            await ws.send(sample.payload[offset:offset + BENCHMARK_CHUNK_SIZE])
        await ws.send(end_msg)
        t2 = loop.time()

        t3: float | None = None
        response_timeout = max(60.0, sample.duration_sec * 2.5 + 30.0)
        while True:
            raw_msg = await asyncio.wait_for(ws.recv(), timeout=response_timeout)
            if isinstance(raw_msg, bytes):
                continue

            try:
                data = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            if t3 is None:
                t3 = loop.time()

            if _is_final_response(data):
                break

        t4 = loop.time()
        if t3 is None:
            t3 = t4

        return ConnectionTiming(
            connect_ms=(t1 - t0) * 1000,
            upload_ms=(t2 - t1) * 1000,
            first_response_ms=(t3 - t2) * 1000,
            post_upload_wait_ms=(t4 - t2) * 1000,
            total_ms=(t4 - t0) * 1000,
        )


@dataclass
class ConcurrentBenchmarkResult:
    """Result from a single concurrency-level benchmark run."""
    wall_clock_sec: float
    timings: list[ConnectionTiming]
    concurrent_post_upload_ms: float
    upload_spread_ms: float


async def _benchmark_concurrent(
    uri: str,
    ssl_ctx: ssl.SSLContext | None,
    sample: BenchmarkSample,
    concurrency: int,
) -> ConcurrentBenchmarkResult:
    """Send `concurrency` copies of `sample` with synchronized upload start.

    V2: all connections are established first, then an asyncio.Event triggers
    simultaneous uploads. Returns a ConcurrentBenchmarkResult with per-connection
    timings plus aggregate metrics computed from absolute timestamps.

    Raises on ANY failure — partial results under overload produce
    misleadingly optimistic throughput_rtf that would corrupt scheduling weights.
    """
    if concurrency == 1:
        timing = await _benchmark_single_sample(uri, ssl_ctx, sample)
        return ConcurrentBenchmarkResult(
            wall_clock_sec=timing.total_ms / 1000,
            timings=[timing],
            concurrent_post_upload_ms=timing.post_upload_wait_ms,
            upload_spread_ms=0.0,
        )

    loop = asyncio.get_running_loop()
    start_msg, end_msg = _build_benchmark_messages(sample)
    fire_event = asyncio.Event()
    ready_count = 0
    ready_event = asyncio.Event()
    timings: list[ConnectionTiming | BaseException] = [None] * concurrency  # type: ignore[list-item]
    upload_done_abs: list[float] = [0.0] * concurrency
    final_resp_abs: list[float] = [0.0] * concurrency

    async def worker(idx: int) -> None:
        nonlocal ready_count

        t0 = loop.time()

        async with connect_websocket(
            uri,
            subprotocols=["binary"],
            ping_interval=None,
            ssl=ssl_ctx,
            close_timeout=10,
            max_size=1024 * 1024 * 1024,
            open_timeout=10,
        ) as ws:
            await ws.send(start_msg)
            t1 = loop.time()

            ready_count += 1
            if ready_count == concurrency:
                ready_event.set()
            await fire_event.wait()

            # Record upload start AFTER barrier so upload_ms is pure send time
            t_upload_start = loop.time()

            for offset in range(0, len(sample.payload), BENCHMARK_CHUNK_SIZE):
                await ws.send(sample.payload[offset:offset + BENCHMARK_CHUNK_SIZE])
            await ws.send(end_msg)
            t2 = loop.time()
            upload_done_abs[idx] = t2

            t3: float | None = None
            response_timeout = max(60.0, sample.duration_sec * 2.5 + 30.0)
            while True:
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=response_timeout)
                if isinstance(raw_msg, bytes):
                    continue
                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue
                if t3 is None:
                    t3 = loop.time()
                if _is_final_response(data):
                    break

            t4 = loop.time()
            if t3 is None:
                t3 = t4
            final_resp_abs[idx] = t4

            timings[idx] = ConnectionTiming(
                connect_ms=(t1 - t0) * 1000,
                upload_ms=(t2 - t_upload_start) * 1000,
                first_response_ms=(t3 - t2) * 1000,
                post_upload_wait_ms=(t4 - t2) * 1000,
                total_ms=(t4 - t0) * 1000,
            )

    wall_start = loop.time()
    tasks = [asyncio.create_task(worker(i)) for i in range(concurrency)]

    # Wait for all workers to be ready OR detect early failures.
    # Workers block on fire_event after incrementing ready_count, so any
    # task that completes before ready_event must have failed during
    # connect/handshake — surface that immediately instead of waiting 30s.
    ready_waiter = asyncio.create_task(ready_event.wait())
    done, _ = await asyncio.wait(
        [ready_waiter, *tasks],
        timeout=30.0,
        return_when=asyncio.FIRST_COMPLETED,
    )

    if ready_waiter not in done:
        for t in tasks:
            t.cancel()
        ready_waiter.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        early_errors = [t.exception() for t in done
                        if t.done() and t.exception() is not None]
        if early_errors:
            raise RuntimeError(
                f"Concurrent benchmark N={concurrency}: "
                f"worker failed during setup — {early_errors[0]}"
            )
        raise RuntimeError(
            f"Concurrent benchmark N={concurrency}: "
            f"only {ready_count}/{concurrency} connections established within 30s"
        )

    fire_event.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    wall_clock = loop.time() - wall_start

    errors = [r for r in results if isinstance(r, BaseException)]
    timing_errors = [t for t in timings if isinstance(t, BaseException)]
    all_errors = errors + timing_errors
    if all_errors:
        logger.warning(
            "benchmark_concurrent_failure",
            concurrency=concurrency,
            failed=len(all_errors),
            total=concurrency,
            errors=[str(e) for e in all_errors[:3]],
        )
        raise RuntimeError(
            f"Concurrent benchmark N={concurrency}: "
            f"{len(all_errors)}/{concurrency} tasks failed — "
            f"discarding this gradient level to avoid writing distorted throughput baseline"
        )

    valid_timings = [t for t in timings if isinstance(t, ConnectionTiming)]

    # Aggregate metrics from absolute timestamps:
    # concurrent_post_upload_ms = time from last upload finishing to last result arriving
    # This is the server-perspective "time to process all N requests"
    cpuw = (max(final_resp_abs) - max(upload_done_abs)) * 1000
    uspread = (max(upload_done_abs) - min(upload_done_abs)) * 1000

    return ConcurrentBenchmarkResult(
        wall_clock_sec=wall_clock,
        timings=valid_timings,
        concurrent_post_upload_ms=round(cpuw, 1),
        upload_spread_ms=round(uspread, 1),
    )
