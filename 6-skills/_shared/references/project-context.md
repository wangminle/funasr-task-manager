# 项目上下文（共享）

> **唯一事实源**：各 skill 的 `references/project-context.md` 应 symlink 或引用此文件。
> 仅在此处维护，避免多处同步。
>
> **适配项目版本**：V0.4.26-Build0469-20260517（Alembic 迁移至 007）

## 关键路径

### 代码与页面

- 前端目录：`3-dev/src/frontend`
- 后端目录：`3-dev/src/backend`
- 上传页组件：`3-dev/src/frontend/src/views/UploadView.vue`
- 任务页组件：`3-dev/src/frontend/src/views/TaskListView.vue`
- 路由：`/upload`、`/tasks`
- 前端 API 基础路径：`/api/v1`
- 默认本地地址：
  - 前端：`http://localhost:15798`（Vite 开发服务器）
  - 后端：`http://localhost:15797`（Uvicorn）

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
| GET | `/api/v1/task-groups/{id}/tasks` | 批次任务列表 | 普通 API Key |
| GET | `/api/v1/task-groups/{id}/results` | 批次结果（`format` 四选一：`json` / `txt` / `srt` / `zip`） | 普通 API Key |
| GET | `/api/v1/servers` | 服务器列表 | **AdminUser** |
| POST | `/api/v1/servers/{id}/benchmark` | 单节点 benchmark | **AdminUser** |
| POST | `/api/v1/servers/benchmark` | 全量 benchmark（并发） | **AdminUser** |
| GET | `/api/v1/admin/active-slots` | 诊断服务器真实 active slot 占用 | **AdminUser** |
| POST | `/api/v1/admin/emergency-stop` | dry-run 或确认执行急停并释放 slot | **AdminUser** |

### 任务创建参数

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `segment_level` | `off` / `10m` / `20m` / `30m` | `10m` | 切分策略。`off` 关闭切分；`10m`/`20m`/`30m` 按时长阈值自动决定是否切分，控制目标切分时长和搜索窗口 |

### 任务状态流转

```
PENDING → PREPROCESSING → QUEUED → DISPATCHED → TRANSCRIBING → SUCCEEDED
                                                              → FAILED
                                              → CANCELED
```

长音频（超过 `segment_level` 对应触发阈值，即 target × 1.2）在 PREPROCESSING 阶段自动 VAD 切分为多个内部 segment。切分采用双向交替搜索策略（后→前→后），搜索步长按档位比例设定（10m=60s, 20m=120s, 30m=180s）。segment 独立调度执行，全部完成后合并结果。父任务状态对外不变。触发阈值：10m=720s（<12分钟不拆）、20m=1440s（<24分钟不拆）、30m=2160s（<36分钟不拆）。

### 调度统计口径

调度器使用 LPT/EFT + 槽位队列预规划 + slot refill + work stealing。只有 `ONLINE` 且 `max_concurrency > 0` 的服务器参与实际调度。新 plan 刚生成时只派发 `estimated_start <= IMMEDIATE_START_TOLERANCE` 的 work item；非重规划轮次中，某个 slot 释放且本队列没有 immediate item 时，才允许从既有 plan 中释放未来槽位任务补位。分段任务参与同一 PlanPool，但受单个父任务的段级并发上限约束；若 work stealing 的最佳 segment 候选暂时达到上限，调度器会在本轮跳过它继续寻找其它候选。work stealing 收益按候选任务的计划完成时间计算，避免多槽源队列被串行相加。

批次概况接口 `GET /api/v1/task-groups/{id}` 的 `scheduling.idle_slot_seconds` 按 `wall_clock × ONLINE+enabled 总并发 - busy_processing_seconds` 估算，完全空闲但可用的服务器也计入总 slot；当前无可用服务器时，退回按本批次已分配过的服务器容量估算。

批量任务统计必须使用 `GET /api/v1/task-groups/{id}/tasks?page_size=500` 或 CLI `task-group status`。通用任务列表 `GET /api/v1/tasks` 默认分页大小为 20，不可用默认响应推断完整批次。

### 服务器状态

- `ONLINE`：正常
- `OFFLINE`：不可用
- `DEGRADED`：降级

### 文件格式支持

允许列表：`.wav` `.mp3` `.mp4` `.flac` `.ogg` `.webm` `.m4a` `.aac` `.wma` `.mkv` `.avi` `.mov` `.pcm`

不转码：`.wav`、`.pcm`（直接发送给 FunASR）
需转码：其他格式（ffmpeg 转为 16kHz 单声道 WAV）

### 安全默认值（V0.4.14）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `ssrf_protection_enabled` | `True` | callback URL SSRF 校验（私有 IP 拒绝） |
| DNS Fail-Closed | 启用 | DNS 解析失败视为私有地址 |
| `callback.secret` | 可选 | HMAC 时间安全比较 |
| SSE 认证 | `X-API-Key` header | `?token=` query deprecated |
| Webhook 请求体限制 | 1MB | Alertmanager webhook |
| 敏感值脱敏 | 自动 | CLI `config set` 回显 / 日志中长 token 脱敏 |

### 数据库迁移

当前迁移链：`001` → `002` → `003` → `004` → `005` → `006` → `007`（head）

- `005_fix_nullable_and_defaults.py`：修复 `task_events.from_status` nullable、`server_instances.status` server_default、多表 `updated_at` nullable 不一致
- `006_add_server_enabled.py`：为 `server_instances` 增加 `enabled` 开关，允许禁用节点且不被心跳覆盖
- `007_add_run_generation.py`：为 `tasks` / `task_segments` 增加 `run_generation`，避免取消/重试后的旧 worker 回写污染结果
