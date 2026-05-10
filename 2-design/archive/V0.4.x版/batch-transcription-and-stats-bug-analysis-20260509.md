# 批量转写与 Stats 端点 Bug 分析报告

日期：2026-05-09（含日志取证更新）

## 摘要

本报告记录 2026-05-09 批量转写过程中暴露的三个问题：

1. 批量转写任务出现失败、重试循环和任务卡住。
2. `/api/v1/stats` 在没有 24 小时完成任务时，`success_rate` 为 `None`，调用 `round()` 抛错，影响 benchmark 前置检查。
3. 调度器 `running_count` 将分段任务（segments）与其父任务（task）双重计入 slot 使用量，导致调度器误判服务器已满，剩余任务无法调度。

第一个问题的主要根因是后端在批处理期间发生持续重启，导致任务运行器重复处理同一任务、重复分段、清理临时分段目录，最终造成分段文件丢失。第二个问题是空数据场景没有被正确建模，API 返回模型与计算逻辑不一致。第三个问题是调度器 slot 计算逻辑缺陷，segments 和 tasks 被当作独立占位单元累加，膨胀了服务器占用数。Bug 1 和 Bug 3 共同作用导致了 10 个任务"既排不上队又跑不完"的现象。

## Bug 1：批量转写失败与重试循环

### 现象

CPU-only 批量转写 46 个文件时，最终观察到：

- 成功：35 / 46
- 失败：1
- 仍处理中：10
- 失败文件：`230429下午4楼、2楼上师开示.wav`
- 失败原因显示为：`Segment 2 音频片段读取失败`
- 10 个任务处于 `DISPATCHED` / `TRANSCRIBING` 状态反复重试或卡住

同时，原本预期的 CPU-only 实验并未真正成立。`asr-server-10097` 被手动设为 `OFFLINE` 后，又被 heartbeat/server probe 自动恢复为 `ONLINE`，后续仍参与调度。

### 关键证据（日志取证数据）

日志来源：

- `1-discussion/Agent实际运行记录/backend-20260509.log`（75,424 行）
- `1-discussion/Agent实际运行记录/batch-cpu-only-46files-20260509.log`

#### 证据 1：App 持续重启循环

| 指标 | 数值 |
|------|------|
| `application_shutting_down` 总次数 | **997 次** |
| `reset_stale_tasks` 总次数 | **988 次** |
| `task_runner_started` 总次数 | **998 次** |
| 重启周期 | 每 **~7-8 秒** |
| 持续时间 | 09:14 UTC ~ 11:19 UTC（约 **2 小时 5 分钟**） |

在 09:14 UTC 之前（无批量任务），App 运行稳定（仅 1 次正常重启于 07:09 UTC）。
从 09:14 UTC（批量提交前后）开始进入快速重启循环，直到日志结束。

#### 证据 2：`reset_stale_tasks` 每次重启都回退 11-18 个任务

```
09:17:00 [warning] reset_stale_tasks count=15 hint='Tasks in DISPATCHED/TRANSCRIBING reset to QUEUED after restart'
09:17:09 [warning] reset_stale_tasks count=14
09:17:18 [warning] reset_stale_tasks count=15
09:17:26 [warning] reset_stale_tasks count=16
09:17:35 [warning] reset_stale_tasks count=17
...（每 ~8 秒一次，共 988 次）
```

这条 SQL 绕过了 ORM 状态机，直接修改数据库，**不触发 `task_status_changed` 日志**：

```python
# main.py:101-106 — 每次 App 启动时执行
update(Task)
    .where(Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]))
    .values(status=TaskStatus.QUEUED, assigned_server_id=None, started_at=None)
```

#### 证据 3：单个任务被调度 1000+ 次

对 46 个任务（task_id 前缀 `01KR609Y` / `01KR609Z`）的 `QUEUED→DISPATCHED` 转换统计：

| 任务 ID（后缀） | DISPATCHED 次数 | 说明 |
|-----------------|:---------:|------|
| `01KR609Z1B0CT90GJQAZJKCEGE` | **1024** | 最多 |
| `01KR609Z19AW3CVAZNB9MDK29H` | **1020** | |
| `01KR609Z169YBY4S1C55PCCN11` | **1016** | |
| `01KR609YY3JZVXF77E2XQ99CNN` | **992** | |
| ...（另 6 个任务） | **917~958** | |
| 总 DISPATCHED 次数 | **11,817** | 46 个任务合计 |

每次 DISPATCHED 都会真正发送音频给 FunASR 服务器转写，浪费了大量 CPU 算力。

#### 证据 4：转写成功但状态永远无法固化

以 `01KR609Z1B0CT90GJQAZJKCEGE` 为例，`task_transcription_succeeded` 被触发了 **30+ 次**：

```
09:17:48 task_transcription_succeeded  ← 第 1 次转写成功
09:28:24 task_transcription_succeeded  ← 第 2 次
09:29:40 task_transcription_succeeded  ← 第 3 次
...（每完成一次就被重启回退，再次派发）
10:05:30 task_transcription_succeeded  ← 第 N 次
```

但日志中该任务 **0 次** `to_status=SUCCEEDED`，**0 次** `to_status=FAILED`。
原因：`_mark_task_succeeded` 读取任务时，状态已被 `reset_stale_tasks` 回退为 QUEUED，
而 `QUEUED → SUCCEEDED` 不在合法转换表中，所以 `can_transition_to(SUCCEEDED)` 永远返回 False。

合法转换表（`models/task.py`）：

```
QUEUED      → {DISPATCHED, CANCELED, FAILED}
DISPATCHED  → {TRANSCRIBING, FAILED, CANCELED}
TRANSCRIBING → {SUCCEEDED, FAILED, CANCELED}  ← 只有从 TRANSCRIBING 才能到 SUCCEEDED
SUCCEEDED   → {}  (终态)
```

#### 证据 5：分段并发冲突导致 1 个任务永久失败

`01KR609YZZWZ0PRGKCXVWRA4P1`（`230429下午4楼、2楼上师开示.wav`）完整生命周期：

```
09:15:13 PENDING → PREPROCESSING
09:16:30 segment_split_ok idx=0  ← 第一次分段（每个 seg 出现两次！）
09:16:30 segment_split_ok idx=0  ← 第二次分段（并发执行）
...（idx 0~6 都被重复生成两次）
09:16:32 segments_created count=7
09:16:32 PREPROCESSING → QUEUED
09:16:32 [warning] segmentation_failed_fallback_to_whole_file
         error="UNIQUE constraint failed: task_segments.task_id, task_segments.segment_index"
09:16:53 QUEUED → DISPATCHED
09:16:54 DISPATCHED → TRANSCRIBING
09:16:54 [error] audio_file_read_error
         error="[Errno 2] No such file or directory: ...seg002.wav"
09:16:54 segment_retry_queued retry=1
09:17:10 [error] audio_file_read_error error="No such file or directory: ...seg001.wav"
...（重试 3 次后）
09:17:29 TRANSCRIBING → FAILED
```

链条：并发分段 → UNIQUE 约束冲突 → 失败方清理了 temp 目录 → 成功方的 seg 文件消失 → 读文件 404 → 最终 FAILED。

#### 证据 6：`_recover_orphaned_segments` 放大伤害

每次重启还调用 `_recover_orphaned_segments`：

```
09:17:00 recovered_orphaned_segments count=16
09:17:09 recovered_orphaned_segments count=20
```

将正在转写的分段重置为 PENDING，导致已派发的分段任务也被重复处理。

### 根因分析

该问题是 **4 个缺陷叠加**造成的，不是单点故障。以下按因果链排列：

#### 缺陷 1（触发源）：`start.sh` 默认 `--reload` 导致 App 持续重启

`3-dev/src/start.sh` 默认给 uvicorn 添加 `--reload`，除非设置 `ASR_NO_RELOAD=1`。
批量处理期间，文件写入（上传、分段、结果保存）触发 reload，导致应用每 ~7 秒重启一次。

#### 缺陷 2（致命回退）：`reset_stale_tasks` 无条件回退所有进行中任务

`main.py:101-106` 的 lifespan 启动逻辑：

```python
update(Task)
    .where(Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]))
    .values(status=TaskStatus.QUEUED, assigned_server_id=None, started_at=None)
```

这是为**单次崩溃恢复**设计的逻辑，在单次重启场景下合理。但在每 ~8 秒重启的场景下，它变成了一把钝刀：

- 用直接 SQL UPDATE 绕过 ORM 状态机，**不记录状态变更日志**
- 无条件回退所有正在执行的任务，不区分"真正孤立"还是"仍在转写"
- 使得 `_mark_task_succeeded` 读到的状态永远不是 TRANSCRIBING，转换永远失败

#### 缺陷 3（并发破坏）：预处理任务缺少原子抢占

`_promote_preprocessing_tasks` 扫描 `PREPROCESSING` 任务并执行分段，但没有先用原子更新 claim 住任务。重启前后两个 runner 实例可能同时处理同一任务：

- 同一 `task_id` 下重复生成 `seg000` ~ `segN` 文件（日志显示每个 seg 的 `segment_split_ok` 出现两次）
- 多个执行流同时尝试插入相同 `task_id + segment_index` → `UNIQUE constraint failed`
- 失败方执行异常清理（`shutil.rmtree`），删掉了成功方已创建的分段文件
- 成功方的后续转写读取已删除的文件 → `No such file or directory`

#### 缺陷 4（实验污染）：手动 OFFLINE 被 heartbeat 覆盖

用户手动将 `asr-server-10097` 设为 `OFFLINE` 后，heartbeat 根据探活可达性又自动置为 `ONLINE`。因此 CPU-only 实验没有隔离 GPU 服务器，调度统计和结论被污染。

### 解决方案

#### 第一层：立即运维规避（无需改代码）

**规避 1：批量转写和 benchmark 期间必须关闭 reload**

```bash
ASR_NO_RELOAD=1 ASR_BIND_HOST=127.0.0.1 bash 3-dev/src/start.sh
```

这是目前**最紧急、最有效**的措施。关闭 reload 后，App 不会因文件写入而重启，`reset_stale_tasks` 只在启动时执行一次，整个调度循环恢复正常。

**规避 2：避免用"改状态为 OFFLINE"做 CPU-only 实验**

临时排除服务器时，应直接停止对应的 FunASR Docker 容器，或使用后续新增的维护状态字段。

#### 第二层：代码修复（按因果链优先级排列）

**修复 1（P0 — 针对缺陷 2）：`reset_stale_tasks` 增加 stale 时长保护**

当前的无条件回退在频繁重启时是致命的。建议改为只回退**真正孤立**的任务：

```python
# main.py lifespan — 修复后
stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
result = await session.execute(
    update(Task)
    .where(
        Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
        Task.started_at < stale_cutoff,
    )
    .values(status=TaskStatus.QUEUED, assigned_server_id=None, started_at=None)
)
```

或引入 `generation_id`（每次启动生成唯一 ID），只回退上一代进程派发的任务。对 segment 级别任务也应做一致的租约恢复。

**修复 2（P0 — 针对缺陷 3）：预处理任务增加原子 claim**

`_promote_preprocessing_tasks` 需要在分段前用原子更新抢占任务：

```python
# 方案 A：条件更新（推荐，不新增状态）
result = await session.execute(
    update(Task)
    .where(Task.task_id == task_id, Task.status == TaskStatus.PREPROCESSING)
    .values(assigned_server_id=f"preprocessor-{os.getpid()}")
)
await session.commit()
if result.rowcount != 1:
    continue  # 已被其他 runner 抢走
```

或新增 `PREPROCESSING_LOCKED` 子状态作为抢占标志。

**修复 3（P0 — 针对缺陷 3）：分段异常不删除已发布目录**

当前代码在分段失败时无条件删除整个目录（`shutil.rmtree`），会破坏另一个并发执行流已创建的文件。改为 worker 临时目录 + 原子发布：

```python
# 修复后：先写入临时目录
tmp_dir = output_dir_path.parent / f"{task_id}.tmp-{os.getpid()}"
# ... 分段写入 tmp_dir ...
# 全部成功后原子 rename
os.rename(tmp_dir, output_dir_path)
# 异常时只清理 tmp_dir，不动正式目录
```

或至少在清理前检查 DB 是否已有引用：

```python
existing = await seg_repo.list_segments_by_task(task_id)
if not existing and output_dir_path.exists():
    shutil.rmtree(output_dir_path)
```

**修复 4（P1）：分段创建幂等化**

对 `UNIQUE constraint failed` 不应 fallback 到整文件转写，而应检测并复用已有记录：

```python
try:
    await seg_repo.create_segments(segments)
except IntegrityError:
    await session.rollback()
    existing = await seg_repo.list_segments_by_task(task_id)
    if existing:
        logger.info("segments_already_exist", task_id=task_id)
        return  # 复用已有分段
    raise
```

**修复 5（P1）：`_mark_task_succeeded` 状态转换失败时输出警告**

当前 `can_transition_to(SUCCEEDED)` 失败时静默跳过，且仍然记录 `task_transcription_succeeded`，严重误导排查：

```python
# 修复后
if task.can_transition_to(TaskStatus.SUCCEEDED):
    event = await repo.update_task_status(task, TaskStatus.SUCCEEDED)
    logger.info("task_transcription_succeeded", task_id=task_id)
else:
    logger.warning(
        "task_succeeded_but_transition_blocked",
        task_id=task_id,
        current_status=task.status,
        hint="Status was likely reset by a concurrent restart",
    )
```

**修复 6（P1 — 针对缺陷 4）：增加服务器维护/禁用语义**

在 `ServerInstance` 模型增加 `enabled` 字段：

```python
enabled: Mapped[bool] = mapped_column(Boolean, default=True)
```

- 调度器只选择 `enabled=True AND status=ONLINE` 的服务器
- heartbeat 只更新 `status`（可达性），不覆盖 `enabled`（人为意图）
- API 提供 `PUT /api/v1/servers/{id}/enabled` 端点

### 验证方案

1. **启动稳定性验证**：
   - 使用 `ASR_NO_RELOAD=1` 启动
   - 跑 10 分钟批量任务，确认 `application_shutting_down` 不重复出现
   - 确认 `reset_stale_tasks` 只在首次启动时出现 1 次

2. **并发预处理验证**：
   - 构造同一批长音频任务
   - 人为启动多个 runner 或模拟重启
   - 验证同一 `task_id + segment_index` 不会重复插入
   - 验证 `segment_split_ok` 每个 idx 只出现 1 次

3. **临时文件安全验证**：
   - 对分段任务执行失败注入
   - 确认已有数据库引用的 segment 文件不会被异常路径删除
   - 确认 `No such file or directory` 不再出现

4. **状态转换完整性验证**：
   - 转写完成后，确认 `to_status=SUCCEEDED` 出现且只出现 1 次
   - 确认 `task_transcription_succeeded` 只在状态真正转换成功时记录

5. **CPU-only 调度验证**：
   - 将 `asr-server-10097` 设为 `enabled=false`
   - 确认 heartbeat 不会将其重新纳入调度
   - 确认调度计划中没有 `asr-server-10097`

## Bug 3：调度器 `running_count` 双重计算 segments 和 tasks — 导致任务"排不上队"

### 现象

日志中出现调度报告如 `funasr-remote-02(4/2)`，表示 remote-02 服务器（`max_concurrency=2`）上有 4 个"正在运行"的工作单元。实际上该服务器只在处理 1 个被分段的任务，但调度器认为它已经满载，后续任务无法分配到该服务器（`no_free_slots_for_scheduling`）。

当所有服务器都因此被判定为"满"，QUEUED 状态的任务将无法调度，卡在队列中。

### 根因分析

代码位置：`task_runner.py:414-436`（`_dispatch_queued_tasks` 方法内）

**第一步 — 统计 Task 级别的占用**（414-421 行）：

```python
count_stmt = (
    select(Task.assigned_server_id, func.count())
    .where(Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]))
    .group_by(Task.assigned_server_id)
)
running_count: dict[str, int] = dict(
    (await session.execute(count_stmt)).all()
)
```

**第二步 — 累加 Segment 级别的占用**（424-436 行）：

```python
seg_server_count_stmt = (
    select(TaskSegment.assigned_server_id, func.count())
    .where(TaskSegment.status.in_([
        SegmentStatus.DISPATCHED, SegmentStatus.TRANSCRIBING,
    ]))
    .group_by(TaskSegment.assigned_server_id)
)
seg_running: dict[str, int] = dict(
    (await session.execute(seg_server_count_stmt)).all()
)
for sid, cnt in seg_running.items():
    if sid:
        running_count[sid] = running_count.get(sid, 0) + cnt
```

**关键冲突 — 分段派发时父任务也被设为 DISPATCHED**（560-564 行）：

```python
await session.refresh(parent_task, ["status"])
if parent_task.status == TaskStatus.QUEUED.value:
    if parent_task.can_transition_to(TaskStatus.DISPATCHED):
        parent_task.assigned_server_id = decision.server_id
        await repo.update_task_status(parent_task, TaskStatus.DISPATCHED)
```

**双重计算示例**：

一个任务被分成 3 个 segments，全部在 remote-02（`max_concurrency=2`）上运行：

| 查询 | 对象 | 状态 | assigned_server_id | 计数 |
|------|------|------|-------------------|:----:|
| 第一步（Task） | 父任务 | DISPATCHED | remote-02 | **+1** |
| 第二步（Segment） | seg_000 | TRANSCRIBING | remote-02 | **+1** |
| 第二步（Segment） | seg_001 | TRANSCRIBING | remote-02 | **+1** |
| 第二步（Segment） | seg_002 | TRANSCRIBING | remote-02 | **+1** |
| **合计** | | | `running_count["remote-02"]` | **4** |

```
free_slots = max(max_concurrency - running_tasks, 0)
           = max(2 - 4, 0) = 0  ← 调度器认为没有空闲 slot
```

实际上只有 1 个逻辑任务的 3 个分段在使用 FunASR 连接，真正占用 slot 应为 **3**（每个 segment 独立占用一个 WebSocket 连接），而不是 **4**。

### 与 Bug 1 的关系

Bug 1（重启循环）和 Bug 3（双重计算）是**两个独立的 bug**，但它们**叠加**造成了 10 个任务卡住的现象：

| 阶段 | Bug 3 的作用 | Bug 1 的作用 |
|------|-------------|-------------|
| 批量提交后 | 分段任务膨胀 slot 计数，后续任务排不上 | — |
| 前 35 个任务 | 短任务/非分段任务可以正常调度 | 重启间隔偶尔够长，任务能完成 |
| 后 10 个任务 | 分段任务 slot 被双重计算，堵住调度 | 每次重启又回退状态，形成无限循环 |
| 最终表现 | **排不上队** | **跑不完** |

修复 Bug 1 可以解决"跑不完"，修复 Bug 3 可以解决"排不上队"。两者都需要修复才能彻底消除批量转写卡住问题。

### 解决方案

**修复方案：父任务不计入 `running_count`，只统计 segments**

核心思路：对于有 segments 的任务，实际占用 FunASR 服务器 slot 的是每个 segment（各自独立建立 WebSocket 连接），父任务只是一个逻辑容器，不占用服务器资源。因此 `running_count` 应排除有活跃 segments 的父任务。

```python
# ---- 修复后的 running_count 计算 ----

# 1. 找出有活跃 segments 的父任务 ID
active_segment_parent_ids_stmt = (
    select(TaskSegment.task_id)
    .where(TaskSegment.status.in_([
        SegmentStatus.DISPATCHED, SegmentStatus.TRANSCRIBING,
    ]))
    .distinct()
)
active_segment_parent_ids: set[str] = set(
    (await session.execute(active_segment_parent_ids_stmt)).scalars().all()
)

# 2. 统计 Task 级别 — 排除有活跃 segments 的父任务
count_stmt = (
    select(Task.assigned_server_id, func.count())
    .where(
        Task.status.in_([TaskStatus.DISPATCHED, TaskStatus.TRANSCRIBING]),
        Task.task_id.not_in(active_segment_parent_ids) if active_segment_parent_ids else true(),
    )
    .group_by(Task.assigned_server_id)
)
running_count: dict[str, int] = dict(
    (await session.execute(count_stmt)).all()
)

# 3. 累加 Segment 级别（不变）
seg_server_count_stmt = (
    select(TaskSegment.assigned_server_id, func.count())
    .where(TaskSegment.status.in_([
        SegmentStatus.DISPATCHED, SegmentStatus.TRANSCRIBING,
    ]))
    .group_by(TaskSegment.assigned_server_id)
)
seg_running: dict[str, int] = dict(
    (await session.execute(seg_server_count_stmt)).all()
)
for sid, cnt in seg_running.items():
    if sid:
        running_count[sid] = running_count.get(sid, 0) + cnt
```

修复后同一个例子的计算结果：

| 查询 | 对象 | 计数 |
|------|------|:----:|
| Task（排除有活跃 seg 的父任务） | 父任务被排除 | **0** |
| Segment | seg_000 + seg_001 + seg_002 | **3** |
| **合计** | `running_count["remote-02"]` | **3** |

```
free_slots = max(2 - 3, 0) = 0  ← 确实满了（3 个 segment 占 3 个连接，超过 max=2）
```

如果只有 2 个 segments 在跑：

```
free_slots = max(2 - 2, 0) = 0  ← 正确：2 个连接用完了 2 个 slot
```

如果只有 1 个 segment 在跑：

```
free_slots = max(2 - 1, 0) = 1  ← 正确：还有 1 个空闲 slot 可以调度
```

### 验证方案

1. **分段任务 slot 计算验证**：
   - 提交一个会被分 3 段的长音频到 max_concurrency=2 的服务器
   - 确认 `running_count` 为段数（不含父任务）
   - 确认日志中的服务器负载显示正确（如 `remote-02(2/2)` 而非 `remote-02(3/2)`）

2. **混合任务调度验证**：
   - 同时提交分段任务和非分段任务
   - 确认非分段任务不受分段任务父任务的 slot 膨胀影响
   - 确认所有服务器的空闲 slot 都能被正确利用

3. **边界条件**：
   - 所有 segments 都完成后，父任务不应继续占用 slot
   - 部分 segments 失败时，slot 释放正确

## Bug 2：Stats 端点 success_rate 为 None 时 round 报错

### 现象

执行 benchmark 前置检查时，请求：

```http
GET /api/v1/stats
```

当最近 24 小时没有完成任务时，`finished_24h == 0`，代码将 `success_rate` 设为 `None`。随后返回响应时执行：

```python
success_rate_24h=round(success_rate, 1)
```

此时会抛出：

```text
TypeError: type NoneType doesn't define __round__ method
```

导致前置检查失败，benchmark 无法顺利启动。

### 根因分析

`SystemStats` 模型里 `success_rate_24h` 定义为必填 `float`：

```python
success_rate_24h: float
```

但业务逻辑在无完成任务时会产生 `None`：

```python
success_rate = (succeeded_24h / finished_24h * 100) if finished_24h > 0 else None
```

这说明返回模型和业务语义不一致：

- 如果"没有完成任务"应表达为 0%，则计算逻辑应该返回 `0.0`。
- 如果"没有样本，成功率不可计算"应表达为 `null`，则模型应允许 `float | None`，并且返回时不能直接 `round(None, 1)`。

### 推荐解决方案

建议采用"无样本返回 0.0"的方案，原因是：

- 当前字段类型已经是 `float`，前端和 CLI 更容易处理。
- `/stats` 是概览端点，0 个完成任务时显示 0.0% 比接口报错更符合监控语义。
- 现有测试中也把 `success_rate_24h` 当数值处理。

修复代码：

```python
success_rate = (succeeded_24h / finished_24h * 100) if finished_24h > 0 else 0.0
```

或者保持现有计算逻辑，但返回时做保护：

```python
success_rate_24h=round(success_rate, 1) if success_rate is not None else 0.0
```

同时建议修正 `avg_rtf` 的判断：

```python
avg_rtf=round(avg_rtf, 3) if avg_rtf is not None else None
```

当前 `if avg_rtf` 会把合法的 `0.0` 当作空值，虽然 RTF 为 0 的现实概率极低，但语义上应避免 truthy 判断。

### 测试建议

增加 Stats 端点单元测试或集成测试：

1. 无任务：
   - `finished_24h = 0`
   - 期望 `success_rate_24h == 0.0`
   - API 返回 200

2. 只有失败任务：
   - `finished_24h = 1`
   - `succeeded_24h = 0`
   - 期望 `success_rate_24h == 0.0`

3. 成功和失败混合：
   - `finished_24h = 4`
   - `succeeded_24h = 3`
   - 期望 `success_rate_24h == 75.0`

4. 无在线服务器 RTF：
   - 期望 `avg_rtf is None`
   - API 返回 200

## Benchmark 校准结果影响

本次全量 benchmark 后，3 台服务器校准结果为：

| 服务器 | 单线程 RTF | 吞吐量 RTF | 推荐并发 | 状态 |
| --- | ---: | ---: | ---: | --- |
| remote-01 (10096) | 0.0785 | 0.0326 | 4 | 正常 |
| remote-02 (10095) | 0.0703 | 0.0480 | 2 | 正常 |
| remote-03 (10097) | 0.0818 | 0.0908 | 1 | 并发退化 |

调度影响：

- 总推荐 slot：7
- 单线程最快：remote-02
- 最高吞吐：remote-01
- remote-03 并发退化，推荐并发保持 1

需要注意：benchmark 校准本身已经恢复调度器参数，但它不解决上面的后端重启、重复预处理和临时文件删除问题。批量任务稳定性仍需要按 Bug 1 的方案修复。

## 修复优先级总表

| 优先级 | 项目 | 对应 Bug | 原因 |
| --- | --- | --- | --- |
| P0 | 批处理启动强制 `ASR_NO_RELOAD=1` | Bug 1 缺陷 1 | 立即阻断重启循环，是最直接的运行规避 |
| P0 | `running_count` 排除有活跃 segments 的父任务 | **Bug 3** | **修复 slot 双重计算，解决分段任务堵死调度的问题** |
| P0 | `reset_stale_tasks` 增加 stale 时长保护 | Bug 1 缺陷 2 | 防止频繁重启时正在执行的任务被无条件回退 |
| P0 | 预处理任务原子 claim | Bug 1 缺陷 3 | 防止同一任务被多个 runner 重复分段 |
| P0 | 分段异常不删除已发布目录 | Bug 1 缺陷 3 | 防止数据库引用的 segment 文件被删 |
| P1 | 分段创建幂等化 | Bug 1 缺陷 3 | `UNIQUE constraint` 不应 fallback 整文件，应复用已有分段 |
| P1 | `_mark_task_succeeded` 转换失败警告 | Bug 1 缺陷 2 | 避免日志误导，暴露状态被外部重置的问题 |
| P1 | `/stats` 空样本返回 0.0 | Bug 2 | 修复 benchmark 前置检查和监控端点 |
| P1 | 增加服务器 `enabled` / maintenance 状态 | Bug 1 缺陷 4 | 支持真正的 CPU-only / GPU-only 对照实验 |
| P2 | 增加失败注入和重启恢复测试 | 全部 | 防止同类问题回归 |

## 建议落地顺序

1. **立即**：批处理环境默认设置 `ASR_NO_RELOAD=1`，阻断重启循环。
2. **修复 `running_count` 双重计算**：排除有活跃 segments 的父任务，解决分段任务堵死调度。
3. 修复 `reset_stale_tasks`：增加 `started_at < now - 10min` 的 stale 时长保护。
4. 修复 `/api/v1/stats` 的 `success_rate` 空样本问题，并补测试。
5. 为任务预处理添加原子 claim，保证同一任务只能被一个 runner 分段。
6. 重构分段临时目录策略，使用 worker 临时目录 + 原子发布。
7. 修复 `_mark_task_succeeded` 日志语义，转换失败时输出明确警告。
8. 增加服务器 `enabled` 字段，避免 heartbeat 覆盖手动禁用意图。
9. 新增回归测试：后端重启、重复 runner、分段失败、slot 计算、服务器禁用调度。
