---
name: funasr-task-manager-result-delivery
description: >
  Monitor transcription tasks and deliver results with quality checks.
  Use when: task_id or task_group_id needs monitoring or result return,
  user asks to export txt/json/srt/zip, user requests quality check on
  transcription output, or results need re-delivery for an existing task.
---

# 结果交付与质量初筛

`funasr-task-manager-result-delivery` 是运行时闭环的出口 Skill。它负责在任务创建后监控状态、拉取结果、做基础质量检查，并把结果以合适形式返回到 channel。

## 触发条件

### 自动触发

- `funasr-task-manager-channel-intake` 成功创建任务后，传入 `task_id(s)` 或 `task_group_id`
- 批量任务提交成功后，需要持续回报完成进度

### 用户显式触发

- 用户说"把这个任务结果发我""重新导出字幕""下载 json""检查这批结果质量"
- 用户提供 `task_id` 或 `task_group_id`

### 关键词

`结果` / `导出` / `下载` / `字幕` / `srt` / `json` / `txt` / `质量` / `乱码` / `空文本` / `result` / `export` / `download`

### 不触发

- 新文件上传请求（→ `funasr-task-manager-channel-intake`）
- benchmark 请求（→ `funasr-task-manager-server-benchmark`）
- 普通服务器注册请求

## 执行流程

### Phase 1：接收任务上下文

- 输入：`task_id(s)` 或 `task_group_id`
- 输入：期望格式 `txt` / `json` / `srt` / `zip`
- 输入：channel 回传能力（可发文本/可发文件/大小限制）
- 缺少任务标识 → 向用户询问

### Phase 2：监控任务状态

- 单任务：`GET /api/v1/tasks/{task_id}`
- 批量：`GET /api/v1/task-groups/{group_id}`
- 可选：`GET /api/v1/tasks/{task_id}/progress`（SSE 实时进度）
- 状态变化时回报关键节点：
  - `PREPROCESSING` → "文件预处理中..."（长音频会自动 VAD 切分）
  - `QUEUED` → "等待调度..."
  - `TRANSCRIBING` → "正在转写..."
  - `SUCCEEDED` → "转写完成！"
  - `FAILED` → "转写失败：{原因}"
- 分段任务：响应中包含 `segments` 字段（`total`/`succeeded`/`failed`/`pending`/`active`），可用于汇报 "3/5 段已完成"
- 批量任务：定期汇报 "{completed}/{total} 已完成"
- 超时后给出当前状态和下一步建议，不盲目取消任务

### Phase 3：拉取结果

- 单任务：`GET /api/v1/tasks/{task_id}/result?format=txt`（`format` 参数为 `json` | `txt` | `srt` 三选一）
- 批量：`GET /api/v1/task-groups/{group_id}/results?format=zip`（`format` 参数为 `json` | `txt` | `srt` | `zip` 四选一）
- 无成功任务 → 返回失败摘要，不假装成功

### Phase 4：质量初筛

- **空文本** → 标记异常，建议检查音频是否静音或语言/模型是否匹配
- **明显乱码** → 标记异常，建议检查编码、音频质量或输入格式
- **文本过短** → 提醒可能是静音、噪声或截断
- **批量任务** → 汇总成功/失败/空文本数量
- **正常** → 进入交付

### Phase 5：结果交付

- 短文本（默认 < 500 字）→ 直接发送到 channel
- 长文本 → 发送文件 + 摘要
- SRT/JSON → 优先作为附件返回
- 批量 → 发送 zip 或汇总文件
- 安全模式 → 见下方「安全模式交付 Fallback」

## 输出摘要模板

### 单任务成功

```
✅ 转写完成

  文件: {filename}
  状态: SUCCEEDED
  服务器: {assigned_server_id}
  音频时长: {duration_sec}s
  处理耗时: {elapsed_sec}s
  RTF: {rtf}
  文本长度: {text_length} 字
  结果格式: txt/json/srt

  质量提示:
  - 文本非空
  - 未发现明显乱码
```

### 批量任务汇总

```
✅ 批量转写完成

  批次: {task_group_id}
  成功: {succeeded}/{total}
  失败: {failed}/{total}
  空文本: {empty_count}
  总耗时: {total_elapsed}

  文件明细:
  {per_file_table}

  结果已打包，可通过以下方式获取:
  - zip: GET /api/v1/task-groups/{group_id}/results?format=zip
  - 单文件: GET /api/v1/tasks/{task_id}/result?format=txt
```

### 任务失败

```
❌ 转写失败

  文件: {filename}
  任务: {task_id}
  状态: FAILED
  原因: {error_message}

  建议:
  - {suggestion}
```

## 失败处理规范

| 场景 | Agent 应做的事 | 不应做的事 |
|------|--------------|----------|
| 任务仍在运行 | 告知当前状态和预计等待方式 | 重复创建任务 |
| 任务失败 | 返回 `error_message`，并建议是否重试 | 隐藏失败原因 |
| 没有成功任务 | 返回批次失败摘要 | 返回空 zip 当作成功 |
| 文本为空 | 标记质量异常 | 直接说"转写成功" |
| 用户要求安全返回 | 按安全模式 fallback 处理（见下方说明） | 直接明文贴回 channel |
| 用户要求重新导出 | 重新拉取并按新格式导出 | 拒绝或重新创建任务 |

## 结果格式说明

| 格式 | 端点参数 | 内容 |
|------|---------|------|
| `txt` | `?format=txt` | 纯文本转写结果 |
| `json` | `?format=json` | JSON 结构（含时间戳、置信度等元数据） |
| `srt` | `?format=srt` | SRT 字幕格式（含时间轴） |
| `zip` | `?format=zip`（仅批量） | 打包所有任务的结果文件 |

## 安全模式交付 Fallback

`funasr-task-manager-secure-ingest` **当前尚未创建**（P2）。在此 Skill 创建之前，结果交付阶段遇到安全/敏感场景时，Agent **不可假装具备加密返回能力**。

**当前阶段的处理规则：**

1. 如果任务在创建时已被 `channel-intake` 标记为敏感/加密场景：
   - 结果交付前**必须再次确认**用户是否接受明文返回
   - 明确告知："当前系统尚未支持加密结果输出。转写结果将以明文形式返回，请确认是否继续。"
   - 用户确认 → 以明文方式交付，在结果中标注"⚠ 以明文返回，请注意信息安全"
   - 用户拒绝 → 建议通过 CLI 在本地获取结果：`python -m cli task result <task_id> --format txt --save ./private-results/result.txt`
2. 如果用户在结果交付阶段首次提出保密/加密要求：
   - 告知："当前系统不支持加密结果输出。"
   - 提供替代方案（同上 CLI 本地下载）
3. **严禁**：
   - 静默以明文返回敏感任务结果
   - 假装结果已加密
   - 将转写全文写入日志

**未来 `funasr-task-manager-secure-ingest` 上线后**，本 Skill 应将安全模式结果交付切换到该规程处理。

## 与其他 Skill 的协作

| 协作场景 | Skill | 状态 | 说明 |
|---------|-------|------|------|
| 接收任务上下文 | `funasr-task-manager-channel-intake` | ✅ 可用 | intake 在 Phase 5 交接 task_id(s) 和用户偏好 |
| 安全模式结果 | `funasr-task-manager-secure-ingest` | ⏳ 未创建 | **当前 fallback**：见上方安全模式交付说明 |

## 相关文件

- `references/result-formats.md`：txt/json/srt/zip 导出规则
- `references/quality-checklist.md`：空文本、乱码、异常短文本检查规则
- `references/response-templates.md`：结果回传模板
