# 结果格式导出规则

## 支持的格式

### txt — 纯文本

- 端点：`GET /api/v1/tasks/{id}/result?format=txt`
- 内容：纯文本转写结果
- 适用场景：快速阅读、即时分享到 channel

### json — 结构化数据

- 端点：`GET /api/v1/tasks/{id}/result?format=json`
- 内容：JSON 对象，包含转写文本、时间戳、置信度等元数据
- 适用场景：程序处理、数据分析、与其他系统集成

### srt — 字幕格式

- 端点：`GET /api/v1/tasks/{id}/result?format=srt`
- 内容：SRT 字幕文件格式（序号、时间轴、文本）
- 适用场景：视频配字幕、字幕编辑

### zip — 批量打包

- 端点：`GET /api/v1/task-groups/{id}/results?format=zip`
- 内容：ZIP 文件，包含批次内所有任务的结果文件
- 适用场景：批量下载
- 要求：文件名不重复，ZIP 可正常解压

## 批量导出

批量 API 端点每次只接受**单一格式**参数（`json` / `txt` / `srt` / `zip`）：

```
GET /api/v1/task-groups/{id}/results?format=txt
GET /api/v1/task-groups/{id}/results?format=zip
```

后端 `format` 参数校验规则为 `^(json|txt|srt|zip)$`，传入逗号分隔值（如 `format=txt,json,srt`）会返回 422。

如需同时获取多种格式，Agent 应**分别调用**多次 API，每次传入一种格式。

## CLI 等价命令

CLI 的 `--format` 参数支持逗号分隔多格式（这是 CLI 客户端层的能力，内部会逐个任务循环下载），但 REST API 不支持：

```bash
# 单任务：输出到终端
python -m cli task result <task_id> --format json

# 单任务：保存到指定文件（--save 接受文件路径）
python -m cli task result <task_id> --format txt --save ./results/output.txt

# 批量：下载到目录（--output-dir 接受目录路径，不能与 --save 组合）
python -m cli task result --group <group_id> --format txt,json,srt --output-dir ./results
```

> 注意：CLI 的 `--format txt,json,srt` 是客户端侧循环调用，不等于 REST API 支持逗号分隔。

## 验证要点

- `txt` 结果非空
- `json` 结果中存在非空 `text` 字段
- `srt` 结果包含时间戳和字幕文本
- `zip` 是可解压的 ZIP 文件且无重复文件名
