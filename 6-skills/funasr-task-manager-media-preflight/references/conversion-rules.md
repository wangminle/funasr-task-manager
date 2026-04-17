# 转码规则与条件

## 当前实现

转码判断基于 `audio_preprocessor.py` 的 `needs_conversion()`：

- **判断方式**：仅按文件扩展名
- **不转码**：`.wav`、`.pcm`
- **转码**：所有其他允许的格式（`.mp3`、`.mp4`、`.flac`、`.ogg`、`.webm`、`.m4a`、`.aac`、`.wma`、`.mkv`、`.avi`、`.mov`）

## 转码目标格式

ffmpeg 统一转为：

- 采样率：16000 Hz
- 声道数：1（单声道）
- 编码：s16le（16-bit signed little-endian PCM）
- 容器：WAV

## 已知局限

WAV 文件即使采样率不是 16kHz 或声道数不是 1，当前也**不会触发转码**。这可能导致 FunASR 处理异常。

preflight 应在 `warnings` 中提示该情况，但不应断言后端会自动重采样。

## 转码耗时估算

粗略估算公式：`文件大小 (MB) / 10 ≈ 转码秒数`

实际耗时受 CPU 性能、磁盘 I/O 和源文件编码复杂度影响。
