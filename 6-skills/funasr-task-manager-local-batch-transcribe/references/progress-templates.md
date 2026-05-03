# 进度反馈模板

> 本文件定义 `funasr-task-manager-local-batch-transcribe` Skill 在各阶段向用户发送的通知模板。
> Agent 必须严格按模板拼接输出，不可自由组织回复内容。

## 1. 启动通知

扫描开始时：

```
我将扫描 {source_dir} 中的待转写文件。
如果你想处理其他目录，请直接告诉我目录路径。
```

## 2. 扫描完成通知

```
扫描完成：共发现 {total} 个音视频文件，其中 {pending} 个待转写，{skipped} 个已完成跳过，{failed_precheck} 个预检查失败。
总音频时长 {total_duration_human}。
基于当前 {server_count} 台在线服务器（RTF ≈ {rtf}），预计处理时间约 {eta_human}。
我将按批量模式提交 {pending} 个文件。
```

## 3. 提交确认通知

**单批（单 chunk）**

```
已提交 {file_count} 个文件到转写队列。
批次 ID：{batch_id}
任务组：{task_group_id}
预计耗时：{eta_human}
```

**大批量（多 chunk）**

```
已提交第 {chunk_index}/{total_chunks} 批（{chunk_file_count} 个文件）。
批次 ID：{batch_id}
当前任务组：{task_group_id}
累计提交：{submitted}/{total} 个文件
```

## 4. 进度更新通知

**常规进度**

```
批量转写进度：{succeeded}/{total} 已完成，{failed} 个失败，{in_progress} 个处理中。
当前批次：{batch_id}
最近完成：{last_completed_file}
结果目录：runtime/agent-local-batch/outputs/{batch_id}/
```

**无新变化（心跳）**

```
转写仍在运行，目前没有新的完成项。后端状态正常，当前仍有 {in_progress} 个任务处于 TRANSCRIBING。
```

## 5. 异常通知

**上传失败**

```
⚠️ {failed_count} 个文件上传失败：{error_summary}
已跳过这些文件，继续处理剩余 {remaining} 个。
```

**后端不可达**

```
⚠️ 后端服务不可达（{error_detail}）。
已暂停提交，将在 30 秒后重试。如果问题持续，请检查后端状态。
```

**任务失败**

```
⚠️ 文件 {filename} 转写失败：{error_message}
任务 ID：{task_id}
建议：{suggestion}
```

## 6. 完成汇总通知

```
批量转写完成：成功 {succeeded} 个，失败 {failed} 个。
结果已保存到 runtime/agent-local-batch/outputs/{batch_id}/。
总耗时：{total_elapsed_human}
{failed_section}
```

`{failed_section}`：仅当 `failed > 0` 时拼接以下段落；否则为空（不输出额外行）。

```
失败清单已写入 runtime/agent-local-batch/manifests/{batch_id}-failed.json。
可以说"重试失败项"来重新处理这些文件。
```

## 7. 让步通知（与 channel-intake 协作）

**Batch 暂停**

```
⏸️ 收到实时转写请求（{intake_file_count} 个文件），batch 队列暂停新任务启动。
当前正在运行的 {running_count} 个 batch 任务不受影响，预计 {eta} 后完成。
完成后将优先处理实时请求，然后恢复 batch 队列。
```

**Batch 恢复**

```
▶️ 实时转写已完成，batch 队列恢复。
继续处理剩余 {remaining_count} 个文件，当前进度 {completed}/{total}。
```

## 8. 断点续传通知

```
发现一个未完成的批量转写批次 ({batch_id})：
  已提交 {submitted}/{total} 个文件，其中 {succeeded} 已完成，{in_progress} 仍在运行。
  是否继续？
```

## 使用规则

1. 所有 `{变量}` 必须从 manifest 或 API 返回值中获取，不可由 Agent 推断或美化。
2. 不在模板外添加额外内容（如性能表格、分段 JSON 等）。
3. 时间格式：秒数 < 120 用 "Xs"，120-3600 用 "Xm Ys"，>3600 用 "Xh Ym"。
4. 文件名超过 30 字符时截断为前 27 字符 + "..."。
