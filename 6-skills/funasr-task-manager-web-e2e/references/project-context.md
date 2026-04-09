# 项目上下文

## 目标

为 `funasr-task-manager` 提供可重复执行的浏览器端到端测试流程，覆盖真实用户路径：

1. 打开前端上传页
2. 拖入或选择多个音视频文件
3. 统一设置任务参数
4. 提交批量转写
5. 等待调度与执行完成
6. 在任务页确认状态与结果
7. 归档本次运行的工件

这份参考文件用于补充项目特有的固定事实，避免每次重新探索。

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

## 跨平台环境适配

### 推荐入口

优先在 `3-dev/src/frontend/` 下使用现成 npm 命令，因为这些命令已经处理了 Python 命令探测与 `ASR_E2E_PROFILE` 的跨平台传递：

- `npm run test:e2e:prepare:smoke`
- `npm run test:e2e:prepare:remote-standard`
- `npm run test:e2e:prepare:standard`
- `npm run test:e2e:prepare:full`
- `npm run test:e2e:smoke`
- `npm run test:e2e:remote-standard`
- `npm run test:e2e:standard`
- `npm run test:e2e:full`

### 平台检测

- Node.js：`node -p "process.platform"`
- Python：`python -c "import platform; print(platform.system())"`

### 命令差异

- Windows PowerShell
  - Python 通常用 `python`
  - 环境变量用 `$env:NAME='value'`
- macOS/Linux
  - Python 优先 `python3`
  - 环境变量用 `NAME=value command`

### FunASR 服务节点选择

- 若后端已存在可用服务节点，浏览器 E2E 会直接复用第一个已注册节点。
- 若需要显式指定节点，设置：
  - `ASR_E2E_SERVER_HOST`
  - `ASR_E2E_SERVER_PORT`
  - 可选：`ASR_E2E_SERVER_ID`、`ASR_E2E_SERVER_NAME`、`ASR_E2E_SERVER_PROTOCOL`、`ASR_E2E_SERVER_MAX_CONCURRENCY`
- 不再依赖固定 IP 地址作为默认值。

### 上传页已知行为

- 上传控件使用 Element Plus `el-upload`
- 支持多文件
- 接受格式：`.wav,.mp3,.mp4,.flac,.ogg,.webm,.m4a,.aac,.mkv,.avi,.mov`
- 默认流程：
  - 选中文件后先进入待上传列表
  - 点击 `提交转写` 才会逐个上传并批量创建任务
  - 成功后会出现“已创建任务”表格，并可跳转到任务列表

### 任务页已知行为

- 默认每 5 秒轮询一次任务与统计数据
- 成功状态为 `SUCCEEDED`
- 成功任务可下载 `txt` / `json` / `srt` 结果

### 批次管理 API

多文件上传会自动创建 `task_group_id`，以下端点可用于验证批次流程：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/task-groups/{group_id}` | 批次概况（total/succeeded/failed 等） |
| GET | `/api/v1/task-groups/{group_id}/tasks` | 批次内任务列表 |
| GET | `/api/v1/task-groups/{group_id}/results?format=` | 批次结果（txt/json/srt/zip） |
| DELETE | `/api/v1/task-groups/{group_id}` | 删除整批 |

### 节点管理 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/servers` | 注册 ASR 节点 |
| GET | `/api/v1/servers` | 节点列表 |
| POST | `/api/v1/servers/{server_id}/probe` | 探测连通性与能力（connect_only/offline_light/twopass_full），不执行 benchmark |
| POST | `/api/v1/servers/{server_id}/benchmark` | 单节点 benchmark（单线程 RTF + 梯度并发吞吐量） |
| POST | `/api/v1/servers/benchmark` | 全量 benchmark（所有在线节点） |
| PATCH | `/api/v1/servers/{server_id}` | 更新配置（max_concurrency/name） |

### 系统诊断 API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/diagnostics` | 系统诊断（database_schema/ffprobe/upload_dir/asr_servers） |
| GET | `/health` | 健康检查 |

### CLI 批量模式

CLI `transcribe` 命令传入多个文件时自动启用批量模式，一次性上传并创建任务，后端并行调度。结果可通过 `task result --group <group_id> --format txt,json,srt` 多格式导出。

## 测试素材策略

测试素材位于 `4-tests/batch-testing/assets/1-测试audioFiles/`。
这些文件大小与格式不同，适合做分层回归，而不是每次都全量跑。

### 推荐批次

- `smoke`
  - 用于日常快速回归
  - 目标：覆盖 1 个音频短样本、1 个压缩音频样本、1 个视频样本
  - 建议数量：3 个
- `remote-standard`
  - 用于远端 FunASR 节点、受限带宽或需要稳定五文件回归的场景
  - 目标：固定选择按体积从小到大排序的前 5 个样本
  - 当前文件：`test001.wav`、`tv-report-1.wav`、`test.mp4`、`办公平台20250724-144218.mp4`、`tv-report-1.mp4`
  - 建议数量：5 个
- `standard`
  - 用于本地或资源较充足环境下的功能合并前验证
  - 目标：在 `smoke` 基础上加入更多格式或更长文件，可能包含 100MB+ 素材
  - 建议数量：4 到 6 个
- `full`
  - 用于发布前或调度/转写链路大改动后验证
  - 目标：覆盖全部可用样本

优先使用 `scripts/build_fixture_batch.py` 自动生成批次，不要手写文件列表。

## 断言策略

### 硬性通过条件

- 上传页能正常打开
- 文件能进入待上传列表
- 点击 `提交转写` 后文件能完成上传
- 创建任务数量等于选中文件数量
- 任务能从 `PENDING/PREPROCESSING/QUEUED` 推进到终态
- 成功任务能下载结果

### 结构性结果校验

- `txt` 结果非空
- `json` 结果中存在非空 `text`
- `srt` 结果包含时间戳和字幕文本
- 批次 `format=json` 返回合法 JSON 数组（每个元素含 `task_id`, `file_name`, `result`）
- `format=zip` 返回可解压的 ZIP 文件，内含各任务结果文件（文件名不重复）
- 视频文件不会因为预处理链路问题直接失效

### 语义校验

如果某些文件已经有人维护“期望关键词”或“参考文本”，优先做关键词匹配，不做整段全文强匹配。

推荐的校验层级：

1. 必须非空
2. 必须包含目标语言的合理字符
3. 如有基线，再校验关键词或短语

在未建立基线前，不要把模型措辞微调当作失败。

### 语义基线文件格式

项目默认语义基线文件为 `6-skills/funasr-task-manager-web-e2e/references/semantic-baseline.json`。

字段约定：

- `expected_language`：例如 `zh-CN`
- `keywords_all`：必须全部命中的关键词数组
- `keywords_any`：至少命中一个即可的关键词数组
- `notes`：人工维护说明

维护流程建议：

1. 先运行 smoke 或 remote-standard，确认链路稳定。
2. 从 `4-tests/batch-testing/outputs/e2e/<timestamp>/results/` 抽取当前可接受文本。
3. 为稳定样本补充 `keywords_all` 或 `keywords_any`，优先写短语，不写整段全文。
4. 语音模型、协议版本或采样链路调整后，重新回归并更新关键词。

## 测试代码布局建议

这个仓库已有 `pytest` E2E，但那是后端/API 视角。
浏览器自动化建议优先放在前端工具链附近，例如：

- `3-dev/src/frontend/tests/e2e/`
- `3-dev/src/frontend/playwright.config.js`
- `3-dev/src/frontend/scripts/run-python.js`
- `3-dev/src/frontend/scripts/start-backend.js`

原因：

- Playwright 与前端 `npm` 依赖天然同目录
- 更容易直接复用 Vite 和浏览器测试命令
- 不会把 Node 浏览器测试强行塞进 Python `pytest` 目录

不要在仓库根目录额外保留一套默认 Playwright 初始化脚手架（例如 `playwright.config.ts`、`tests/example.spec.ts`、`e2e/example.spec.ts`）。
该仓库的正式浏览器 E2E 工程以 `3-dev/src/frontend/` 下的配置与用例为准。

如果用户明确要求统一到 `4-tests/`，可以保留报告或运行摘要在 `4-tests/reports/`，但浏览器测试主体仍优先贴近前端目录。

## 超时策略

不同 Profile 的推荐超时：

- `smoke`：单任务 5 分钟，整体 10 分钟
- `remote-standard`：单任务 8 分钟，整体 20 分钟
- `standard`：单任务 10 分钟，整体 30 分钟
- `full`：单任务 20 分钟，整体 90 分钟

超时依据：大文件（>100MB）需要预处理转码 + 上传 + 转写，412MB 视频可能需要 15 分钟以上；`remote-standard` 用于规避这类远端链路瓶颈。
超时后应截图当前页面状态，记录各任务当前 status，不要静默失败。

## Playwright 浏览器安装

### 通用命令

在 `3-dev/src/frontend/` 下执行：

```bash
npx playwright install chromium
```

### Windows PowerShell 网络受限场景

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST='https://npmmirror.com/mirrors/playwright'
npx playwright install chromium
```

### macOS/Linux 网络受限场景

```bash
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright npx playwright install chromium
```

### 离线缓存位置

- Windows：`%LOCALAPPDATA%/ms-playwright`
- macOS：`~/Library/Caches/ms-playwright`
- Linux：`~/.cache/ms-playwright`

## CI/CD 集成建议

推荐把 smoke 作为最小自动化门禁，remote-standard 作为手动或定时任务。

### 建议的流水线步骤

1. 安装前端依赖：`npm ci`
2. 安装后端依赖并确保 `alembic`、`uvicorn` 可用
3. 安装 Playwright 浏览器：`npx playwright install chromium`
4. 提供 FunASR 节点环境变量，或在流水线预先调用 `/api/v1/servers` 注册节点
5. 执行 `npm run test:e2e:smoke`
6. 归档 `4-tests/batch-testing/outputs/e2e/` 为流水线工件

## 工件归档

每次运行都应写入 `4-tests/batch-testing/outputs/e2e/<timestamp>/`，至少包含：

- `fixture-batch.json`
- `run-summary.json` 或 `run-summary.md`
- `results/` 下的转写文本快照
- `screenshots/` 或 `traces/`（失败时必须保留）

以下目录属于本地测试工件，应保持 gitignore：

- `4-tests/batch-testing/outputs/e2e/`
- `3-dev/src/frontend/test-results/`
- `3-dev/src/frontend/playwright-report/`
- `3-dev/src/frontend/blob-report/`

推荐摘要字段：

- 运行时间
- profile
- 文件列表
- 成功数 / 失败数
- 失败原因
- 是否命中语义基线

### 报告模板示例

```text
═══════════════════════════════════════════
  FunASR E2E 测试报告
  执行时间: {timestamp}
  测试方案: {profile}
  整体耗时: {total_duration}
═══════════════════════════════════════════

文件名                       大小       状态    耗时     文本长度   格式
───────────────────────────────────────────
test001.wav                  3.7MB      ✅     12s      156字      ✓json ✓txt ✓srt
tv-report-1.wav              5.5MB      ✅     18s      342字      ✓json ✓txt ✓srt
test.mp4                     6.4MB      ❌     --       --         超时

批次ID: {task_group_id}
总计: 2/3 通过  |  1 失败  |  整体耗时: 45s
失败项: test.mp4 - 任务停留在 TRANSCRIBING 超过 5 分钟
批次结果: /api/v1/task-groups/{group_id}/results?format=zip
═══════════════════════════════════════════
```

## 推荐 data-testid 清单

为保证 Playwright 选择器稳定性，建议在以下元素上添加 `data-testid`：

| 页面 | 元素 | 建议 testid |
| ---- | ---- | ----------- |
| UploadView | 上传拖拽区 | `upload-dropzone` |
| UploadView | 提交转写按钮 | `submit-transcribe` |
| UploadView | 待上传文件表格 | `pending-files-table` |
| UploadView | 已创建任务表格 | `created-tasks-table` |
| TaskListView | 任务列表表格 | `task-list-table` |
| TaskListView | 状态标签 | `task-status-{taskId}` |
| TaskListView | 下载结果按钮 | `download-result-{taskId}` |

## 实施建议

### 第一次建设

1. 给上传页和任务页补最小量 `data-testid`（见上表）
2. 在前端引入 Playwright
3. 先落地 `smoke` 场景
4. 再扩到 `standard`
5. 最后补语义基线文件

### 日常使用

- 前端改动后：跑 `smoke`
- 远端 FunASR 节点回归：跑 `remote-standard`
- 调度、上传、预处理或协议改动后：本地优先跑 `standard`
- 发布前：跑 `full`

### 失败排查优先级

1. 页面元素定位失效
2. 前端代理或 API 地址错误
3. 上传链路失败
4. 调度无可用服务节点
5. 结果下载失败
6. 文本质量异常
