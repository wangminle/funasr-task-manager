"""
三台 FunASR 服务器 RTF 对比基准测试
====================================
使用公开 benchmark 样本目录中的两个真实文件依次发送给 10095/10096/10097，
测量各自的解码耗时和 RTF。

用法:
    cd 3-dev/src/backend
    python ../../../4-tests/scripts/analysis/benchmark_servers_rtf.py

或直接:
    python 4-tests/scripts/analysis/benchmark_servers_rtf.py
"""

import asyncio
import json
import struct
import sys
import time
import wave
from pathlib import Path

try:
    import websockets
except ImportError:
    print("[ERROR] 需要安装 websockets: pip install websockets")
    sys.exit(1)

import ssl as _ssl_mod

# ─── 配置 ───────────────────────────────────────────────────────

SERVER_HOST = "100.116.250.20"
SERVERS = [
    {"name": "funasr-cpu   (10095)", "port": 10095},
    {"name": "funasr-cpu-2 (10096)", "port": 10096},
    {"name": "funasr-cpu-3 (10097)", "port": 10097},
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
AUDIO_DIR = PROJECT_ROOT / "3-dev" / "benchmark" / "samples"

TEST_FILES = [
    "test.mp4",
    "tv-report-1.wav",
]

CHUNK_SIZE = 65536  # 64KB per chunk


# ─── 音频读取 ───────────────────────────────────────────────────

def read_audio(filepath: Path) -> tuple[bytes, int, str, float]:
    """读取音频文件，返回 (data, sample_rate, wav_format, estimated_duration_sec)."""
    ext = filepath.suffix.lower()
    size_mb = filepath.stat().st_size / (1024 * 1024)

    if ext == ".wav":
        try:
            with wave.open(str(filepath), "rb") as wf:
                sr = wf.getframerate()
                nframes = wf.getnframes()
                nch = wf.getnchannels()
                sw = wf.getsampwidth()
                pcm = wf.readframes(nframes)
                duration = nframes / sr
                print(f"  WAV: {sr}Hz, {nch}ch, {sw*8}bit, {nframes} frames, "
                      f"{duration:.1f}s, {size_mb:.1f}MB")
                return bytes(pcm), sr, "pcm", duration
        except wave.Error:
            pass

    data = filepath.read_bytes()
    duration_est = len(data) / (16000 * 2) if ext == ".pcm" else 0
    print(f"  Binary: {size_mb:.1f}MB, format=others (duration unknown for non-PCM)")
    return data, 16000, "others", duration_est


# ─── WebSocket 转写 ─────────────────────────────────────────────

async def transcribe_single(
    host: str, port: int, audio_data: bytes,
    sample_rate: int, wav_format: str, wav_name: str,
) -> dict:
    """向单台服务器发送音频并测量耗时。"""
    # FunASR C++ Runtime SDK defaults to SSL (wss://)
    ssl_ctx = _ssl_mod.SSLContext(_ssl_mod.PROTOCOL_TLS_CLIENT)
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl_mod.CERT_NONE
    uri = f"wss://{host}:{port}"

    start_msg = json.dumps({
        "mode": "offline",
        "wav_name": wav_name,
        "wav_format": wav_format,
        "audio_fs": sample_rate,
        "is_speaking": True,
        "itn": True,
    })
    end_msg = json.dumps({"is_speaking": False})

    result = {
        "server": f"{host}:{port}",
        "text_length": 0,
        "text_preview": "",
        "elapsed_sec": 0,
        "error": None,
    }

    try:
        connect_kwargs = {
            "subprotocols": ["binary"],
            "ping_interval": None,
            "close_timeout": 60,
            "max_size": 1024 * 1024 * 1024,
            "open_timeout": 120,
            "ssl": ssl_ctx,
        }
        try:
            ws_ctx = websockets.connect(uri, proxy=None, **connect_kwargs)
        except TypeError:
            ws_ctx = websockets.connect(uri, **connect_kwargs)

        async with ws_ctx as ws:
            # 发送 start
            await ws.send(start_msg)

            # 分块发送音频
            total = len(audio_data)
            sent = 0
            t_send_start = time.perf_counter()
            while sent < total:
                end = min(sent + CHUNK_SIZE, total)
                await ws.send(audio_data[sent:end])
                sent = end
            t_send_done = time.perf_counter()

            # 发送 end
            await ws.send(end_msg)

            # 等待识别结果
            t_infer_start = time.perf_counter()
            text = ""
            async for raw_msg in ws:
                if isinstance(raw_msg, bytes):
                    continue
                try:
                    data = json.loads(raw_msg)
                except json.JSONDecodeError:
                    continue

                text = data.get("text", "") or text
                mode = data.get("mode", "").lower()
                is_final = data.get("is_final", False)
                if isinstance(is_final, str):
                    is_final = is_final.lower() in ("true", "1")

                if is_final or mode == "offline" or "2pass-offline" in mode:
                    break

            t_done = time.perf_counter()

            result["elapsed_sec"] = round(t_done - t_send_start, 2)
            result["send_sec"] = round(t_send_done - t_send_start, 2)
            result["infer_sec"] = round(t_done - t_infer_start, 2)
            result["text_length"] = len(text)
            result["text_preview"] = text[:80] + ("..." if len(text) > 80 else "")

    except Exception as e:
        result["error"] = str(e)
        result["elapsed_sec"] = -1

    return result


# ─── 主流程 ─────────────────────────────────────────────────────

async def run_benchmark():
    print("=" * 72)
    print("  FunASR 三台服务器 RTF 对比基准测试")
    print("=" * 72)
    print(f"  服务器: {SERVER_HOST}  端口: {[s['port'] for s in SERVERS]}")
    print(f"  测试文件: {TEST_FILES}")
    print()

    all_results: list[dict] = []

    for filename in TEST_FILES:
        filepath = AUDIO_DIR / filename
        if not filepath.exists():
            print(f"[SKIP] 文件不存在: {filepath}")
            continue

        print(f"{'─' * 72}")
        print(f"📁 测试文件: {filename}")
        audio_data, sr, fmt, duration = read_audio(filepath)
        print(f"  数据大小: {len(audio_data):,} bytes")
        print()

        for srv in SERVERS:
            tag = f"  [{srv['name']}]"
            print(f"{tag} 正在发送...")

            res = await transcribe_single(
                SERVER_HOST, srv["port"],
                audio_data, sr, fmt, filename,
            )
            res["file"] = filename
            res["server_name"] = srv["name"]
            res["audio_duration"] = duration

            if res.get("error"):
                print(f"{tag} ❌ 错误: {res['error']}")
            else:
                rtf = res["elapsed_sec"] / duration if duration > 0 else 0
                res["rtf"] = round(rtf, 4)
                accel = 1 / rtf if rtf > 0 else 0
                res["acceleration"] = round(accel, 2)
                print(f"{tag} ✅ 耗时={res['elapsed_sec']:.2f}s "
                      f"(发送={res['send_sec']:.2f}s, 推理等待={res['infer_sec']:.2f}s)")
                if duration > 0:
                    print(f"{tag}    RTF={res['rtf']:.4f}, "
                          f"加速比={res['acceleration']:.2f}x, "
                          f"音频时长={duration:.1f}s")
                print(f"{tag}    文本长度={res['text_length']}, "
                      f"预览: {res['text_preview']}")
            print()

            all_results.append(res)

            # 每次转写后等 2 秒，让服务器完全释放资源
            await asyncio.sleep(2)

    # ─── 汇总表 ──────────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  📊 汇总对比")
    print("=" * 72)

    header = f"{'文件':<30} {'服务器':<25} {'耗时(s)':>8} {'RTF':>8} {'加速比':>8} {'文本长':>6}"
    print(header)
    print("─" * len(header))

    for r in all_results:
        if r.get("error"):
            print(f"{r['file']:<30} {r['server_name']:<25} {'ERROR':>8}")
        else:
            rtf_str = f"{r.get('rtf', 0):.4f}" if r.get('audio_duration', 0) > 0 else "N/A"
            accel_str = f"{r.get('acceleration', 0):.2f}x" if r.get('audio_duration', 0) > 0 else "N/A"
            print(f"{r['file']:<30} {r['server_name']:<25} "
                  f"{r['elapsed_sec']:>8.2f} {rtf_str:>8} {accel_str:>8} "
                  f"{r['text_length']:>6}")

    print()

    # 按文件分组，对比服务器间差异
    files_tested = list(dict.fromkeys(r["file"] for r in all_results))
    for f in files_tested:
        file_results = [r for r in all_results if r["file"] == f and not r.get("error")]
        if len(file_results) < 2:
            continue
        fastest = min(file_results, key=lambda x: x["elapsed_sec"])
        slowest = max(file_results, key=lambda x: x["elapsed_sec"])
        ratio = slowest["elapsed_sec"] / fastest["elapsed_sec"] if fastest["elapsed_sec"] > 0 else 0
        print(f"  {f}:")
        print(f"    最快: {fastest['server_name']} ({fastest['elapsed_sec']:.2f}s)")
        print(f"    最慢: {slowest['server_name']} ({slowest['elapsed_sec']:.2f}s)")
        print(f"    差距: {ratio:.2f}x")
        print()

    # ─── 并发吞吐量测试 ────────────────────────────────────────────
    print()
    print("=" * 72)
    print("  📊 并发吞吐量 RTF 测试 (梯度: 1→2→4→8)")
    print("=" * 72)

    wav_file = AUDIO_DIR / "tv-report-1.wav"
    if wav_file.exists():
        wav_data, wav_sr, wav_fmt, wav_dur = read_audio(wav_file)
        for srv in SERVERS:
            tag = f"[{srv['name']}]"
            print(f"\n{tag} 梯度并发测试:")
            for n in [1, 2, 4, 8]:
                tasks_list = [
                    transcribe_single(SERVER_HOST, srv["port"], wav_data, wav_sr, wav_fmt, f"concurrent-{i}")
                    for i in range(n)
                ]
                t_start = time.perf_counter()
                results_concurrent = await asyncio.gather(*tasks_list, return_exceptions=True)
                wall_clock = time.perf_counter() - t_start

                errors = [r for r in results_concurrent if isinstance(r, Exception)]
                per_file_rtf = wall_clock / wav_dur if wav_dur > 0 else 0
                total_audio = wav_dur * n
                tp_rtf = wall_clock / total_audio if total_audio > 0 else 0
                print(f"  N={n}: wall={wall_clock:.2f}s, "
                      f"per_file_rtf={per_file_rtf:.4f}, "
                      f"throughput_rtf={tp_rtf:.4f}, "
                      f"errors={len(errors)}")
                await asyncio.sleep(2)
    else:
        print(f"  [SKIP] 并发测试文件不存在: {wav_file}")

    # 保存结果到 JSON
    output_dir = PROJECT_ROOT / "4-tests" / "batch-testing" / "outputs" / "benchmark"
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    output_file = output_dir / f"server-rtf-comparison-{ts}.json"
    with open(output_file, "w", encoding="utf-8") as fp:
        json.dump({
            "timestamp": ts,
            "servers": SERVERS,
            "results": all_results,
        }, fp, ensure_ascii=False, indent=2)
    print(f"  结果已保存: {output_file}")


if __name__ == "__main__":
    asyncio.run(run_benchmark())
