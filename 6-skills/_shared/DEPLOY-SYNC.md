# 部署同步指南

> 本文件用于跟踪 OpenClaw workspace 与源仓库之间的同步状态。
> 每次同步后更新下方记录表。

---

## 同步记录

| 同步时间 | 源仓库 commit | 操作人 | 同步范围 | 备注 |
|---------|--------------|--------|---------|------|
| _(首次同步时填写)_ | — | — | — | — |

---

## 同步 Checklist（4 步操作）

每次源仓库有 Skill 或 CLI 变更时，按以下步骤同步到 OpenClaw workspace：

### Step 1：确认源仓库最新版本

```bash
cd /path/to/funasr-task-manager
git log -1 --format="%H %s"
```

记录 commit hash，后续写入同步记录。

### Step 2：同步 Skills 到 workspace

```bash
WORKSPACE_NAME="asr"  # 根据实际 workspace 名称
WORKSPACE_SKILLS="$HOME/.openclaw/workspace-$WORKSPACE_NAME/skills"
REPO_SKILLS="/path/to/funasr-task-manager/6-skills"

for skill_dir in "$REPO_SKILLS"/funasr-task-manager-*/; do
  skill_name=$(basename "$skill_dir")
  rm -rf "$WORKSPACE_SKILLS/$skill_name"
  cp -r "$skill_dir" "$WORKSPACE_SKILLS/$skill_name"
  echo "✅ $skill_name"
done

# 同步共享文件
WORKSPACE_ROOT="$HOME/.openclaw/workspace-$WORKSPACE_NAME"
cp "$REPO_SKILLS/_shared/ASR-WORKFLOW.md" "$WORKSPACE_ROOT/ASR-WORKFLOW.md"
cp "$REPO_SKILLS/_shared/CHANNEL-NOTIFICATION.md" "$WORKSPACE_ROOT/CHANNEL-NOTIFICATION.md"
cp "$REPO_SKILLS/_shared/DEPLOY-SYNC.md" "$WORKSPACE_ROOT/DEPLOY-SYNC.md"
echo "✅ 共享文件已同步"
```

### Step 3：同步 CLI 代码（如果 workspace 需要 `cli notify`）

```bash
WORKSPACE_ROOT="$HOME/.openclaw/workspace-$WORKSPACE_NAME"
REPO_BACKEND="/path/to/funasr-task-manager/3-dev/src/backend"

# 同步整个 CLI 模块（先删后拷贝，避免 cp -r 创建嵌套 cli/cli/）
mkdir -p "$WORKSPACE_ROOT/3-dev/src/backend"
rm -rf "$WORKSPACE_ROOT/3-dev/src/backend/cli"
cp -r "$REPO_BACKEND/cli" "$WORKSPACE_ROOT/3-dev/src/backend/cli"
cp "$REPO_BACKEND/pyproject.toml" "$WORKSPACE_ROOT/3-dev/src/backend/pyproject.toml" 2>/dev/null || true

# 安装依赖（如需要）
cd "$WORKSPACE_ROOT/3-dev/src/backend"
pip install -e . 2>/dev/null || pip install -r requirements.txt 2>/dev/null || true

# 验证
python -m cli notify --help
```

### Step 4：更新同步记录

在上方"同步记录"表中追加一行，记录：

- 同步时间（ISO 8601）
- 源仓库 commit hash
- 操作人
- 同步范围（Skills / CLI / 全部）
- 备注（如有特殊变更）

---

## 同步触发条件

以下变更应触发同步：

| 变更类型 | 触发文件 | 影响范围 |
|---------|---------|---------|
| Skill 逻辑变更 | `6-skills/funasr-task-manager-*/SKILL.md` | Step 2 |
| 通知规范变更 | `6-skills/_shared/CHANNEL-NOTIFICATION.md` | Step 2 |
| CLI notify 功能变更 | `3-dev/src/backend/cli/commands/notify.py` | Step 3 |
| CLI 新增子命令 | `3-dev/src/backend/cli/` | Step 3 |
| ASR 工作流变更 | `6-skills/_shared/ASR-WORKFLOW.md` | Step 2 |
| 部署同步指南变更 | 本文件 | Step 2 |

---

## 版本比对

`funasr-task-manager-init` Phase 7.5 会自动执行版本比对。手动比对命令：

```bash
WORKSPACE_SKILLS="$HOME/.openclaw/workspace-asr/skills"
REPO_SKILLS="/path/to/funasr-task-manager/6-skills"

for skill_dir in "$REPO_SKILLS"/funasr-task-manager-*/; do
  skill_name=$(basename "$skill_dir")
  if [ ! -d "$WORKSPACE_SKILLS/$skill_name" ]; then
    echo "❌ $skill_name 未安装"
  else
    diff -rq "$skill_dir" "$WORKSPACE_SKILLS/$skill_name" 2>/dev/null
    [ $? -eq 0 ] && echo "✅ $skill_name" || echo "⚠️ $skill_name 有差异"
  fi
done
```
