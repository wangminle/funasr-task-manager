# 结果回传模板

## 进度通知

### 关键状态变化

```
⏳ 任务 {task_id} 状态更新: {status}
{status_description}
```

### 批量进度

```
📊 批量进度: {completed}/{total} 已完成
  成功: {succeeded}
  失败: {failed}
  处理中: {processing}
```

## 单任务结果

### 成功（短文本，< 500 字）

```
✅ 转写完成

  文件: {filename}
  时长: {duration}s | RTF: {rtf} | 耗时: {elapsed}s
  文本长度: {text_length} 字

  ──────────────────
  {transcription_text}
  ──────────────────
```

### 成功（长文本，≥ 500 字）

```
✅ 转写完成

  文件: {filename}
  时长: {duration}s | RTF: {rtf} | 耗时: {elapsed}s
  文本长度: {text_length} 字

  文本较长，已生成文件附件。
  摘要（前 200 字）：
  {text_preview}...
```

### 失败

```
❌ 转写失败

  文件: {filename}
  任务: {task_id}
  原因: {error_message}

  建议:
  - {suggestion}
```

## 批量结果

### 全部成功

```
✅ 批量转写完成

  批次: {task_group_id}
  全部成功: {total}/{total}
  总耗时: {total_elapsed}

  结果已打包为 zip，请查收附件。
```

### 部分失败

```
⚠ 批量转写部分完成

  批次: {task_group_id}
  成功: {succeeded}/{total}
  失败: {failed}/{total}

  失败文件:
  {failed_file_list}

  成功文件的结果已打包为 zip。
```

### 质量异常

```
⚠ 结果质量提醒

  以下文件的转写结果可能存在问题:

  {quality_issues}

  建议检查音频质量或参数设置后重试。
```

## 重新导出

```
📤 已为任务 {task_id} 重新导出 {format} 格式结果。
  文件: {filename}
  请查收附件。
```
