# 用户交互模板

## 意图确认

### 高置信度（文件 + 关键词）

```
收到 {count} 个音视频文件，准备转写。
默认输出格式：txt + json。需要调整参数吗？还是直接开始？
```

### 中置信度（有文件，无关键词）

```
检测到您发送了 {count} 个音视频文件。
需要转写为文字吗？
```

### 低置信度（有关键词，无文件）

```
您提到了转写，但我没有收到音频/视频文件。
请发送需要转写的文件，支持格式：wav, mp3, mp4, flac, ogg, webm, m4a, aac, mkv, avi, mov, pcm
```

## 预检查反馈

```
📋 文件预检查完成

  文件: {filename}
  格式: {format} ({codec})
  时长: {duration_human}
  大小: {file_size_mb} MB

  {conversion_info}
  ⏱ 预计处理时间: {estimated_time}

  {warnings}

  {status}
```

## 提交确认

### 单文件

```
✅ 任务已提交

  文件: {filename}
  任务编号: {task_id}
  预计完成: {estimated_time}

  正在处理中，完成后会自动通知您。
```

### 批量任务

```
✅ 批量任务已提交

  文件数量: {count}
  批次编号: {task_group_id}
  预计完成: {estimated_time}

  正在处理中，会定期汇报进度。
```

## 结果交付

### 单文件（短文本）

```
✅ 转写完成

  文件: {filename}
  时长: {duration}s | RTF: {rtf} | 耗时: {elapsed}s
  文本长度: {text_length} 字

  ──────────────────
  {transcription_text}
  ──────────────────
```

### 单文件（长文本）

```
✅ 转写完成

  文件: {filename}
  时长: {duration}s | RTF: {rtf} | 耗时: {elapsed}s
  文本长度: {text_length} 字

  文本较长，已生成文件附件。
  摘要（前 200 字）：
  {text_preview}...
```

### 批量汇总

```
✅ 批量转写完成

  批次: {task_group_id}
  成功: {succeeded}/{total}
  失败: {failed}/{total}
  总耗时: {total_elapsed}

  {per_file_summary}

  结果已打包为 zip，请查收附件。
```

## 错误提示

### 服务器不可用

```
⚠ 当前所有转写服务器不可用。

  请检查：
  1. 后端服务是否运行中（GET /health）
  2. 是否已注册 FunASR 服务器
  3. 服务器节点是否处于 ONLINE 状态

  文件已保留，服务恢复后可重新提交。
```

### 上传失败

```
❌ 文件上传失败

  文件: {filename}
  原因: {error_message}

  {suggestion}
```
