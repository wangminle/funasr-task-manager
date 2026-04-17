# NDJSON 事件参考

Benchmark 端点以 NDJSON（`application/x-ndjson`）格式流式返回进度事件。每行是一个 JSON 对象，包含 `type` 字段标识事件类型。

> 本文件基于 `3-dev/src/backend/app/services/server_benchmark.py` 和 `3-dev/src/backend/app/api/servers.py` 的实际实现编写。如后端代码有变更，请同步更新。

## 单节点 Benchmark 事件

**端点**：`POST /api/v1/servers/{server_id}/benchmark`

### 事件流程

服务层（`server_benchmark.py`）发出进度事件，API 层（`servers.py`）为每个事件注入 `server_id` 字段后转发。服务层最后发出 `benchmark_complete`，API 层在此之后发出 `benchmark_result`（含完整 `ServerBenchmarkItem`）或 `benchmark_error`。

### 进度事件详情

#### `benchmark_start` — benchmark 开始

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"benchmark_start"` | — |
| `server_id` | string | API 层注入 |
| `uri` | string | WebSocket URI |
| `samples` | string[] | 使用的音频样本文件名 |
| `gradient_levels` | int[] | 并发梯度级别，如 `[1, 2, 4, 8]` |
| `repeats` | int | 每级重复采样次数 |
| `total_steps` | int | 总步骤数 |

#### `phase_start` — 阶段开始

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"phase_start"` | — |
| `server_id` | string | API 层注入 |
| `phase` | int | 阶段号（1=单线程 RTF, 2=并发梯度） |
| `description` | string | 如 `"单线程 RTF 测试"` / `"并发吞吐量梯度测试"` |
| `sample` | string | 使用的音频样本文件名 |
| `duration_sec` | float | Phase 1 专有：样本时长 |
| `repeats` | int | 重复采样次数 |
| `gradient_levels` | int[] | Phase 2 专有：并发梯度级别列表 |

#### `phase_progress` — 阶段内采样进度（Phase 1）

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"phase_progress"` | — |
| `server_id` | string | API 层注入 |
| `phase` | int | `1` |
| `rep` | int | 当前第几次采样（从 1 开始） |
| `total_reps` | int | 总采样次数 |
| `rtf` | float | 本次采样 RTF |
| `elapsed_ms` | float | 本次采样耗时（毫秒） |

#### `phase_complete` — 阶段完成（Phase 1）

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"phase_complete"` | — |
| `server_id` | string | API 层注入 |
| `phase` | int | `1` |
| `single_rtf` | float | 单线程 RTF（取中位数） |
| `elapsed_sec` | float | 中位数采样耗时（秒） |
| `audio_sec` | float | 样本音频时长（秒） |

#### `gradient_start` — 并发梯度级别开始

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"gradient_start"` | — |
| `server_id` | string | API 层注入 |
| `concurrency` | int | 当前并发数（如 1, 2, 4, 8） |
| `level_index` | int | 当前级别索引（从 1 开始） |
| `total_levels` | int | 总梯度级别数 |
| `repeats` | int | 每级重复采样次数 |

#### `gradient_complete` — 并发梯度级别完成

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"gradient_complete"` | — |
| `server_id` | string | API 层注入 |
| `concurrency` | int | 并发数 |
| `level_index` | int | 级别索引（从 1 开始） |
| `total_levels` | int | 总梯度级别数 |
| `per_file_rtf` | float | 单文件 RTF |
| `throughput_rtf` | float | 吞吐量 RTF |
| `wall_clock_sec` | float | 实际耗时（秒） |

#### `gradient_error` — 并发梯度级别失败

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"gradient_error"` | — |
| `server_id` | string | API 层注入 |
| `concurrency` | int | 失败时的并发数 |
| `level_index` | int | 级别索引 |
| `error` | string | 错误原因（如 `"concurrency N=8 failed"`） |

#### `benchmark_complete` — 服务层 benchmark 完成

这是服务层（`server_benchmark.py`）发出的最终事件，表示 benchmark 计算完成。

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"benchmark_complete"` | — |
| `server_id` | string | API 层注入 |
| `single_rtf` | float | 单线程 RTF |
| `throughput_rtf` | float | 推荐并发下的吞吐量 RTF |
| `recommended_concurrency` | int | 推荐并发数 |
| `gradient_complete` | bool | 梯度测试是否完整完成 |

#### `ssl_fallback` — WSS 回退到 WS

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"ssl_fallback"` | — |
| `server_id` | string | API 层注入 |
| `description` | string | `"WSS 连接失败，回退到 WS 重试"` |

### 终结事件（API 层包装）

API 层在收到 `benchmark_complete` 后写回 DB，然后发出以下终结事件之一：

#### `benchmark_result` — 成功

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"benchmark_result"` | — |
| `server_id` | string | — |
| `data` | object | 完整 `ServerBenchmarkItem`（见下方） |

`data` 字段包含 `ServerBenchmarkItem` 的所有字段：

```json
{
  "server_id": "asr-10095",
  "reachable": true,
  "responsive": true,
  "error": null,
  "single_rtf": 0.1234,
  "throughput_rtf": 0.0358,
  "benchmark_concurrency": 4,
  "recommended_concurrency": 4,
  "benchmark_audio_sec": 6.08,
  "benchmark_elapsed_sec": 0.75,
  "benchmark_samples": ["tv-report-1.wav", "test.mp4"],
  "benchmark_notes": ["[single] ...", "[concurrent] ..."],
  "gradient_complete": true,
  "concurrency_gradient": [
    {
      "concurrency": 1,
      "per_file_rtf": 0.118,
      "throughput_rtf": 0.118,
      "wall_clock_sec": 0.72,
      "total_audio_sec": 6.08,
      "avg_connect_ms": 5.2,
      "avg_upload_ms": 12.3,
      "upload_spread_ms": 0.0,
      "avg_post_upload_wait_ms": 680.5,
      "max_post_upload_wait_ms": 680.5,
      "concurrent_post_upload_ms": 680.5,
      "avg_first_response_ms": 695.0,
      "server_per_file_rtf": 0.112,
      "server_throughput_rtf": 0.112,
      "ping_rtt_ms": 1.2
    }
  ]
}
```

#### `benchmark_error` — 失败

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"benchmark_error"` | — |
| `server_id` | string | — |
| `error` | string | 错误信息 |

### 完整事件示例（单节点）

```json
{"type": "benchmark_start", "server_id": "asr-10095", "uri": "wss://192.168.1.100:10095", "samples": ["tv-report-1.wav", "test.mp4"], "gradient_levels": [1, 2, 4, 8], "repeats": 2, "total_steps": 10}
{"type": "phase_start", "server_id": "asr-10095", "phase": 1, "description": "单线程 RTF 测试", "sample": "tv-report-1.wav", "duration_sec": 6.08, "repeats": 2}
{"type": "phase_progress", "server_id": "asr-10095", "phase": 1, "rep": 1, "total_reps": 2, "rtf": 0.1256, "elapsed_ms": 763.7}
{"type": "phase_progress", "server_id": "asr-10095", "phase": 1, "rep": 2, "total_reps": 2, "rtf": 0.1212, "elapsed_ms": 736.9}
{"type": "phase_complete", "server_id": "asr-10095", "phase": 1, "single_rtf": 0.1234, "elapsed_sec": 0.75, "audio_sec": 6.08}
{"type": "phase_start", "server_id": "asr-10095", "phase": 2, "description": "并发吞吐量梯度测试", "sample": "test.mp4", "gradient_levels": [1, 2, 4, 8], "repeats": 2}
{"type": "gradient_start", "server_id": "asr-10095", "concurrency": 1, "level_index": 1, "total_levels": 4, "repeats": 2}
{"type": "gradient_complete", "server_id": "asr-10095", "concurrency": 1, "level_index": 1, "total_levels": 4, "per_file_rtf": 0.118, "throughput_rtf": 0.118, "wall_clock_sec": 0.72}
{"type": "gradient_start", "server_id": "asr-10095", "concurrency": 2, "level_index": 2, "total_levels": 4, "repeats": 2}
{"type": "gradient_complete", "server_id": "asr-10095", "concurrency": 2, "level_index": 2, "total_levels": 4, "per_file_rtf": 0.0634, "throughput_rtf": 0.0634, "wall_clock_sec": 0.77}
{"type": "gradient_start", "server_id": "asr-10095", "concurrency": 4, "level_index": 3, "total_levels": 4, "repeats": 2}
{"type": "gradient_complete", "server_id": "asr-10095", "concurrency": 4, "level_index": 3, "total_levels": 4, "per_file_rtf": 0.0358, "throughput_rtf": 0.0358, "wall_clock_sec": 0.87}
{"type": "gradient_start", "server_id": "asr-10095", "concurrency": 8, "level_index": 4, "total_levels": 4, "repeats": 2}
{"type": "gradient_error", "server_id": "asr-10095", "concurrency": 8, "level_index": 4, "error": "concurrency N=8 failed"}
{"type": "benchmark_complete", "server_id": "asr-10095", "single_rtf": 0.1234, "throughput_rtf": 0.0358, "recommended_concurrency": 4, "gradient_complete": false}
{"type": "benchmark_result", "server_id": "asr-10095", "data": {"server_id": "asr-10095", "reachable": true, "responsive": true, "single_rtf": 0.1234, "throughput_rtf": 0.0358, "benchmark_concurrency": 4, "recommended_concurrency": 4, "concurrency_gradient": [{"concurrency": 1, "throughput_rtf": 0.118, "wall_clock_sec": 0.72}, {"concurrency": 2, "throughput_rtf": 0.0634, "wall_clock_sec": 0.77}, {"concurrency": 4, "throughput_rtf": 0.0358, "wall_clock_sec": 0.87}]}}
```

## 全量 Benchmark 事件

**端点**：`POST /api/v1/servers/benchmark`

全量 benchmark 会为所有 ONLINE 节点**并发**执行 benchmark。每个进度事件都包含 `server_id` 字段标识来源节点。

### 额外的全量控制事件

#### `all_benchmark_start` — 全量 benchmark 开始

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"all_benchmark_start"` | — |
| `server_ids` | string[] | 即将测试的服务器 ID 列表 |
| `total_servers` | int | 总服务器数 |

#### `server_benchmark_done` — 单节点完成

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"server_benchmark_done"` | — |
| `server_id` | string | — |
| `completed` | int | 已完成节点数 |
| `total` | int | 总节点数 |
| `data` | object | `ServerBenchmarkItem` |

#### `server_error` — 单节点失败

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"server_error"` | — |
| `server_id` | string | — |
| `error` | string | 错误信息 |

#### `all_complete` — 全量完成

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"all_complete"` | — |
| `data.results` | object[] | 各节点 `ServerBenchmarkItem` |
| `data.capacity_comparison` | object[] | 各节点容量对比 |

#### `keepalive` — 超时保活

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"keepalive"` | 300 秒无事件时发出 |

### 全量事件时序

```
all_benchmark_start (列出所有 server_ids)
  ├─ 各节点的进度事件（交错到达，每个带 server_id）
  │   ├─ benchmark_start (server_id=A)
  │   ├─ benchmark_start (server_id=B)
  │   ├─ phase_start (server_id=A, ...)
  │   ├─ phase_start (server_id=B, ...)
  │   └─ ... 进度事件交错 ...
  ├─ server_benchmark_done (server_id=A, completed=1, total=2)
  ├─ server_benchmark_done (server_id=B, completed=2, total=2)
  └─ all_complete (results=[], capacity_comparison=[])
```

注意：全量事件中不会出现 `benchmark_result`/`benchmark_error`（这两个是单节点 API 层的终结事件）。全量 API 使用 `server_benchmark_done`/`server_error` 替代。

## Agent 汇报策略

- **Phase 级别**（必报）：每个 `phase_start`/`phase_complete` 时汇报
- **梯度级别**（必报）：每个 `gradient_complete` 时汇报 throughput_rtf 和 wall_clock_sec
- **采样级别**（静默）：`phase_progress` 不主动汇报，除非用户要求详细日志
- **全量节点级别**（必报）：每个 `server_benchmark_done` 时汇报 `completed/total` 进度
- **终结事件**（必报）：使用结果解读模板（见 `result-templates.md`）展示完整结果
- **错误事件**（必报）：`gradient_error`、`benchmark_error`、`server_error` 均需汇报
