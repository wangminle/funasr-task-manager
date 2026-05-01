---
name: funasr-task-manager-web-e2e
description: Build, run, or maintain browser-based end-to-end tests for the funasr-task-manager project. Use when Codex needs to simulate a real user dragging audio or video files into the web upload page, creating a batch of transcription tasks, waiting for scheduler execution, and validating that the system returns reasonable recognition results with saved test artifacts.
---

# FunASR Task Manager 浏览器 E2E 测试

本技能用于项目的真实浏览器端到端测试流程，而非 API-only 的验证。
已有的 `4-tests/scripts/e2e/` 下的 pytest E2E 用例覆盖后端/API 流程；本技能覆盖缺失的路径：浏览器上传、批量任务创建、任务列表观察、结果下载和工件归档。

测试配置（Profile）说明：

- `smoke`：日常最小快速回归
- `remote-standard`：远端 FunASR 节点推荐批次，固定选择按体积从小到大排序的前 5 个文件
- `standard`：较重的本地/合并前覆盖，可能包含 100MB+ 素材
- `full`：覆盖全部可用素材

## 快速开始

1. 修改或运行浏览器 E2E 前，先阅读 [references/project-context.md](references/project-context.md)。
2. 优先使用前端 `npm run test:e2e:prepare:*` 命令生成素材批次，因为它们已处理了跨平台的 Python 命令差异。
3. 尽量复用已有的前后端命令，不要发明第二套应用布局。
4. 浏览器自动化优先使用 Playwright。
5. Fixture 清单保存在 `4-tests/batch-testing/outputs/e2e/fixture-batches/<profile>.json`，运行工件保存在 `4-tests/batch-testing/outputs/e2e/<timestamp>/`。
6. 正式的 Playwright 工程保持在 `3-dev/src/frontend/` 下；不要在仓库根目录额外添加 `playwright.config.*`、`tests/example.spec.*` 或 `e2e/example.spec.*` 脚手架。

## 工作流程

### 0. 检测平台并适配命令

执行任何命令前，先检测当前平台并选择对应的命令风格。

1. 通过 `node -p "process.platform"` 或 Python `platform.system()` 检测 OS。
2. Windows PowerShell：
   - 优先使用 `python` 而非 `python3`
   - 环境变量用 `$env:NAME='value'`
3. macOS/Linux shell：
   - 优先使用 `python3`，仅在需要时回退到 `python`
   - 环境变量用 `NAME=value command` 或 `export NAME=value`
4. 在 `3-dev/src/frontend/` 目录下执行时，优先使用已有的 `npm run test:e2e:*` 入口，它们已经规范化了 Python 查找和 `ASR_E2E_PROFILE` 处理。
5. 在运行工件中记录检测到的平台。

### 1. 选择正确的目标

- 如果用户要求设计或改进浏览器 E2E 策略，先更新 Playwright 流程、素材选择和归档规则。
- 如果用户要求快速回归，使用 `smoke` 批次，除非他们明确要求更广的覆盖。
- 如果用户使用远端 FunASR 节点、带宽有限或需要稳定的五文件回归，使用 `remote-standard`。
- 如果用户要求合并前或发布级别的信心，在本地或高带宽环境使用 `standard` 或 `full`，并扩大断言范围。

### 2. 构建素材批次

使用辅助脚本而非手工挑选文件：

```bash
cd 3-dev/src/frontend
npm run test:e2e:prepare:smoke
npm run test:e2e:prepare:remote-standard
npm run test:e2e:prepare:standard
npm run test:e2e:prepare:full
```

如果需要从仓库根目录直接运行 Python 脚本，按 shell 类型区分：

```bash
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile smoke
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile remote-standard --write
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile standard --write
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile full --output 4-tests/batch-testing/outputs/e2e/fixture-batches/full.json
python3 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --list
```

```powershell
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile smoke
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile remote-standard --write
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile standard --write
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile full --output 4-tests/batch-testing/outputs/e2e/fixture-batches/full.json
python 6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --list
```

脚本输出作为以下信息的唯一来源：

- 哪些文件进入本次运行
- 为什么选择这些文件
- 工件应保存在哪里

### 3. 运行前环境检查

在运行任何浏览器测试前，验证所有依赖：

1. 后端健康：`GET http://localhost:15797/health` → `status: "ok"`。
2. 前端可达：`GET http://localhost:15798` → HTTP 200。
3. 至少有一台在线 FunASR 服务器：有 admin token 时用 `GET /api/v1/servers` → `len(servers) > 0`；无 admin token 时用 `GET /api/v1/stats` → `server_online > 0`。
4. 测试素材存在：`4-tests/batch-testing/assets/1-测试audioFiles/` 包含音视频文件。

任何检查失败时，停止并报告缺失的前置条件，而不是继续运行到令人困惑的浏览器错误。

### 4. 准备环境

- 优先使用项目已有的本地开发流程：后端 `http://localhost:15797`，前端 `http://localhost:15798`。
- 复用仓库中现有的命令，不要发明额外的包装器。
- 如果浏览器自动化依赖缺失，优先在 `3-dev/src/frontend/` 中添加，保持浏览器测试代码靠近前端工具链。
- 浏览器测试输出、截图、trace、结果快照和运行摘要保存在 `4-tests/batch-testing/outputs/e2e/`。
- `4-tests/batch-testing/outputs/e2e/`、`3-dev/src/frontend/test-results/`、`3-dev/src/frontend/playwright-report/` 视为本地工件，不入库。
- 如果尚未注册 FunASR 服务器，通过 `ASR_E2E_SERVER_HOST` 和 `ASR_E2E_SERVER_PORT` 指定；否则测试会复用第一个已注册的服务器。

### 5. 模拟真实用户路径

驱动用户实际操作的完整流程：

1. 打开 `/upload`。
2. 通过上传控件添加多个文件。
3. 设置共享任务选项（语言、ASR 参数等）。
4. 点击「提交转写」。
5. 验证创建的任务数量等于选中的文件数量。
6. 从批量创建响应中捕获 `task_group_id`，用于后续验证。
7. 打开 `/tasks`。
8. 轮询直到任务到达终态（SUCCEEDED / FAILED / CANCELED）。使用 [references/project-context.md](references/project-context.md) 中各 profile 对应的超时。后端也提供 SSE（`GET /api/v1/tasks/{id}/progress`），优先使用 5-10 秒间隔的 API 轮询。
9. 通过 `GET /api/v1/task-groups/{group_id}` 验证批次完整性——确认 `succeeded + failed == total`。
10. 以多种格式下载结果：`GET /api/v1/task-groups/{group_id}/results?format=txt`、`?format=json`、`?format=zip`。

使用 Playwright 时优先通过 `input[type=file].setInputFiles(...)` 添加文件。
Element Plus 拖拽最终走的是同一个上传组件状态，直接文件赋值比模拟拖拽事件更稳定。
只有在任务明确要求验证拖拽行为时才模拟拖拽事件。

### 6. 分层验证

按以下顺序执行断言：

1. **硬性门槛**：页面加载、上传成功、任务创建、任务离开队列、可达终态、结果可下载。
2. **批次门槛**：`GET /api/v1/task-groups/{group_id}` 返回正确的计数；`format=json` 返回合法 JSON 数组；`format=zip` 是可解压的 ZIP 文件且无重复文件名。
3. **结构门槛**：每个成功任务返回非空文本或 JSON 文本内容，已完成结果的数量符合预期。
4. **语义门槛**：如果某个文件有基线，验证期望的关键词或短语；如果没有基线，记录转写文本并标记待审查，而不是发明严格的全文匹配。

除非项目已经为该文件和模型组合维护了 golden baseline，否则不要求精确的转写文本匹配。

### 7. 报告与归档

每次运行都应留下机器可读和人类可读的工件：

- 选中的素材清单（fixture manifest）
- 最终任务状态摘要
- 转写文本快照或下载的 `.txt` 结果
- 浏览器流程失败时的截图或 Playwright trace
- 简洁的运行摘要：本次使用的 profile、成功/失败文件数、需要跟进的内容

## 浏览器测试实现规范

- 本仓库的浏览器 E2E 优先使用 Playwright 而非 Cypress。
- 优先通过可见文本或有意添加的稳定属性定位元素，避免脆弱的 CSS 链式选择器。
- 如果页面缺少稳定选择器，在应用中添加最小量的 `data-testid` 而非编写脆弱的定位器。
- 保持测试逻辑确定性：使用对 API 结果或 UI 状态的显式等待，而非任意 sleep。
- 浏览器测试聚焦于上传到结果的核心流程，不要把不相关的设置或监控覆盖混入同一场景。

## 结果质量规范

- 空转写、不可读的乱码、下载失败均视为失败。
- 在没有人工维护基线时，措辞漂移视为警告而非失败。
- 已知为中文语音的文件，优先检查转写结果包含中文文本和可用的锚定关键词。
- 视频文件应验证系统仍能产出转写结果，而非在预处理阶段失败。

## 平台决策树

1. 运行 `node -p "process.platform"`。
2. 如果结果是 `win32`：
   - 使用 PowerShell 语法，如 `$env:ASR_E2E_SERVER_HOST='127.0.0.1'`
   - 使用 `python` 或前端 `npm run test:e2e:*` 脚本
3. 如果结果是 `darwin` 或 `linux`：
   - 使用 `ASR_E2E_SERVER_HOST=127.0.0.1 command`
   - 使用 `python3` 或前端 `npm run test:e2e:*` 脚本
4. 尽可能优先使用 `npm run test:e2e:smoke` 等封装命令，而非临时拼装的 shell 单行命令。

## 参考文件

- [references/project-context.md](references/project-context.md)：路由、页面行为、素材策略、工件位置、验收策略
- [references/semantic-baseline.json](references/semantic-baseline.json)：语义校验基线（关键词匹配）
