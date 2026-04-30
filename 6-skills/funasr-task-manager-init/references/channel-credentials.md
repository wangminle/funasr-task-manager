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

  Agent 现在可以自动从飞书下载用户发送的文件。
```
