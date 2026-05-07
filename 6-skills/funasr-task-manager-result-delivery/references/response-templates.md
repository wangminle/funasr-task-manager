# 结果回传模板（强制格式）

> Agent 必须严格按以下模板拼接发送，所有动态字段来自 API 返回值和任务上下文，不由大模型自由生成。

## 进度通知

### 状态变化通知（仅在状态变化时发送一次）

```
⏳ {original_filename} — {status_description}
```

`status_description` 映射表：

| 状态 | status_description |
|------|-------------------|
| PREPROCESSING | 文件预处理中... |
| QUEUED | 等待调度... |
| DISPATCHED | 已分配服务器，准备转写... |
| TRANSCRIBING | 正在转写... |
| SUCCEEDED | 转写完成！ |
| FAILED | 转写失败：{error_message} |

### 分段进度

```
⏳ {original_filename} — 正在转写...（{succeeded}/{total} 段已完成）
```

### 批量进度

```
📊 批量进度: {completed}/{total} 已完成（成功 {succeeded} / 失败 {failed} / 处理中 {processing}）
```

## 单任务结果

### 成功

```
✅ 转写完成

  文件名:   {original_filename}
  音频时长: {duration_human}（{duration_sec}s）
  音频格式: {file_format}
  转写耗时: {elapsed_sec}s（RTF: {rtf}）
  文本长度: {text_length} 字
  结果文件: {result_filename}

  📎 结果文件已发送，请查收。
```

**字段计算规则**：

- `duration_human`：从 `duration_sec` 计算
  - >= 3600 → `{h}h {m}m {s}s`
  - >= 60 → `{m}m {s}s`
  - < 60 → `{s}s`
- `file_format`：原始文件扩展名去点号后大写（`.mp4` → `MP4`）
- `elapsed_sec`：`completed_at - started_at`，取整秒
- `rtf`：`elapsed_sec / duration_sec`，保留 2 位小数
- `text_length`：`len(result_txt.strip())`
- `result_filename`：`{original_filename 去扩展名}_result.txt`，必须从当前任务的 `file_name` / `original_filename` 重新计算，禁止复用上一轮临时文件名

### 失败

```
❌ 转写失败

  文件名:   {original_filename}
  任务 ID:  {task_id}
  失败原因: {error_message}

  建议:
  - {suggestion}
```

`suggestion` 映射表：

| error_code / 特征 | suggestion |
|-------------------|-----------|
| `SEGMENTATION_FAILED` | 尝试使用 segment_level=off 关闭切分后重试 |
| `TIMEOUT` | 服务器负载较高，请稍后重试 |
| `SERVER_OFFLINE` | 检查 FunASR 服务器状态 |
| 其他 | 检查音频文件是否完整，或联系管理员 |

## 批量任务结果

### 全部成功

```
✅ 批量转写完成

  批次 ID:  {task_group_id}
  文件数量: {total} 个
  成功:     {total}/{total}
  总耗时:   {total_elapsed}s

  文件明细:
  ┌──────────────────────────┬─────────┬─────────┬──────────┐
  │ 文件名                   │ 时长    │ 耗时    │ 状态     │
  ├──────────────────────────┼─────────┼─────────┼──────────┤
  │ {filename_1}             │ {dur_1} │ {ela_1} │ ✅ 成功  │
  │ {filename_2}             │ {dur_2} │ {ela_2} │ ✅ 成功  │
  └──────────────────────────┴─────────┴─────────┴──────────┘

  📎 结果文件已逐个发送，请查收。
```

### 部分失败

```
⚠ 批量转写部分完成

  批次 ID:  {task_group_id}
  文件数量: {total} 个
  成功:     {succeeded}/{total}
  失败:     {failed}/{total}
  总耗时:   {total_elapsed}s

  文件明细:
  ┌──────────────────────────┬─────────┬─────────┬──────────┐
  │ 文件名                   │ 时长    │ 耗时    │ 状态     │
  ├──────────────────────────┼─────────┼─────────┼──────────┤
  │ {filename_1}             │ {dur_1} │ {ela_1} │ ✅ 成功  │
  │ {filename_2}             │ {dur_2} │ -       │ ❌ 失败  │
  └──────────────────────────┴─────────┴─────────┴──────────┘

  📎 成功文件的结果已发送。失败文件请检查后重试。
```

## 文件交付规则

### 交付方式

1. 先发送结构化摘要消息（上方模板）
2. 再以**文件附件**形式发送结果 txt 文件到原渠道
3. 用户通过飞书发送 → 飞书回复文件
4. 用户通过其他渠道发送 → 对应渠道回复文件

### 文件命名

| 场景 | 命名规则 | 示例 |
|------|---------|------|
| 单文件 txt | `{stem}.txt` | `会议录音.mp4` → `会议录音.txt` |
| 单文件 srt | `{stem}.srt` | `采访.mp3` → `采访.srt` |
| 单文件 json | `{stem}.json` | `演讲.wav` → `演讲.json` |
| 批量同名去重 | `{stem}_{序号}.txt` | 两个 `录音.wav` → `录音.txt`、`录音_1.txt` |
| 批量 zip | `batch_{group_id_short}.zip` | `batch_01JARX.zip` |

### 严禁

- 不在消息中粘贴/引用转写全文（无论文本长短）
- 不由大模型自行决定回复格式
- 不生成"摘要"代替发送文件
- 不把转写内容写入日志

## 重新导出

```
📤 已为 {original_filename} 重新导出 {format} 格式结果。
  结果文件: {result_filename}
  📎 请查收附件。
```
