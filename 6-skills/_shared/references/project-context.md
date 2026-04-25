# 项目上下文（共享）

> **唯一事实源**：各 skill 的 `references/project-context.md` 应 symlink 或引用此文件。
> 仅在此处维护，避免多处同步。

## 关键路径

### 代码与页面

- 前端目录：`3-dev/src/frontend`
- 后端目录：`3-dev/src/backend`
- 上传页组件：`3-dev/src/frontend/src/views/UploadView.vue`
- 任务页组件：`3-dev/src/frontend/src/views/TaskListView.vue`
- 路由：`/upload`、`/tasks`
- 前端 API 基础路径：`/api/v1`
- 默认本地地址：
  - 前端：`http://localhost:5173`（Vite 开发服务器）
  - 后端：`http://localhost:8000`（Uvicorn）

### CLI 入口

- 开发环境：`python -m cli`
- 生产环境：`asr-cli`（通过 `pip install -e .` 安装后可用，二者等价）

### 核心 API 端点

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | `/health` | 健康检查 | 无需认证 |
| GET | `/api/v1/stats` | 系统统计（server_online、queue_depth 等） | 普通 API Key |
| POST | `/api/v1/files/upload` | 上传文件 | 普通 API Key |
| POST | `/api/v1/tasks` | 创建转写任务 | 普通 API Key |
| GET | `/api/v1/tasks/{id}` | 查询任务状态 | 普通 API Key |
| GET | `/api/v1/tasks/{id}/result` | 获取转写结果 | 普通 API Key |
| GET | `/api/v1/tasks/{id}/progress` | SSE 实时进度 | 普通 API Key |
| GET | `/api/v1/task-groups/{id}` | 批次概况 | 普通 API Key |
| GET | `/api/v1/task-groups/{id}/results` | 批次结果（`format` 四选一：`json` / `txt` / `srt` / `zip`） | 普通 API Key |
| GET | `/api/v1/servers` | 服务器列表 | **AdminUser** |
| POST | `/api/v1/servers/{id}/benchmark` | 单节点 benchmark | **AdminUser** |
| POST | `/api/v1/servers/benchmark` | 全量 benchmark（并发） | **AdminUser** |

### 任务创建参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `auto_segment` | `auto` / `on` / `off` | `auto` | VAD 切分策略。`auto` 按时长阈值自动决定；`on` 强制切分；`off` 关闭切分 |
| `segment_level` | `10m` / `20m` / `30m` | `10m` | 切分力度。控制目标切分时长和搜索窗口。`auto_segment=off` 时忽略 |

### 任务状态流转

```
PENDING → PREPROCESSING → QUEUED → DISPATCHED → TRANSCRIBING → SUCCEEDED
                                                              → FAILED
                                              → CANCELED
```

长音频（超过 `segment_level` 对应阈值）在 PREPROCESSING 阶段自动 VAD 切分为多个内部 segment，segment 独立调度执行，全部完成后合并结果。父任务状态对外不变。

### 服务器状态

- `ONLINE`：正常
- `OFFLINE`：不可用
- `DEGRADED`：降级

### 文件格式支持

允许列表：`.wav` `.mp3` `.mp4` `.flac` `.ogg` `.webm` `.m4a` `.aac` `.wma` `.mkv` `.avi` `.mov` `.pcm`

不转码：`.wav`、`.pcm`（直接发送给 FunASR）
需转码：其他格式（ffmpeg 转为 16kHz 单声道 WAV）
