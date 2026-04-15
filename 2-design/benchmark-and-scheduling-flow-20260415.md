# Benchmark 流程图 & 调度策略时序图

> 基于 `server_benchmark.py`、`scheduler.py`、`task_runner.py`、`servers.py` 源码绘制

---

## 一、Benchmark 测试流程图

### 1.1 顶层入口：API `POST /api/v1/servers/benchmark`

```mermaid
flowchart TD
    START([CLI: python -m cli server benchmark]) --> API["POST /api/v1/servers/benchmark"]
    API --> LOAD["加载所有 ONLINE 服务器<br/>servers = list_online_servers()"]
    LOAD --> PARALLEL["asyncio.gather(*[_bench_one(s) for s in servers])<br/>并行对所有服务器执行 benchmark"]

    PARALLEL --> BENCH_ENTRY["benchmark_server_full_with_ssl_fallback()<br/>带 wss→ws 降级兜底"]
    BENCH_ENTRY --> TRY_WSS{尝试 wss://}
    TRY_WSS -->|SSL 错误/连接失败| TRY_WS["降级 ws:// 重跑"]
    TRY_WSS -->|成功| PHASE1
    TRY_WS --> PHASE1

    subgraph PHASE1 ["Phase 1: 单线程 RTF (tv-report-1.wav)"]
        P1_LOAD["加载 tv-report-1.wav<br/>duration ≈ 179.5s"]
        P1_LOAD --> P1_REPEAT["重复 2 次 (_benchmark_single_sample)"]
        P1_REPEAT --> P1_WS["建立 WebSocket 连接"]
        P1_WS --> P1_SEND["发送 start_msg JSON"]
        P1_SEND --> P1_UPLOAD["分块上传 payload (64KB/chunk)"]
        P1_UPLOAD --> P1_END["发送 end_msg (is_speaking=false)"]
        P1_END --> P1_WAIT["等待 is_final 响应"]
        P1_WAIT --> P1_TIME["记录时序:<br/>connect_ms / upload_ms /<br/>post_upload_wait_ms / total_ms"]
    end

    P1_TIME --> P1_CALC["取中位数 RTF<br/>single_rtf = median(total_ms/1000 / audio_sec)"]
    P1_CALC --> PHASE2

    subgraph PHASE2 ["Phase 2: 并发梯度吞吐量 (test.mp4 × N)"]
        P2_LOAD["加载 test.mp4<br/>duration ≈ 16.9s"]
        P2_LOAD --> P2_GRADIENT["梯度并发: N = 1 → 2 → 4 → 8"]
        P2_GRADIENT --> P2_LEVEL["对每个 N:"]
        P2_LEVEL --> P2_REPEAT2["重复 2 次, 取中位数 wall_clock"]
        P2_REPEAT2 --> P2_BARRIER["同步屏障 (asyncio.Event)<br/>全部连接就绪后同时上传"]
        P2_BARRIER --> P2_UPLOAD["N 个 worker 并行上传 + 等待响应"]
        P2_UPLOAD --> P2_METRICS["计算指标:<br/>per_file_rtf = wall / audio_sec<br/>throughput_rtf = wall / (audio_sec × N)<br/>upload_spread / ping_rtt"]
        P2_METRICS --> P2_DEGRADE{"退化检测"}
        P2_DEGRADE -->|throughput 改善 < 10%| P2_STOP["停止梯度, 记录退化点"]
        P2_DEGRADE -->|per_file_rtf > 2×single_rtf| P2_STOP
        P2_DEGRADE -->|仍在改善| P2_NEXT["进入下一个 N"]
        P2_NEXT --> P2_LEVEL
    end

    P2_STOP --> OPTIMAL["_detect_optimal_concurrency()<br/>确定 recommended_concurrency<br/>和最优 throughput_rtf"]
    OPTIMAL --> WRITE["_apply_benchmark_result(server, bench)<br/>写入 DB:"]
    WRITE --> W1["server.rtf_baseline = single_rtf"]
    WRITE --> W2["server.throughput_rtf = throughput_rtf"]
    WRITE --> W3["server.benchmark_concurrency = rec_N"]
    WRITE --> W4["server.max_concurrency = rec_N<br/>(退化检测自动调整)"]

    W1 & W2 & W3 & W4 --> COMPARE["compare_server_capacity()<br/>输出容量对比表"]
    COMPARE --> COMMIT["db.commit() 持久化"]
    COMMIT --> DONE([Benchmark 完成])
```

### 1.2 并发测试同步屏障细节

```mermaid
sequenceDiagram
    participant M as Main (benchmark)
    participant W1 as Worker-1
    participant W2 as Worker-2
    participant W3 as Worker-3
    participant W4 as Worker-4
    participant S as FunASR Server

    Note over M,S: N=4 并发测试, 使用 test.mp4

    par 建立连接
        W1->>S: WebSocket connect + send start_msg
        W1->>M: ready_count += 1
    and
        W2->>S: WebSocket connect + send start_msg
        W2->>M: ready_count += 1
    and
        W3->>S: WebSocket connect + send start_msg
        W3->>M: ready_count += 1
    and
        W4->>S: WebSocket connect + send start_msg
        W4->>M: ready_count += 1
    end

    Note over M: ready_count == 4 → ready_event.set()
    M->>W1: fire_event.set() (屏障释放)
    M->>W2: fire_event.set()
    M->>W3: fire_event.set()
    M->>W4: fire_event.set()

    par 同步上传
        W1->>S: 上传 payload chunks + end_msg
    and
        W2->>S: 上传 payload chunks + end_msg
    and
        W3->>S: 上传 payload chunks + end_msg
    and
        W4->>S: 上传 payload chunks + end_msg
    end

    Note over S: 服务器并行处理 4 个请求

    par 等待响应
        W1<<--S: is_final response
        W2<<--S: is_final response
        W3<<--S: is_final response
        W4<<--S: is_final response
    end

    Note over M: 计算:<br/>upload_spread = max(upload_done) - min(upload_done)<br/>concurrent_post_upload = max(final_resp) - max(upload_done)
```

### 1.3 退化检测算法

```mermaid
flowchart TD
    START(["梯度结果: N=1, 2, 4, 8"]) --> INIT["best = gradient[0] (N=1)"]
    INIT --> LOOP["遍历 i = 1..len-1"]
    LOOP --> CHECK_TP{"throughput_rtf[i] 改善<br/>>= 10% ?<br/>(1 - cur/prev >= 0.10)"}
    CHECK_TP -->|否| DEGRADE["检测到吞吐退化<br/>break (停止梯度)"]
    CHECK_TP -->|是| CHECK_PF{"per_file_rtf[i] <=<br/>single_rtf × 2.0 ?"}
    CHECK_PF -->|否| DEGRADE
    CHECK_PF -->|是| UPDATE["best = gradient[i]<br/>继续下一个 N"]
    UPDATE --> LOOP
    DEGRADE --> RESULT["返回 (best.concurrency,<br/>best.throughput_rtf)"]
    LOOP -->|遍历完| RESULT

    style DEGRADE fill:#ff6b6b,color:#fff
    style UPDATE fill:#51cf66,color:#fff
```

---

## 二、调度策略时序图

### 2.1 TaskRunner 主循环

```mermaid
flowchart TD
    START([BackgroundTaskRunner.start]) --> LOOP["while not stop_event"]
    LOOP --> PROMO["Step 1: _promote_preprocessing_tasks()<br/>PREPROCESSING → QUEUED<br/>(created_at 超过 2s 的任务)"]
    PROMO --> DISPATCH["Step 2: _dispatch_queued_tasks()<br/>核心调度逻辑 (见 2.2)"]
    DISPATCH --> RETRY["Step 3 (每10轮): _retry_failed_tasks()<br/>FAILED → QUEUED (retry_count < max)"]
    RETRY --> CALLBACK["Step 4 (每30轮): _retry_pending_callbacks()<br/>重试未送达的回调"]
    CALLBACK --> WAIT["_wait_for_dispatch_signal()<br/>等 1s 或被唤醒"]
    WAIT --> LOOP

    TASK_DONE["任务完成/失败"] --> CLEAR["清除已完成的 slot queue"]
    CLEAR --> REQ["_request_dispatch()<br/>唤醒主循环立即调度"]
    REQ --> WAIT
```

### 2.2 核心调度流程 `_dispatch_queued_tasks`

```mermaid
flowchart TD
    START(["_dispatch_queued_tasks()"]) --> LOAD_SRV["加载所有 ONLINE 服务器<br/>ServerInstance → ServerProfile"]
    LOAD_SRV --> LOAD_RT["rtf_baseline / throughput_rtf /<br/>penalty_factor 从 DB 读入"]
    LOAD_RT --> LOAD_TASK["加载 QUEUED 状态的任务<br/>(limit 200, order by created_at)"]
    LOAD_TASK --> LOAD_COUNT["查询 DISPATCHED+TRANSCRIBING 状态<br/>统计每台服务器 running_count"]
    LOAD_COUNT --> BUILD_PROF["构建 ServerProfile:<br/>running_tasks = running_count[server_id]<br/>free_slots = max_concurrency - running_tasks"]

    BUILD_PROF --> CB{"Circuit Breaker<br/>允许请求?"}
    CB -->|否| SKIP["跳过该服务器"]
    CB -->|是| HAS_TASK{"有待调度任务?"}
    HAS_TASK -->|否| END1([返回])
    HAS_TASK -->|是| CHECK_PLAN

    CHECK_PLAN{"需要重新规划?<br/>has_unplanned /<br/>servers_changed /<br/>queue_imbalanced"}
    CHECK_PLAN -->|是| CLEAR_Q["清除旧 slot queues"]
    CLEAR_Q --> BATCH_SCHED

    subgraph BATCH_SCHED ["schedule_batch() — 批量规划"]
        direction TB
        BS1["1. 筛选 ONLINE 服务器<br/>   计算每个 server 的 free slots"]
        BS1 --> BS2["2. _allocate_quotas():<br/>   tp_speed = 1 / throughput_rtf<br/>   quota = round(N × speed / total_speed)"]
        BS2 --> BS3["3. 估算每任务最优服务器:<br/>   est_time = audio_dur × rtf × (1+penalty×running)<br/>   + overhead(5s)"]
        BS3 --> BS4["4. LPT 排序: 按估算时间降序<br/>   (长任务优先分配)"]
        BS4 --> BS5["5. EFT 分配: 对每个任务<br/>   选 eligible_slots 中<br/>   earliest_free + est_time 最小的槽"]
        BS5 --> BS6["6. 更新 slot.earliest_free<br/>   输出 ScheduleDecision 列表"]
    end

    BATCH_SCHED --> BUILD_SQ["build_slot_queues()<br/>按 server:slot 分组<br/>形成有序队列"]
    BUILD_SQ --> PHASE_A

    CHECK_PLAN -->|否, 使用已有计划| PHASE_A

    subgraph PHASE_A ["Phase A: 从预规划队列分发"]
        direction TB
        PA1["遍历所有 slot queues"]
        PA1 --> PA2{"该 server 有空闲槽?"}
        PA2 -->|否| PA1
        PA2 -->|是| PA3["取队列头部 decision"]
        PA3 --> PA4["task.assigned_server_id = server_id<br/>task.eta_seconds = est_duration<br/>QUEUED → DISPATCHED"]
        PA4 --> PA5["free_slots[server] -= 1<br/>pop 队列头部"]
    end

    PHASE_A --> PHASE_B

    subgraph PHASE_B ["Phase B: Work Stealing 空闲窃取"]
        direction TB
        PB1["遍历仍有 free_slots 的服务器"]
        PB1 --> PB2["_find_steal_candidate():<br/>扫描其他服务器队列尾部<br/>找 improvement 最大的任务"]
        PB2 --> PB3{"找到可窃取任务?"}
        PB3 -->|否| PB1
        PB3 -->|是| PB4["将任务从源队列移到当前服务器<br/>重新计算 est_time<br/>DISPATCHED"]
        PB4 --> PB5["free_slots[server] -= 1"]
        PB5 --> PB1
    end

    PHASE_B --> EXEC["asyncio.create_task(_execute_task)<br/>为每个 to_start 任务创建协程"]
    EXEC --> END2([返回, 等 1s 或唤醒])

    style BATCH_SCHED fill:#e8f4f8,stroke:#333
    style PHASE_A fill:#fff3bf,stroke:#333
    style PHASE_B fill:#d3f9d8,stroke:#333
```

### 2.3 任务执行与 RTF 校准时序

```mermaid
sequenceDiagram
    participant TR as TaskRunner
    participant DB as Database
    participant SCH as Scheduler
    participant SRV as FunASR Server
    participant CB as CircuitBreaker

    Note over TR: _execute_task(task_id)

    TR->>DB: 加载 task + server + file
    TR->>DB: DISPATCHED → TRANSCRIBING<br/>记录 started_at

    alt 需要音频格式转换
        TR->>TR: ensure_wav(audio_path)
    end

    TR->>TR: _build_message_profile(task)<br/>设置 wav_name / format / language / hotwords

    TR->>SRV: adapter.transcribe(wss://host:port)
    Note over SRV: 服务器处理语音识别

    alt 识别成功
        SRV-->>TR: result.text + result.raw
        TR->>CB: breaker.record_success()
        TR->>SCH: calibrate_after_completion()<br/>actual_rtf = actual_sec / audio_sec
        Note over SCH: 更新 RTF 滚动窗口<br/>计算新 p90<br/>调整 penalty_factor
        TR->>DB: 保存结果 (json/txt/srt)
        TR->>DB: TRANSCRIBING → SUCCEEDED<br/>记录 completed_at
    else 识别失败
        SRV-->>TR: result.error
        TR->>CB: breaker.record_failure()
        TR->>DB: TRANSCRIBING → FAILED
    else SSL 连接失败
        TR->>SRV: 降级 ws:// 重试
    end

    TR->>TR: _unmark_inflight(task_id)
    TR->>TR: 清除已完成的 slot queues
    TR->>TR: _request_dispatch()<br/>唤醒主循环立即调度下一批
```

### 2.4 RTF 校准与 Penalty 调整

```mermaid
flowchart TD
    START(["任务完成: calibrate_after_completion()"]) --> CALC["actual_rtf = actual_sec / audio_sec"]
    CALC --> RECORD["rtf_tracker.record(server_id, actual_rtf)<br/>写入滚动窗口 (size=50)"]
    RECORD --> P90["new_p90 = 求窗口内 p90 值"]

    P90 --> HAS_PRED{"有 predicted_duration?"}
    HAS_PRED -->|否| LOG["记录日志, 返回"]
    HAS_PRED -->|是| DEVIATION["deviation = actual / predicted"]

    DEVIATION --> CHECK_HIGH{"deviation > 1.3 ?<br/>(慢了 30% 以上)"}
    CHECK_HIGH -->|是| PENALTY_UP["penalty_factor +=<br/>penalty × 0.2 (增加 20%)"]
    PENALTY_UP --> LOG_WARN["日志: eta_calibration_penalty_increase"]
    LOG_WARN --> LOG

    CHECK_HIGH -->|否| CHECK_LOW{"deviation < 0.7 ?<br/>(快了 30% 以上)"}
    CHECK_LOW -->|是| FAST["consecutive_fast[server] += 1"]
    FAST --> FAST_CHECK{"连续快速 >= 10 次?"}
    FAST_CHECK -->|是| PENALTY_DOWN["penalty_factor -=<br/>penalty × 0.1 (减少 10%)<br/>min = 0.01"]
    PENALTY_DOWN --> LOG_INFO["日志: eta_calibration_penalty_decrease"]
    LOG_INFO --> LOG
    FAST_CHECK -->|否| LOG

    CHECK_LOW -->|否| STABLE["偏差在 ±30% 内<br/>penalty 不变"]
    STABLE --> LOG

    style PENALTY_UP fill:#ff6b6b,color:#fff
    style PENALTY_DOWN fill:#51cf66,color:#fff
    style STABLE fill:#74c0fc,color:#fff
```

### 2.5 Quota 分配算法

```mermaid
flowchart TD
    START(["_allocate_quotas(task_count=25, servers)"]) --> FILTER["过滤有空闲槽的服务器"]
    FILTER --> SPEED["计算每台服务器吞吐速度:<br/>tp_speed = 1 / throughput_rtf"]
    SPEED --> TOTAL["total_speed = Σ(tp_speed)"]
    TOTAL --> QUOTA["quota[server] = round(25 × speed / total)"]
    QUOTA --> CHECK_SUM{"Σ(quota) == 25?"}
    CHECK_SUM -->|差值 > 0| ADD_FAST["差额加给最快服务器"]
    CHECK_SUM -->|差值 < 0| SUB_SLOW["从最慢服务器扣除"]
    CHECK_SUM -->|恰好 25| LOG["记录 quota_allocation 日志"]
    ADD_FAST --> LOG
    SUB_SLOW --> LOG

    LOG --> RETURN["返回 quotas dict"]

    style SPEED fill:#e8f4f8,stroke:#333
    style QUOTA fill:#fff3bf,stroke:#333
```

---

## 三、关键数据流总结

### 3.1 Benchmark 数据写入 → 调度读取

```
┌─────────────────────────────────────────────────────────┐
│  Benchmark (server_benchmark.py)                        │
│                                                         │
│  tv-report-1.wav ──→ single_rtf ──→ DB.rtf_baseline    │
│  test.mp4 × N   ──→ throughput_rtf → DB.throughput_rtf │
│  退化检测        ──→ recommended_N → DB.max_concurrency │
└──────────────────────┬──────────────────────────────────┘
                       │ DB commit
                       ▼
┌─────────────────────────────────────────────────────────┐
│  Scheduler (task_runner.py)                             │
│                                                         │
│  DB.rtf_baseline     → ServerProfile.rtf_baseline       │
│  DB.throughput_rtf   → ServerProfile.throughput_rtf     │
│  DB.max_concurrency  → ServerProfile.max_concurrency    │
│  DB.penalty_factor   → ServerProfile.penalty_factor     │
│                                                         │
│  tp_speed = 1 / throughput_rtf   → Quota 分配           │
│  effective_rtf = p90_rtf × (1+penalty×running)          │
│  est_time = audio_dur × effective_rtf + overhead(5s)    │
└──────────────────────┬──────────────────────────────────┘
                       │ 任务完成后
                       ▼
┌─────────────────────────────────────────────────────────┐
│  RTF 校准 (calibrate_after_completion)                  │
│                                                         │
│  actual_rtf = wall_time / audio_dur                     │
│  → 滚动窗口 p90 更新                                    │
│  → deviation = actual / predicted                       │
│  → penalty_factor 自适应调整 (±)                         │
│  (仅内存, 不回写 DB)                                    │
└─────────────────────────────────────────────────────────┘
```

### 3.2 一次 25 任务批处理的完整时序

```
时间线 ──────────────────────────────────────────────────────►

[CLI] 上传25文件 ──→ 创建25任务 ──→ 轮询等待 ──→ 下载结果

[DB]  PENDING → PREPROCESSING (2s) → QUEUED

[TR]  主循环检测到 25 个 QUEUED 任务:
      │
      ├─ schedule_batch():
      │   ├─ quota 分配: {10095:9, 10096:12, 10097:4}
      │   ├─ LPT 排序: tv-report-1.mp4 排前, test.mp4 排后
      │   └─ EFT 填入 slot queues
      │
      ├─ Phase A: 从 slot queues 分发首波
      │   └─ 7个任务 DISPATCHED (占满全部空闲槽)
      │
      ├─ Phase B: Work Stealing (本波无空闲, 跳过)
      │
      ├─ asyncio.create_task × 7 (并行执行)
      │   ├─ TRANSCRIBING → wss://server → SUCCEEDED
      │   └─ calibrate_after_completion()
      │
      ├─ 任务完成 → _request_dispatch() 唤醒主循环
      │
      ├─ 第二轮 dispatch:
      │   ├─ 检测 slot queues 有剩余计划 → 直接分发
      │   └─ 又分发 N 个任务...
      │
      ├─ ... 多轮接力 ...
      │
      └─ 最后一个任务完成 → 25/25 SUCCEEDED
```
