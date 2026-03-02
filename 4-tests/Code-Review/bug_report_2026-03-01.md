# FunASR Task Manager — Bug 检查报告

> 检查时间：2026-03-01  
> 检查范围：`3-dev/src/backend/app/**`（全量核心源码）

---

## 🔴 严重 Bug（会导致功能异常）

### BUG-1：`task_runner.py` — 重试时双重状态转换顺序有误

**文件**：`3-dev/src/backend/app/services/task_runner.py`  
**行号**：79–90  
**问题代码**：

```python
for task in tasks:
    if task.can_transition_to(TaskStatus.PENDING):
        task.retry_count += 1
        task.assigned_server_id = None
        task.error_code = None
        task.error_message = None
        task.started_at = None
        task.completed_at = None
        await repo.update_task_status(task, TaskStatus.PENDING)      # ① FAILED → PENDING
        await repo.update_task_status(task, TaskStatus.PREPROCESSING) # ② PENDING → PREPROCESSING
        logger.info(...)
await session.commit()
```

**问题分析**：

1. **状态转换规则**（见 `task.py`）：
   - `FAILED → PENDING`：✅ 合法
   - `PENDING → PREPROCESSING`：✅ 合法

2. **逻辑缺陷**：`can_transition_to(TaskStatus.PENDING)` 只在 `FAILED` 状态为 `True`，但**在调用 `update_task_status(task, TaskStatus.PENDING)` 之后**，任务状态已经改变为 `PENDING`，**紧接着直接再调用 `update_task_status(task, TaskStatus.PREPROCESSING)` 没有再次检查** `can_transition_to(TaskStatus.PREPROCESSING)`。

3. **更深层问题**：如果 `update_task_status` 内部调用 `task.transition_to()` 且转换是合法的，则功能上可以跑通。但这里**两次 update 在同一个事务未提交的 session 里**，且 `TaskEvent` 事件记录会写两条，造成状态历史数据冗余/混乱。更干净的做法是直接从 `FAILED → PREPROCESSING`（如果转换规则允许），或仅写一次目标状态。

**影响**：任务历史事件表中每次重试都会产生 2 条事件记录（`FAILED→PENDING`，`PENDING→PREPROCESSING`），数据量倍增，且逻辑上中间 `PENDING` 状态对客户端完全不可见。

---

### BUG-2：`scheduler.py` — `calibrate_after_completion` 中 `deviation` 逻辑矛盾

**文件**：`3-dev/src/backend/app/services/scheduler.py`  
**行号**：262–290  
**问题代码**：

```python
if abs(deviation - 1.0) > CALIBRATION_THRESHOLD:    # 偏差超过 0.3（包含过快和过慢）
    # 增加 penalty
    ...
elif deviation < (1.0 - CALIBRATION_THRESHOLD):     # deviation < 0.7（只有过快）
    # 减少 penalty
    ...
```

**问题**：逻辑分支有 **覆盖重叠**：

- 当 `deviation < 0.7` 时，`abs(deviation - 1.0) > 0.3` **同样为 True**（因为 `|0.7 - 1.0| = 0.3`，而 `< 0.7` 意味着偏差 `> 0.3`）
- 因此，**任务完成过快时，`if` 分支先命中，执行增加 penalty（错误！）**，`elif` 分支永远不会执行

**正确逻辑应为**：

```python
if deviation > (1.0 + CALIBRATION_THRESHOLD):       # 实际比预测慢：增加 penalty
    ...
elif deviation < (1.0 - CALIBRATION_THRESHOLD):     # 实际比预测快：减少 penalty
    ...
```

**影响**：调度器 ETA 校准完全失效，过快完成的任务反而会增加 penalty，导致 RTF 持续向错误方向漂移。

---

## 🟡 中等问题（逻辑缺陷 / 潜在崩溃）

### ISSUE-3：`task_runner.py` — `_execute_task` 中双查库 TOCTOU 冗余

**文件**：`3-dev/src/backend/app/services/task_runner.py`  
**行号**：176–215  
**问题代码**：

```python
async def _execute_task(self, task_id: str) -> None:
    ...
    dispatch_info = await self._load_dispatch_info(task_id)
    task, server, file_record = dispatch_info           # ← session 1 已关闭

    if not task.can_transition_to(TaskStatus.TRANSCRIBING):  # ← 使用 session 1 的对象
        return

    async with async_session_factory() as session:      # session 2
        db_task = await repo.get_task(task_id)
        if db_task.can_transition_to(TaskStatus.TRANSCRIBING):
            ...
            await repo.update_task_status(db_task, TaskStatus.TRANSCRIBING)
            await session.commit()

    # ↓ 使用的是 file_record（来自 session 1，因 lazy="selectin" 已预加载，不会报错）
    audio_path = file_record.storage_path

    profile = self._build_message_profile(task, audio_path)
    # _build_message_profile 访问 task.file（来自 session 1）
```

**说明**：由于 `Task.file` 设置了 `lazy="selectin"`，ORM 会在 session 内提前加载关联，所以 session 关闭后访问 `task.file` 不会抛 `DetachedInstanceError`。这里**不是错误**，但存在两个疑虑点：

- `_load_dispatch_info` 与 `_execute_task` 内再次 `get_task` 造成**双查库重复查询**，浪费资源
- `line 183`：`if not task.can_transition_to(TaskStatus.TRANSCRIBING)` 检查的是 session 1 快照（`DISPATCHED`），而 `line 191` 再次检查 `db_task.can_transition_to(TaskStatus.TRANSCRIBING)`，两次检查的是不同 session 的对象，虽然功能正确，但存在**TOCTOU（Time-Of-Check-Time-Of-Use）逻辑冗余**。

---

### ISSUE-4：`probe.py`（新版）与 `server_probe.py`（旧版）并存，命名冲突

**文件**：

- `3-dev/src/backend/app/services/probe.py` — 定义了新版 `ProbeLevel`（`StrEnum`）和 `ServerCapabilities`（有 `server_id` 字段）
- `3-dev/src/backend/app/services/server_probe.py` — 定义了旧版 `ProbeLevel`（`Enum`）和 `ServerCapabilities`（无 `server_id`）

**问题**：两个文件定义**同名但不兼容的类**（`ProbeLevel`、`ServerCapabilities`），`api/servers.py` 和 `services/heartbeat.py` 都导入自 `server_probe.py`，而 `probe.py` 里的新版实现似乎是在重构过程中遗留的，**但没有完成切换**。

这两套实现的 `ProbeLevel.FULL_2PASS` vs `ProbeLevel.TWOPASS_FULL` 也不一致，极易在未来扩展时引发导入混淆。

**建议**：删除旧版 `probe.py` 或将 `server_probe.py` 替换为 `probe.py`，统一实现。

---

### ISSUE-5：`result_formatter.py` — `parse_timestamp_segments` 中字符索引逻辑有误

**文件**：`3-dev/src/backend/app/services/result_formatter.py`  
**行号**：37–44  
**问题代码**：

```python
for i, ts_pair in enumerate(timestamp):
    if isinstance(ts_pair, list) and len(ts_pair) >= 2:
        char_start = sum(len(str(t)) for t in text[:i]) if i < len(text) else 0  # ← 无用变量
        segments.append(TimestampSegment(
            start_ms=int(ts_pair[0]),
            end_ms=int(ts_pair[1]),
            text=str(text[i]) if i < len(text) else "",
        ))
```

**问题**：

1. `char_start` 被计算出来但**从未使用**（dead code）
2. `text[:i]` 对每个字符做字符串化求长度，逻辑上意义不明（`text` 是字符串，`text[:i]` 是子串，`len(str(t))` 对每个字符都是 1），实际上 `char_start` 始终等于 `i`，这段代码是一个 O(n²) 的无用计算

**建议**：直接删除 `char_start` 那行。

---

### ISSUE-6：`delete_all_tasks` API — `skipped_active` 统计逻辑不准确

**文件**：`3-dev/src/backend/app/api/tasks.py`  
**行号**：101–108  
**问题代码**：

```python
skip_stmt = select(func.count()).select_from(Task).where(
    Task.user_id == user_id,
    Task.status.in_([s.value for s in _ACTIVE_STATUSES]),
)
skipped = (await db.execute(skip_stmt)).scalar() or 0

if total == 0:
    return {"deleted": 0, "skipped_active": skipped}
```

**问题**：当 `status` 参数不为 `None` 时（用户指定了要删除某个状态），`skipped` 统计的是**所有活跃任务总数**，而不是"因为是活跃状态而被跳过的任务数"（在这种情况下 skipped 应该是 0，因为 status 不是活跃状态）。这会在 API 响应中返回误导性数值。

---

### ISSUE-7：`audio_preprocessor.py` — 临时文件创建在源文件目录，非 `temp_dir`

**文件**：`3-dev/src/backend/app/services/audio_preprocessor.py`  
**行号**：92–94  
**问题代码**：

```python
settings.temp_dir.mkdir(parents=True, exist_ok=True)
fd, tmp_path = tempfile.mkstemp(suffix=".wav", dir=str(out_path.parent))  # ← 临时文件在源文件父目录
```

**问题**：代码创建了 `settings.temp_dir`，但实际临时文件写到的是 `out_path.parent`（即源音频文件所在目录），两者可能不同。如果 `settings.temp_dir.mkdir(...)` 是为了存放临时文件，则实际写入位置与预期不符。

**建议**：将 `dir=str(out_path.parent)` 改为 `dir=str(settings.temp_dir)` 或者删除多余的 `mkdir` 调用。

---

## 🟢 低优先级建议

### TIP-1：`scheduler.py` — `_consecutive_fast` 字典访问方式不一致

- `tracker._consecutive_fast.get(server_id, 0)` 与 `tracker._consecutive_fast[server_id] = 0` 混用
- 而 `_consecutive_fast` 是 `defaultdict(int)`，直接用 `tracker._consecutive_fast[server_id]` 即可，无需 `.get(..., 0)`

### TIP-2：`heartbeat.py` — `DEGRADED` 状态下 `last_heartbeat` 无更新

当服务器短暂失联进入 `DEGRADED` 状态时，`update_status_fn(server_id, ServerStatus.DEGRADED, None)` 传入 `None` 不更新心跳时间戳，导致 timeout 判断的基准时间永远是上次成功探活的时间，不会随时间流逝而更新，这实际上是合理的设计，但需确认是否合乎预期。

### TIP-3：`main.py` — CORS 全部允许，生产环境存在安全风险

```python
allow_origins=["*"],
allow_credentials=True,
```

同时设置 `allow_origins=["*"]` 和 `allow_credentials=True` 在大多数浏览器中会被拒绝。生产部署时需要将 `allow_origins` 设为具体域名列表。

---

## 汇总表

| 编号 | 严重程度 | 文件 | 问题描述 |
|------|----------|------|----------|
| BUG-1 | 🔴 严重 | `task_runner.py:79-90` | 重试逻辑双写状态事件，中间状态对客户端不可见 |
| BUG-2 | 🔴 严重 | `scheduler.py:262-290` | `deviation` 分支逻辑覆盖重叠，penalty 校准反向 |
| ISSUE-3 | 🟡 中等 | `task_runner.py:176-215` | 双查库 TOCTOU 冗余，逻辑可靠但浪费资源 |
| ISSUE-4 | 🟡 中等 | `probe.py` vs `server_probe.py` | 两套同名不兼容类并存，重构未完成 |
| ISSUE-5 | 🟡 中等 | `result_formatter.py:37-44` | `char_start` 死代码 + O(n²) 无意义计算 |
| ISSUE-6 | 🟡 中等 | `tasks.py:101-108` | `skipped_active` 统计数值在有 status 过滤时不准确 |
| ISSUE-7 | 🟡 中等 | `audio_preprocessor.py:92-94` | 临时文件写到源文件目录，而非配置的 `temp_dir` |
| TIP-1 | 🟢 低 | `scheduler.py` | `defaultdict` 的访问方式不一致 |
| TIP-2 | 🟢 低 | `heartbeat.py` | `DEGRADED` 状态不更新心跳时间戳（设计待确认） |
| TIP-3 | 🟢 低 | `main.py` | CORS 全通配 + credentials=True 生产环境安全风险 |
