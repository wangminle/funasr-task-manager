# CLI 批量转写 Bug 报告

> **日期**：2026-05-10
> **版本**：V0.4.16 (00c8802) | 对比基线：V0.4.13 (145dd5f)
> **发现场景**：CLI 批量转写测试（25 文件 + HP Pro Mini 50 文件压力测试 + v0.4.13/v0.4.16 A-B 对比）
> **严重程度**：高（含 1 个阻断级 CLI Bug + 多个导致任务卡死/结果丢失的引擎 Bug）
> **数据来源**：① 本机 CLI 25 文件测试 ② HP Pro Mini 50 文件运行记录 + 后端原始日志 ③ v0.4.13 vs v0.4.16 同条件对比测试

---

## 第一部分：CLI 层 Bug（已确认，本次测试直接复现）

### Bug #1：CLI 连接了错误的后端地址（阻断级）

**现象**

`python -m cli --verbose upload <file>` 输出：

```
Server: http://127.0.0.1:28000
API Key: (none)
Error: 上传失败 test001-copy-batch-1.wav:
```

25 个文件全部上传失败，错误信息为空。

**发现路径**

1. 执行 `python -m cli transcribe --batch` → 25/25 上传失败
2. 用 `curl` 直接调用 `POST /api/v1/files/upload` → 201 成功 → 排除后端问题
3. 加 `--verbose` 发现 CLI 连接的是 `http://127.0.0.1:28000`，而非 `http://localhost:15797`

**根因**

`~/.asr-cli.yaml` 中 `server` 字段为历史残留值 `http://127.0.0.1:28000`。

CLI 地址解析优先级链（`cli/main.py:30-40` `_resolve()` 函数）：

```
CLI --server 参数 > 环境变量 ASR_API_SERVER > ~/.asr-cli.yaml server > 代码默认值
```

代码默认值全部正确：

| 位置 | 文件 | 行号 | 值 |
|------|------|------|----|
| 配置默认值 | `cli/config_store.py` | 11 | `"server": "http://localhost:15797"` |
| `_resolve` 回退 | `cli/main.py` | 62 | `"http://localhost:15797"` |
| 后端配置 | `app/config.py` | 86 | `port: int = 15797` |

端口 28000 在整个项目代码中无任何引用（`rg "28000"` 零结果），环境变量也未设置。

**临时修复**

```bash
python -m cli config set server http://localhost:15797
```

---

### Bug #2：上传失败时错误信息为空字符串

**现象**

```
上传失败 test001-copy-batch-1.wav:
```

冒号后无任何内容。

**发现路径**

1. 观察 25 个文件的错误输出，全部是 `上传失败 xxx:` 后面空白
2. 检查 `cli/api_client.py:_check()` 逻辑 → 发现 `detail` 为空时不做兜底
3. 用 Python 模拟连接 28000 → 得到 `APIError: status_code=503, detail=""`

**根因**

错误处理链路：

```
api_client.py:84  → self._client.post("/api/v1/files/upload", ...)
                     ↓ 连接到 28000，返回 HTTP 503，body 为空
api_client.py:37  → resp.json()  ⟹ JSONDecodeError（空字符串）
api_client.py:39  → fallback: detail = resp.text  ⟹ ""
api_client.py:41  → raise APIError(503, "")
transcribe.py:167 → out.error(f"上传失败 {fp.name}: {e.detail}")  ⟹ 空
```

**代码定位**

- `cli/api_client.py:35-41` — `_check()` 方法，`detail` 为空时无兜底消息
- `cli/commands/transcribe.py:82,167` — `except APIError` 直接拼接 `e.detail`

---

### Bug #3：`upload_file()` 未捕获 httpx 网络异常

**现象**

当目标地址完全不可达时，`httpx` 抛出 `httpx.ConnectError`，但 `transcribe.py` 的 `except APIError` 无法捕获，会以未处理异常形式崩溃。

**代码定位**

- `cli/api_client.py:82-88` — `upload_file()` 没有 try/except 包裹 `self._client.post()`
- `cli/commands/transcribe.py:79-83, 165-167` — 只 catch `APIError`

---

### Bug #4：init SKILL.md 缺少 CLI server 配置验证

**现象**

Phase 4A Step 6 启动后端后只验证了 `curl /health`，没有验证 CLI 连接地址。Phase 5 验证报告也无 CLI 检查项。

**文档定位**

- `6-skills/funasr-task-manager-init/SKILL.md` Phase 4A Step 6（~第 154-162 行）
- `6-skills/funasr-task-manager-init/SKILL.md` Phase 5 验证报告（~第 217-229 行）
- 对比：Phase 7.1 有 `python -m cli config set notify.*` 步骤，说明文档作者知道 CLI 需要配置，但遗漏了 `server`

---

## 第二部分：转写引擎层差异分析（0.4.13 → 0.4.16）

> 以下基于代码对比 `145dd5f`（V0.4.13）与 `00c8802`（V0.4.16）的 diff 分析，尚需通过构造特定 FunASR 返回来复现。

### Bug #5：`_should_complete()` stamp_sents 兜底被限制为 `mode=="offline"`（潜在卡住级）

**证据链**

0.4.13 代码（`funasr_ws.py` `_should_complete()`）:

```python
stamp_sents = data.get("stamp_sents")
if stamp_sents and isinstance(stamp_sents, list) and len(stamp_sents) > 0:
    return True   # ← 任何 mode 只要有 stamp_sents 就判定完成
```

0.4.16 代码（`funasr_ws.py:168-170`）:

```python
stamp_sents = data.get("stamp_sents")
if mode == "offline" and stamp_sents and isinstance(stamp_sents, list) and len(stamp_sents) > 0:
    return True   # ← 新增 mode == "offline" 前置条件
```

**问题分析**

0.4.16 在 stamp_sents 兜底前加了 `mode == "offline"` 条件。但 `_should_complete()` 在前面第 163 行已经判定 `if mode == "offline": return True`，所以第 169 行的 `mode == "offline" and stamp_sents` **永远不会命中**——如果 mode 是 offline，早在第 163 行就已经返回了。

这意味着：**0.4.16 实际上完全移除了 stamp_sents 兜底逻辑**。

如果某些 FunASR 服务端版本返回的响应中 `mode` 字段不是严格的 `"offline"`（例如空字符串、`"Offline"` 大写、或其他变体），且 `is_final` 不为 true，也没有 `2pass-offline` 标记，但包含了有效的 `stamp_sents`：

- **0.4.13**：通过 stamp_sents 兜底判定完成，正常结束
- **0.4.16**：三个条件都不满足，继续等 WebSocket 消息 → 直到连接关闭或 timeout（默认 300 秒）

**与"卡住"现象的吻合度**

高度吻合。如果存在 mode 字段不严格匹配的 FunASR 服务器，0.4.16 下这些任务会超时而非正常完成。

---

### 差异 #6：预处理原子抢占机制（改进，有副作用）

**0.4.13**（`task_runner.py` `_promote_preprocessing_tasks()`）：

```python
stmt = select(Task).options(selectinload(Task.file)).where(
    Task.status == TaskStatus.PREPROCESSING,
    Task.created_at <= cutoff,
).order_by(Task.created_at.asc()).limit(100)
```

直接查询所有符合条件的 PREPROCESSING 任务，无抢占锁。

**0.4.16**（`task_runner.py:220-260`）：

新增 `Task.started_at.is_(None)` 过滤条件 + `sql_update` 原子 claim：

```python
claim_result = await session.execute(
    sql_update(Task)
    .where(Task.task_id == task.task_id,
           Task.status == TaskStatus.PREPROCESSING,
           Task.started_at.is_(None))
    .values(started_at=claim_time)
)
if claim_result.rowcount != 1:
    continue  # 其他 runner 已抢占
```

**改进意图**：防止多个 runner 同时分段同一个任务。

**潜在副作用**：如果进程在 claim 后、分段完成前崩溃，任务会停在 `PREPROCESSING + started_at != NULL`。0.4.16 在 `main.py:121-138` 加了启动时释放机制，但仅在进程重启时才触发——如果后端不重启，这些任务就永远卡住。

---

### 差异 #7：启动恢复策略从无条件回退改为保守回退

**0.4.13**（`main.py` lifespan）：

```python
result = await session.execute(
    update(Task)
    .where(Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]))
    .values(status=TaskStatus.QUEUED, assigned_server_id=None, started_at=None)
)
```

无条件将所有 DISPATCHED/TRANSCRIBING 任务回退到 QUEUED。

**0.4.16**（`main.py:101-119`）：

```python
stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
result = await session.execute(
    update(Task)
    .where(
        Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
        or_(Task.started_at < stale_cutoff, Task.started_at.is_(None)),
    )
    .values(status=TaskStatus.QUEUED, assigned_server_id=None, started_at=None)
)
```

只回退 `started_at` 为空或超过 10 分钟的任务。

**风险**：长音频转写超过 10 分钟时后端重启，started_at 在 10 分钟内的任务**不会被回退**，但其对应的 asyncio 协程已经丢失 → 任务永远停在 TRANSCRIBING，不会完成也不会重试。

---

### 差异 #8：服务器 `enabled` 字段（新增）

**0.4.13**（`server.py`）：

```python
def is_available(self) -> bool:
    return self.status == ServerStatus.ONLINE
```

无 `enabled` 字段，只看 `status`。

**0.4.16**（`server.py:34, 38-39`）：

```python
enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

def is_available(self) -> bool:
    return self.enabled and self.status == ServerStatus.ONLINE
```

新增 `enabled` 字段，调度查询也增加了 `ServerInstance.enabled.is_(True)` 过滤。

**风险**：如果数据库迁移不完整，或手动操作导致某台服务器 `enabled=false`，该服务器会静默退出调度，不参与批量转写。

---

### 差异 #9：分段目录发布从直接写改为 tmp+rename（改进）

**0.4.13**：直接写入正式目录，失败时删除正式目录。

**0.4.16**（`task_runner.py:331-363`）：先写 `{task_id}.tmp-{pid}` 临时目录，成功后 `rename` 到正式目录。

这是正向改进，降低了并发分段互相删除文件的风险。**无 Bug。**

---

### 差异 #10：分段任务 slot 计数排除父任务（改进，但仍需验证超派）

**0.4.13**：分段父任务和正在运行的 segment 都计入 server 占用 → 可能误判服务器满载。

**0.4.16**（`task_runner.py:461-478`）：排除有 active segment 的父任务，只计子任务。

这是正向改进，避免双重计数。

**但 HP Pro Mini 实际运行记录显示仍存在 slot 超派**：

- session report 中出现 `funasr-remote-01(5/4), funasr-remote-02(2/2), funasr-remote-03(2/2)`
- raw backend log 中出现 `funasr-remote-02(3/2)`

因此，0.4.16 的改动只能说明“父任务重复计数”这个方向被修正，不能证明调度派发已经安全。仍需检查 segment 派发、whole-file 派发、并发调度 tick、任务恢复后的 slot 计数是否都走同一套原子占位逻辑。

---

## 第 2.5 部分：v0.4.13 vs v0.4.16 A-B 对比测试（决定性证据）

> 来源：`1-discussion/Agent实际运行记录/20260510-hp-pro-mini/0413ver-0416ver-comparison-report-20260510.md`
> 测试条件：同一批 50 个 mp4 文件（3.06 GB，12.15 小时音频）、同一组 3 台服务器、同一 Benchmark 基线 4:2:1

### A-B 测试结果

| 指标 | 5/8 v0.4.13 | 5/10 v0.4.16 | 5/10 v0.4.13 (4:2:1) |
|------|:-:|:-:|:-:|
| 完成/总数 | **50/50 ✅** | **7/50 ❌** | **50/50 ✅** |
| 总耗时 | 8 分 24 秒 | 42 分钟未完成 | 8 分 22 秒 |
| transcription_completed | 85 | ~16 | 85 |
| 错误 | 0 | 0 | 0 |

v0.4.13 在相同 4:2:1 基线下两次跑出 8m22s / 8m24s，结果高度稳定。v0.4.16 直接卡死在 7/50。

### 关键事实

**1. v0.4.13 两次稳定完成，v0.4.16 卡死——回归铁证**

v0.4.13 在 5/8 和 5/10 两次测试中分别用 8m24s 和 8m22s 完成 50/50，结果高度稳定（85 次 transcription_completed）。v0.4.16 在同一天同条件下只完成 7/50（~16 次 transcription_completed）。这**铁证回归在 v0.4.14~v0.4.16 的代码变更中**。

**2. "v0.4.13 也出现 slots=16/7" — 需谨慎解读**

报告记录了 v0.4.13 出现 `slots=16/7` 过载但没卡死。但注意：

- v0.4.13 的 slot 计数包含 parent task + segment task **双重计数**
- v0.4.16 的 slot 计数排除了 parent，`5/4` 代表更接近真实 FunASR 连接数
- 两个数字**不是同一套计数口径**，"16/7" 中有多少是 parent 虚高无法从报告中确定
- 因此 "v0.4.13 超载更严重但没卡死" 的结论需要验证，不能直接采信

**3. 85 vs ~16 的 transcription_completed 数量**

v0.4.13 的 85 次 = 24 个整文件成功 + 26 个分段任务各完成 2 段 (≈61 segment) = 85。v0.4.16 只有 ~16 次就完全停了。冻结点在**第一批 segment 完成后、第二批接手时**。

**4. 对比报告识别出的 8 个代码变更**

v0.4.14~v0.4.16 间 `task_runner.py` 有 +348/-172 行变更，报告识别出 8 个具体改动。其中两个是本报告之前未重点关注的：

- **Segment 存在性检查**：v0.4.16 在 `_dispatch`、`_execute_task`、`_run_segment` 三处新增了 segment 存在性检查。如果 DB 中有残留 segment 记录，任务会被 dispatch 和 execute 双重跳过——不报错、不失败、不重试。这是一个潜在的静默卡死点。
- **Segment 失败处理变更**：v0.4.13 先标记 FAILED 再 retry；v0.4.16 直接 `increment_retry`。如果 `increment_retry` 内部出错，segment 状态可能不一致。

**5. v0.4.16 的 remote-01 确实出现了 5/4**

报告确认 remote-01 被分配了 5 个并发任务，超出 `max_concurrency=4`。slot 计数变更（排除 active segment parent）是否直接导致了这个超派，以及超派是否是卡死的触发点，需要代码级验证。

---

## 第 2.6 部分：HP Pro Mini 后端原始日志分析

> 来源：`1-discussion/Agent实际运行记录/20260510-hp-pro-mini/funasr-backend-logs-20260510.txt`
> 运行时间：2026-05-10 全天后端日志
> 结果：多个批次出现同类卡住模式，印证 v0.4.16 的系统性问题

**证据边界说明**：

- `session-report-20260510.md` 记录了 20:21 批次 `01KR8XC8...` 的完整测试结论：7/50 完成、9 个 TRANSCRIBING 卡死、43 个手动取消。
- `funasr-backend-logs-20260510.txt` 的原始日志只覆盖到约 19:49/19:50，没有包含 20:21 批次 `01KR8XC8...` 的后端原始事件。
- 但 raw backend log 中 19:03 左右的 `01KR8RZ...` 批次出现同类模式：部分 segment 成功后无后续完成/失败/超时，持续 `no_free_slots_for_scheduling`，最终多个 TRANSCRIBING 任务被手动取消。因此它可以作为 20:21 批次卡死机制的强印证，而不是同一批任务的直接原始日志。

### 关键事实

1. **`no_free_slots_for_scheduling` 日志出现 11,846 次**
   - raw backend log 全天统计出现 11,846 次
   - session report 中 20:28:46 起无任何 `transcription_completed` 事件
   - 42 分钟内，9 个 TRANSCRIBING 任务的 progress 完全冻结（精确到小数点后 4 位不变）

2. **零 `transcription_timeout` 事件**
   - `task_timeout_seconds = 3600`（1 小时）
   - 42 分钟的卡死尚未触及超时阈值，用户在超时前手动取消了

3. **零 `websocket_error` / `ConnectionClosed` 事件**
   - 日志中无任何显式断连记录
   - WebSocket 连接使用 `ping_interval=None`（无心跳）
   - 后端无法感知静默断开的 TCP 连接

4. **服务器 probe 全部 reachable**
   - 每 30 秒 probe 一次，3 台服务器全部返回 `reachable=True`
   - 说明 FunASR 服务端本身在线，但已经不再为卡住的 WebSocket 连接发送数据

5. **所有成功的转写只收到 1 条消息**
   - `transcription_completed` 日志全部显示 `messages=1`
   - FunASR offline 模式下，服务端处理完后发 1 条响应 → 完成

6. **19:03 raw log 批次复现同类卡住**
   - `01KR8RZ...` 批次最后一次 `transcription_completed` / `segment_transcription_succeeded` 出现在 19:09:50
   - 19:09:50 后到 19:38:23 手动取消前，持续刷 `no_free_slots_for_scheduling`
   - 19:38:23 有 8 个 `TRANSCRIBING -> CANCELED` 任务
   - 同期无 `transcription_timeout`、无 `websocket_error`、无 `transcription_error`

7. **实际 slot 占用超过配置上限**
   - session report：`funasr-remote-01(5/4), funasr-remote-02(2/2), funasr-remote-03(2/2)`
   - raw backend log：`funasr-remote-01(4/4), funasr-remote-02(3/2), funasr-remote-03(1/1)`
   - 说明卡死不只是连接层问题，调度/slot 计数也出现了超过 `max_concurrency` 的实际占用

### 新发现：Bug #11 — WebSocket `ping_interval=None` + `task_timeout_seconds=3600` 致命组合

**这是一个跨版本的存量问题**（0.4.13 和 0.4.16 都存在），但在 0.4.16 中更容易被触发。

**机制**：

```
funasr_ws.py:242 → ping_interval=None（禁用 WebSocket keepalive）
config.py:100   → task_timeout_seconds=3600（1 小时超时）

当 FunASR 服务端因过载静默丢弃连接时：
1. TCP 层不产生 RST/FIN（半开连接）
2. 后端的 `async for raw_msg in ws:` 永远阻塞
3. 无 ping/pong → 无法检测连接已死
4. 要等 3600 秒超时才会释放 → 整个服务器的 slot 被锁死 42+ 分钟
5. 新任务无法调度，队列完全停滞
```

**证据链**：

| 证据 | 说明 |
|------|------|
| 11,846 次 `no_free_slots` | slot 被占用但无任务完成 |
| 0 次 `transcription_timeout` | 3600s 超时未触发（卡死只有 42 分钟） |
| 0 次 `websocket_error` | 连接未显式断开 |
| 0 次 `is_complete=False` | 但凡收到消息的都正确完成了 |
| 所有成功项 `messages=1` | FunASR 正常时只发 1 条响应 |
| probe 全部 reachable | 服务端在线但不再响应已有连接 |

**与 0.4.13 的关系**：`ping_interval=None` 和 `task_timeout_seconds=3600` 在 0.4.13 中也存在，但 0.4.13 的 `_should_complete()` stamp_sents 兜底更宽松 + 启动恢复更激进，可能掩盖了部分卡死场景。0.4.16 收紧了这两处后，卡死变得更加明显和难以自愈。

### 新发现：Bug #12 — 无任务级 progress 冻结检测

后端没有 "progress 长时间不变则判定 stuck" 的机制。9 个任务的 progress 冻结了 42 分钟，但后端只是反复报 `no_free_slots`，没有任何告警或自动回收。

**影响**：即使 `task_timeout_seconds` 设为更短的值（如 600 秒），在超时前也没有任何主动监控手段发现问题。

### 新发现：Bug #13 — slot 计数/派发仍可能超派

0.4.16 虽然修正了“分段父任务 + segment 子任务双重计数”的问题，但实际运行中仍然出现了超过服务器 `max_concurrency` 的占用。

**证据链**：

| 证据 | 说明 |
|------|------|
| `funasr-remote-01(5/4)` | session report 显示 remote-01 实际占用 5，配置上限 4 |
| `funasr-remote-02(3/2)` | raw backend log 显示 remote-02 实际占用 3，配置上限 2 |
| 持续 `no_free_slots_for_scheduling` | 调度器认为全部服务器满载，但 slot 已经处于超额状态 |
| 无对应失败/超时释放 | 超额占用没有被健康检查或调度器主动纠正 |

**可能原因**：

1. segment 派发和 whole-file 派发没有统一使用原子 slot claim。
2. 多个调度 tick 或多个异步任务同时读取同一份 server usage 快照，随后都派发成功。
3. 任务恢复、取消、segment 父任务排除逻辑之间存在计数口径不一致。
4. HP Pro Mini 运行代码与当前 checkout 不完全一致，部分中间版本代码仍存在超派路径。

**影响**：FunASR 服务端被过量并发压垮后，更容易出现 WebSocket 静默无响应，进一步放大 Bug #11。

### 新发现：Bug #14 — schema check 漏检 `server_instances.enabled` 缺列

0.4.16 新增 `server_instances.enabled` 字段后，启动日志先出现 `schema_check_passed`，随后 heartbeat loop 和 task runner 立即报错：

```text
schema_check_passed
heartbeat_loop_error: no such column: server_instances.enabled
task_runner_loop_error: no such column: server_instances.enabled
```

**证据链**：

| 时间 | 事件 | 说明 |
|------|------|------|
| 11:29:54 | `schema_check_passed` | 启动 schema 检查误判通过 |
| 11:29:54 | `heartbeat_loop_error` | `SELECT ... server_instances.enabled ... FROM server_instances` 失败 |
| 11:29:55 ~ 11:30:02 | `task_runner_loop_error` 多次出现 | 调度查询 `WHERE server_instances.status = ? AND server_instances.enabled IS 1` 失败 |
| 11:30:06 | 新进程再次启动后通过 | 说明问题与迁移/运行环境状态有关 |

**影响**：如果数据库迁移没有正确执行，0.4.16 的调度器会无法查询 ONLINE 服务器，表现为批量转写完全无法派发或 heartbeat 异常。现有 `schema_check` 没有覆盖新增列，属于启动前置校验缺口。

### 新发现：Bug #15 — 完成事件被 `QUEUED` 状态拦截，成功无法落库

raw backend log 中出现 6 条 `task_succeeded_but_transition_blocked`，共同特征是转写完成后尝试落库时，任务当前状态已经是 `QUEUED`，因此无法合法转为 `SUCCEEDED`。

**证据链**：

| 事件 | 说明 |
|------|------|
| `task_succeeded_but_transition_blocked` 共 6 次 | 不是推测，是运行日志中的明确诊断日志 |
| `current_status=QUEUED` | 完成事件回来时任务已被恢复/回退到队列态 |
| 14:46 ~ 15:04 有多次后端重启 | 与启动恢复逻辑、任务状态回退窗口高度相关 |
| `reset_stale_tasks` 计数为 0 | 说明不是简单地从日志名就能看到恢复动作，仍需检查启动恢复和任务 claim 的具体路径 |

**影响**：即使 WebSocket 返回了正常结果，任务也可能因为状态机不接受 `QUEUED -> SUCCEEDED` 而无法固化成功。用户侧看到的结果可能是任务重新排队、重复转写、最终卡住或被取消。

### 新发现：风险 #16 — HP Pro Mini 运行代码与当前 checkout 可能不一致

raw backend log 中出现了本地当前代码未能搜索到的日志 key，例如：

- `dispatch_skip_segmented_task`
- `periodic_segment_retry`

**影响**：这说明 HP Pro Mini 当时运行的服务可能是中间构建、未同步版本，或包含尚未提交/已回滚的代码。后续复现和修复时，必须先记录运行版本、commit、镜像/服务文件来源，否则容易把某个中间版本的问题误判为当前 HEAD 的问题。

---

## 第三部分：影响评估总表

| # | 问题 | 类型 | 位置 | 严重度 | 确认状态 | 修复状态 |
|---|------|------|------|--------|----------|----------|
| 1 | CLI 连接错误端口 28000 | 运维残留 + 文档缺陷 | `~/.asr-cli.yaml` + init SKILL | **高（阻断）** | ✅ 已复现已修复 | ✅ 运维侧手动修复 |
| 2 | 上传失败错误信息为空 | 代码缺陷 | `cli/api_client.py:35-46` | 中 | ✅ 已确认 | ✅ **第一轮修复**：`_check()` 空 detail 给出可操作提示 |
| 3 | 未捕获 httpx 网络异常 | 代码缺陷 | `cli/api_client.py:49-61` | 中 | ✅ 已确认 | ✅ **第一轮修复**：新增 `_request()` 统一捕获 ConnectError/Timeout/HTTPError |
| 4 | init SKILL 缺 CLI 验证步骤 | 文档缺陷 | init SKILL Phase 4A/5 | 中 | ✅ 已确认 | ✅ **第二轮修复**：新增 Step 7 CLI 配置验证 + 错误处理表 |
| 5 | stamp_sents 兜底被移除 | **代码回归** | `funasr_ws.py:169-171` | **中-高** | ✅ 代码确认 dead branch | ✅ **第一轮修复**：恢复 0.4.13 行为，stamp_sents 非空即判定完成 |
| 6 | 预处理 claim 崩溃后任务卡住 | 代码副作用 | `task_runner.py:_promote_preprocessing_tasks` | 中 | ✅ 已确认 | ✅ **第二轮修复**：5 分钟超时释放 stale claim |
| 7 | 启动恢复/状态回退可能导致活跃任务丢协程 | 代码改动风险 | `main.py:lifespan`, `config.py` | 中 | ✅ 已确认 | ✅ **第二轮修复**：`stale_task_timeout_minutes` 可配置 + 保留近期任务 |
| 8 | enabled=false 静默退出调度 | 新功能风险 | `task_runner.py:_dispatch_queued_tasks_locked` | 低 | ✅ 代码确认 | ✅ **第二轮修复**：`servers_disabled_excluded_from_dispatch` warning |
| 9 | 分段目录 tmp+rename | 正向改进 | `task_runner.py:331-363` | — | ✅ 无 Bug | — |
| **10** | **slot 计数排除 parent（口径变更）** | **v0.4.16 变更，因果待验** | `task_runner.py:461-478` | **高（需代码级验证）** | ✅ 代码变更确认；v0.4.16 出现 5/4 超派事实确认；但 v0.4.13 的 16/7 是不同计数口径，两者不直接可比 | ✅ **第一轮** dispatch lock + slot_overbooked；**第二轮** post-dispatch invariant 检查 |
| **10a** | **Segment 存在性检查 → 静默跳过** | **v0.4.16 新增逻辑** | `task_runner.py:_create_segments_for_task` | **高（潜在卡死点）** | ⚠️ A-B 对比识别 | ✅ **第二轮修复**：区分 actionable / all-FAILED 状态，打 warning 而非静默跳过 |
| **11** | **WebSocket 无心跳 + 超时过长** | **存量缺陷** | `funasr_ws.py:237-245` + `config.py:101-103` | **高** | ✅ HP Pro Mini 铁证 | ✅ **第一轮修复**：ping_interval=20s, ping_timeout=10s, read_idle_timeout=120s |
| **12** | **无 progress 冻结检测** | **缺失功能** | `task_runner.py:_detect_frozen_tasks` | **中** | ✅ HP Pro Mini 日志铁证 | ✅ **第二轮修复**：每 60 tick 扫描 TRANSCRIBING 超时任务 |
| **13** | **slot 计数/派发仍可能超派** | **调度一致性缺陷** | `scheduler` + `task_runner` | **高（放大卡死）** | ✅ HP Pro Mini 日志铁证 | ✅ **第一轮** dispatch lock + slot_overbooked；**第二轮** post-dispatch DB invariant |
| **14** | **schema check 漏检 enabled 缺列** | **迁移/启动校验缺陷** | `app/services/diagnostics.py:49-59` | **高（调度阻断）** | ✅ raw backend log 已确认 | ✅ **第一轮修复**：`EXPECTED_CORE_TABLE_COLUMNS` 检查 server_instances.enabled 等核心列 |
| **15** | **完成事件被 QUEUED 状态拦截** | **状态机/恢复缺陷** | `task_runner.py:881-911` | **高（成功无法落库）** | ✅ raw backend log 已确认 | ✅ **第一轮修复**：QUEUED/DISPATCHED/TRANSCRIBING 状态遇到迟到完成事件 → 收敛为 SUCCEEDED |
| 16 | 运行代码与当前 checkout 可能不一致 | 复盘/发布流程风险 | `main.py:_get_git_short_sha` | 中 | ✅ raw backend log 提示 | ✅ **第二轮修复**：启动日志输出 git_sha + python_version + 关键配置 |

---

## 第 3.5 部分：第一轮修复记录（2026-05-10 夜间）

> **覆盖范围**：10 个文件，+538/-248 行
> **单元测试**：新增回归测试 65 passed；全量 unit 597 passed / 3 skipped / 1 failed（已有的 Windows 文件锁问题，不在本轮改动范围）

### 已完成修复

| Bug | 修复内容 | 关键文件 | 测试覆盖 |
|-----|---------|----------|----------|
| #2 | `_check()` 空 detail 给出可操作提示（检查 server 配置） | `api_client.py:35-46` | `test_cli_api_client.py::test_empty_error_detail_has_fallback_message` |
| #3 | 新增 `_request()` 统一捕获 ConnectError / Timeout / HTTPError → APIError | `api_client.py:49-61` | `test_cli_api_client.py::test_connect_error_is_converted_to_api_error` |
| #5 | 恢复 0.4.13 的 stamp_sents 无条件兜底：非空 stamp_sents 即判完成 | `funasr_ws.py:169-171` | `test_protocol_adapter.py::test_stamp_sents_complete_even_when_mode_missing` |
| #11 | WebSocket ping_interval=20s / ping_timeout=10s + read_idle_timeout=120s | `config.py:101-103`, `funasr_ws.py:237-295` | 配置验证 |
| #13（缓解） | dispatch lock 减少同进程重入派发 + slot_overbooked 检测日志 | `task_runner.py:513` | — |
| #14 | `EXPECTED_CORE_TABLE_COLUMNS` 覆盖 server_instances.enabled 等核心列 | `diagnostics.py:49-59` | `test_migration_compat.py::test_missing_server_enabled_detected_as_drift` |
| #15 | 迟到完成事件遇到 QUEUED/DISPATCHED/TRANSCRIBING 状态 → 收敛为 SUCCEEDED | `task_runner.py:881-911` | `test_task_runner_dispatch.py::test_late_completion_from_queued_recovers_to_succeeded` |
| 新增 | segment/短任务动态超时：按 `max(segment_timeout_min_seconds, duration × multiplier)` 收紧，上限仍为 task_timeout_seconds | `task_runner.py:1220-1226`, `config.py:104-105` | — |

### 待后续轮次

~~全部已在第二轮完成~~

---

## 第 3.6 部分：第二轮修复记录（2026-05-10 深夜）

> **覆盖范围**：5 个文件（`task_runner.py`, `main.py`, `config.py`, `diagnostics.py` 未改, init SKILL）
> **单元测试**：新增 3 个回归测试；全量 unit 601 passed / 3 failed（均为已有问题：1 个 Windows 文件锁 + 2 个长音频分段窗口断言，不在本轮改动范围）

### 已完成修复

| Bug | 修复内容 | 关键文件 | 测试覆盖 |
|-----|---------|----------|----------|
| #4 | init SKILL 新增 Step 7：CLI 配置验证（`config get server` + `health`），错误处理表新增"CLI 连接错误地址"场景 | `6-skills/funasr-task-manager-init/SKILL.md` | 文档审阅 |
| #6 | 预处理 claim 5 分钟超时释放：`_promote_preprocessing_tasks()` 开头检查 `started_at < 5min ago` 的 PREPROCESSING 任务并清空 `started_at` | `task_runner.py:_promote_preprocessing_tasks` | `test_task_runner_dispatch.py::TestPreprocessingClaimTimeout::test_stale_preprocessing_claim_released` |
| #7 | 启动恢复逻辑优化：`stale_task_timeout_minutes` 可配置（默认 10min），日志区分 reset_count vs preserved_count，preserved 的任务由迟到完成恢复路径兜底 | `config.py:stale_task_timeout_minutes`, `main.py:lifespan` | 配置验证 |
| #8 | `enabled=false` 防御日志：dispatch 时分离 disabled servers，打 `servers_disabled_excluded_from_dispatch` warning 并给出 `cli server update` 提示 | `task_runner.py:_dispatch_queued_tasks_locked` | `test_task_runner_dispatch.py::TestDisabledServerWarning::test_disabled_server_excludes_from_dispatch` |
| #10（增强） | Post-dispatch slot invariant：每轮 dispatch 后重新查 DB 验证 `active_count <= max_concurrency`，违反时打 `slot_overcommit_invariant_violated` | `task_runner.py` dispatch Phase A/B 后 | — |
| #10a | Segment 存在性检查增强：区分 actionable（PENDING/DISPATCHED/TRANSCRIBING/SUCCEEDED）和全 FAILED 状态，全 FAILED 时打 `segments_all_terminal_failed` warning 而非静默跳过 | `task_runner.py:_create_segments_for_task` | — |
| #12 | Progress 冻结检测：`_detect_frozen_tasks()` 每 60 个 loop tick 运行一次，检查 TRANSCRIBING 超过 `task_timeout_seconds` 的任务/segment，打 `progress_frozen_detected` | `task_runner.py:_detect_frozen_tasks` | `test_task_runner_dispatch.py::TestFrozenTaskDetection::test_frozen_task_detected_without_error` |
| #16 | 启动版本日志：`application_starting` 事件包含 git_sha、python_version、关键配置参数（stale_task_timeout_minutes, task_timeout_seconds, websocket_ping_interval 等） | `main.py:_get_git_short_sha`, `main.py:lifespan` | — |

### 下一步验证计划

1. ~~**本机 25 文件回归**：用修复后的代码重复之前的 25 文件 CLI 批量转写，确认基本功能无退化~~ → ✅ **已完成**（见下方 3.7 部分）
2. **HP Pro Mini 50 文件压力测试**：复现之前的卡死场景，验证 WebSocket 健康检测 + 迟到完成恢复 + slot 超载检测 + 冻结检测是否有效缓解冻结

## 第 3.7 部分：25 文件本机回归测试

### 第一次（00:00，旧服务器进程）

**批次 ID**: `01KR99VVP1M0NV1GQM7606T4RC`

> ⚠️ **注意**：此次测试使用的后端进程（uptime 4h20m）未重启，实际运行的是修复前的旧代码。
> 结果仅证明功能无退化，**不能证明新代码正确性**。

| 指标 | 值 |
|------|----|
| 测试文件 | 5 种原始 × 5 副本 = 25 个 copy 文件 (15 mp4 + 10 wav) |
| 成功率 | **25/25 (100%)** |
| 总耗时 | **48 秒** |
| 服务器分布 | asr-10096: 15 (60%), asr-10095: 7 (28%), asr-10097: 3 (12%) |

### 第二次（00:28，重启后的新代码 — 有效验证）

**批次 ID**: `01KR9BGPA5B5F4CMCF91J5T303`

> ✅ 服务器重启后加载了两轮修复的全部代码，启动日志确认：
> `application_starting git_sha=00c8802 websocket_ping_interval=20 websocket_read_idle_timeout=120 stale_task_timeout_minutes=10`

| 指标 | 值 |
|------|----|
| 测试文件 | 5 种原始 × 5 副本 = 25 个 copy 文件 (15 mp4 + 10 wav) |
| 成功率 | **25/25 (100%)** |
| 总耗时 | **42 秒** |
| 服务器分布 | asr-10096: 15 (60%), asr-10095: 7 (28%), asr-10097: 3 (12%) |

**后端日志诊断 — 新增防护全部通过**：

| 检查项 | 结果 |
|--------|------|
| `slot_overcommit_invariant_violated` | 0 — 无 slot 超派 ✅ |
| `progress_frozen_detected` | 0 — 无任务冻结 ✅ |
| `task_succeeded_but_transition_blocked` | 0 — 无状态阻塞 ✅ |
| `websocket_read_idle_timeout` | 0 — 无 WebSocket 静默超时 ✅ |
| `servers_disabled_excluded_from_dispatch` | 0 — 所有服务器参与 ✅ |
| `released_stale_preprocessing_claims` | 0 — 无预处理 claim 超时 ✅ |
| `task_succeeded_after_status_recovery` | 0 — 无迟到完成恢复 ✅ |

**与修复前对比**：性能略优（42s vs 46s），同时新增了 7 项运行时安全检测均未触发异常。

### 第三次（00:15，Claude Code Agent 独立测试 — 12 个量化课程视频）

**批次 ID**: `01KR9AS4CYBRNS38S8TCM9XSQK`

| 指标 | 值 |
|------|----|
| 测试文件 | 12 个量化课程 .mp4（总时长 2.9 小时） |
| 成功率 | **12/12 (100%)** |
| 总耗时 | **141 秒** |
| 服务器分布 | asr-10096: 6, asr-10095: 5, asr-10097: 1 |
| RTF | ~0.0136 |

> 此次测试还发现了我们第二轮修复引入的 Bug：`main.py` 缺少 `select` 导入（详见 3.8 部分）。

**详细报告**: `4-tests/batch-testing/outputs/cli/regression-25files-newcode-20260511-002846/`

**结论**: 重启后新代码 25 文件回归 + 12 个长视频批量测试均通过 ✅

## 第 3.8 部分：修复过程中引入的新 Bug

### Bug #17：`main.py` 缺少 `select` 导入（第二轮修复引入）

| 属性 | 值 |
|------|----|
| 严重度 | **P0 — 服务器无法启动** |
| 引入时机 | 第二轮修复 Bug #7 时新增 `select(sa_func.count())` 查询 |
| 根本原因 | 顶部 import 仍为 `from sqlalchemy import or_, update`，缺少 `select` |
| 发现方式 | Claude Code Agent 重启服务器时触发 `NameError` |
| 修复 | 改为 `from sqlalchemy import or_, select, update` |
| 修复状态 | ✅ 已修复（工作区文件已更新） |
| 未被我们发现的原因 | 单元测试不走 FastAPI lifespan 启动路径；回归测试未重启服务器 |

**教训**：任何修改启动路径的代码，必须做一次真正的服务器重启验证，不能仅依赖单元测试。

---

## 第四部分：解决方案

### 方案 A：CLI 层修复（Bug #1-#4）

#### A1：`_check()` 空 detail 兜底（Bug #2）

```python
# cli/api_client.py:35-41
def _check(self, resp: httpx.Response) -> httpx.Response:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        if not detail:
            detail = (
                f"服务返回 HTTP {resp.status_code} (无详情，"
                f"请检查 server 配置: python -m cli config get server)"
            )
        raise APIError(resp.status_code, str(detail))
    return resp
```

#### A2：网络异常统一捕获（Bug #3）

在 `ASRClient` 中添加 `_request()` 包裹方法，统一捕获 `httpx.ConnectError` / `httpx.TimeoutException`，转为 `APIError`：

```python
def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return getattr(self._client, method)(url, **kwargs)
    except httpx.ConnectError:
        raise APIError(
            0, f"无法连接到服务器 {self._client.base_url} "
               f"(请检查后端是否启动、地址是否正确: python -m cli config get server)"
        )
    except httpx.TimeoutException:
        raise APIError(0, f"连接超时 {self._client.base_url}")
```

然后将 `upload_file()` 等方法中的 `self._client.post(...)` 替换为 `self._request("post", ...)`。

#### A3：init SKILL.md 补 CLI 验证（Bug #4）

- Phase 4A Step 6 后增加：`python -m cli --verbose health` 验证 CLI 连接地址
- Phase 5 验证报告增加 `CLI 连接: ✅ http://localhost:15797`
- 错误处理表增加 "CLI 连接错误地址" 场景

### 方案 B：转写引擎修复（Bug #5）

#### B1：恢复 stamp_sents 无条件兜底（最小回退）

```python
# funasr_ws.py:_should_complete() 第 168-170 行
# 将：
stamp_sents = data.get("stamp_sents")
if mode == "offline" and stamp_sents and isinstance(stamp_sents, list) and len(stamp_sents) > 0:
    return True
# 改为（恢复 0.4.13 行为）：
stamp_sents = data.get("stamp_sents")
if stamp_sents and isinstance(stamp_sents, list) and len(stamp_sents) > 0:
    return True
```

**理由**：第 163 行 `if mode == "offline": return True` 已经处理了 offline 模式，第 169 行的 `mode == "offline"` 条件使 stamp_sents 兜底永远不触发。恢复无条件兜底可以覆盖 mode 字段不标准的 FunASR 服务端。

#### B2：预处理 claim 超时保护（差异 #6）

在 `_promote_preprocessing_tasks()` 中增加 claim 超时检测：如果 `started_at` 超过 5 分钟仍在 PREPROCESSING，认为 claim 方已死，重置 `started_at=None`。

#### B3：启动恢复改进（差异 #7）

将 10 分钟硬编码改为配置项 `settings.stale_task_timeout_minutes`，并在日志中明确提示被跳过的任务 ID，方便运维排查。

同时引入任务运行 lease / generation 概念：

- 派发任务时写入 `run_id` 或 `lease_token`
- WebSocket 完成事件落库时校验当前任务仍属于同一次运行
- 启动恢复只回收 lease 已过期且没有活跃 worker 的任务
- 旧 worker 的迟到完成事件不能覆盖新一轮任务状态

#### B4：处理 `task_succeeded_but_transition_blocked`（Bug #15）

当前逻辑只打 warning，实际应该进入可恢复分支：

1. 如果转写结果已经保存成功，且任务没有被用户显式取消，则允许从 `QUEUED` / `DISPATCHED` / `TRANSCRIBING` 收敛到 `SUCCEEDED`。
2. 如果任务已进入新一轮运行，则按 `run_id` 判断旧完成事件过期，记录为 `stale_completion_ignored`。
3. 如果任务已被用户取消，则删除临时结果或记录 `completion_after_cancel`，避免结果文件和任务状态不一致。
4. 为该路径增加单元测试：模拟任务完成前被恢复到 `QUEUED`，确认不会丢失已完成结果。

### 方案 C：WebSocket 连接健康修复（Bug #11，HP Pro Mini 铁证）

#### C1：启用 WebSocket ping/pong 心跳

```python
# funasr_ws.py:242 — 将 ping_interval=None 改为合理值
async with connect_websocket(
    uri,
    subprotocols=["binary"],
    ping_interval=20,       # 每 20 秒发送 ping
    ping_timeout=10,        # 10 秒内无 pong 则判定断连
    ssl=ssl_ctx,
    close_timeout=60,
    max_size=1024 * 1024 * 1024,
) as ws:
```

**效果**：静默断连 30 秒内即可检测，而非等 3600 秒超时。

#### C2：缩短 segment 级超时

segment 是 ~10 分钟音频片段，RTF≈0.09，理论转写时间 < 60 秒。当前 `task_timeout_seconds=3600` 对 segment 过长。

方案：在 `_transcribe_with_protocol_fallback` 中，根据音频时长动态计算超时：

```python
timeout = min(
    float(settings.task_timeout_seconds),
    max(audio_duration_sec * 2, 120.0),  # 至少 120 秒，最多 2 倍音频时长
)
```

#### C3：progress 冻结检测与告警（Bug #12）

在 `_run_loop()` 中增加 stall 检测：每 60 秒扫描所有 TRANSCRIBING 任务，如果 progress 在 5 分钟内无变化，标记为 STUCK 并触发重试或告警。

### 方案 D：调度 slot 一致性修复（Bug #13）

#### D1：派发前原子 claim slot

不要只用内存快照判断 server usage。派发前应在数据库事务内完成“检查容量 + 占位”：

```sql
-- 伪代码：同一事务内计算 running_count 并 claim
SELECT running_count(server_id) FOR UPDATE;
IF running_count < max_concurrency:
  UPDATE task SET assigned_server_id=?, status='DISPATCHED', run_id=? WHERE task_id=? AND status='QUEUED';
ELSE:
  skip
```

SQLite 不支持完整行级锁时，可以使用单调递增的 `server_slot_leases` 表或单进程调度锁，确保同一时刻只有一个调度循环执行派发。

#### D2：统一 whole-file 与 segment 的 slot 计数口径

- whole-file 任务、segment 子任务都使用同一套 active task 查询。
- 分段父任务只作为聚合任务，不占 server slot。
- 任务取消、失败、成功、超时都必须释放 slot lease。
- 每次 `no_free_slots_for_scheduling` 输出应同时打印 `task_id` 列表，方便追踪哪个任务占了 slot。

#### D3：增加 slot 超限自检

每轮调度后做 invariant check：

```text
if active_count(server_id) > server.max_concurrency:
    log error slot_overbooked server_id active_count max_concurrency active_task_ids
```

短期可以先记录 error，不自动杀任务；确认路径后再决定是否回收多余任务。

### 方案 E：迁移、schema check 与运行版本校验（Bug #14、风险 #16）

#### E1：schema check 覆盖新增列

启动时检查 `server_instances` 必须包含 `enabled` 列，缺失时不要继续启动 task runner / heartbeat：

```python
required_columns = {
    "server_instances": {"server_id", "status", "enabled", "max_concurrency"},
    "tasks": {"task_id", "status", "started_at", "assigned_server_id"},
}
```

#### E2：缺列时 fail fast 或自动迁移

二选一：

- 保守方案：启动失败，输出明确命令：`python -m app.storage.migrate`
- 自动方案：在 SQLite 环境执行兼容迁移：`ALTER TABLE server_instances ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1`

不建议继续允许 `schema_check_passed` 后运行时报 SQL 错。

#### E3：启动日志输出运行版本

后端启动时记录：

- git commit / build version
- 数据库 schema version
- migration revision
- 配置文件路径

这样后续再看到 `dispatch_skip_segmented_task` 这类本地 checkout 不存在的日志时，可以直接判断是否运行了中间版本。

### 方案 F：防御性增强

#### F1：服务器 enabled 状态检查

`cli server list` 命令输出中增加 `enabled` 列，方便运维快速发现被禁用的服务器。

#### F2：CLI 启动时自动校验连接

`main()` callback 中，当 verbose 模式或首次运行时，自动尝试 `GET /health`，连接失败时输出明确提示。

---

## 第五部分：WBS（工作分解结构）

### 阶段一：CLI 层修复（预计 1 小时）— Bug #1-#4

| 任务 | 文件 | 工作量 | 依赖 |
|------|------|--------|------|
| A1: `_check()` 空 detail 兜底 | `cli/api_client.py` | 10 min | — |
| A2: 网络异常统一捕获 | `cli/api_client.py` | 20 min | A1 |
| A3: init SKILL.md 补 CLI 验证 | `6-skills/.../init/SKILL.md` | 15 min | — |
| 验证：用错误地址测试 CLI 错误信息 | — | 10 min | A1+A2 |

### 阶段二：转写完成判定与状态机修复（预计 2.5 小时）— Bug #5-#8、#15

| 任务 | 文件 | 工作量 | 依赖 |
|------|------|--------|------|
| B1: 恢复 stamp_sents 无条件兜底 | `app/adapters/funasr_ws.py` | 5 min | — |
| B1-验证: 构造 stamp_sents 返回的单元测试 | `tests/` | 30 min | B1 |
| B2: 预处理 claim 超时保护 | `app/services/task_runner.py` | 20 min | — |
| B3: 启动恢复 stale timeout 可配 | `app/main.py` + `app/config.py` | 15 min | — |
| B3-增强: 引入 run_id/lease_token 防止迟到完成事件污染状态 | `app/services/task_runner.py` + model/schema | 45 min | B3 |
| B4: 处理 `task_succeeded_but_transition_blocked` 恢复路径 | `app/services/task_runner.py` | 30 min | B3-增强 |
| B4-验证: QUEUED 状态收到完成事件的单元测试 | `tests/` | 20 min | B4 |

### 阶段三：WebSocket 连接健康修复（预计 2 小时）— Bug #11-#12

| 任务 | 文件 | 工作量 | 依赖 |
|------|------|--------|------|
| C1: 启用 ping/pong 心跳 | `app/adapters/funasr_ws.py` | 10 min | — |
| C2: segment 级动态超时 | `app/services/task_runner.py` | 30 min | — |
| C3: progress 冻结检测 | `app/services/task_runner.py` | 45 min | — |
| C-验证: 模拟静默断连测试 | — | 30 min | C1+C2 |

### 阶段四：slot 原子派发与超限自检（预计 2 小时）— Bug #13

| 任务 | 文件 | 工作量 | 依赖 |
|------|------|--------|------|
| D1: 派发前原子 claim slot / 单调度锁 | `app/services/scheduler.py` + `app/services/task_runner.py` | 45 min | — |
| D2: 统一 whole-file 与 segment slot 计数 | `app/services/task_runner.py` | 35 min | D1 |
| D3: 增加 `slot_overbooked` invariant 日志 | `app/services/scheduler.py` | 20 min | D2 |
| D-验证: 构造并发调度 tick 测试，确认不超过 max_concurrency | `tests/` | 20 min | D1+D2 |

### 阶段五：schema / 迁移 / 版本一致性修复（预计 1 小时）— Bug #14、风险 #16

| 任务 | 文件 | 工作量 | 依赖 |
|------|------|--------|------|
| E1: schema check 增加 `server_instances.enabled` 等必需列 | `app/main.py` 或 schema check 模块 | 20 min | — |
| E2: 缺列 fail fast 或自动迁移 | migration 脚本 / storage 初始化 | 25 min | E1 |
| E3: 启动日志输出 commit/schema/migration/config 路径 | `app/main.py` | 15 min | — |

### 阶段六：防御性增强（预计 30 分钟，可选）

| 任务 | 文件 | 工作量 | 依赖 |
|------|------|--------|------|
| F1: server list 增加 enabled 列 | `cli/commands/server.py` | 10 min | — |
| F2: CLI 启动连接校验 | `cli/main.py` | 15 min | A2 |

### 阶段七：集成验证（预计 2 小时）

| 任务 | 描述 | 依赖 |
|------|------|------|
| 25 文件批量转写回归测试 | 复现 CLI 测试场景，确认无阻断 | 阶段一+二 |
| 50 文件长音频压力测试 | 复现 HP Pro Mini 卡死场景 | 阶段二+三+四 |
| 错误地址 CLI 测试 | 验证错误信息可读性 | A1+A2 |
| stamp_sents 场景测试 | 构造 mode 非标准的 FunASR 响应 | B1 |
| slot 超派回归测试 | 验证 `active_count <= max_concurrency` invariant | 阶段四 |
| schema 缺列启动测试 | 用缺少 `enabled` 的测试库验证 fail fast / 自动迁移 | 阶段五 |
| 版本一致性检查 | 启动日志能定位 commit 与 schema version | 阶段五 |

**总预计工时**：约 11 小时（阶段一 1h + 阶段二 2.5h + 阶段三 2h + 阶段四 2h + 阶段五 1h + 阶段六 0.5h + 阶段七 2h）

**建议执行顺序**：

```
阶段三 C1（ping/pong）→ 阶段四 D1（slot 原子 claim）→ 阶段二 B1（stamp_sents）
  ↓
阶段二 B3/B4（状态恢复与完成落库）→ 阶段三 C2/C3（动态超时 + 冻结检测）
  ↓
阶段五（schema/版本校验）→ 阶段一（CLI 错误信息与文档）→ 阶段七（集成验证）
```

**优先级说明**：最高优先级不应只放在 WebSocket 心跳。HP Pro Mini 记录显示卡住是组合问题，必须优先处理 `WebSocket 无读超时`、`slot 超派`、`完成状态被 QUEUED 拦截` 三条主线；schema/版本校验用于避免修复后继续被迁移或部署不一致问题误导。

---

## 第六部分：测试覆盖缺口分析

> 项目 `4-tests/scripts/` 下共 66 个测试文件（unit 35 + integration 11 + e2e 6 + analysis 2 + eval 1 + load 1），但本次暴露的 16 个问题中，**无一被现有测试拦截**。原因不是测试数量不够，而是测试金字塔缺少关键类型。

### 6.1 现有测试的覆盖盲区

下表逐一分析每个 Bug 为什么未被现有测试发现：

| Bug | 未发现原因 | 现有测试的局限 | 证据 |
|-----|-----------|---------------|------|
| **#1 CLI 错端口** | E2E 每次显式传 `--server` | `_base_args()` 硬编码 `--server E2E_SERVER`，绕过 `~/.asr-cli.yaml` | `test_cli_batch_transcribe.py:27` |
| **#2-#3 CLI 空错误/网络异常** | 单元测试 mock 了 httpx，不触发真实网络异常 | `test_cli_api_client.py` 测的是已知 status code 场景 | — |
| **#5 stamp_sents 回归** | 测试用例的 `mode="offline"` 会被第 163 行提前判完成 | `test_protocol_adapter.py:61-62` 的 case 实际没走到 stamp_sents 兜底分支 | `_should_complete()` 第 163 行 `if mode == "offline": return True` |
| **#11 WebSocket 静默卡住** | 无故障注入测试 | 所有 WS 测试都是 happy path 或 mock probe | 搜索全部测试：零个模拟 "WS 不回消息也不 close" |
| **#13 Slot 超派** | 测试验证首轮调度计划结果 | `test_scheduler.py` 测的是算法输出，不是运行时不变量 | `test_scheduler.py:504` 只断言计划中的分配数 |
| **#14 Schema 缺列** | 测试 fixture 用全新内存库 | `conftest.py:52` `Base.metadata.create_all` 天然包含所有 ORM 字段 | `test_migration_compat.py` 只覆盖 `callback_outbox`，不覆盖 `server_instances.enabled` |
| **#15 状态回退致结果丢失** | 故障测试只验证状态机能走通 | `test_fault_tolerance_e2e.py:118-143` 只手动推状态，没模拟重启+迟到完成事件的时序 | 无重启后旧 worker 结果回来的测试 |
| **压力/长时间** | E2E 只用 3 个小 fixture 音频 | `test_cli_batch_transcribe.py:31` 默认 3 文件 | 与生产 50 文件 × 3 服务器 × 40 分钟不在一个量级 |

### 6.2 核心结论：测试像"功能验收"，缺少"生产故障模型"

现有测试覆盖的维度：
- ✅ API 接口能不能跑通（integration）
- ✅ 纯函数逻辑对不对（unit）
- ✅ 基本工作流（E2E happy path）

**缺失的关键测试类型**：

| 测试类型 | 描述 | 对应 Bug |
|---------|------|---------|
| **配置真实路径测试** | 不传 `--server`，在临时 HOME 下放 `.asr-cli.yaml`，测 CLI 是否正确解析 | #1 |
| **旧数据库升级测试** | 用缺少 `enabled` 列的旧 schema 启动当前代码，验证 fail fast 或自动迁移 | #14 |
| **完成判定边界测试** | `mode` 非标准（如 `"2pass"`）但有 `stamp_sents` 的响应 | #5 |
| **WebSocket 故障注入测试** | 服务端上传后既不 close、也不发消息、也不 pong | #11 |
| **调度不变量测试** | 多轮 dispatch + segment + work stealing 后断言 `active(s) <= max_concurrency` | #13 |
| **重启/迟到完成时序测试** | 模拟：dispatch → 转写中 → 后端重启/recovery → 旧完成事件回来 | #15 |
| **批量 Soak Test** | 25-50 文件、多服务器、长音频、固定超时判据 | 全部 |

### 6.3 测试改进方案

#### 原则：精简 + 强化，测正确的东西

不追求增加测试数量，而是**补齐缺失的故障模型维度**。每个新增测试都要能拦截本次报告中至少 1 个真实 Bug。

#### 新增测试清单（按优先级）

**P0：修复验证测试**（随 Bug 修复一起写，确保不回归）

| 测试 | 类型 | 验证什么 | 对应 Bug | 预计工时 |
|------|------|---------|---------|---------|
| `test_ws_silent_disconnect` | unit | mock WS 连接后不发任何消息，验证 ping/pong 超时触发 | #11 | 20 min |
| `test_stamp_sents_non_offline_mode` | unit | `mode="2pass"` + 非空 `stamp_sents` → `_should_complete()` 返回 True | #5 | 10 min |
| `test_slot_invariant_multi_round` | unit | 10 轮 dispatch 循环，每轮断言 `active(s) <= max_concurrency` | #13 | 30 min |
| `test_transition_blocked_recovery` | unit | QUEUED 状态收到完成事件 → 强制恢复到 SUCCEEDED，结果不丢 | #15 | 20 min |
| `test_schema_check_missing_column` | unit | 用缺列的旧 schema 调用 schema check → 不能返回 passed | #14 | 15 min |

**P1：配置和迁移测试**

| 测试 | 类型 | 验证什么 | 对应 Bug | 预计工时 |
|------|------|---------|---------|---------|
| `test_cli_reads_config_file` | unit | 临时 HOME + `.asr-cli.yaml` 写入错误地址 → CLI 连接报错含地址信息 | #1-#3 | 20 min |
| `test_old_db_upgrade_enabled` | integration | 缺 `enabled` 列的 DB → 启动后自动迁移或 fail fast | #14 | 20 min |

**P2：Soak / 压力测试**（CI 外运行，结果归档）

| 测试 | 类型 | 验证什么 | 预计工时 |
|------|------|---------|---------|
| `test_batch_50_files_3_servers` | soak | 50 文件 × 3 服务器，超时 15 分钟，`succeeded >= 45` | 1h（含环境） |
| `test_restart_during_transcription` | chaos | 转写中杀后端 → 重启 → 最终所有任务 succeeded 或 failed（不卡死） | 30 min |

#### 现有测试精简建议

| 动作 | 说明 |
|------|------|
| **合并** `test_bugfix_round2/3/4` | 3 个文件拆分没有意义，按功能域合入对应的 unit test |
| **去重** `test_scheduler.py` 的相似 case | 504 行中有多个只改参数的测试，可用 `@pytest.mark.parametrize` 压缩 |
| **标注** E2E 对远程服务器的依赖 | 加 `@pytest.mark.requires_remote_asr`，CI 中默认 skip |
| **添加** fault injection marker | 新增的故障注入测试统一用 `@pytest.mark.fault_injection` 标记 |

#### 测试分层调整

```
测试金字塔（调整后）

    ╱ Soak / Chaos ╲         ← 新增：批量压力 + 重启混沌测试（CI 外手动）
   ╱   E2E (6 个)    ╲       ← 现有：补配置路径测试
  ╱ Integration (11 个) ╲    ← 现有：补旧 DB 升级测试
 ╱  Unit (35 → 40 个)    ╲   ← 新增 5 个故障模型测试 + 合并 3 个 bugfix 文件
╱   Fixture / Mock base    ╲ ← 现有：conftest.py 不变
```

**总新增工时**：约 3.5 小时（P0: 1.5h + P1: 40min + P2: 1.5h）

**关键原则**：每个新测试都必须能直接拦截本次报告中的某个真实 Bug。不写"可能有用"的测试，只写"如果有这个测试，今天这个 Bug 就不会漏到生产"的测试。
