# 渠道文件获取 API 参考

Agent 通过聊天渠道接收用户文件时，需要先从渠道平台下载文件到本地，再上传到 ASR 后端。本文档提供各渠道的鉴权和文件操作 API 参考。

> **核心原则**：凭据必须预配置，不要在运行时探索。运行时探索鉴权路径（grep 源码、find 配置文件）是 8:14→8:23 延迟的主因。

## 飞书 / Lark

### 鉴权

```
POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
Content-Type: application/json

{
  "app_id": "{FEISHU_APP_ID}",
  "app_secret": "{FEISHU_APP_SECRET}"
}

→ {"tenant_access_token": "t-xxx", "expire": 7200}
```

- Token 有效期 2 小时，应缓存复用
- 凭据来源：环境变量 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 或 Agent 配置文件

### 下载消息中的文件

```
GET https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file
Authorization: Bearer {tenant_access_token}

→ 二进制文件流
```

- `message_id`：从消息事件 webhook 或消息列表 API 获取
- `file_key`：消息体中的文件标识（`msg.content.file_key`）
- `type`：`file`（普通文件）或 `image`（图片）
- ⚠️ **大文件限制**：超过约 50MB 时返回错误码 `234037: Downloaded file size exceeds limit`，需改用 HTTP Range 分块下载（每块 10MB），详见 intake SKILL 的"飞书大文件回退"部分

### 发送文件到会话

```
# Step 1: 上传文件到飞书
POST https://open.feishu.cn/open-apis/im/v1/files
Authorization: Bearer {tenant_access_token}
Content-Type: multipart/form-data

file_type=stream
file_name={filename}
file=@{local_path}

→ {"file_key": "file_v2_xxx"}

# Step 2: 发送文件消息
# ⚠️ 必须在 URL 中指定 receive_id_type，否则飞书会以参数错误拒绝请求
POST https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id
Authorization: Bearer {tenant_access_token}
Content-Type: application/json

{
  "receive_id": "{chat_id}",
  "msg_type": "file",
  "content": "{\"file_key\": \"file_v2_xxx\"}"
}

# receive_id_type 可选值：
#   chat_id  — 群聊 ID（最常用）
#   open_id  — 用户 open_id（私聊）
#   user_id  — 用户 user_id（私聊）
#   union_id — 用户 union_id（私聊）
```

### 所需权限（飞书应用配置）

- `im:message` — 读取消息
- `im:message:send_as_bot` — 发送消息
- `im:resource` — 下载消息中的文件/图片
- `im:file` — 上传文件

## 企业微信

### 鉴权

```
GET https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={WECOM_CORP_ID}&corpsecret={WECOM_CORP_SECRET}

→ {"access_token": "xxx", "expires_in": 7200}
```

### 下载临时素材

```
GET https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token={access_token}&media_id={media_id}

→ 二进制文件流
```

- `media_id`：从消息回调中获取
- 临时素材 3 天有效

### 发送文件到会话

```
# Step 1: 上传临时素材
POST https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={access_token}&type=file
Content-Type: multipart/form-data

media=@{local_path}

→ {"media_id": "xxx"}

# Step 2: 发送文件消息
POST https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}
Content-Type: application/json

{
  "touser": "{user_id}",
  "msgtype": "file",
  "agentid": {agent_id},
  "file": {"media_id": "{media_id}"}
}
```

## Slack

### 鉴权

Slack Bot 使用 OAuth Token（`xoxb-...`），在创建 Slack App 时获取，无需运行时获取。

### 下载文件

```
# 从消息事件中获取文件信息
GET https://slack.com/api/files.info?file={file_id}
Authorization: Bearer {SLACK_BOT_TOKEN}

→ {"file": {"url_private_download": "https://files.slack.com/..."}}

# 下载文件（需要 Authorization header）
GET {url_private_download}
Authorization: Bearer {SLACK_BOT_TOKEN}

→ 二进制文件流
```

### 发送文件到 channel

```
POST https://slack.com/api/files.uploadV2
Authorization: Bearer {SLACK_BOT_TOKEN}
Content-Type: multipart/form-data

channel_id={channel_id}
file=@{local_path}
filename={filename}
initial_comment={摘要消息}
```

## Discord

### 鉴权

Discord Bot 使用 Bot Token，在 Developer Portal 创建 Bot 时获取。

### 下载文件

Discord 消息附件直接包含 CDN URL，无需额外 API 调用：

```
# 从消息事件中获取附件 URL
attachment.url = "https://cdn.discordapp.com/attachments/{channel_id}/{attachment_id}/{filename}"

# 直接下载（无需鉴权）
curl -o "$TMPDIR/{filename}" "{attachment.url}"
```

### 发送文件到 channel

```
POST https://discord.com/api/v10/channels/{channel_id}/messages
Authorization: Bot {DISCORD_BOT_TOKEN}
Content-Type: multipart/form-data

content={摘要消息}
files[0]=@{local_path}
```

## 性能优化建议

| 优化点 | 说明 |
|--------|------|
| Token 缓存 | 飞书/企微 token 有效期 2 小时，缓存到文件或内存，避免每次请求 |
| 并行下载 | 批量文件应并行下载，不串行等待 |
| 流式转发 | 大文件可边下载边上传（streaming），减少磁盘占用 |
| 预鉴权 | Agent 启动时即获取 token，不等到收到文件才鉴权 |
| 进度通知 | 下载大文件时通知用户"正在下载..."，避免用户以为卡死 |

## 凭据配置位置

凭据应在 `funasr-task-manager-init` Phase 7 中配置。推荐存储位置：

| Agent 平台 | 配置文件 | 格式 |
|-----------|---------|------|
| OpenClaw | `~/.openclaw/agents/{agent}/agent/auth-profiles.json` | JSON |
| Hermes | `~/.hermes/agents/{agent}/credentials.yaml` | YAML |
| 环境变量 | `.env` 或 shell profile | `FEISHU_APP_ID=xxx` |
