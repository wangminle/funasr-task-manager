# 渠道凭据配置参考

Phase 7 的详细配置步骤。

## 飞书凭据配置

1. 在[飞书开放平台](https://open.feishu.cn/)创建企业自建应用
2. 开通权限：`im:message`、`im:message:send_as_bot`、`im:resource`、`im:file`
3. 获取 `App ID` 和 `App Secret`
4. 写入 Agent 配置：

```bash
# OpenClaw 平台
cat >> ~/.openclaw/agents/{agent}/agent/.env << 'EOF'
FEISHU_APP_ID=cli_xxxxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
EOF

# 或通用环境变量
export FEISHU_APP_ID=cli_xxxxxxxxxxxx
export FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
```

5. 验证凭据有效：

```bash
curl -s -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal \
  -H "Content-Type: application/json" \
  -d '{"app_id": "'$FEISHU_APP_ID'", "app_secret": "'$FEISHU_APP_SECRET'"}'
# 应返回 {"code": 0, "tenant_access_token": "t-xxx", ...}
```

## 飞书实时通知配置（notify 子系统）

> 此配置用于 `send_user_notice()` 的 CLI fallback（`python -m cli notify send`）。
> OpenClaw 环境优先使用平台 `message` tool，CLI notify 作为降级保障。

### 方式一：通过 CLI config 命令配置（推荐）

```bash
cd 3-dev/src/backend

# 配置飞书应用凭据（与文件下载使用相同应用即可）
python -m cli config set notify.feishu_app_id "cli_xxxxxxxxxxxx"
python -m cli config set notify.feishu_app_secret "xxxxxxxxxxxxxxxxxxxxxxxx"

# 配置默认群聊 ID（Agent 发通知的目标群）
python -m cli config set notify.default_chat_id "oc_xxxxxxxxxxxxxxxxxxxxxxxx"

# （可选）配置默认回复消息 ID，用于话题内回复
python -m cli config set notify.default_reply_to "om_xxxxxxxxxxxxxxxxxxxxxxxx"
```

配置存储在 `~/.asr-cli.yaml`，结构如下：

```yaml
notify:
  feishu_app_id: cli_xxxxxxxxxxxx
  feishu_app_secret: xxxxxxxxxxxxxxxxxxxxxxxx
  default_chat_id: oc_xxxxxxxxxxxxxxxxxxxxxxxx
  default_reply_to: om_xxxxxxxxxxxxxxxxxxxxxxxx  # 可选
```

### 方式二：通过环境变量配置

```bash
export FEISHU_APP_ID=cli_xxxxxxxxxxxx
export FEISHU_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxx
export FEISHU_CHAT_ID=oc_xxxxxxxxxxxxxxxxxxxxxxxx
export FEISHU_REPLY_TO=om_xxxxxxxxxxxxxxxxxxxxxxxx  # 可选
```

> 环境变量优先级高于配置文件。Agent 启动脚本或 `.env` 文件中设置均可。

### 获取 chat_id 的方法

1. 将机器人加入目标群聊
2. 在群里发送一条消息
3. 通过飞书开放平台 API 调试台调用 `im/v1/messages` 查看群聊 ID
4. 或从 OpenClaw session 日志的 `toolResult` 中获取（`chatId` 字段）

### 验证 notify 配置

```bash
# 检查凭据是否有效
python -m cli notify auth-check
# 成功输出：飞书凭据有效 (app_id: cli_xxx, token 已缓存...)

# 发送测试消息
python -m cli notify send --text "🔔 通知系统测试消息"
# 成功输出：message_id=om_xxx...

# 带回复消息 ID 的测试（发送到话题内）
python -m cli notify send --text "话题内回复测试" --reply-to "om_xxxxxxxx"
```

### 故障排查

| 错误 | 原因 | 解决方案 |
|------|------|---------|
| `缺少飞书凭据` | 未配置 app_id/app_secret | 执行 `python -m cli config set notify.feishu_app_id ...` |
| `token 获取失败 (code=10003)` | app_secret 错误 | 在飞书开放平台重新获取 secret |
| `消息发送失败 (code=230001)` | 机器人未加入群聊 | 将机器人邀请到目标群 |
| `消息发送失败 (code=230002)` | chat_id 不正确 | 重新获取正确的 chat_id |

## 企业微信凭据配置

```bash
export WECOM_CORP_ID=wxxxxxxxxxxxxxxxxx
export WECOM_CORP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Slack 凭据配置

```bash
export SLACK_BOT_TOKEN=xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx
```

## 配置验证输出

```
✅ 渠道凭据已配置

  渠道:     飞书
  App ID:   cli_xxx...xxx（已验证）
  Token:    有效，2 小时后过期
  权限:     im:message ✅ / im:resource ✅ / im:file ✅
  Notify:   ✅ 已配置（chat_id: oc_xxx...xxx）

  Agent 现在可以自动从飞书下载用户发送的文件，并通过 notify 发送实时进度通知。
```
