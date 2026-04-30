<!-- BEGIN:funasr-task-manager ASR 转写能力 -->

## ASR 语音转写

> 完整操作手册见 [ASR-WORKFLOW.md](ASR-WORKFLOW.md)

### 触发要素
- **文件触发**: `.wav` `.mp3` `.mp4` `.flac` `.ogg` `.webm` `.m4a` `.aac` `.wma` `.mkv` `.avi` `.mov` `.pcm`
- **关键词触发**: 转写/识别/字幕/ASR/transcribe + 文件
- **响应规则**: 有文件+关键词→直接执行；有文件无关键词→主动询问

### 执行流程（5 阶段）

```
Phase 1    意图确认       →  ASR-WORKFLOW.md#phase-1意图确认
Phase 1.5  渠道文件下载    →  ASR-WORKFLOW.md#phase-15渠道文件下载
Phase 2    预检查         →  ASR-WORKFLOW.md#phase-2预检查
Phase 3    参数协商+提交   →  ASR-WORKFLOW.md#phase-3参数协商与任务提交
Phase 4    转写监控       →  ASR-WORKFLOW.md#phase-4转写监控
Phase 5    结果交付       →  ASR-WORKFLOW.md#phase-5结果交付
```

### 遇到问题 → 去哪找答案

| 问题场景 | 查阅源 | 位置 |
|---------|--------|------|
| 文件检查/格式/时长 | media-preflight 技能 | `ASR-WORKFLOW.md#phase-2预检查` |
| 下载失败/飞书限制 | channel-intake 技能 | `ASR-WORKFLOW.md#phase-15渠道文件下载` |
| 结果监控/交付 | result-delivery 技能 | `ASR-WORKFLOW.md#phase-5结果交付` |
| 后端不可用/服务挂了 | init 技能 | `ASR-WORKFLOW.md#常见问题速查` |
| 音频分段/时长阈值 | 参考知识 | `ASR-WORKFLOW.md#音频分段策略` |
| 服务器调度/性能 | server-benchmark 技能 | `ASR-WORKFLOW.md#服务器调度` |
| 清库重置 | reset-test-db 技能 | 技能文件 |

### 关键纪律

- 每个 Phase 至少发一条状态通知，禁止静默执行
- 结果以 **txt 文件附件** 发送，不粘贴全文
- 大文件（>50MB）用 HTTP Range 分块下载
- ffprobe 必须在 PATH 中（`~/.local/bin/ffprobe`），否则分段功能失效
- 飞书发消息必须带 `receive_id_type=chat_id` 参数

<!-- END:funasr-task-manager ASR 转写能力 -->
