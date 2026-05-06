---
name: funasr-task-manager-media-preflight
description: >
  Pre-check audio/video files before transcription submission.
  Use when: validating whether a file can be transcribed, diagnosing
  file metadata or duration issues, estimating processing time before
  committing, or checking ffmpeg/ffprobe availability.
---

# 媒体文件预检查

在提交转写任务前，对音视频文件做格式、元数据、转码需求和耗时预估的检查。

> **实时通知规范**：当本 Skill 在聊天渠道或 `channel-intake` 链路中产生用户可见汇报时，必须遵循 `6-skills/_shared/CHANNEL-NOTIFICATION.md`，通过 `send_user_notice()` 发送，不要只写普通 assistant 文本。

本 Skill 可被 `funasr-task-manager-channel-intake` 在 Phase 2 自动进入，也可独立用于排查文件问题（如"为什么时长不准""这个文件能转写吗"）。

## 触发条件

- `funasr-task-manager-channel-intake` 在提交前自动进入本规程
- 用户问"这个文件能转写吗""文件格式对吗""时长是多少"
- Agent 需要在提交任务前评估文件可行性
- 关键词：`预检查` / `文件检查` / `格式` / `时长` / `转码` / `ffprobe` / `preflight`

## 检查清单

按顺序执行以下检查，任一步骤失败时报告具体原因但不中断后续检查（尽量收集全部问题）：

### CHECK 1：文件存在性与完整性

- 文件是否存在？
- 文件大小是否 > 0？
- 文件大小是否超过上限？（由 `settings.max_upload_size_mb` 控制）
- 失败 → 报告具体原因

### CHECK 2：格式识别

- 扩展名是否在允许列表中？

  允许列表（与后端 `file_manager.py` 的 `ALLOWED_EXTENSIONS` 一致）：

  `.wav` `.mp3` `.mp4` `.flac` `.ogg` `.webm` `.m4a` `.aac` `.wma` `.mkv` `.avi` `.mov` `.pcm`

- 扩展名不匹配 → 报告"不支持的格式: .xxx"，列出支持的格式
- 可选：通过 file magic bytes 二次验证（防止扩展名伪造）

### CHECK 3：元数据提取

- 检查 `ffprobe` 是否可用
  - 不可用 → 标记 `precise_metadata=false`，说明 ETA 只能估算
- 调用 `ffprobe` 获取：
  - `duration`（时长，秒）
  - `codec_name`（编码格式）
  - `sample_rate`（采样率）
  - `channels`（声道数）
  - `bit_rate`（比特率）
  - `format_name`（容器格式）
- `ffprobe` 失败 → 不直接判定不可处理；按文件大小估算 duration，提醒"精确元数据不可用，仍可提交但 ETA 不准"

### CHECK 4：转码需求评估

判断是否需要转码（与 `audio_preprocessor.py` 的 `needs_conversion()` 逻辑一致）：

- 当前实现：**仅按扩展名判断**——`.wav` 和 `.pcm` 不转码，其他格式一律转码
- 非 WAV/PCM → 需要转码（ffmpeg 会统一转为 16kHz 单声道 s16 WAV）
- `.wav` / `.pcm` → 直接使用（即使采样率≠16000 或多声道，当前也不会触发转码）

**已知局限**：WAV 文件的采样率/声道数重采样是目标能力，当前未实现。preflight 可以在 `warnings` 中提示"WAV 文件采样率非 16kHz / 多声道，FunASR 可能处理异常"，但不应断言后端会自动重采样。

- 需要转码 → 报告"文件需要预处理（转码到 16kHz 单声道 WAV）"
- 估算转码耗时（粗略：文件大小 / 10MB ≈ 秒数）

### CHECK 5：任务耗时预估

- 如果服务器有 `rtf_baseline` → `estimated_time = duration × rtf_baseline`
- 如果无基线 → `estimated_time = duration × DEFAULT_RTF (0.3)`
- 加上转码耗时（如需要）
- 报告"预计处理时间: {estimated_time}s"

### CHECK 6：风险提醒

- 时长 > 1 小时 → 提醒"超长音频，预计耗时较久"
- 文件 > 500MB → 提醒"超大文件，上传和转码耗时较长"
- 编码为压缩格式但文件异常大 → 提醒"文件可能包含视频轨道"
- 视频文件 → 提醒"视频文件将提取音频轨道后转写"

## 输出格式

预检查结果应包含以下字段：

```json
{
  "filename": "会议录音-20260415.mp4",
  "file_size_mb": 156.3,
  "format": "mp4",
  "duration_sec": 3720.5,
  "precise_metadata": true,
  "duration_human": "1h 2m 0s",
  "codec": "aac",
  "sample_rate": 44100,
  "channels": 2,
  "needs_conversion": true,
  "conversion_reason": "非 WAV 格式，需转码到 16kHz 单声道 WAV",
  "estimated_conversion_sec": 15,
  "estimated_processing_sec": 1116,
  "estimated_processing_human": "约 18 分钟",
  "warnings": [
    "超长音频（> 1 小时），耗时较久",
    "视频文件，将提取音频轨道"
  ],
  "ready": true
}
```

## 用户友好汇报模板

在聊天渠道中，以下模板应作为 `send_user_notice()` 内容发送；纯本地终端交互可直接输出。

```
📋 文件预检查: 会议录音-20260415.mp4

  格式: MP4 (AAC 音频)
  时长: 1h 2m 0s
  大小: 156.3 MB
  声道: 2（立体声）
  采样率: 44100 Hz

  ⚙ 需要预处理: 是（转码到 16kHz 单声道 WAV）
  ⏱ 预计转码: ~15s
  ⏱ 预计转写: ~18 分钟

  ⚠ 注意: 超长音频，耗时较久
  ⚠ 注意: 视频文件，将提取音频轨道

  ✅ 可以提交转写
```

## 错误处理

| 场景 | Agent 应做的事 | 不应做的事 |
|------|--------------|----------|
| `ffprobe` 不可用 | 返回估算值和 warning，继续 | 直接中断整个预检查 |
| 文件为空 | 报告"文件为空，无法处理" | 提交空文件任务 |
| 格式不支持 | 列出支持的格式，建议转换 | 尝试强行提交 |
| 文件过大 | 报告大小和限制 | 尝试压缩 |

## 与其他 Skill 的关系

- **被 `funasr-task-manager-channel-intake` 调用**：intake 在 Phase 2 进入本规程执行预检查
- **可独立触发**：用户排查文件问题时直接使用
- **安全模式下**：只能读取解密后的临时副本，不得把明文元数据写入普通日志

## 相关文件

- `3-dev/src/backend/app/storage/file_manager.py`：`ALLOWED_EXTENSIONS` 定义
- `3-dev/src/backend/app/services/audio_preprocessor.py`：`needs_conversion()` 逻辑
- `references/supported-formats.json`：支持的格式列表
- `references/conversion-rules.md`：转码规则与条件
