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
from typing import Any

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

# Degradation detection thresholds
THROUGHPUT_MIN_IMPROVEMENT = 0.10  # throughput_rtf must improve >=10% per concurrency doubling
PER_FILE_MAX_DEGRADATION = 2.0    # per_file_rtf must stay below 2× single_rtf


@dataclass
class BenchmarkSample:
    name: str
    path: Path
    payload: bytes
    sample_rate: int
    wav_format: str
    duration_sec: float


@dataclass
class ConcurrencyGradient:
    concurrency: int
    per_file_rtf: float
    throughput_rtf: float
    wall_clock_sec: float
    total_audio_sec: float


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

    # Phase 1: single-thread RTF using tv-report-1.wav
    try:
        async with asyncio.timeout(timeout):
            elapsed = await _benchmark_single_sample(uri, ssl_ctx, single_sample)
            result.reachable = True
            result.responsive = True
    except asyncio.TimeoutError:
        result.error = "single-thread benchmark timeout"
        return result
    except ConnectionRefusedError:
        result.error = "connection refused"
        return result
    except OSError as exc:
        result.error = f"network error: {exc}"
        return result
    except Exception as exc:
        result.error = str(exc)
        logger.warning("server_benchmark_single_error", uri=uri, error=str(exc))
        return result

    result.single_rtf = round(elapsed / single_sample.duration_sec, 4)
    result.benchmark_audio_sec = round(single_sample.duration_sec, 3)
    result.benchmark_elapsed_sec = round(elapsed, 3)
    result.benchmark_notes.append(
        f"[single] {single_sample.name}: {elapsed:.2f}s / {single_sample.duration_sec:.2f}s → RTF {result.single_rtf}"
    )
    logger.info(
        "benchmark_single_complete",
        uri=uri,
        sample=single_sample.name,
        single_rtf=f"{result.single_rtf:.4f}",
        elapsed=f"{elapsed:.2f}s",
        audio=f"{single_sample.duration_sec:.2f}s",
    )

    # Phase 2: gradient concurrency throughput using test.mp4 ×N
    # Always test full gradient (1, 2, 4, 8) to detect the true optimal
    # concurrency. Not capping by current max_concurrency prevents the
    # spiral-down where a previously lowered max blocks future discovery.
    # Pass max_concurrency=1 from legacy wrappers to skip gradient entirely.
    if max_concurrency <= 1:
        capped_gradient = [1]
    else:
        capped_gradient = list(CONCURRENCY_GRADIENT)

    for n in capped_gradient:
        try:
            async with asyncio.timeout(timeout):
                wall_clock = await _benchmark_concurrent(uri, ssl_ctx, throughput_sample, n)
        except asyncio.TimeoutError:
            result.benchmark_notes.append(f"[concurrent] {throughput_sample.name}×{n}: timeout")
            logger.warning("benchmark_concurrent_timeout", uri=uri, concurrency=n)
            result.gradient_complete = False
            break
        except Exception as exc:
            result.benchmark_notes.append(f"[concurrent] {throughput_sample.name}×{n}: error: {exc}")
            logger.warning("benchmark_concurrent_error", uri=uri, concurrency=n, error=str(exc))
            result.gradient_complete = False
            break

        per_file_rtf = round(wall_clock / throughput_sample.duration_sec, 4)
        total_audio_concurrent = throughput_sample.duration_sec * n
        tp_rtf = round(wall_clock / total_audio_concurrent, 4)

        gradient = ConcurrencyGradient(
            concurrency=n,
            per_file_rtf=per_file_rtf,
            throughput_rtf=tp_rtf,
            wall_clock_sec=round(wall_clock, 3),
            total_audio_sec=round(total_audio_concurrent, 3),
        )
        result.concurrency_gradient.append(gradient)

        result.benchmark_notes.append(
            f"[concurrent] {throughput_sample.name}×{n}: wall={wall_clock:.2f}s, "
            f"per_file_rtf={per_file_rtf:.4f}, throughput_rtf={tp_rtf:.4f}"
        )
        logger.info(
            "benchmark_concurrent_level",
            uri=uri,
            sample=throughput_sample.name,
            concurrency=n,
            wall_clock=f"{wall_clock:.2f}s",
            per_file_rtf=f"{per_file_rtf:.4f}",
            throughput_rtf=f"{tp_rtf:.4f}",
        )

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

    return result


async def benchmark_server_full_with_ssl_fallback(
    host: str,
    port: int,
    max_concurrency: int = 8,
    timeout: float = 900.0,
) -> ServerBenchmarkResult:
    """Full benchmark with wss→ws fallback.

    Raises FileNotFoundError/ValueError immediately — these are local config
    errors (missing samples), NOT server connectivity issues. Callers must
    handle them separately and must NOT mark servers OFFLINE because of them.
    """
    try:
        result = await benchmark_server_full(
            host, port, max_concurrency, use_ssl=True, timeout=timeout,
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
        try:
            return await benchmark_server_full(
                host, port, max_concurrency, use_ssl=False, timeout=timeout,
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

    A concurrency level N is accepted (and becomes the new "best") only if:
      1. throughput_rtf(N) improved ≥ THROUGHPUT_MIN_IMPROVEMENT over level N-1
      2. per_file_rtf(N) ≤ single_rtf × PER_FILE_MAX_DEGRADATION

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


async def _benchmark_single_sample(uri: str, ssl_ctx: ssl.SSLContext | None, sample: BenchmarkSample) -> float:
    """Send one sample and return elapsed seconds."""
    start_msg = json.dumps({
        "mode": "offline",
        "wav_name": f"{sample.path.stem}-benchmark",
        "wav_format": sample.wav_format,
        "audio_fs": sample.sample_rate,
        "is_speaking": True,
        "itn": True,
    })
    end_msg = json.dumps({"is_speaking": False})

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

        start = asyncio.get_running_loop().time()
        for offset in range(0, len(sample.payload), BENCHMARK_CHUNK_SIZE):
            await ws.send(sample.payload[offset:offset + BENCHMARK_CHUNK_SIZE])
        await ws.send(end_msg)

        response_timeout = max(60.0, sample.duration_sec * 2.5 + 30.0)
        while True:
            raw_msg = await asyncio.wait_for(ws.recv(), timeout=response_timeout)
            if isinstance(raw_msg, bytes):
                continue

            try:
                data = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            mode = str(data.get("mode", "")).lower()
            is_final = data.get("is_final")
            if isinstance(is_final, str):
                is_final = is_final.lower() in ("true", "1")

            if is_final or mode == "offline" or "2pass-offline" in mode:
                break

        return asyncio.get_running_loop().time() - start


async def _benchmark_concurrent(
    uri: str,
    ssl_ctx: ssl.SSLContext | None,
    sample: BenchmarkSample,
    concurrency: int,
) -> float:
    """Send `concurrency` copies of `sample` simultaneously; return wall-clock seconds.

    Raises on ANY failure — partial results under overload produce
    misleadingly optimistic throughput_rtf that would corrupt scheduling weights.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()

    tasks = [
        _benchmark_single_sample(uri, ssl_ctx, sample)
        for _ in range(concurrency)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    wall_clock = loop.time() - start

    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        logger.warning(
            "benchmark_concurrent_failure",
            concurrency=concurrency,
            failed=len(errors),
            total=concurrency,
            errors=[str(e) for e in errors[:3]],
        )
        raise RuntimeError(
            f"Concurrent benchmark N={concurrency}: "
            f"{len(errors)}/{concurrency} tasks failed — "
            f"discarding this gradient level to avoid writing distorted throughput baseline"
        )

    return wall_clock
