# 端到端测试现状、Playwright 安装结论与剩余问题

> 日期：2026-03-26
> 项目：funasr-task-manager
> 目的：汇总今天的浏览器端到端测试结果、Playwright 依赖现状、剩余问题，以及后续应采用的 E2E 建设方案。
>
> 2026-03-27 补充：
> - 前端目录下的正式 Playwright 工程已成为当前唯一推荐入口：`3-dev/src/frontend/playwright.config.js`
> - `remote-standard` 已在 macOS（`darwin`）环境再次验证通过，5/5 成功
> - `4-tests/batch-testing/outputs/e2e/`、`3-dev/src/frontend/test-results/`、`3-dev/src/frontend/playwright-report/` 已按本地测试工件处理，不应提交 Git

---

## 一、结论摘要

### 1.1 是否要参考现有文档

要。

本项目后续浏览器 E2E 建设与执行，应同时参考以下两类文档：

1. 设计基线文档：`2-design/端到端浏览器测试Skill设计方案-20260319.md`
2. 执行规范与项目上下文：
   - `6-skills/funasr-task-manager-web-e2e/SKILL.md`
   - `6-skills/funasr-task-manager-web-e2e/references/project-context.md`

三者分工如下：

- `端到端浏览器测试Skill设计方案-20260319.md` 负责说明为什么这样设计、测试分层、目录布局和后续建设路线。
- `SKILL.md` 负责规定 Agent 实际执行浏览器 E2E 时应遵循的工作流。
- `project-context.md` 负责补充仓库特有的页面、路由、素材、超时和归档规则。

因此，后续如果要把浏览器 E2E 做成仓库内长期可复用的正式能力，应以该设计文档为建设蓝图，以 skill 文档为运行规范。

### 1.2 今天的浏览器 E2E 结果概览

成功。

今天实际上完成了多轮浏览器 E2E：

1. 下午通过 Agent 浏览器自动化完成一次真实 smoke，产物位于：`4-tests/batch-testing/outputs/e2e/20260326-162641/`
2. 晚上通过仓库内正式 Playwright 工程完成首次 smoke，产物位于：`4-tests/batch-testing/outputs/e2e/20260326-191950/`
3. 修复路径与结果映射问题后，再次通过正式 Playwright 工程回归，最新产物位于：`4-tests/batch-testing/outputs/e2e/20260326-192816/`
4. 使用重型 `standard` 批次回归，结果为 4/5 成功、1 个大 mp3 在 30 分钟上限超时，产物位于：`4-tests/batch-testing/outputs/e2e/20260326-193618/`
5. 使用正式 `remote-standard` 批次回归，结果 5/5 成功，产物位于：`4-tests/batch-testing/outputs/e2e/20260326-205431/`

当前最有代表性的结论是：

- 正式 `smoke`：3/3 成功
- 重型 `standard`：4/5 成功，178.9 MB mp3 超时
- `remote-standard`：5/5 成功

因此，后续应区分两种“标准回归”：

- `standard`：保留给本地或资源较充足环境
- `remote-standard`：固定为按大小排序的前 5 个文件，作为远端 FunASR 节点推荐标准批次

`remote-standard` 对应的 5 个文件为：

- `test001.wav`
- `tv-report-1.wav`
- `test.mp4`
- `办公平台20250724-144218.mp4`
- `tv-report-1.mp4`

最新成功摘要见：

- `4-tests/batch-testing/outputs/e2e/20260326-205431/run-summary.json`
- `4-tests/batch-testing/outputs/e2e/20260326-205431/run-summary.md`

### 1.3 Playwright 是否可以直接只依赖系统安装

不建议。

当前系统环境下可以通过 `npx playwright --version` 临时拉起 Playwright CLI，但这种方式不适合作为项目内长期稳定、可复现、可交接的测试依赖来源。

原因如下：

1. 它不在项目的 `package.json` 中声明，团队成员或 CI 环境无法直接复现。
2. 版本会漂移，不利于固定测试运行时。
3. 无法证明项目已经具备“仓库内可执行的浏览器 E2E 基础设施”。

因此，正确做法仍然是把 Playwright 依赖安装到项目目录中。

---

## 二、今天对 Playwright 的实际检查与安装结果

### 2.1 安装前状态

安装前，前端项目 `3-dev/src/frontend/package.json` 中没有任何 Playwright 依赖，也没有以下文件：

- `playwright.config.*`
- `tests/e2e/`
- 与浏览器 E2E 相关的 `npm scripts`

这说明仓库此前尚未把浏览器 E2E 正式落地为前端工程的一部分。

### 2.2 已完成的安装

今天已将以下依赖安装到前端项目目录：

- `@playwright/test`

安装后，`3-dev/src/frontend/package.json` 中已包含：

```json
"devDependencies": {
  "@playwright/test": "^1.58.2",
  "@vitejs/plugin-vue": "^5.2.0",
  "cross-env": "^10.1.0",
  "vite": "^6.0.0"
}
```

同时，前端目录已补齐基础 Playwright 工程骨架：

- `3-dev/src/frontend/playwright.config.js`
- `3-dev/src/frontend/tests/e2e/upload-to-result.spec.js`
- `3-dev/src/frontend/tests/e2e/helpers/fixture-batch.js`
- `3-dev/src/frontend/tests/e2e/helpers/artifacts.js`
- `package.json` 中的 `test:e2e:*` 命令入口
- `build_fixture_batch.py` 中新增 `remote-standard` profile

### 2.3 未完成的部分

`npx playwright install chromium` 在今天的安装过程中确实出现过网络超时和 `ECONNRESET`，但这不再构成当前机器上的阻塞项。

实际情况已经更新为：

- 当前用户缓存目录中已有可用浏览器二进制
- 项目内 `npm run test:e2e:smoke` 已成功拉起 Chromium 并完整执行用例

因此，本机环境中的 Playwright 运行时已经可用；真正剩下的问题是“换一台新机器时仍需准备浏览器缓存或重新安装”。

### 2.4 当前可用结论

截至本次记录时，Playwright 状态如下：

| 项目 | 状态 | 说明 |
|------|------|------|
| 系统临时 `npx playwright` | 可用 | 可查看版本，不适合作为正式项目依赖 |
| 项目内 `@playwright/test` 依赖 | 已安装 | 已写入前端 `package.json` |
| 当前机器上的 Chromium 运行时 | 可用 | 已被正式 smoke 用例实际使用 |
| 新机器上的浏览器准备 | 待确认 | 首次部署仍可能受网络与缓存影响 |

因此，当前结论是：

- **依赖层面**：项目内安装已经完成
- **运行时层面**：当前机器已可运行，跨机器复现仍需补一次浏览器准备说明

---

## 三、今天的浏览器端到端测试是如何完成的

今天下午那次 smoke 浏览器 E2E，是按照 `funasr-task-manager-web-e2e` skill 的工作流执行的，但不是通过“仓库内现成 Playwright 测试工程”完成的。

实际执行方式如下：

1. 读取 skill 文档与项目上下文文档
2. 运行 `build_fixture_batch.py --profile smoke --write`
3. 启动本地前后端
4. 复用已注册的 FunASR 节点，或通过环境变量指定目标节点
5. 用浏览器自动化工具打开 `/upload`
6. 通过 `input[type=file].setInputFiles(...)` 注入测试文件
7. 提交转写并确认创建任务数量正确
8. 通过 API 轮询等待任务进入终态
9. 下载/归档转写结果、截图和摘要

这条链路已经证明：

- 前端上传页主流程可用
- 后端上传、建任务、调度、转写、结果下载链路可用
- 远程 FunASR 节点可被正常使用

而今晚补齐工程化后，`npm run test:e2e:smoke` 已经可以直接在前端项目内完成同样流程。这意味着：

- 浏览器 E2E 不再只是“通过 Agent 执行的能力”
- 它已经具备“仓库内可直接运行的 smoke 测试工程”雏形

---

## 四、剩余问题清单

以下问题按优先级排序。

### 4.1 P1：Playwright 工程已具备 smoke 与 remote-standard 闭环

当前前端目录已经补齐：

- `playwright.config.js`
- `tests/e2e/`
- `upload-to-result.spec.js`
- `test:e2e:prepare:*`
- `test:e2e:smoke`
- `test:e2e:remote-standard`

影响：

- smoke 与 remote-standard 已能被团队成员以标准命令重复执行。
- 但更重的 `standard` / `full` 场景、trace 归档增强、CI 接入仍未补齐。

### 4.2 ~~P1：跨机器的浏览器运行时准备仍需补文档~~ ✅ 已解决

当前机器已经能直接跑通 Playwright smoke，但新机器首次安装时仍可能遇到浏览器二进制下载问题。

**已补齐（2026-03-27 更新）：**

浏览器缓存/镜像源/离线安装说明已在 `project-context.md` 的「Playwright 浏览器安装」章节补齐，包含：

- 通用安装命令
- Windows PowerShell 网络受限场景
- macOS/Linux 网络受限场景
- 离线缓存位置（Windows/macOS/Linux）

影响：

- 此问题已闭环，新机器准备说明可通过 `project-context.md` 直接获取。

### 4.3 P1：前端仅补齐了最小一组 `data-testid`

当前已补齐以下稳定钩子：

- `upload-dropzone`
- `submit-transcribe`
- `pending-files-table`
- `created-tasks-table`
- `task-list-table`

这已经足够支撑 smoke 主流程，但仍未覆盖更细粒度交互，例如：

- 单任务结果下载按钮
- 状态筛选器
- 任务行级操作

影响：

- 后续扩大到 standard / full 场景时，仍建议继续补更细的选择器。

### 4.4 P2：标准命令入口已建立，当前主要剩余的是扩展与编排优化

当前仓库已具备：

- `npm run test:e2e:prepare:smoke`
- `npm run test:e2e:prepare:remote-standard`
- `npm run test:e2e:prepare:standard`
- `npm run test:e2e:prepare:full`
- `npm run test:e2e:smoke`
- `npm run test:e2e:remote-standard`
- `npm run test:e2e:standard`
- `npm run test:e2e:full`

影响：

- 当前命令入口已经满足人工执行和后续 CI 对接的最小要求。
- 后续更值得补的是多场景 trace 归档、失败重试策略和 CI 编排参数化。

### 4.5 P2：语义基线框架已落地，关键词待填充

当前 smoke 回归验证了：

- 页面加载成功
- 文件能进入待上传列表
- 任务数量正确
- 任务进入终态
- 文本结果非空且包含中文字符

**已完成的语义基线基础设施（2026-03-27 更新）：**

- `tests/e2e/helpers/semantic-baseline.js`：基线加载与关键词判定逻辑
- `6-skills/funasr-task-manager-web-e2e/references/semantic-baseline.json`：语义基线模板文件
- 测试执行时自动读取并应用基线，区分 `configured`/`not-configured` 状态

**待业务填充：**

当前基线模板中 `keywords_all`/`keywords_any` 为空数组，需要业务确认后填充具体关键词。填充后可识别”结果质量明显下降但仍非空”的问题。

影响：

- 当前能发现大部分链路回归，语义校验能力已就绪，待关键词填充后生效。

### 4.6 P2：页面仍存在小的静态资源问题

本次浏览器执行中看到 `favicon.ico` 404。

影响较小，不影响核心功能，但会在浏览器控制台产生噪音。

### 4.7 P2：上传阶段仍会出现 `ffprobe not found` 元数据告警

本次正式 Playwright smoke 中，上传接口日志仍出现：

- `metadata_extraction_failed`
- `error='ffprobe not found. Install ffmpeg.'`

不过转写主流程没有因此失败，因为后续音频转换实际使用了 `imageio_ffmpeg` 提供的 ffmpeg 二进制，视频样本仍成功转写。

影响：

- 当前问题不阻塞主链路
- 但会让上传阶段日志存在误报，后续应统一元数据提取与音频转换的 ffmpeg/ffprobe 发现逻辑

### 4.8 P2：旧 bug 报告需要一次重新验收，而不是直接沿用旧结论

`4-tests/Code-Review/bug_report_2026-03-01.md` 中提到的一些问题，当前代码抽查显示至少部分已经修复，例如：

- 重试状态双写问题
- ETA penalty 校准分支问题
- CORS 全通配 + credentials 冲突问题
- `result_formatter.py` 中的死代码问题
- 旧 `probe.py` 与 `server_probe.py` 双实现冲突问题也已看不到旧文件

因此，这份旧报告不能再原样当作当前问题清单使用。

仍建议单独做一次新的代码审查，用于生成截至 2026-03-26 的最新问题基线。

---

## 五、推荐的端到端测试方案

以下是截至今天最合理的正式化建设方案。

### 5.1 目录布局

浏览器 E2E 主体已经按设计文档落在前端目录：

```text
3-dev/src/frontend/
├── tests/
│   └── e2e/
│       ├── upload-to-result.spec.js
│       └── helpers/
│           └── fixture-batch.js
├── playwright.config.js
└── package.json
```

理由：

- 与 Vite / npm 工具链自然同目录
- 团队成员更容易执行
- 不需要把 Node 浏览器测试硬塞进 Python pytest 结构

### 5.2 运行入口

当前前端 `package.json` 已增加 prepare 与 smoke 入口，下一步建议补齐 direct run 入口：

```json
{
  "scripts": {
    "test:e2e:prepare:smoke": "node ./scripts/run-python.js ../../../6-skills/funasr-task-manager-web-e2e/scripts/build_fixture_batch.py --profile smoke --write",
    "test:e2e:smoke": "npm run test:e2e:prepare:smoke && cross-env ASR_E2E_PROFILE=smoke npx playwright test tests/e2e/upload-to-result.spec.js",
    "test:e2e:standard": "npm run test:e2e:prepare:standard && cross-env ASR_E2E_PROFILE=standard npx playwright test tests/e2e/upload-to-result.spec.js",
    "test:e2e:full": "npm run test:e2e:prepare:full && cross-env ASR_E2E_PROFILE=full npx playwright test tests/e2e/upload-to-result.spec.js"
  }
}
```

### 5.2.1 跨平台运行说明

- Windows PowerShell：优先直接执行 `npm run test:e2e:*`，避免手写 `$env:` 与 `python` 组合时漏配环境变量。
- macOS/Linux：同样优先使用 `npm run test:e2e:*`；如需直跑 Python，使用 `python3`。
- Playwright 浏览器安装：首次运行前执行 `npx playwright install chromium`；网络受限时可按项目上下文文档设置 `PLAYWRIGHT_DOWNLOAD_HOST`。

### 5.3 测试分层

应继续沿用当前 skill 设计的三档：

| 档位 | 用途 | 说明 |
|------|------|------|
| smoke | 日常快速回归 | 最小 3 文件，重点验证主链路通，当前已落地 |
| standard | 合并前验证 | 覆盖更长音频、更重视频、更多格式 |
| full | 发版前回归 | 覆盖全部素材 |

### 5.4 断言策略

建议分三层执行：

1. 硬性断言：页面可用、上传成功、任务创建正确、任务进入终态、结果可下载
2. 结构断言：结果非空、视频不因预处理失败而退出
3. 语义断言：如有基线则校验关键词，无基线则只记录文本并提示人工审阅

### 5.5 工件归档

当前已经验证有效的目录结构如下：

```text
4-tests/batch-testing/outputs/e2e/<timestamp>/
├── fixture-batch.json
├── run-summary.json
├── run-summary.md
├── results/
├── screenshots/
└── traces/
```

今天的 `20260326-162641/` 和 `20260326-191950/` 都已经证明该归档方式可行。当前实现已稳定写入 `fixture-batch.json`、`run-summary.*`、`results/` 和 `screenshots/`；`traces/` 仍可在后续增强中统一归档。

另需区分两类路径：固定的 fixture 清单位于 `4-tests/batch-testing/outputs/e2e/fixture-batches/<profile>.json`，单次运行产物位于 `4-tests/batch-testing/outputs/e2e/<timestamp>/`。

---

## 六、建议的后续执行顺序

建议按以下顺序推进。

### 第一优先级

1. 把 trace 文件也复制归档到 `4-tests/batch-testing/outputs/e2e/<timestamp>/`
2. 给更多任务操作补细粒度 `data-testid`
3. 补一份新机器 Playwright 浏览器准备说明
4. 评估是否将后端 Python 环境路径配置写入更稳定的启动脚本

### remote-standard 定位

- `remote-standard`：远端 FunASR 节点推荐标准批次，固定使用最小的 5 个文件，已实测 5/5 成功
- `standard`：保留为更重的本地/高资源环境标准回归，不再建议直接作为远端默认批次

### 第二优先级

1. 新增 standard / full 场景用例或参数化扩展
2. 建立关键词或参考文本基线
3. 评估接入 CI

### 第三优先级

1. 统一 `ffprobe` 与 `ffmpeg` 的依赖发现逻辑
2. 处理 `favicon.ico` 404 等非核心噪音
3. 做一次新的代码审查，替换旧 bug 报告基线

---

## 七、最终判断

截至 2026-03-26，可以做出以下判断：

1. 项目的真实浏览器主链路今天已经被成功验证两次，其中一次还是通过仓库内正式 Playwright 工程完成的。
2. `funasr-task-manager-web-e2e` skill 已经能指导 Agent 正确执行浏览器 E2E。
3. 仓库已经完成了浏览器 E2E 的 smoke 工程化落地，因此“测试能力已验证”现在也对应了一套最小可运行测试工程。
4. 但测试体系还没有完全建设完成，主要差在 standard/full、跨机器浏览器准备说明、trace 归档和更细粒度断言。

换句话说：

- **功能链路层面**：今天已经证明可用。
- **工程体系层面**：smoke 已正式落地，完整体系仍需继续补齐。
