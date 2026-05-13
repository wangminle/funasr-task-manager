# V0.4.22 调度坍缩修复方案

> 日期：2026-05-13
> 适用版本：V0.4.22
> 背景材料：
> - `1-discussion/Agent实际运行记录/20260512/batch-run-report-20260512.md`（GPU 掩盖问题的报告）
> - `1-discussion/Agent实际运行记录/20260512/v0.4.21-batch-run-report-20260512.md`（暴露调度坍缩的报告）
> - `2-design/dynamic-dispatch-work-steal-optimization-20260511.md`

## 1. 问题描述

V0.4.21 在两组不同 agent 上的批量转写测试暴露了严重的调度退化：

| 报告 | 测试集 | 服务器配置 | 结果 | 是否暴露问题 |
|------|--------|-----------|------|------------|
| batch-run-report | 46 wav, 35.5h | 2 CPU + 1 GPU (20 slot) | 34min (-55%) | 没暴露——GPU 太快，掩盖了调度缺陷 |
| v0.4.21-batch-run-report | 50 mp4, 12.15h | 3 台 CPU (7 slot) | 9m39s (+78.7%) | 暴露了——同质 CPU 下调度坍缩直接伤害吞吐 |

第二份报告的关键证据：首次分配正确（3 台 46:27:11），但 remote-01 先完成 1 个任务后，后续 77 个任务全部分给了 remote-01，`servers_used=1` 持续到结束。

## 2. 根因分析

四个联动的问题，按因果链排序：

### 根因 1（核心）：`_allocate_quotas` 只看当前空闲 slot

```python
# 修复前
servers_with_slots = [s for s in servers
                      if max(s.max_concurrency - s.running_tasks, 0) > 0]
```

配额分配（结构性 backlog 分布）被绑死在瞬时 slot 可用性上。remote-01 先释放 1 slot → 它是唯一 `servers_with_slots` 成员 → 独占 77 个任务。

### 根因 2：`_clear_plan_pool` 过于激进地重置 `_planned_available_server_ids`

两条路径制造虚假的 `servers_changed`：

- **路径 A**：task success/failure 回调在 plan pool 为空时调用 `_clear_plan_pool()` → `_planned_available_server_ids = frozenset()` → 下一轮 `servers_changed = True`
- **路径 B**：`schedule_batch` 返回空（全满载）时调用 `_clear_plan_pool()` → 同上

形成自增强循环：`_clear → _planned_ids = ∅ → servers_changed → replan → schedule_batch(无空槽) → [] → _clear → ...`

### 放大因素 3：`servers_changed` 无 cooldown

`servers_changed` 是唯一不受 `REPLAN_COOLDOWN_SEC` 约束的 replan 触发器，10 分钟内触发了 312 次。

### 补偿失效 4：`work_steal` 门槛过高

`min_gain = max(10.0, estimated_duration * 0.15)` 在同质 CPU 场景下 improvement ≈ 0，work steal 完全失效。

## 3. 修复方案

### Fix 1a：`_allocate_quotas` 使用所有在线服务器

去掉 `servers_with_slots` 过滤，所有在线服务器按总能力（`max_concurrency / rtf`）分配配额。

**文件**：`scheduler.py` `_allocate_quotas()`

### Fix 1b：`schedule_batch` 为满载服务器创建虚拟槽位

为已占用的 slot 创建 `ServerSlot`，`earliest_free` 按预估完成时间错开排列。EFT 算法自然把 backlog 任务排到这些"即将空闲"的 slot 上，空闲 slot（`earliest_free=0`）仍然优先。

**文件**：`scheduler.py` `schedule_batch()`

预估时间公式：`est_per_slot = avg_task_dur(180s) × base_rtf + DEFAULT_OVERHEAD`，错开系数 `(occupied - i) / occupied`。

### Fix 2a：replan 返回空时保留已有 plan pool

当 `schedule_batch` 返回空但 plan pool 非空时，保留现有计划不清空。所有分支都更新 `_planned_available_server_ids = current_available_ids`，避免下一轮产生虚假 `servers_changed`。

**文件**：`task_runner.py` replan 分支

### Fix 2b：回调路径不重置 `_planned_available_server_ids`

`_clear_plan_pool()` 增加 `reset_server_ids` 参数（默认 `True`）。回调路径（task success/failure、segment failure、overcommit rollback）传入 `reset_server_ids=False`。只有 "没有在线服务器"（`not servers`）才完全重置。

**文件**：`task_runner.py` `_clear_plan_pool()` 及其 5 个调用点

### Fix 3：`servers_changed` 加 cooldown 门控

新增 `SERVERS_CHANGED_COOLDOWN_SEC = 3.0`，`servers_changed` 也受冷却约束。首次 replan（`_planned_available_server_ids` 为空）仍然立即触发。

**文件**：`task_runner.py` replan 判断逻辑

### Fix 4：`work_steal` 门槛下调

| 类型 | 修复前 | 修复后 |
|------|--------|--------|
| segment | `max(5.0, est × 0.10)` | `max(2.0, est × 0.05)` |
| task | `max(10.0, est × 0.15)` | `max(3.0, est × 0.05)` |

**文件**：`task_runner.py` `_find_steal_candidate()`

## 4. 预期效果

以第二份报告的场景（3 台同质 CPU，7 slot，50 mp4）为例：

| 维度 | 修复前 (V0.4.21) | 修复后 (V0.4.22) |
|------|-----------------|-----------------|
| 首次分配 | 3 台 46:27:11 ✅ | 3 台 46:27:11 ✅ |
| remote-01 先释放 1 slot 后 | 配额塌到 remote-01 独占 77 | 配额仍按 4:2:1 分配 |
| replan 风暴 | 312 次 / 10 min | 受 cooldown 正常控制 |
| servers_used | 1（持续到结束） | 3（持续到结束） |
| work steal 有效性 | 同质场景完全失效 | 门槛降低后可生效 |

## 5. 代码审查后追加修复

### Fix 5：canonical WAV 缓存路径碰撞（P1）

`audio_preprocessor.py` 的 `_canonical_output_path()` 只用 `src.stem` 生成缓存路径，
当不同目录下存在同名文件（如两个用户都上传 `meeting.mp4`）时会复用错误音频。

修复：在文件名中加入源路径 SHA-256 前 12 位 hash：

```python
path_hash = hashlib.sha256(str(src.resolve()).encode()).hexdigest()[:12]
return settings.temp_dir / f"{src.stem}_{path_hash}_canonical.wav"
```

**文件**：`audio_preprocessor.py` `_canonical_output_path()`

### Fix 6：虚拟槽等待时间未计入偷取收益（P2）

`_find_steal_candidate()` 的 `source_remaining` 只累加了 `estimated_duration`，
没有包含虚拟槽的等待时间（`estimated_start > 0`）。在"一台满载、另一台空闲"的场景下
偷取收益被低估，空闲服务器不会偷取本可更早完成的任务。

修复：用 `max(duration_based, decision.estimated_finish)` 确保虚拟槽等待时间被计入：

```python
source_remaining = max(duration_based, decision.estimated_finish)
```

**文件**：`task_runner.py` `_find_steal_candidate()`

### Fix 7：work-steal 跳过受限 segment 继续寻找候选（P2）

当最佳偷取候选属于某个已达到 `segment_max_parallel_per_task` 上限的 segment 时，
旧逻辑会直接结束当前空闲服务器的偷取循环，导致后续可偷取任务被该候选阻塞。

修复：为单轮偷取维护 `skipped_steal_ids`，遇到受限 segment 时临时排除该候选，
继续调用 `_find_steal_candidate()` 查找其它任务。

**文件**：`task_runner.py` work-steal 分支与 `_find_steal_candidate()`

### Fix 8：完全空闲服务器计入 `idle_slot_seconds`（P2）

`idle_slot_seconds` 原本只从已有任务/segment 分配记录收集服务器 ID，
当批次期间存在完全未分配任务的可用服务器时，会漏算这些服务器的空闲 slot 时间。

修复：优先按当前 `ONLINE + enabled` 的所有服务器汇总 `max_concurrency`；
若没有在线启用服务器，再回退到历史分配记录中的服务器集合。

**文件**：`task_groups.py` `_compute_idle_slot_seconds()`

### Fix 9：未来虚拟槽任务不得抢占当前空闲槽（P2）

虚拟占用槽引入后，`PlanPool` 仍按 `estimated_finish` 排序。若未来槽上的短任务
finish 早于当前空闲槽上的长任务，`pop_dispatchable()` 会先弹出未来任务，
让它抢占当前空闲槽，破坏 LPT/EFT 的首次派发意图。

修复：`pop_dispatchable()` 优先派发 `estimated_start <= IMMEDIATE_START_TOLERANCE`
的任务；若队列中已经没有 immediate 任务，则允许后续补位派发继续推进。

**文件**：`scheduler.py` `PlanPool.pop_dispatchable()`

## 6. 测试验证

- `test_scheduler.py`：78 passed（含更新的 `test_running_tasks_reduce_available_slots`、`test_all_slots_occupied_still_plans_backlog` 和 `test_pop_dispatchable_skips_future_virtual_slot_items`）
- `test_task_runner_dispatch.py`：41 passed（含更新的 `test_plan_cleared_on_task_success_when_pool_empty`）
- 调度相关合并验证：`test_scheduler.py + test_task_runner_dispatch.py` 共 119 passed
- `test_m5_integration.py`：4 passed
- canonical/preprocessor 相关测试：17 passed
- 全量单元测试：665 passed / 4 failed（全部为预先存在的无关问题）
- 全量集成测试：86 passed / 2 failed（全部为预先存在的无关问题）
