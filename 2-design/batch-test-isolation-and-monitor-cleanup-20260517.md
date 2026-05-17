# 批量测试独占窗口与 Monitor 清理方案-20260517

> 日期：2026-05-17
> 背景：round-8 / round-9 / round-10 本地批量测试
> 范围：`runtime/agent-local-batch/`、`local-batch-transcribe`、`batch-monitor`

## 1. 日志结论

round-8、round-9、round-10 三轮均为 45 个文件，数据库记录全部 `SUCCEEDED`：

| 批次 | task_group_id | wall-clock |
|------|---------------|------------|
| round-8 | `01KRT8DGJY55RGSSHFJ7QJX25F` | 约 301 秒 |
| round-9 | `01KRT9JGEXV0KRRP560V60NWT8` | 约 363 秒 |
| round-10 | `01KRTA2PXY9FVQNT628NVG9M1D` | 约 332 秒 |

慢点集中在长任务和少数整文件任务，符合 FunASR 节点被外部调用抢占后 RTF 变差的表现。

## 2. 问题 A：外部直连拖慢批量

task-manager 只能调度通过自身 API 创建的任务。若其它机器直接访问 FunASR `10095` / `10096` / `10097`，这些请求不会进入 task-manager 队列，后端无法让对方等待。

可执行规则：

1. 本地批量测试前创建 `runtime/agent-local-batch/locks/asr-exclusive-{batch_id}.json`。
2. Agent 管理的入口发现 active exclusive lock 时等待，不提交新任务。
3. 批量结束后释放并归档 lock。
4. 真正防止外部直连需要网关或防火墙：只允许 task-manager 访问 10095-10097，其它调用方改走 task-manager API。

## 3. 问题 B：monitor 残留

新增 monitor state：

```text
runtime/agent-local-batch/monitors/{batch_id}.json
runtime/agent-local-batch/archive/monitors/{batch_id}.json
```

规则：

1. 子 Agent ack 后写入 monitor state。
2. 每次轮询刷新 `last_poll_at`、`last_notice_at` 和 totals。
3. 全部终态后发送汇总，标记 `completed` 并移动到 archive。
4. 下一轮 local-batch Phase 0 必须扫描 monitors；若 group 已全部 `SUCCEEDED` / `FAILED` / `CANCELED`，主动归档并关闭仍存在的子 Agent。

## 4. 已同步文档

- `6-skills/funasr-task-manager-local-batch-transcribe/SKILL.md`
- `6-skills/funasr-task-manager-batch-monitor/SKILL.md`
- `6-skills/_shared/ASR-WORKFLOW.md`
