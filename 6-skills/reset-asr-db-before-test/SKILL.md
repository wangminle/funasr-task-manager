---
name: reset-asr-db-before-test
description: Use when preparing a clean backend database state for local debugging, pytest, Playwright E2E, or repeated regression runs in the funasr-task-manager repository. Trigger when task data must be wiped, the SQLite backend must be rebuilt after a failed run, or server configuration should be preserved or reseeded before tests.
---

# Reset ASR DB Before Test

在这个仓库里，需要在测试前把后端 SQLite 数据库恢复到干净状态时使用本技能。

本技能显式针对 `3-dev/src/backend/data/` 工作，而不是仓库根目录的历史 `data/` 目录。后者通常是从错误工作目录启动后端时产生的遗留数据。

这个技能封装的是一个可执行脚本，而不是已经注册好的聊天斜杠命令。实际脚本位于 `scripts/` 目录。手动执行时，直接运行下面的命令：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py
```

在 macOS 或 Linux 上如果默认解释器是 Python 3，请优先使用：

```bash
python3 6-skills/reset-asr-db-before-test/scripts/reset_db.py
```

## 适用场景

- 本地调试前，需要清空任务、结果和临时文件
- 跑 pytest、CLI 回归、浏览器 E2E 前，需要可重复的数据库初始状态
- 上一次测试中断，导致 `3-dev/src/backend/data/asr_tasks.db` 缺失或状态不可信
- 需要保留已有 ASR 服务器节点配置，但重新生成任务数据库
- 需要彻底重置服务器节点配置并重新插入默认测试节点

## 默认行为

默认执行：

1. 如果旧数据库存在，则先备份到 `3-dev/src/backend/data/backups/`
2. 清空 `results/` 和 `temp/`
3. 删除并重建数据库
4. 运行 Alembic 迁移到最新版本
5. 默认保留已有服务器配置
6. 默认不删除 `uploads/` 中的音视频文件
7. 默认不插入新的测试服务器，除非显式指定 `--reset-servers`

如果数据库文件原本不存在，脚本会直接创建一份新的空库并完成迁移，不会因为“找不到旧库”而失败。

## Dry Run 评估

如果只想评估当前 backend data 的状态，而不真的执行清理，使用：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py --dry-run
```

这个模式会输出：

- 当前服务器配置摘要
- SQLite 数据库相关文件数量和大小
- `results/`、`temp/`、`uploads/` 目录的文件数量和大小
- 当前任务总数、状态分布、最近任务摘要
- 按当前重置逻辑预计可释放的空间

如果希望把 `uploads/` 也纳入估算，可以组合：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py --dry-run --clear-uploads
```

注意：`--dry-run` 只读取和评估，不会触发备份、删库、迁移或目录清理。

如果仓库根目录已经存在顶层 `data/` 目录，`--dry-run` 不会扫描它；需要先确认你要处理的是推荐运行目录 `3-dev/src/backend/data/` 还是历史遗留目录。

## 常用命令

基础重置：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py
```

重置服务器配置并插入 3 台默认测试节点：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py --reset-servers
```

删除上传文件并跳过确认：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py --clear-uploads --force
```

跳过备份以加快执行：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py --no-backup
```

只做容量和状态评估：

```bash
python 6-skills/reset-asr-db-before-test/scripts/reset_db.py --dry-run
```

## 参数规则

- `--no-backup`: 仅跳过数据库备份，不影响重建和清理
- `--dry-run`: 只做评估，不执行任何清理或迁移
- `--reset-servers`: 不保留旧服务器配置，改为走默认测试服务器种子数据
- `--skip-seed-servers`: 只在 `--reset-servers` 时有效，表示重置后不插入默认测试节点
- `--clear-uploads`: 删除上传目录中的文件。未配合 `--force` 时会二次确认
- `--force`: 跳过危险操作确认，适合 CI 或非交互环境

## 执行结果

脚本输出 JSON，字段示例：

```json
{
  "status": "success",
  "message": "测试数据库重置完成，已准备好干净测试环境",
  "data": {
    "backup_path": "/abs/path/to/asr_tasks_test_backup_20260401_123045.db",
    "servers_preserved": 2,
    "database_recreated": true,
    "seed_data_inserted": false
  }
}
```

dry-run 输出会额外包含 `servers`、`database_files`、`tasks`、`results`、`temp`、`uploads`、`estimated_savings` 和 `summary`。

## 安全约束

- 这是测试辅助技能，不要用于生产数据库
- `--clear-uploads` 会删除上传文件，默认要求确认
- 只有显式指定 `--reset-servers` 才会清空旧服务器配置

## 相关文件

- `scripts/reset_db.py`: 实际执行脚本