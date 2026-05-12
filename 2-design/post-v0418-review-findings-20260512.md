# V0.4.18 代码审查发现的遗留问题

> 日期：2026-05-12
> 审查范围：V0.4.12 → V0.4.18 全部代码变更（6 个版本，94 个文件，+5063/-1783 行）
> 审查依据：`cli-batch-transcribe-bug-report-20260510.md` + `dynamic-dispatch-work-steal-optimization-20260511.md`
> 审查结论：两个文档提出的所有已确认问题均已修复；以下 5 个问题为修复过程中新引入或遗漏的逻辑问题

---

## 问题 A [中等严重度]：SEGMENT_RETRY_EXHAUSTED 任务无法整文件降级重试

### 现象

当一个长音频的所有 segment 重试耗尽后，任务永久停留在 FAILED 状态，无法自动降级为整文件转写重试。

### 根因

`_retry_failed_tasks()` 对有 segment 记录的任务只处理 `error_code == "MERGE_FAILED"` 的情况，`SEGMENT_RETRY_EXHAUSTED` 被 `continue` 跳过：

```python
# task_runner.py _retry_failed_tasks()
for task in tasks:
    seg_counts = await seg_repo.count_by_status(task.task_id)
    has_segments = sum(seg_counts.values()) > 0
    if has_segments:
        if task.error_code == "MERGE_FAILED" and ...:
            ...  # 只处理 MERGE_FAILED
            continue
        continue  # SEGMENT_RETRY_EXHAUSTED 在这里被跳过
```

同时 `_create_segments_for_task()` 的 `segments_all_terminal_failed` 日志写了 "task will be dispatched as whole-file fallback"，但这条路径在实际 retry 流程中不可能被执行到。

### 修复方案

在 `_retry_failed_tasks()` 中为 `SEGMENT_RETRY_EXHAUSTED` 新增分支：删除所有旧 segment 记录，将任务回退到 QUEUED 作为整文件重试。增加 `whole_file_fallback_count` 上限（默认 1 次），避免无限降级循环。

---

## 问题 B [低-中严重度]：_count_server_active_work() 对整文件降级任务的计数盲区

### 现象

`_count_server_active_work()` 使用 `select(TaskSegment.task_id).distinct()` 排除所有有 segment 记录的父任务。如果一个任务之前被分段（segment 记录存在，哪怕全 FAILED），后来被整文件重新派发，该任务仍被排除在 whole-file slot 计数之外。

### 当前影响

由于问题 A 的存在（`SEGMENT_RETRY_EXHAUSTED` 无法自动重试），这个路径在当前代码中**不会被触发**。但修复问题 A 后，这里就会变成真实的 slot 泄漏 bug。

### 修复方案

将子查询改为只排除有**活跃 segment**（PENDING/DISPATCHED/TRANSCRIBING/SUCCEEDED）的父任务，而不是有**任何 segment 记录**的父任务。这样当所有 segment 都是 FAILED 状态时，整文件降级的任务能正确计入 slot。

---

## 问题 C [低严重度]：Phase A/B 弹出 PlanPool 中的过期条目后丢失

### 现象

Phase A 从 PlanPool `pop_dispatchable()` 弹出一个 decision 后，如果该 task_id 在当前 `segment_items` 中找不到（segment 已在上一个 dispatch 周期完成），item 被 `continue` 跳过但已从 pool 中永久移除。

### 影响

自愈性问题：下一个 dispatch 周期（~1 秒后）会检测到 `has_unplanned=True`，触发增量 merge 或 replan。延迟最多 1 秒。

### 修复方案

在 pop 前先通过 `get_queue_snapshot()` 预检查 item 是否在当前 `work_map` 中有效。无效的 item 直接从 pool 中 `remove()` 清理掉，不浪费 pop 额度。

---

## 问题 D [低严重度]：迟到完成事件的双写竞争（缺少 run_generation 保护）

### 现象

后端重启后：
1. 旧 FunASR 连接结果到达 → `save_result()` 写入 → `_mark_task_succeeded()` 设置 SUCCEEDED
2. 同时 task 被重新 dispatch → 新 worker 完成 → `save_result()` 覆盖旧结果 → `_mark_task_succeeded()` 发现已 SUCCEEDED

两次 `save_result()` 存在文件覆盖竞争。

### 影响

最终结果正确（较新的转写结果），状态正确（SUCCEEDED），但浪费了一次转写资源。

### 修复方案

在 Task 模型上新增 `run_generation: int` 字段。每次 dispatch 时递增。`save_result()` 和 `_mark_task_succeeded()` 在操作前检查 `run_generation` 是否匹配，过时的 worker 结果不写入。

---

## 问题 E [信息级]：FAILED 父任务的孤儿 PENDING segments 永久残留

### 现象

`_mark_segment_failed()` 中 segment 重试耗尽时将父任务设为 FAILED，但同一父任务下其他 PENDING 状态的 segment 未被同步标记为 FAILED。

### 影响

- 不影响功能正确性（FAILED 父任务的 PENDING segments 不会被 dispatch）
- 每次 dispatch 循环的 `parent_ids_with_pending` 查询会返回多余条目
- 数据库中残留无用记录

### 修复方案

在父任务被标记为 FAILED 的同时，批量将该父任务下所有非终态 segment 标记为 FAILED（error_message 标注 "Parent task failed"）。

---

## 修复优先级

| 优先级 | 问题 | 理由 |
|--------|------|------|
| P0 | A + B（联合修复） | A 是功能缺失，B 是 A 修复后的必要配套 |
| P1 | E | 数据清洁性，且是 A 的前置清理 |
| P1 | D | 防御性增强，避免重启后资源浪费 |
| P2 | C | 自愈性问题，延迟 1 秒可接受 |
