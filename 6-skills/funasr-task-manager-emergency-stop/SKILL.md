---
name: funasr-task-manager-emergency-stop
description: >
  Emergency stop workflow for FunASR Task Manager. Use when the user asks to
  immediately stop running transcription tasks, release occupied server slots,
  clear zombie TRANSCRIBING/DISPATCHED segments, diagnose active slot usage, or
  recover from scheduler pollution after canceled batches.
---

# FunASR Task Manager 急停与 Slot 清理

> **适配项目版本**：V0.4.27-Build0475-20260517

本 Skill 用于处理运行中批次失控、服务器 slot 被僵尸任务占用、需要立即中止转写任务的运维场景。核心原则是：**先诊断，后确认，再通过后端 admin API/CLI 原子执行**。除非用户明确授权，不直接改数据库。

## 触发场景

- 用户说"立即停止所有任务"、"急停"、"清空 slot"、"释放 10097"。
- 批次取消后仍有 `TRANSCRIBING` / `DISPATCHED` 占用。
- 调度器明显被历史残留污染，需要先恢复容量再继续跑批量。
- 需要确认每台服务器真实 active slot 来源。

## 执行流程

### Step 1：确认作用域

优先选择最小作用域：

| 用户意图 | scope | 参数 |
|---------|-------|------|
| 只停当前批次 | `group` | `--group-id {task_group_id}` |
| 停全部运行任务 | `all` | 无 |
| 只诊断不停止 | 不执行 stop | 只跑 `active-slots` |

全局急停是破坏性操作，必须让用户明确确认。

### Step 2：只读诊断

先执行：

```bash
cd 3-dev/src/backend
python -m cli --output json admin active-slots
```

检查：

- `total_active_slots`
- `zombie_segments`
- 每台服务器的 `active_slots / max_concurrency`
- segment 的 `parent_status` 和 `is_zombie`

如果只是排查，不继续执行急停。

### Step 3：dry-run 预演

默认先 dry-run：

```bash
python -m cli --output json admin emergency-stop --scope all
```

或只停指定批次：

```bash
python -m cli --output json admin emergency-stop --scope group --group-id {task_group_id}
```

向用户报告：

- 将取消多少个任务：`tasks_to_cancel`
- 将释放多少个 segment：`segments_to_release`
- 急停前 slot 数：`active_slots_before`
- 急停前僵尸段数：`zombie_segments_before`

### Step 4：二次确认后执行

只有用户明确确认后才加 `--confirm`：

```bash
python -m cli --output json admin emergency-stop --scope all --confirm
```

指定批次：

```bash
python -m cli --output json admin emergency-stop --scope group --group-id {task_group_id} --confirm
```

禁止使用 SQL 直接更新任务，除非 admin API/CLI 不可用且用户明确授权数据库修复。

### Step 5：执行后复查

急停后必须再次执行：

```bash
python -m cli --output json admin active-slots
```

成功标准：

- `total_active_slots == 0`，或只剩用户明确允许继续运行的 scope 外任务。
- `zombie_segments == 0`。
- 目标服务器的 `active_slots` 已恢复到 0。

### Step 6：用户汇报

汇报必须包含：

- 已取消任务数。
- 已释放 segment 数。
- 急停前后 slot 对比。
- 是否仍有僵尸 segment。
- 后续建议：是否重跑失败项、是否恢复批量、是否继续查某台服务器。

## 错误处理

| 场景 | 处理 |
|------|------|
| admin API 401/403 | 提示需要 admin API Key，不尝试绕过 |
| dry-run 显示 0 任务但 active slot 不为 0 | 运行 `active-slots`，按僵尸 segment 异常上报 |
| `--confirm` 后仍有 slot | 立即复查明细，报告残留 task/segment ID |
| 后端不可达 | 不做数据库修复，先恢复后端或请求用户授权 |
| 用户只说"停一下"但未说明范围 | 默认询问 scope，不直接全局急停 |

## 禁止事项

- 不直接 `DELETE` 任务或 segment。
- 不绕过 admin API 批量写数据库。
- 不只停止监控子 Agent 却不取消后端任务。
- 不在未确认时执行 `--confirm`。
