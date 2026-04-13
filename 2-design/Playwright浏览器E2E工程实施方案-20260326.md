# Playwright 浏览器 E2E 工程实施方案

> 日期：2026-03-26
> 定位：实施方案 / 落地计划

**Goal:** 在前端目录正式落地 Playwright 浏览器 E2E 工程，补齐 smoke 级上传到结果测试链路，并提供标准 npm 命令入口。

**Architecture:** 以前端目录作为 Playwright 宿主，使用 Playwright 配置同时拉起后端和前端开发服务，测试通过 fixture batch 选择小文件样本。fixture 清单写入 `4-tests/batch-testing/outputs/e2e/fixture-batches/<profile>.json`，单次运行的截图和摘要写入 `4-tests/batch-testing/outputs/e2e/<timestamp>/`。生产代码最小变更仅限于为上传页和任务列表页添加稳定的 `data-testid`。

**Tech Stack:** Vue 3, Vite, Playwright, Node.js, FastAPI, Python helper script.

## 跨平台约束

- npm 命令统一使用 `cross-env` 传递 `ASR_E2E_PROFILE`，避免 Windows 下环境变量失效。
- fixture prepare 统一通过 `node ./scripts/run-python.js` 调用 Python，避免 Windows 缺少 `python3`。
- Playwright 后端启动统一通过 `node ./scripts/start-backend.js` 顺序执行迁移和 Uvicorn，避免 shell `&&` 链接依赖具体终端语法。
- 文档中的实际工程文件统一以 `.js` 为准，除非未来整体迁移到 TypeScript。

---

### Task 1: 建立 Playwright 测试骨架

**Files:**
- Create: `3-dev/src/frontend/playwright.config.js`
- Create: `3-dev/src/frontend/tests/e2e/upload-to-result.spec.js`
- Create: `3-dev/src/frontend/tests/e2e/helpers/fixture-batch.js`
- Create: `3-dev/src/frontend/tests/e2e/helpers/artifacts.js`
- Create: `3-dev/src/frontend/scripts/run-python.js`
- Create: `3-dev/src/frontend/scripts/start-backend.js`

**Step 1: 写失败测试**

创建 smoke 测试，使用期望中的 `data-testid` 和标准路由，断言上传区、提交按钮、任务列表可被定位。

**Step 2: 运行验证失败**

运行: `npx playwright test tests/e2e/upload-to-result.spec.js --grep @smoke`

预期: 因页面尚无 `data-testid` 或配置未完成而失败。

**Step 3: 增加最小测试基础设施**

补齐 Playwright 配置、fixture 读取与工件归档 helper，但不修改业务行为。

**Step 4: 再跑一次确认仍失败在业务钩子**

预期: 测试能启动，但仍因页面缺少稳定钩子失败。

### Task 2: 添加稳定测试钩子

**Files:**
- Modify: `3-dev/src/frontend/src/views/UploadView.vue`
- Modify: `3-dev/src/frontend/src/views/TaskListView.vue`

**Step 1: 为上传页添加最小 `data-testid`**

添加 `upload-dropzone`、`submit-transcribe`、`pending-files-table`、`created-tasks-table`。

**Step 2: 为任务页添加最小 `data-testid`**

添加 `task-list-table` 以及下载按钮稳定标识。

**Step 3: 重新运行 smoke 测试**

预期: 页面定位步骤转绿，若后续失败则失败在服务编排或结果归档逻辑。

### Task 3: 打通完整 smoke E2E 链路

**Files:**
- Modify: `3-dev/src/frontend/playwright.config.js`
- Modify: `3-dev/src/frontend/tests/e2e/upload-to-result.spec.js`

**Step 1: 配置前后端服务启动**

使用 Playwright `webServer` 同时启动后端和前端，端口固定为 8000/5173。

**Step 2: 在测试中注册远程 FunASR 节点并执行上传到结果流程**

优先使用 smoke 批次中的小文件，避免占用远程带宽。

**Step 3: 归档截图、文本结果与摘要**

把 run-summary 和截图写入 `4-tests/batch-testing/outputs/e2e/<timestamp>/`。

**Step 4: 运行 smoke 测试并验证通过**

运行: `npm run test:e2e:smoke`

预期: 通过并生成工件。

### Task 4: 增加标准命令入口

**Files:**
- Modify: `3-dev/src/frontend/package.json`

**Step 1: 添加 fixture prepare 与 smoke 命令**

增加 `test:e2e:prepare:smoke`、`test:e2e:smoke`。

**Step 2: 预留 standard/full 命令入口**

增加 `test:e2e:prepare:standard`、`test:e2e:prepare:full` 及对应执行脚本。

**Step 3: 运行脚本验证命令可用**

运行: `npm run test:e2e:prepare:smoke`

预期: 生成 smoke fixture manifest。

## 新机器首次准备

1. 在 `3-dev/src/frontend/` 执行 `npm install`
2. 执行 `npx playwright install chromium`
3. 如果是 Windows PowerShell 网络受限环境，先执行 `$env:PLAYWRIGHT_DOWNLOAD_HOST='https://npmmirror.com/mirrors/playwright'`
4. 优先使用 `npm run test:e2e:*` 标准命令，而不是手写环境变量前缀

## CI/CD 建议接入方式

1. 以 smoke 作为最小门禁，保留 remote-standard 作为手动触发或定时任务
2. 在流水线中显式提供 `ASR_E2E_SERVER_HOST` 与 `ASR_E2E_SERVER_PORT`，避免依赖临时人工注册状态
3. 归档 `4-tests/batch-testing/outputs/e2e/`，至少保留 `run-summary.json`、`run-summary.md`、`results/`、`screenshots/`
4. 语义基线文件使用 `6-skills/funasr-task-manager-web-e2e/references/semantic-baseline.json`，由业务确认后维护关键词

### Task 5: 更新设计文档状态

**Files:**
- Modify: `2-design/端到端测试现状、Playwright安装结论与剩余问题-20260326.md`

**Step 1: 更新 Playwright 工程完成度**

把“缺少 Playwright 工程骨架”从剩余问题中移除或降级。

**Step 2: 更新今天的执行证据**

记录标准 npm 命令和新工件路径。
