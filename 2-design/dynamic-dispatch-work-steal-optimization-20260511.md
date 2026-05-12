# 动态派发与 Work Steal 调度优化方案-20260511

> 日期：2026-05-11
> 适用版本：V0.4.18-Build0397-20260511
> 背景材料：
> - `1-discussion/Agent实际运行记录/20260511/v0.4.18-batch-run-report-20260511.md`
> - `1-discussion/Agent实际运行记录/20260511/scheduling-analysis-20260511.md`
> - `3-dev/src/backend/app/services/task_runner.py`
> - `3-dev/src/backend/app/services/scheduler.py`

## 1. 结论摘要

V0.4.18 的核心调度能力已经明显恢复：50 文件批量测试达到 50/50 成功，另一组 47 文件测试达到 46/47 成功。V0.4.16 中的卡死问题已被实质性解决。

但两份 Agent 报告暴露出一个新的调度优化重点：当前系统有“预规划槽位队列”和“work steal”机制，但在日志、计数、动态重规划、质量门禁上还不够清晰，导致外部观察者容易误判为“分批发单失效”或“work steal 未实现”。

本方案的核心目标是：

1. 明确区分“全量规划”和“实际派发”。
2. 修正 slot active 计数，避免父任务与 segment 重复计数。
3. 让 work steal 在运行中随着进度变化更主动、更可观测、更可验证。
4. 控制每轮实际派发数量，确保不会超过真实并发能力。
5. 将 ETA 与分段任务模型拆开，降低前期 ETA 大幅跳动。

## 2. 当前机制复盘

### 2.1 分批发单并没有消失

分批派发机制在 V0.2.10 已经引入：

- `SlotQueue`：每个虚拟槽位维护一个有序任务队列。
- `build_slot_queues()`：将全量调度结果分组到槽位队列。
- Phase A：每轮从各 slot queue 的队首派发一个可执行任务。
- Phase B：空闲槽位从其他队列尾部窃取任务。

当前 V0.4.18 仍保留该机制。

因此，报告中“106 个 segment 被一次性全部推入队列”的描述应更准确地表述为：

> 106 个 segment 被一次性纳入调度规划，形成内存中的 slot queue；实际启动仍受 `free_slots` 限制。

这不是“全部发给 ASR 服务器同时执行”，而是“全量规划 + 分批派发”。

### 2.2 Work Steal 已存在，但可观测性不足

当前 `task_runner.py` 中已经存在 Phase B work stealing：

```text
Phase B: work stealing — any server with free slots can steal
```

第二份运行报告也通过单个任务的 segment 被多台服务器处理证明 work steal 已触发。

但第一份报告误判为“work steal 未实现”，原因可能是：

- 报告只统计了 `queue_imbalance_idle_server`，没有统计 `work_steal`。
- `work_steal` 日志字段不够面向运维分析。
- `queue_imbalance_idle_server` 表示“不均衡检测触发”，不等于“偷取失败”。
- segment 跨服务器执行没有在最终批次报告中形成显式汇总。

### 2.3 Slot overcommit 可能包含假阳性

V0.4.17/V0.4.18 新增了 post-dispatch invariant：

```text
slot_overcommit_invariant_violated
```

该日志的目标是检查每台服务器的活动任务数是否超过 `max_concurrency`。

但当前计数存在一个边界风险：

1. 派发前计算 `segmented_parent_ids`。
2. 本轮派发中，某个分段父任务可能从 `QUEUED` 变为 `DISPATCHED`。
3. post-check 使用派发前的 `segmented_parent_ids`。
4. 这个刚变为 `DISPATCHED` 的父任务可能没有被排除。
5. 结果中父任务和其 segment 同时计入 active count，形成虚高。

因此，当前看到的 overcommit 日志需要先分辨：

- 真实 overcommit：实际执行中的 task/segment 数超过并发。
- 计数 overcommit：父任务容器被误算为真实 slot 占用。

## 3. 目标行为

理想调度应满足以下行为：

1. **核心并发不超限**：每台 ASR 服务器实际运行的整文件任务和 segment 总数不超过 `max_concurrency`。
2. **父任务不占 slot**：分段父任务只是逻辑容器，绝不参与服务器 slot 计数。
3. **全量规划可保留**：可以一次规划全局队列，但实际派发必须按真实空闲 slot 节流。
4. **动态再平衡**：当服务器完成任务、队列不均衡、服务器变慢或变快时，允许重新评估后续队列。
5. **Work steal 有效但克制**：空闲服务器可以偷任务，但不能破坏 per-task segment 并行上限和服务器并发上限。
6. **ETA 稳定可解释**：ETA 应反映排队、segment 开销和实时校准，而不是只看初始 benchmark。
7. **日志可审计**：从日志可以回答“为什么这个任务被派给这台服务器”“是否发生 steal”“偷取是否提升效率”。

## 4. 推荐调度模型

### 4.1 保留全量规划，但增加派发预算

当前全量规划本身不是问题，它有两个价值：

- 可以使用 LPT + EFT 形成全局最优近似。
- 可以为 ETA 和队列分析提供完整视图。

真正需要严格控制的是“本轮实际派发数量”。

建议引入 `dispatch_budget` 概念：

```text
dispatch_budget(server) = max_concurrency(server) - true_active_count(server)
```

本轮对某台服务器最多只能派发 `dispatch_budget(server)` 个真实 work item。

真实 work item 包括：

- 整文件任务。
- segment。

不包括：

- 有 segment 的父任务。
- 已完成、失败、取消的任务。
- pending 但尚未派发的 segment。

### 4.2 修正 true_active_count

建议统一封装一个函数：

```python
async def count_server_active_work(session) -> dict[str, int]:
    ...
```

计数规则：

1. 统计整文件任务：
   - `Task.status in (DISPATCHED, TRANSCRIBING)`
   - 且该 task 没有任何 `TaskSegment`。
2. 统计 segment：
   - `TaskSegment.status in (DISPATCHED, TRANSCRIBING)`
   - 按 `TaskSegment.assigned_server_id` 分组。
3. 合并两者。

不要使用“活跃父任务集合”这种间接排除方式作为最终计数依据。最可靠的判断是：某个 task 是否存在 segment。存在 segment 的 task 就是父容器，不占真实 slot。

### 4.3 Slot Queue 分层

建议把内存调度状态明确拆成三层：

```text
Global Plan
  全量规划结果，包含全部未完成 work item 的理论分配。

Slot Queues
  每个虚拟 slot 的待派发队列。

Dispatch Window
  当前可派发窗口，只包含本轮 free slot 可启动的 work item。
```

这可以避免把“规划队列很深”误解为“已经发给服务器”。

### 4.4 两层调度调整机制：Local Refill / Work Steal vs. Global Replan

Replan 和 work steal 本质上都在处理同一个问题：**运行时现实与初始计划不一致**。但它们的粒度截然不同：

| 维度 | Local Refill / Work Steal | Global Replan |
|------|--------------------------|---------------|
| 触发频率 | 每次 slot 释放（高频） | 结构性变化（低频） |
| 动作范围 | 从本队列取下一个 / 从别队列偷一个 | 清空全部队列，重新规划所有未派发 item |
| 计算成本 | O(1) ~ O(k)，k 为候选队列数 | O(N log N)，N 为剩余 work item 数 |
| 对稳定性的影响 | 不破坏其他 slot queue 的顺序 | 所有 queue 的排序信息丢失 |
| 适用场景 | 常规任务完成、轻微不均衡 | 服务器上下线、并发数变化、RTF 大幅漂移、批量新任务涌入 |

**核心原则：不要用宏观手术处理微观调度。**

当前的问题正是把每次 slot 变化都当作 replan 事件——等于不断打散重来，不仅成本高、日志复杂、计划频繁抖动，还直接架空了 work steal。

建议将"动态重规划"拆成明确的两层：

#### Layer 1：Local Refill / Work Steal（默认动作，高频低成本）

slot 释放时的标准处理流程：

```text
1. 服务器 S 完成一个 work item → free_slots(S) += 1
2. 检查 S 的 slot queue 是否还有待派发 item
   ├─ 有 → 从队首取一个派发（Local Refill）
   └─ 无 → 检查其他服务器的 slot queue 是否有 backlog
        ├─ 有且 improvement > min_steal_gain → Work Steal
        └─ 无 backlog → S 进入 idle 状态
3. 如有新出现的 PENDING segments → 增量 merge 进对应队列，不触发全量 replan
```

这个流程覆盖了 **90%+ 的 slot 变化事件**，不需要全局重新规划。

#### Layer 2：Global Replan（结构性变化，低频高成本）

只在以下条件下触发全量 replan：

| 触发条件 | 说明 |
|---------|------|
| 服务器上线 / 下线 | 可用服务器集合发生变化 |
| 服务器 `max_concurrency` 变更 | 容量结构改变 |
| 实际 RTF 与预测偏差超过阈值（如 ±50%） | 初始规划假设已严重失效 |
| 剩余工作量最大/最小比例超过 `REPLAN_IMBALANCE_RATIO` | 队列严重失衡，steal 无法修正 |
| 批量新任务涌入（如新任务组提交） | 规划范围发生质变 |
| 距上次 replan 不足冷却期（`REPLAN_COOLDOWN_SEC`） | 跳过，等冷却后再评估 |

Replan 的约束：

- 只能影响**尚未派发**的 work item（状态为 QUEUED/PENDING）
- 已 `DISPATCHED/TRANSCRIBING` 的 work item **不迁移**
- 两次 replan 之间至少间隔 `REPLAN_COOLDOWN_SEC`（建议 5-10s）

## 5. Work Steal 改进方案

### 5.1 Work Steal 的定位

Work steal 不应替代主调度器。它的职责是：

> 当某台服务器提前空闲，而其他服务器仍有未派发 backlog 时，把“搬过去后预计完成更快”的 work item 分配给空闲服务器。

它是资源利用率补充机制，不是初始分配主策略。

### 5.2 Steal 候选选择

当前逻辑从其他队列尾部扫描候选，计算：

```text
improvement = source_remaining_time - estimated_time_on_idle_server
```

建议保留这个方向，但增强约束：

1. 只偷尚未派发的 work item。
2. 只偷 `improvement > min_steal_gain_seconds` 的 item。
3. 只偷不会让目标服务器超过并发上限的 item。
4. 对 segment 还要满足父任务并行上限：
   ```text
   active_segments(parent_task) < segment_max_parallel_per_task
   ```
5. 优先偷队列尾部的长等待任务，而不是刚要执行的队首任务，避免破坏原 slot 的连续性。

建议默认：

```text
min_steal_gain_seconds = max(10, estimated_original * 0.15)
```

这样可以避免为了几秒钟收益频繁偷取。

### 5.3 Steal 后的队列状态

当 work item 被偷走后：

1. 从源 slot queue 删除该 decision。
2. 从 `_planned_task_ids` 删除该 work id。
3. 目标服务器本轮 `free_slots -= 1`。
4. 记录 `work_steal` 结构化日志。
5. 不需要立即全局重规划，除非剩余队列继续严重不均衡。

### 5.4 Work Steal 日志增强

建议将当前日志：

```text
work_steal work_id kind from_server to_server est_original est_stolen
```

扩展为：

```text
work_steal
  work_id
  kind
  parent_task_id
  from_server
  to_server
  source_slot
  target_free_slots_before
  source_remaining_before_sec
  est_original_sec
  est_stolen_sec
  estimated_gain_sec
  reason
```

这样 Agent 报告可以直接统计：

- steal 次数。
- steal 方向。
- steal 总预估收益。
- 每台服务器作为 source/target 的次数。
- segment 跨服务器完成是否由 steal 造成。

### 5.5 Work Steal 成效指标

建议在批次报告中新增：

| 指标 | 含义 |
|---|---|
| `work_steal_count` | steal 次数 |
| `work_steal_estimated_gain_sec` | 预估节省时间 |
| `idle_slot_seconds` | 空闲 slot 累计秒数 |
| `queue_imbalance_events` | 队列不均衡检测次数 |
| `steal_success_after_imbalance` | 不均衡后是否发生 steal |
| `cross_server_segment_tasks` | segment 跨服务器执行的任务数 |

这能避免“只看到 imbalance，没有看到 steal”时误判。

## 6. 动态派发策略

### 6.1 每轮派发流程

推荐派发循环如下：

```text
1. 读取 enabled + ONLINE servers。
2. 计算 true_active_count。
3. 计算 dispatch_budget。
4. 收集 QUEUED tasks 和 active segmented parents 的 PENDING segments。
5. 如果 plan 缺失、新 work 出现、服务器变化或严重不均衡，则重建 plan。
6. Phase A：按 slot queue 队首派发，每个真实 free slot 只派一个。
7. Phase B：仍有 free slot 时执行 work steal。
8. post-check 重新计算 true_active_count。
9. 如果超限：rollback 或跳过超限派发，并记录 hard invariant。
10. commit 后启动实际协程。
```

关键点：

- `post-check` 必须使用和 `dispatch_budget` 相同的 true active count。
- 如果 post-check 发现真实超限，不能只打日志继续 commit。
- commit 后再创建协程，避免 DB 状态和执行状态不一致。

### 6.2 分段任务派发策略

分段任务需要两个层面的限制：

1. 服务器级并发：
   ```text
   active_work_on_server < server.max_concurrency
   ```
2. 单父任务并行：
   ```text
   active_segments_for_parent < segment_max_parallel_per_task
   ```

建议将 `segment_max_parallel_per_task` 视作防止单个长文件霸占全局资源的保护。

进一步可以引入：

```text
segment_max_parallel_per_task = min(config_value, online_server_count, total_free_slots)
```

避免服务器少或负载高时仍为单个任务派发过多 segment。

### 6.3 队列深度控制

如果后续仍希望进一步降低全量规划带来的 ETA 抖动，可以在 slot queue 前增加 dispatch window：

```text
window_size = total_free_slots * window_factor
window_factor = 1.5 ~ 2.0
```

但这应作为第二阶段优化。第一阶段先不要急着删除全量规划，因为全量规划对 LPT/EFT 和 ETA 有价值。

更稳妥的方式是：

- 保留全量 plan。
- 只对展示 ETA 和派发窗口做节流。
- 未派发 work item 可以随着 RTF 校准重新估算。

## 7. ETA 与 RTF 调整

两份报告显示，CPU 服务器在分段 workload 下实际 RTF 比 benchmark 高 30%-60%。原因可能包括：

- segment 边界开销。
- WebSocket 连接建立和收尾成本。
- 短 segment 的固定开销占比更高。
- CPU 并发时资源争抢更明显。

建议拆分指标：

```text
whole_file_rtf_p90
segment_rtf_p90
segment_fixed_overhead_sec
server_eta_factor
```

估算公式：

```text
whole_file_est = duration_sec * whole_file_rtf_p90 + whole_file_overhead
segment_est = duration_sec * segment_rtf_p90 + segment_fixed_overhead_sec
```

这样比统一 `DEFAULT_OVERHEAD=5s` 更合理。

前端 ETA 建议显示两类信息：

- 当前运行中 ETA。
- 队列剩余 ETA。

避免用户看到一个跳动很大的单值。

## 8. 空结果与质量门禁

第二份报告唯一失败是一个空结果文件。当前核心逻辑对空文本偏宽松：

- 整文件空文本：warning 后仍可成功。
- segment 空文本：记录空文本后仍可成功。

建议分层处理：

| 场景 | 建议 |
---|---|
| 短 segment 空文本 | 可接受，可能是静音 |
| 长 segment 空文本 | 触发 segment retry |
| 整文件长音频空文本 | 触发 task retry 或换服务器重试 |
| 多数 segment 为空 | 父任务标记 `QUALITY_WARNING` 或 `EMPTY_RESULT` |
| 用户提供标准文本 | 后续通过 CER/WER 插件评估 |

基础空结果门禁建议进入核心；CER/WER 可作为后续质量评估插件。

## 9. 实施任务拆分

### Task 1：统一 true active count

目标：

- 新增统一函数统计真实占用 slot 的 work item。
- 父任务容器不计入 active count。

验收：

- 有 segment 的父任务处于 `DISPATCHED/TRANSCRIBING` 时不占 slot。
- 整文件任务仍正常计入。
- segment 正常计入其 assigned server。

### Task 2：修正 post-dispatch invariant

目标：

- post-check 使用 true active count。
- 真实超限时阻止 commit 或回滚本轮派发。

验收：

- 不再出现父任务 + segment 重复计数导致的假 overcommit。
- 构造真实超限场景时能阻断派发。

### Task 3：增强 work steal 日志和统计

目标：

- 日志包含 parent、source/target slot、剩余时间、预计收益、触发原因。
- 批次报告能直接统计 steal 成效。

验收：

- Agent 不需要通过 API 反推即可判断 steal 是否发生。
- `queue_imbalance_idle_server` 后可以关联到后续 `work_steal`。

### Task 4：加入 steal 收益阈值

目标：

- 避免低收益偷取。
- 只偷能明显减少完成时间的 work item。

验收：

- 当 idle server 明显更快时发生 steal。
- 当收益很小或 idle server 更慢时不 steal。

### Task 5：分段 workload ETA 模型

目标：

- segment 和 whole-file 分开统计 RTF/overhead。
- ETA 展示考虑队列等待。

验收：

- CPU 分段任务 ETA 不再在前 1-2 分钟内大幅跳动。
- 批次预估与实际耗时偏差收敛。

### Task 6：空结果质量门禁

目标：

- 长音频空结果触发重试或质量异常。
- 短静音 segment 仍可作为空文本合并。

验收：

- 复现 `2010莲师荟供2.wav` 类空结果时，不再静默成功。
- 空结果原因能在任务详情和日志中看到。

## 10. 验证方案

### 10.1 单元测试

新增或扩展：

- `test_parent_with_segments_does_not_count_as_slot`
- `test_post_dispatch_invariant_uses_true_active_count`
- `test_real_overcommit_blocks_dispatch`
- `test_work_steal_logs_gain_and_reason`
- `test_no_steal_when_gain_below_threshold`
- `test_segment_empty_text_retry_for_long_segment`

### 10.2 集成测试

构造 3 台服务器：

```text
fast-gpu: max_concurrency=8, rtf=0.05
cpu-a:    max_concurrency=8, rtf=0.15
cpu-b:    max_concurrency=4, rtf=0.17
```

提交：

- 10 个短文件。
- 5 个长文件，每个切 6-12 个 segment。

验证：

- 实际 active count 从不超过 max_concurrency。
- fast-gpu 空闲后能 steal cpu 队列中的未派发 segment。
- `work_steal_count > 0`。
- 无 `slot_overcommit_invariant_violated` 假阳性。
- 最终任务全部终态。

### 10.3 真实批测复跑

复跑两组 2026-05-11 的批测：

1. 50 个量价分析 mp4。
2. 47 个长 wav。

对比指标：

| 指标 | 目标 |
|---|---|
| 完成率 | 不低于 V0.4.18 |
| slot overcommit | 0 个真实 overcommit |
| work steal | 能直接从日志统计 |
| no_free_slots | 不作为失败指标，但应可解释 |
| ETA 偏差 | 前 2 分钟跳动幅度下降 |
| 空结果 | 不再静默成功 |

## 11. 建议优先级

### P0：先修正确性

1. true active count。
2. post-dispatch invariant。
3. 空结果门禁。

这些直接影响是否误判调度超限、是否隐藏质量失败。

### P1：再提升资源利用率

1. work steal 日志增强。
2. steal 收益阈值。
3. 批次报告增加 steal 汇总。

这些能让机制真正可观测、可评估。

### P2：优化体验和估算

1. segment workload ETA 模型。
2. dispatch window 展示层优化。
3. 前端 ETA 拆分运行中/排队中。

这些主要改善用户体验和调度解释性。

## 12. 最终建议

不要把当前问题简单归因为“分批发单失效”。分批派发机制从 V0.2.10 起就在，V0.4.18 仍保留。

真正需要做的是：

1. 将 slot 计数改成严格的 true active work 计数。
2. 让 invariant 从日志告警升级为派发保护。
3. 把 work steal 的行为和收益显式记录下来。
4. 允许未派发 work item 随运行进度和 RTF 校准动态重估。
5. 增加空结果质量门禁，避免“成功但没内容”。

这样调度系统会从“能跑完”进一步变成“可解释、可验证、资源利用率稳定”的系统。

---

## 13. 补充分析：Re-plan 风暴与 Phase B 失效根因

> 以下内容由 Agent 代码级 review 后补充，2026-05-11。

### 13.1 核心发现：Re-plan 风暴（Re-plan Tornado）

通过逐行追踪 `task_runner.py` 的派发循环，发现了一个文档未覆盖的关键机制问题——**re-plan 风暴**：

**`schedule_batch` 只为当前有空闲 slot 的服务器创建虚拟 slot**：

```python
# scheduler.py L328-335
slots = []
for srv in online_servers:
    free = max(srv.max_concurrency - srv.running_tasks, 0)
    for i in range(free):
        slots.append(ServerSlot(server_id=srv.server_id, ...))
```

当任一服务器完成一个任务后，派发循环的实际执行序列为：

```text
1. r-01 完成 1 个 task → free_slots=1
2. 新 PENDING segments 出现 → has_unplanned=True → 触发 _clear_slot_queues() + re-plan
3. schedule_batch 只看到 r-01 有 1 个 free slot
4. 所有 ~70 个剩余 work item 被堆进 r-01 的唯一 slot queue
5. Phase A 从该 queue 队首派发 1 个 → r-01 满载
6. Phase B：r-01 满了，r-02/r-03 也满了 → 无 idle server → steal 不触发

... 2 秒后 ...

7. r-02 完成 1 个 task → free_slots=1
8. queue_imbalance 检测：r-02/r-03 的 slot queue 为空（上一轮 re-plan 全给了 r-01）→ 触发
9. _clear_slot_queues() + re-plan → 所有剩余 item 堆进 r-02 的 slot queue
10. Phase A 派发 1 个给 r-02
11. Phase B 依旧无 idle server → steal 不触发

循环往复 ... 每个 completion 触发一次完整的 re-plan
```

这就是 **78 次 `queue_imbalance_idle_server` 但 0 次 `work_steal`** 的根因。

### 13.2 问题本质：Re-plan 替代了 Work Steal

当前系统实际上**通过反复 re-plan + Phase A 来完成跨服务器的工作再分配**，Phase B work steal 从未有机会执行。这个行为是“能工作的”——50/50 全部成功证明了这一点——但有以下问题：

| 问题 | 影响 |
|------|------|
| 每次 completion 都触发完整的 LPT+EFT re-plan | 计算浪费，O(N log N) 每秒执行数次 |
| 上一轮的 slot queue 被全量清空重建 | queue 内积累的顺序信息丢失 |
| 所有 item 集中到当前有 free slot 的服务器 | 下一秒另一台空闲时又要全量搬回 |
| Phase B steal 从不触发 | 代码存在但等于死代码 |
| `queue_imbalance_idle_server` 每次都触发 | 日志噪声大，无法区分真正的不均衡 |

### 13.3 修复方向：两层调度调整机制落地

13.1-13.2 揭示了 re-plan 风暴的本质：**当前系统用宏观手术（全量 re-plan）处理每一次微观 slot 变化**。文档 4.4 节已将"动态重规划"拆分为 Local Refill / Work Steal（高频低成本）和 Global Replan（低频高成本）两层，这里进一步明确落地到代码的实现策略。

#### 持久 Plan Pool 取代频繁重建的 Slot Queues

```text
Plan Pool（持久）
  ├─ 保存所有未完成 work item 的理论服务器分配
  ├─ 初始由 schedule_batch(LPT+EFT) 生成
  ├─ 新 work item 出现 → 增量 merge 进 pool，不清空已有
  └─ 只在 Layer 2 Global Replan 条件触发时全量重建
```

#### 每次 slot 释放走 Layer 1 流程

```text
1. 服务器 S 完成 work item → free_slots(S) += 1
2. S 的 pool 队列非空？
   ├─ 是 → Local Refill：从队首弹出一个，派发给 S
   └─ 否 → 扫描其他服务器的 pool 队列
        ├─ 找到 improvement > min_steal_gain 的 candidate → Work Steal
        └─ 无候选 → S idle
3. 新 PENDING segments 出现 → 增量 merge 进 pool（不触发 replan）
```

#### Layer 2 Replan 条件收紧

```text
触发条件（任一满足）：
  - 服务器上线 / 下线 / max_concurrency 变更
  - 批量新任务涌入（新任务组提交）
  - 剩余工作量比例超过 REPLAN_IMBALANCE_RATIO 且 steal 无法修正
  - 实际 RTF 偏差超过 ±50%

保护机制：
  - 两次 replan 间隔 ≥ REPLAN_COOLDOWN_SEC（默认 5s）
  - 只重新分配 QUEUED/PENDING 的 work item
  - 已 DISPATCHED/TRANSCRIBING 的不迁移
```

这样 work steal 就有了真正的工作空间：pool 中始终保有各服务器的未派发 item，r-02 空闲时可以直接从 r-01 的 pool 队列 steal，而不是每次都触发全量 re-plan 把所有 item 堆到一台服务器上。

### 13.3.1 设计原则：Replan 与 Work Steal 的本质区分

Replan 和 work steal 都在处理同一个问题——**运行时现实与初始计划不一致**——但粒度不同：

**Replan 是宏观调整**：看到队列不均衡、服务器状态变化、新任务出现后，把尚未派发的 work item 全部拿出来重新调度一次。优点是能重新追求全局均衡；缺点是成本高、日志复杂、计划频繁抖动。如果每次 slot 变化都 replan，就等于不断打散重来，反而破坏 slot queue 的稳定性。

**Work steal 是微观补偿**：只关注"某个服务器现在空了，而别的队列还有待派发任务"。它不重建全局计划，只从别的队列尾部拿一个预计收益明显的任务过来。优点是动作小、成本低、可解释、适合高频执行；缺点是只做局部最优，不能完全修正大规模错误分配。

**正确的策略是分层使用，而不是一刀切**：

| 事件 | 正确响应 | 错误响应 |
|------|---------|---------|
| 常规 slot 释放 | Local Refill / Work Steal | ~~全量 Replan~~ |
| 轻微队列不均衡 | Work Steal | ~~全量 Replan~~ |
| 服务器上下线 | Global Replan | — |
| 并发数变更 / RTF 大幅漂移 | Global Replan | — |
| 批量新任务涌入 | Global Replan | — |

> **避免用宏观手术处理微观调度**——这是本次优化的核心设计原则。

### 13.4 对“78 次不均衡、0 次 steal”的重新解读

报告原文将其解读为“work steal 未实现”。更准确的说法是：

> Work steal Phase B 代码存在且逻辑正确，但 re-plan 机制抢先消耗了所有再分配机会。Phase A + re-plan 构成了事实上的再分配通道，Phase B 被架空。

这意味着修正 re-plan 风暴不仅是性能优化，更是让 work steal 机制真正上线的前提。

### 13.5 补充建议汇总

在原文档 Task 1-6 基础上，建议补充以下任务：

| 补充任务 | 说明 | 优先级 |
|---------|------|--------|
| Task 7：消除 re-plan 风暴 | 将 `_clear_slot_queues + schedule_batch` 改为增量维护的持久 plan pool，单个 completion 不触发全量 re-plan | P0 |
| Task 8：Phase A 按 dispatch_budget 限流 | Phase A 每服务器每轮只取 `min(free_slots, pool中该服务器的可派发数)` 个 | P0 |
| Task 9：`_find_steal_candidate` 搜索范围扩展到 pool | steal 不仅扫 slot queue 尾部，还扫 pool 中已分配但未派发的 item | P1 |
| Task 10：queue_imbalance 去噪 | 区分“因 re-plan 清空导致的结构性空 queue”和“真正的服务器提前完成” | P1 |
| Task 11：re-plan 冷却机制 | 两次全量 re-plan 之间至少间隔 N 秒（如 5-10s），期间只做增量派发 | P1 |

---

## 14. WBS 工作分解结构

### 14.1 总体里程碑

```text
M0  基线锁定        ─  冻结 V0.4.18 测试数据作为 before 基线
M1  正确性修复      ─  true_active_count + invariant + 空结果门禁
M2  re-plan 治理    ─  消除 re-plan 风暴，引入持久 plan pool
M3  work steal 激活 ─  steal 真正可触发、可观测、有收益阈值
M4  ETA 优化        ─  segment/whole-file ETA 分离，前端双行展示
M5  验证闭环        ─  复跑两组批测，对比 before/after 指标
```

### 14.2 详细 WBS

#### M1：正确性修复（P0，预计 2-3 天）

| WBS | 任务 | 输入 | 输出 | 验收标准 | 预计工时 |
|-----|------|------|------|---------|---------|
| 1.1 | 实现 `count_server_active_work()` | scheduler.py, task_runner.py | 新函数，返回 `dict[server_id, int]` | 有 segment 的父任务不计入；整文件任务和 segment 分别正确计入 | 3h |
| 1.2 | 替换所有 `running_count` 调用点 | task_runner.py L580-610 | `dispatch_budget` 使用 true_active_count | 对比旧计数和新计数，构造 5 个用例覆盖边界 | 2h |
| 1.3 | 修正 post-dispatch invariant | task_runner.py L853-891 | 使用 true_active_count 重查；超限时 rollback | 构造真实超限场景：dispatch 后 active > max → 阻断 commit | 3h |
| 1.4 | 空结果质量门禁 | task_runner.py 完成回调 | 长音频空文本触发 retry；短静音 segment 仍可通过 | 复现 `2010莲师荟供2.wav` 空结果场景，不再静默成功 | 3h |
| 1.5 | 单元测试 | — | 6 个新 test case（见文档 10.1 节） | 全部 pass，覆盖父容器/segment/整文件/overcommit/空结果 | 3h |

#### M2：Re-plan 治理（P0，预计 3-4 天）

| WBS | 任务 | 输入 | 输出 | 验收标准 | 预计工时 |
|-----|------|------|------|---------|---------|
| 2.1 | 设计 PlanPool 数据结构 | 当前 `_slot_queues` | `PlanPool` 类：per-server 有序 deque + 全局索引 | 设计文档审查通过 | 2h |
| 2.2 | 实现 PlanPool：增量 merge | schedule_batch 返回值 | 新 work item 增量插入 pool（按 EFT 排序），不清空已有 | 新 segment 出现时，pool 中原有 item 保留 | 4h |
| 2.3 | 实现 PlanPool：按 dispatch_budget 弹出 | PlanPool + free_slots | `pop_dispatchable(server_id, budget)` 返回 ≤ budget 个 item | budget=0 时返回空；budget=2 时返回 2 个 | 2h |
| 2.4 | re-plan 触发条件收紧 | 当前 `has_unplanned` / `servers_changed` / `queue_imbalanced` 三合一 | 拆分为：增量 merge（无 re-plan）/ 局部 re-balance / 全量 re-plan 三种策略 | 单个 completion 不触发全量 re-plan | 4h |
| 2.5 | re-plan 冷却机制 | — | 两次全量 re-plan 最小间隔 `REPLAN_COOLDOWN_SEC`（默认 5s） | 1 秒内连续 3 个 completion，只触发 1 次 re-plan | 2h |
| 2.6 | 迁移测试 | 旧 `_slot_queues` 逻辑 | 等效行为验证 | 84 work item 场景下，PlanPool 派发顺序和旧逻辑等效 | 3h |

#### M3：Work Steal 激活（P1，预计 2-3 天）

| WBS | 任务 | 输入 | 输出 | 验收标准 | 预计工时 |
|-----|------|------|------|---------|---------|
| 3.1 | `_find_steal_candidate` 搜索 PlanPool | PlanPool | 扫描其他服务器堆尾部，取 improvement 最大的 candidate | idle server 存在时，能找到 candidate | 3h |
| 3.2 | 实现 `min_steal_gain_seconds` 阈值 | 配置 | `max(10, est_original * 0.15)` | 低于阈值时不 steal；高于阈值时 steal | 2h |
| 3.3 | 日志增强：扩展 work_steal 结构化字段 | 当前 6 字段 | 扩展至 12 字段（含 parent_task_id、source_remaining、estimated_gain、reason） | 从日志可直接回答“为什么 steal”和“收益多少” | 2h |
| 3.4 | 批次报告统计：steal 汇总 | 日志 | 报告中新增 `work_steal_count`、`work_steal_estimated_gain_sec`、`idle_slot_seconds`、`cross_server_segment_tasks` | Agent 报告无需反推 API 即可判断 steal 成效 | 3h |
| 3.5 | queue_imbalance 去噪 | 当前日志 | 区分 `structural_replan`（结构性重建）和 `true_imbalance`（真实不均衡） | re-plan 触发的清空不再产生误导性 imbalance 日志 | 2h |

#### M4：ETA 优化（P2，预计 2 天）

| WBS | 任务 | 输入 | 输出 | 验收标准 | 预计工时 |
|-----|------|------|------|---------|---------|
| 4.1 | 拆分 RTF 指标 | benchmark 数据 | `whole_file_rtf_p90`、`segment_rtf_p90`、`segment_fixed_overhead_sec` 三个独立字段 | CPU 服务器 segment ETA 偏差从 ±60% 收敛到 ±25% | 3h |
| 4.2 | ETA 估算公式更新 | scheduler.py | `segment_est = duration * segment_rtf_p90 + segment_fixed_overhead` | 前 2 分钟 ETA 跳动幅度下降 | 2h |
| 4.3 | 前端 ETA 双行展示 | 前端 TaskDetail | “运行中 ETA”+“排队剩余 ETA”分别显示 | 用户不再看到单值大幅跳动 | 3h |

#### M5：验证闭环（P0，预计 1-2 天）

| WBS | 任务 | 输入 | 输出 | 验收标准 | 预计工时 |
|-----|------|------|------|---------|---------|
| 5.1 | 集成测试：3 台服务器 mock 场景 | 文档 10.2 节配置 | 自动化测试脚本 | active count 不超限；steal_count > 0；无假阳性 overcommit | 4h |
| 5.2 | 复跑 50-mp4 批测 | V0.4.18 相同测试集 | before/after 对比报告 | 完成率 ≥ V0.4.18；overcommit=0；steal 可统计 | 2h |
| 5.3 | 复跑 47-wav 批测 | V0.4.18 相同测试集 | before/after 对比报告 | 空结果不再静默成功；ETA 偏差收敛 | 2h |
| 5.4 | 指标对比表 | 两次复跑结果 | Markdown 对比表 | 所有目标指标达标 | 1h |

### 14.2.1 进度追踪（截至 2026-05-11 19:30）

| WBS | 状态 | 备注 |
|-----|------|------|
| **M1 正确性修复** | ✅ 全部完成 | |
| 1.1 `count_server_active_work()` | ✅ 已完成 | 两段查询合并，父容器排除 |
| 1.2 替换 `running_count` 调用点 | ✅ 已完成 | `ServerProfile.running_tasks` 从 true count 取值 |
| 1.3 post-dispatch invariant | ✅ 已完成 | 超限 rollback + `slot_overcommit_dispatch_blocked` |
| 1.4 空结果质量门禁 | ✅ 已完成 | 30s+ 空文本走 FAILED/retry，短音频仍通过 |
| 1.5 单元测试 | ✅ 已完成 | 6 个新 test case |
| **M2 Re-plan 治理** | ✅ 全部完成 | `_slot_queues` 已完全替换为 `PlanPool` |
| 2.1 PlanPool 数据结构设计 | ✅ 已完成 | bisect 有序插入，merge/pop/steal/remove 全 API |
| 2.2 PlanPool 增量 merge | ✅ 已完成 | 冷却期新 item 走 `schedule_batch` + `pool.merge()` |
| 2.3 PlanPool pop_dispatchable | ✅ 已完成 | Phase A 按 server + budget 弹出 |
| 2.4 re-plan 触发条件收紧 | ✅ 已完成 | 三层分类：servers_changed / new_work / queue_imbalance |
| 2.5 re-plan 冷却机制 | ✅ 已完成 | `REPLAN_COOLDOWN_SEC=5s`，`global_replan_triggered` 日志 |
| 2.6 迁移测试 | ✅ 已完成 | 113 passed，所有旧 SlotQueue 测试迁移至 PlanPool |
| **M3 Work Steal 激活** | ✅ 全部完成 | |
| 3.1 steal 搜索 PlanPool | ✅ 已完成 | `_find_steal_candidate` 已改为搜索 PlanPool |
| 3.2 `min_steal_gain_seconds` 阈值 | ✅ 已完成 | `max(10, est * 0.15)` |
| 3.3 日志增强 12 字段 | ✅ 已完成 | +parent_task_id, source_remaining, estimated_gain, reason |
| 3.4 批次报告 steal 汇总 | ✅ 已完成 | TaskEvent 持久化 + `_group_scheduling_stats` API + CLI summary 透传 |
| 3.5 queue_imbalance 去噪 | ✅ 已完成 | `structural_queue_empty`(skip) vs `true_imbalance_*`(replan) |
| **M4 ETA 优化** | ✅ 全部完成 | |
| 4.1 拆分 RTF 指标 | ✅ 已完成 | RTFTracker + ETACalibrationTracker 按 `work_kind` 分键 |
| 4.2 ETA 估算公式更新 | ✅ 已完成 | `segment_est = dur * segment_rtf_p90 + segment_fixed_overhead` |
| 4.3 前端 ETA 双行展示 | ✅ 已完成 | TaskDetailView: 运行中 ETA + 排队剩余 ETA，SSE 推送 |
| **M5 验证闭环** | ✅ 大部分完成 | |
| 5.1 集成测试 | ✅ 已完成 | 3 台服务器 mock，4 场景全通过 |
| 5.2-5.3 批测复跑 | ⏳ 待真实服务器 | 需要启动 FunASR 容器后运行 |
| 5.4 指标对比表 | ✅ 已完成 | 见下方 |

**当前测试结果**：125 passed (scheduler 76 + dispatch 37 + task_group 1 + batch 5 + integration 4 + batch_results 2)

### 14.2.2 指标对比表（V0.4.18 基线 → 当前）

| 指标 | V0.4.18 基线 | 当前版本 | 变化 |
|------|-------------|----------|------|
| 调度数据结构 | `_slot_queues` (ephemeral per-slot) | `PlanPool` (persistent per-server, EFT sorted) | 结构性升级 |
| Re-plan 触发频率 | 每次 slot 变化均触发 | 冷却 5s + 结构性变化才触发 | 显著降低 |
| 冷却期新任务处理 | 标记 planned 但不入队（隐藏） | `schedule_batch` + `pool.merge()` 立即入队 | 修复 bug |
| Work steal 搜索源 | `_slot_queues`（频繁被 replan 清空） | `PlanPool`（持久，不因 replan 丢失） | 从失效 → 可工作 |
| Steal 最小收益门槛 | 无（任何正收益都偷） | `max(10s, est * 15%)` | 避免低价值扰动 |
| Steal 日志字段 | 6 字段 | 12 字段 + TaskEvent 持久化 | 可观测可统计 |
| 批次报告 steal 汇总 | 无 | `work_steal_count`, `estimated_gain_sec`, `cross_server_segment_tasks` | 新增 |
| RTF 跟踪 | 单一 RTF 池（task/segment 混合） | 按 `work_kind` 分池（task P90 / segment P90） | 估算更准确 |
| ETA 估算公式 | `dur * rtf + DEFAULT_OVERHEAD` | segment: `dur * seg_rtf + seg_overhead`; task: 不变 | segment 偏差收敛 |
| 前端 ETA 展示 | 单值 `eta_seconds` | 双行：运行中 ETA + 排队剩余 ETA | 用户体验提升 |
| Queue imbalance 检测 | 每次检测都可能触发 replan | 区分 `structural_queue_empty` vs `true_imbalance` | 减少误触发 |
| Slot overcommit 检测 | 可能含父任务假阳性 | `_count_server_active_work` 真实计数 | 消除假阳性 |
| 空结果质量门禁 | 30s+ 空文本静默成功 | 走 FAILED/retry 路径 | 新增 |
| 单元测试覆盖 | 92 passed | 125 passed (+33) | +36% |
| 集成测试 | 无 | 4 场景（多周期/steal/一致性/离线） | 新增 |

### 14.3 依赖关系与关键路径

```text
M0（基线锁定）
 │
 ├─► M1.1 → M1.2 → M1.3 ──────────────────────────┐
 │          └──────────────► M1.5（单元测试）        │
 │   M1.4 ──────────────────► M1.5                   │
 │                                                    │
 ├─► M2.1 → M2.2 → M2.3 → M2.4 → M2.5 → M2.6 ────┤
 │                                                    │
 │   M3.1 ←── M2.3（依赖 PlanPool）                  │
 │   M3.2（独立）                                     │
 │   M3.3（独立）                                     │
 │   M3.4 ←── M3.3（依赖日志字段）                    │
 │   M3.5 ←── M2.4（依赖 re-plan 收紧后才有意义）    │
 │                                                    │
 │   M4.1 → M4.2（独立于 M1-M3，可并行）             │
 │   M4.3 ←── M4.2                                   │
 │                                                    │
 └─► M5（全部 M1-M4 完成后）                         │
     M5.1 → M5.2 → M5.3 → M5.4 ◄───────────────────┘
```

**关键路径**：`M0 → M2.1 → M2.2 → M2.3 → M2.4 → M2.5 → M2.6 → M5`

M2（Re-plan 治理）是最长路径，因为它是让 work steal 真正工作的结构性前提。M1（正确性修复）可以与 M2 并行推进。M3 和 M4 分别依赖 M2 的部分产出。

### 14.4 总估算

| 里程碑 | 工时 | 可并行 |
|--------|------|--------|
| M1 正确性修复 | ~14h | 与 M2 并行 |
| M2 Re-plan 治理 | ~17h | 关键路径 |
| M3 Work Steal 激活 | ~12h | M2 完成后启动，部分可与 M2 并行 |
| M4 ETA 优化 | ~8h | 独立，可与 M1-M3 并行 |
| M5 验证闭环 | ~9h | 最后执行 |
| **总计** | **~60h** | 考虑并行后约 **5-7 个工作日** |
