# Benchmark 优化设计：触发时机与进度反馈系统

> **日期**：2026-04-15
> **范围**：注册时可选 benchmark、benchmark 进度流式反馈、日志分层显示
> **依赖**：`server_benchmark.py`、`servers.py`（API）、`MonitorView.vue`、CLI `server.py`
> **原则**：现有 benchmark 业务逻辑（测速、采样、退化检测算法）不变

---

## 一、背景与问题

### 1.1 Benchmark 在调度体系中的角色

Benchmark 是调度准确性的基础设施——它写入 `rtf_baseline`、`throughput_rtf`、`max_concurrency` 三个关键字段，直接决定调度器的名额分配（quota）和 EFT 估算精度。

```
Benchmark → DB 字段 → Scheduler 读取 → 调度决策
  single_rtf      → rtf_baseline      → get_effective_rtf() → est_time
  throughput_rtf   → throughput_rtf    → get_throughput_speed() → quota 分配
  recommended_N    → max_concurrency   → slot 数量 → 并行度
```

### 1.2 当前存在的两个问题

**问题 1：Benchmark 触发时机不便**

服务器注册和 benchmark 是完全分离的两步操作：

```
步骤 1: POST /api/v1/servers                    → 注册（仅做轻量 probe）
步骤 2: POST /api/v1/servers/{id}/benchmark     → 手动 benchmark
```

用户注册服务器后，如果忘记执行 benchmark，服务器的 `rtf_baseline` 和 `throughput_rtf` 为空，调度器只能使用 `DEFAULT_RTF=0.3` 兜底，导致名额分配和 ETA 预估不准确。

**问题 2：Benchmark 执行期间零反馈**

一次完整的 benchmark 耗时 2-10 分钟（Phase 1 单线程 RTF + Phase 2 四级梯度并发测试），期间：

| 调用方 | 用户看到 | 问题 |
|--------|---------|------|
| 前端 | Loading 转圈，无任何文字 | 不知道是否在正常工作 |
| CLI | "正在执行..." 后长时间无输出 | 无法判断进度还是卡住了 |
| API 调用方 | HTTP 连接挂起，无中间数据 | 难以设置合理超时 |

后端 structlog 日志记录了完整的阶段信息（`benchmark_single_complete`、`benchmark_concurrent_level` 等），但这些信息只存在于服务器日志文件中，没有反馈给调用方。

---

## 二、优化方案概览

| 优化项 | 内容 | 状态 |
|--------|------|------|
| **Feature A** | 注册服务器时可选触发 benchmark | ✅ 已实现 |
| **Feature B** | Benchmark 进度流式反馈（NDJSON Streaming） | 📐 设计完成，待实现 |

---

## 三、Feature A：注册时可选触发 Benchmark

### 3.1 设计思路

在注册请求中增加 `run_benchmark` 参数（默认 `false`），当设为 `true` 且服务器探测为 ONLINE 时，注册完成后自动执行一次完整 benchmark 并写入 RTF 基线。

### 3.2 接口变更

**API（`POST /api/v1/servers`）— 请求 body 新增字段：**

```json
{
  "server_id": "asr-10095",
  "host": "100.116.250.20",
  "port": 10095,
  "protocol_version": "funasr-main",
  "max_concurrency": 4,
  "run_benchmark": true
}
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `run_benchmark` | `bool` | `false` | 注册成功且 ONLINE 时是否自动执行 benchmark |

**CLI（`asr server register`）— 新增参数：**

```bash
asr server register \
  --id asr-10095 \
  --host 100.116.250.20 \
  --port 10095 \
  --benchmark          # 新增：注册后自动 benchmark
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--benchmark` | `bool flag` | `false` | 注册后立即执行完整 Benchmark 测速 |

**前端（添加服务器对话框）— 新增 Switch 开关：**

仅在"添加"模式下显示，编辑模式不显示。附带说明文字："开启后将在注册成功时执行一次完整性能基准测试，耗时较长"。

### 3.3 执行流程

```
register_server():
    1. 轻量 probe (OFFLINE_LIGHT)
    2. 写入 ServerInstance → flush
    3. if run_benchmark and status == ONLINE:
       3a. commit (先持久化注册，防止 benchmark 失败丢注册)
       3b. benchmark_server_full_with_ssl_fallback()
       3c. _apply_benchmark_result(server, bench)
    4. 返回 ServerResponse（含 benchmark 结果字段）
```

### 3.4 超时处理

Benchmark 耗时远超普通注册请求，各层超时设置：

| 层 | 正常注册超时 | 带 benchmark 超时 | 实现方式 |
|----|------------|------------------|---------|
| 后端 benchmark | - | 900s | `benchmark_server_full_with_ssl_fallback(timeout=900.0)` |
| 前端 axios | 30s | 600s（10 分钟） | `registerServer()` 检测 `run_benchmark` 动态切换 timeout |
| CLI httpx | 30s | 960s | `api_client.register_server()` 检测 `run_benchmark` 动态切换 |

### 3.5 已修改的文件

| 文件 | 改动 |
|------|------|
| `app/schemas/server.py` | `ServerRegisterRequest` 新增 `run_benchmark: bool = False` |
| `app/api/servers.py` | `register_server` 条件触发 benchmark，先 commit 再执行 |
| `frontend/src/views/MonitorView.vue` | 对话框新增 Switch、defaultForm 新增字段、handleSubmit 适配 |
| `frontend/src/api/index.js` | `registerServer()` 动态超时 |
| `cli/commands/server.py` | `register` 新增 `--benchmark` 参数，输出 benchmark 结果 |
| `cli/api_client.py` | `register_server()` 动态超时 |

---

## 四、Feature B：Benchmark 进度流式反馈系统

### 4.1 现有 Benchmark 流程阶段分析

根据 `server_benchmark.py` 源码，完整 benchmark 有 8 个可观测的关键节点：

```
节点 0: 样本加载完毕                          ~瞬时
节点 1: Phase 1 开始                          ~瞬时
节点 2: Phase 1 单次采样完成 (×2 reps)          ~20-60s/次
节点 3: Phase 1 完成，计算 single_rtf           ~瞬时
节点 4: Phase 2 开始                          ~瞬时
节点 5: 梯度 N=k 开始 (k=1,2,4,8)             ~瞬时
节点 6: 梯度 N=k 完成 (×2 reps 后)             ~10-60s/级
节点 7: 退化检测 + 最终结果                     ~瞬时
```

**现有日志覆盖情况：**

| 节点 | 是否有 structlog | 日志事件名 |
|------|----------------|-----------|
| 0 | ❌ 无 | — |
| 1 | ❌ 无 | — |
| 2 | ❌ 无（只有完成后的汇总） | — |
| 3 | ✅ | `benchmark_single_complete` |
| 4 | ❌ 无 | — |
| 5 | ❌ 无（错误时有 `benchmark_concurrent_timeout`/`error`） | — |
| 6 | ✅ | `benchmark_concurrent_level` |
| 7 | ✅ | `benchmark_full_complete` |

### 4.2 日志三层架构

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: 后端完整日志 (structlog → 日志文件 + 控制台)          │
│  保留所有现有日志，新增阶段起始日志补全覆盖空白                    │
│  受众: 运维/开发者，事后排查分析                                │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: 进度事件流 (NDJSON Streaming Response)              │
│  关键里程碑 + 阶段性结果数据，通过 HTTP 流实时推送给调用方        │
│  受众: 前端 UI / CLI 终端                                     │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: 最终结果 (流的最后一条消息)                           │
│  完整 ServerBenchmarkItem，与现有响应结构一致                   │
│  受众: 所有消费者（机器解析）                                    │
└─────────────────────────────────────────────────────────────┘
```

**Layer 1 补全计划（新增日志点）：**

| 新增日志事件 | 级别 | 触发时机 | 目的 |
|-------------|------|---------|------|
| `benchmark_samples_loaded` | INFO | 样本加载完毕 | 确认配置正确 |
| `benchmark_phase1_start` | INFO | Phase 1 开始 | 标记阶段 |
| `benchmark_phase1_rep` | DEBUG | 每次单样本采样完成 | 细粒度追踪 |
| `benchmark_phase2_start` | INFO | Phase 2 开始 | 标记阶段 |
| `benchmark_gradient_start` | INFO | 每个梯度级别开始 | 便于追踪耗时 |

### 4.3 Layer 2：进度事件定义

**事件类型枚举（共 8 种）：**

| type | 触发时机 | 关键字段 | 用户看到 |
|------|---------|---------|---------|
| `benchmark_start` | 样本加载完毕 | `total_phases`, `samples` | "Benchmark 开始（2 阶段）" |
| `phase_start` | Phase 1 或 2 开始 | `phase`, `phase_name`, `detail` | "Phase 1/2: 单线程测速…" |
| `phase_progress` | Phase 1 单次采样完成 | `phase`, `rep`, `total_reps`, `rtf` | "第 1/2 次采样: RTF=0.12" |
| `phase_complete` | Phase 1 完成 | `phase`, `single_rtf`, `elapsed_sec` | "Phase 1 完成: RTF=0.1234" |
| `gradient_start` | 梯度某级开始 | `concurrency`, `level`, `total_levels` | "并发梯度 N=4（3/4）" |
| `gradient_complete` | 梯度某级完成 | `concurrency`, `throughput_rtf`, `per_file_rtf`, `wall_sec` | "N=4: tp_rtf=0.0456" |
| `gradient_error` | 梯度某级失败 | `concurrency`, `error` | "N=8: 超时" |
| `benchmark_complete` | 最终结果 | 完整 benchmark result | 完整结果 JSON |

**事件 JSON 结构示例：**

```json
{"type":"benchmark_start","total_phases":2,"samples":["tv-report-1.wav","test.mp4"]}
{"type":"phase_start","phase":1,"phase_name":"单线程测速","detail":"tv-report-1.wav (179.5s) × 2 reps"}
{"type":"phase_progress","phase":1,"rep":1,"total_reps":2,"rtf":0.1256,"elapsed_sec":22.5}
{"type":"phase_progress","phase":1,"rep":2,"total_reps":2,"rtf":0.1234,"elapsed_sec":22.1}
{"type":"phase_complete","phase":1,"single_rtf":0.1234,"elapsed_sec":22.1}
{"type":"phase_start","phase":2,"phase_name":"并发梯度测试","detail":"test.mp4 (6.1s), 梯度: 1→2→4→8"}
{"type":"gradient_start","concurrency":1,"level":1,"total_levels":4}
{"type":"gradient_complete","concurrency":1,"throughput_rtf":0.1180,"per_file_rtf":0.1180,"wall_sec":0.72}
{"type":"gradient_start","concurrency":2,"level":2,"total_levels":4}
{"type":"gradient_complete","concurrency":2,"throughput_rtf":0.0634,"per_file_rtf":0.1268,"wall_sec":0.77}
{"type":"gradient_start","concurrency":4,"level":3,"total_levels":4}
{"type":"gradient_complete","concurrency":4,"throughput_rtf":0.0358,"per_file_rtf":0.1432,"wall_sec":0.87}
{"type":"gradient_start","concurrency":8,"level":4,"total_levels":4}
{"type":"gradient_error","concurrency":8,"error":"throughput improvement < 10%"}
{"type":"benchmark_complete","data":{"server_id":"asr-10095","single_rtf":0.1234,"throughput_rtf":0.0358,...}}
```

### 4.4 传输协议：NDJSON (Newline-Delimited JSON)

**选择 NDJSON 而非 SSE 的理由：**

| 考量 | NDJSON | SSE (text/event-stream) |
|------|--------|------------------------|
| 与 POST 方法兼容 | ✅ 天然兼容 | ⚠️ 浏览器 EventSource 只支持 GET |
| 前端已有基础 | ✅ `fetch + ReadableStream` 已在 TaskDetailView 中使用 | ✅ 同样的 ReadableStream 解析 |
| 解析复杂度 | 低（按 `\n` 分割 → JSON.parse） | 中（需解析 `event:` + `data:` 格式） |
| CLI 支持 | ✅ httpx `iter_lines()` | 需要手动解析 |
| Nginx 缓冲 | 需要 `X-Accel-Buffering: no` | 同样需要 |

**HTTP 响应头：**

```
Content-Type: application/x-ndjson
Cache-Control: no-cache
X-Accel-Buffering: no
Transfer-Encoding: chunked
```

### 4.5 服务层改动：progress_callback 机制

核心思路：在 `benchmark_server_full()` 中注入可选的异步回调函数。benchmark 业务逻辑完全不变，仅在关键节点多一个 callback 调用。

```python
# server_benchmark.py 函数签名变化

ProgressCallback = Callable[[dict], Awaitable[None]] | None

async def benchmark_server_full(
    host: str,
    port: int,
    max_concurrency: int = 8,
    *,
    use_ssl: bool = True,
    timeout: float = 900.0,
    progress_callback: ProgressCallback = None,    # 新增
) -> ServerBenchmarkResult:
```

**回调注入点（对应 8 种事件）：**

```python
# 伪代码，展示回调在业务流程中的插入位置

async def benchmark_server_full(..., progress_callback=None):
    samples_map = await load_benchmark_samples_by_role()
    
    # ① benchmark_start
    if progress_callback:
        await progress_callback({"type": "benchmark_start", ...})
    
    # Phase 1
    if progress_callback:    # ② phase_start
        await progress_callback({"type": "phase_start", "phase": 1, ...})
    
    for rep in range(BENCHMARK_REPEATS):
        timing = await _benchmark_single_sample(...)
        if progress_callback:    # ③ phase_progress
            await progress_callback({"type": "phase_progress", "rep": rep+1, ...})
    
    # ④ phase_complete
    if progress_callback:
        await progress_callback({"type": "phase_complete", "single_rtf": ..., ...})
    
    # Phase 2
    if progress_callback:    # ⑤ phase_start
        await progress_callback({"type": "phase_start", "phase": 2, ...})
    
    for idx, n in enumerate(capped_gradient):
        if progress_callback:    # ⑥ gradient_start
            await progress_callback({"type": "gradient_start", "concurrency": n, ...})
        
        # ... 执行并发测试 ...
        
        if level_failed:
            if progress_callback:    # ⑦ gradient_error
                await progress_callback({"type": "gradient_error", ...})
            break
        
        if progress_callback:    # ⑧ gradient_complete
            await progress_callback({"type": "gradient_complete", ...})
    
    # 退化检测 + 最终结果 ... (结果在 API 层发送 benchmark_complete)
    return result
```

**关键设计约束：**

- `progress_callback` 默认为 `None`，不传则无任何额外开销
- 回调失败不影响 benchmark 执行（try/except 包裹）
- `benchmark_server_full_with_ssl_fallback()` 透传 callback 给底层函数
- 所有现有 `logger.info/warning` 保持不变

### 4.6 API 层改动：StreamingResponse

```python
# servers.py — benchmark 端点改为 streaming

@router.post("/{server_id}/benchmark")
async def benchmark_server_endpoint(server_id, db, admin):
    server = ...  # 加载 server
    
    async def generate():
        progress_queue = asyncio.Queue()
        
        async def on_progress(event: dict):
            await progress_queue.put(event)
        
        async def run_benchmark():
            try:
                bench = await benchmark_server_full_with_ssl_fallback(
                    server.host, server.port, timeout=900.0,
                    progress_callback=on_progress,
                )
                if bench.reachable:
                    _apply_benchmark_result(server, bench)
                await db.commit()
                # 发送最终结果
                await progress_queue.put({
                    "type": "benchmark_complete",
                    "data": _build_benchmark_item(server.server_id, bench),
                })
            except Exception as exc:
                await progress_queue.put({
                    "type": "benchmark_complete",
                    "error": str(exc),
                })
        
        task = asyncio.create_task(run_benchmark())
        
        while True:
            event = await progress_queue.get()
            yield json.dumps(event, ensure_ascii=False) + "\n"
            if event["type"] == "benchmark_complete":
                break
        
        await task  # 确保异常被传播
    
    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

**`benchmark_all_servers` 端点的 streaming 设计：**

多服务器并行 benchmark 时，事件需要增加 `server_id` 字段以区分来源：

```json
{"type":"benchmark_start","server_id":"asr-10095",...}
{"type":"benchmark_start","server_id":"asr-10096",...}
{"type":"phase_complete","server_id":"asr-10095","phase":1,"single_rtf":0.12,...}
{"type":"gradient_complete","server_id":"asr-10096","concurrency":2,...}
...
{"type":"all_complete","data":{"results":[...],"capacity_comparison":[...]}}
```

最后一条 `all_complete` 事件包含完整的 `ServerBenchmarkResponse` 数据。

**`register_server` 中的 benchmark streaming：**

注册 + benchmark 场景（`run_benchmark=true`）也应改为 streaming，这样前端可以在注册完成后实时看到 benchmark 进度。响应变为：

```json
{"type":"register_complete","data":{"server_id":"asr-10095","status":"ONLINE",...}}
{"type":"benchmark_start","total_phases":2,...}
{"type":"phase_start","phase":1,...}
...
{"type":"benchmark_complete","data":{...}}
```

第一行是注册结果，后续行是 benchmark 进度。如果 `run_benchmark=false`，则只有一行 register_complete（等价于原来的 JSON 响应）。

### 4.7 前端展示设计

**场景 1：MonitorView 对话框提交（run_benchmark=true）**

```
用户点击"添加"→ 对话框进入 loading 态
    ↓ fetch POST, 获得 ReadableStream
    ↓ 解析第一行 register_complete → 提示 "注册成功"
    ↓ 后续行逐条解析：
┌───────────────────────────────────────────┐
│  ✓ 服务器注册成功                           │
│  ● Phase 1/2: 单线程测速...                 │
│    第 1/2 次采样: RTF = 0.1256             │
│    第 2/2 次采样: RTF = 0.1234             │
│  ✓ Phase 1 完成: 单线程 RTF = 0.1234       │
│  ● Phase 2/2: 并发梯度测试...               │
│    N=1: tp_rtf=0.1180                     │
│    N=2: tp_rtf=0.0634                     │
│    N=4: tp_rtf=0.0358 ← 推荐              │
│    N=8: ⚠ 退化                            │
│  ✓ Benchmark 完成: 推荐并发=4              │
└───────────────────────────────────────────┘
```

实现方式：在对话框内或使用 `ElDrawer` 展示进度时间线，使用 `el-timeline` 组件。

**场景 2：独立 benchmark 操作**

未来可在 MonitorView 的服务器操作列增加"Benchmark"按钮，点击后弹出进度抽屉。

**前端 streaming 解析模板（已有 TaskDetailView.vue 的 ReadableStream 模式可复用）：**

```javascript
async function fetchBenchmarkStream(url, onEvent) {
  const apiKey = getApiKey()
  const resp = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(apiKey ? { 'X-API-Key': apiKey } : {}),
    },
  })
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop()
    for (const line of lines) {
      if (!line.trim()) continue
      onEvent(JSON.parse(line))
    }
  }
}
```

### 4.8 CLI 展示设计

**CLI benchmark 命令的目标输出效果：**

```
$ asr server benchmark asr-10095

ℹ 正在对节点 asr-10095 执行全量 benchmark...

  ● Benchmark 开始 (样本: tv-report-1.wav, test.mp4)
  ● Phase 1/2: 单线程测速 (tv-report-1.wav, 179.5s × 2 reps)
    第 1/2 次: RTF = 0.1256 (22.5s)
    第 2/2 次: RTF = 0.1234 (22.1s)
  ✓ Phase 1 完成: 单线程 RTF = 0.1234

  ● Phase 2/2: 并发梯度测试 (test.mp4 × N, 梯度: 1→2→4→8)
    ├ N=1: throughput_rtf=0.1180, wall=0.72s
    ├ N=2: throughput_rtf=0.0634, wall=0.77s
    ├ N=4: throughput_rtf=0.0358, wall=0.87s  ← 推荐
    └ N=8: ⚠ 退化 (improvement < 10%)

✓ Benchmark 完成: 推荐 max_concurrency=4

┌──────────────────────────────────────────────────┐
│ 节点 benchmark 结果                               │
├──────────┬───────────┬──────────┬────────────────┤
│ 单线程RTF │ 吞吐量RTF  │ 推荐并发  │ 样本            │
├──────────┼───────────┼──────────┼────────────────┤
│ 0.1234   │ 0.0358    │ 4        │ tv-report-1    │
└──────────┴───────────┴──────────┴────────────────┘
```

**CLI 注册 + benchmark 的输出效果：**

```
$ asr server register --id asr-10095 --host 100.116.250.20 --port 10095 --benchmark

ℹ 注册后将自动执行 Benchmark，耗时较长请耐心等待...
✓ 节点注册成功: asr-10095 (ONLINE)

  ● Phase 1/2: 单线程测速...
  ✓ Phase 1 完成: 单线程 RTF = 0.1234
  ● Phase 2/2: 并发梯度测试...
    ├ N=1: tp_rtf=0.1180
    ├ N=2: tp_rtf=0.0634
    ├ N=4: tp_rtf=0.0358 ← 推荐
    └ N=8: ⚠ 退化
✓ Benchmark 完成: RTF 基线 = 0.1234, 推荐并发 = 4
```

**CLI httpx 流式读取实现方式：**

```python
# api_client.py
def benchmark_server(self, server_id, timeout=960.0):
    with self._client.stream(
        "POST", f"/api/v1/servers/{server_id}/benchmark", timeout=timeout,
    ) as resp:
        self._check_status(resp)
        events = []
        for line in resp.iter_lines():
            if not line.strip():
                continue
            event = json.loads(line)
            events.append(event)
            yield event  # 改为 generator，调用方逐条处理
```

### 4.9 合理性审查

**Q1: 为什么不用 WebSocket 做 benchmark 进度推送？**
A: Benchmark 的通信模式是"一次请求 → 期间多条进度 → 一个最终结果"，NDJSON StreamingResponse 完全匹配。WebSocket 需要额外的连接管理和前端订阅逻辑，且 benchmark 不是双向交互——只是服务端单向推送进度。

**Q2: progress_callback 失败会不会影响 benchmark？**
A: 不会。callback 调用外套 try/except，失败只记日志，不中断测速流程。

**Q3: 多服务器并行 benchmark 时，事件交错是否会导致解析混乱？**
A: 每条事件都带 `server_id` 字段，前端/CLI 按 server_id 分组即可。最终的 `all_complete` 事件包含完整聚合结果，可作为兜底解析。

**Q4: 原来的 JSON 响应怎么办？第三方调用会不会受影响？**
A: 当前没有第三方消费者。NDJSON 的最后一行 `benchmark_complete` 的 `data` 字段与原来的 JSON body 结构完全一致。如果未来需要向后兼容，可通过 `Accept` 头协商：`application/json` 返回传统响应，`application/x-ndjson` 或默认返回流式响应。

**Q5: 前端 ElNotification 弹窗 vs 对话框内嵌进度？**
A: 建议对话框内嵌 `el-timeline` 进度。原因：(1) 弹窗容易被用户关闭而丢失上下文；(2) 对话框的 loading 态自然表示"操作进行中"；(3) 进度信息在对话框中有明确的归属感。

**Q6: register + benchmark streaming 的第一行就返回注册结果，是否意味着注册在 benchmark 开始前就已持久化？**
A: 是的。设计中 `await db.commit()` 在 benchmark 之前执行，确保注册信息已持久化。即使 benchmark 失败，服务器注册不受影响。这与原始设计一致。

---

## 五、开发任务分解

### Phase 1：服务层 — progress_callback 机制（无外部影响）

| 任务 | 文件 | 内容 | 工作量 |
|------|------|------|--------|
| T1.1 | `server_benchmark.py` | `benchmark_server_full()` 新增 `progress_callback` 参数 | 小 |
| T1.2 | `server_benchmark.py` | 在 8 个关键节点插入 callback 调用（带 try/except） | 中 |
| T1.3 | `server_benchmark.py` | 补全 Layer 1 日志空白（5 个新增 structlog 日志点） | 小 |
| T1.4 | `server_benchmark.py` | `benchmark_server_full_with_ssl_fallback()` 透传 callback | 小 |
| **验证** | | 不传 callback 时行为与现在完全一致（回归测试） | |

### Phase 2：API 层 — StreamingResponse 改造

| 任务 | 文件 | 内容 | 工作量 |
|------|------|------|--------|
| T2.1 | `servers.py` | `benchmark_server_endpoint` 改为 NDJSON StreamingResponse | 中 |
| T2.2 | `servers.py` | `benchmark_all_servers` 改为 streaming（多服务器并行事件） | 中 |
| T2.3 | `servers.py` | `register_server`（run_benchmark=true）改为 streaming | 中 |
| T2.4 | `schemas/server.py` | 如果需要，新增进度事件的 Pydantic 模型（可选） | 小 |
| **验证** | | `curl -N POST .../benchmark` 能看到逐行 JSON 输出 | |

### Phase 3：CLI — 流式读取与进度展示

| 任务 | 文件 | 内容 | 工作量 |
|------|------|------|--------|
| T3.1 | `cli/api_client.py` | `benchmark_server` / `benchmark_servers` 改为 `stream=True` + generator | 中 |
| T3.2 | `cli/api_client.py` | `register_server` 在 `run_benchmark=true` 时流式读取 | 小 |
| T3.3 | `cli/commands/server.py` | `benchmark` 命令逐行解析 NDJSON 事件，格式化输出进度 | 中 |
| T3.4 | `cli/commands/server.py` | `register` 命令（`--benchmark`）处理 streaming 响应 | 小 |
| **验证** | | CLI 执行 benchmark 时终端实时显示进度 | |

### Phase 4：前端 — streaming 解析与 UI 展示

| 任务 | 文件 | 内容 | 工作量 |
|------|------|------|--------|
| T4.1 | `api/index.js` | 新增 `benchmarkServerStream(serverId, onEvent)` 函数 | 中 |
| T4.2 | `api/index.js` | `registerServer` 在 `run_benchmark=true` 时使用 streaming | 中 |
| T4.3 | `MonitorView.vue` | 添加对话框新增 benchmark 进度展示区域（el-timeline） | 中 |
| T4.4 | `MonitorView.vue` | handleSubmit 改用 streaming API，实时更新进度 | 中 |
| **验证** | | 前端添加服务器 + benchmark 时对话框实时显示进度 | |

### 依赖关系与实施顺序

```
Phase 1 (服务层)
    ↓ T1.1-T1.4 完成后
Phase 2 (API 层)
    ↓ T2.1-T2.3 完成后（可用 curl 验证）
Phase 3 (CLI)          Phase 4 (前端)
    ↓                      ↓
    独立验证                  独立验证
```

Phase 3 和 Phase 4 互不依赖，可并行开发。每个 Phase 完成后可独立验证：
- Phase 1 完成：现有功能不受影响（回归）
- Phase 2 完成：`curl -N` 可看到流式输出
- Phase 3 完成：CLI 实时进度
- Phase 4 完成：前端实时进度

---

## 六、参考：现有代码位置索引

| 组件 | 文件 | 关键位置 |
|------|------|---------|
| Benchmark 核心 | `3-dev/src/backend/app/services/server_benchmark.py` | `benchmark_server_full()` L138-391 |
| SSL 回退封装 | 同上 | `benchmark_server_full_with_ssl_fallback()` L394-432 |
| 退化检测 | 同上 | `_detect_optimal_concurrency()` L537-594 |
| 单样本测速 | 同上 | `_benchmark_single_sample()` L666-718 |
| 并发测速 | 同上 | `_benchmark_concurrent()` L730-888 |
| API 端点 | `3-dev/src/backend/app/api/servers.py` | `benchmark_server_endpoint()` L176-206 |
| API 全部服务器 | 同上 | `benchmark_all_servers()` L104-173 |
| API 注册 | 同上 | `register_server()` L39-94 |
| 结果写入 | 同上 | `_apply_benchmark_result()` L317-357 |
| Schema | `3-dev/src/backend/app/schemas/server.py` | `ServerRegisterRequest` / `ServerBenchmarkItem` |
| 前端表单 | `3-dev/src/frontend/src/views/MonitorView.vue` | `handleSubmit()` / `defaultForm()` |
| 前端 API | `3-dev/src/frontend/src/api/index.js` | `registerServer()` |
| CLI 命令 | `3-dev/src/backend/cli/commands/server.py` | `register()` / `benchmark()` |
| CLI 客户端 | `3-dev/src/backend/cli/api_client.py` | `register_server()` / `benchmark_server()` |
| SSE 参考 | `3-dev/src/backend/app/api/sse.py` | StreamingResponse 模式参考 |
| SSE 前端参考 | `3-dev/src/frontend/src/views/TaskDetailView.vue` | `connectSSE()` L209-260 ReadableStream 解析 |
