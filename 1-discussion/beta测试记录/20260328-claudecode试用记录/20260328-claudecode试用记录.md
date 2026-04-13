# 20260328 Claude Code 试用记录

> 会话时间：2026-03-28 约 07:30 ~ 18:40 (UTC+8)
> 使用工具：Claude Code CLI (GLM-5.1)
> 操作人：wangminle

---

## 一、会话总览

本次 Claude Code 会话覆盖了三个主要阶段：

1. **项目完成度检查** — 确认 funasr-task-manager 开发与测试任务是否全部完成
2. **调度器问题诊断与修复** — 分析 E2E 测试中暴露的调度缺陷，提出并实施 RTF-aware 调度方案
3. **量化课程批量转写测试** — 使用修复后的调度器对 8 集量化课程视频进行端到端转写测试

---

## 二、阶段一：项目完成度检查

### 输入

用户提问：*"检查当前项目是否已经如期完成全部开发和测试任务"*

### 分析过程

Claude Code 依次查阅了以下关键文档和目录：

- `2-design/项目完整性评估报告-20260312.md` — 完整性评估 98%
- `2-design/项目进展检查与开发建议-20260313.md` — 13 项原始开发任务全部完成
- `4-tests/scripts/` 下的测试文件 — 确认 184 个单元测试 + 34 个集成测试
- `4-tests/scripts/pytest.ini` — 测试标记配置（unit/integration/e2e/load/slow）
- Git 历史 — 最近提交为 `V0.2.0-Build0089-20260327`

### 结论

所有开发和测试任务均已完成。E2E 测试依赖真实 FunASR 服务器，脚本已就绪。

---

## 三、阶段二：调度器问题诊断与修复

### 3.1 问题发现

用户报告了 20260327 晚间两次 E2E 测试结果：

| 测试 | 方案 | 结果 | 备注 |
|------|------|------|------|
| 20260327-223619 | remote-standard | 5/5 通过 | 小文件，28s 完成 |
| 20260327-223928 | full | 8/8 通过 | 大文件，15.4min 完成 |

**核心问题**：60 分钟的 `GuruMoringTeaching.mp3`（178.9 MB）被分配到了算力较弱的服务器，耗时 925 秒成为瓶颈。

### 3.2 根因分析

Claude Code 读取了调度器核心源码，定位出 4 个 Bug：

**Bug 1：rtf_baseline 未传递** (`task_runner.py:150-159`)

```python
ServerProfile(
    server_id=srv.server_id,
    host=srv.host,
    port=srv.port,
    max_concurrency=srv.max_concurrency,
    running_tasks=running_count.get(srv.server_id, 0),
    # BUG: missing rtf_baseline=srv.rtf_baseline
    # BUG: missing penalty_factor=srv.penalty_factor
)
```

`ServerInstance` 数据库模型有 `rtf_baseline` 和 `penalty_factor` 字段，但构建 `ServerProfile` 时从未读取，导致所有服务器都使用 `DEFAULT_RTF=0.3`。

**Bug 2：逐任务调度** — `_dispatch_queued_tasks()` 使用 `assign_single_task()` 循环调度，无法进行全局 LPT 优化。

**Bug 3：无初始 RTF 基准** — `server_probe.py` 只检测能力（协议版本、模式），从未测量实际转写速度。

**Bug 4：RTF 校准数据仅存内存** — `RTFTracker` 的数据重启后丢失。

### 3.3 修复方案

提出了 4 项修复措施，并实施了前两项：

| 修复 | 内容 | 状态 |
|------|------|------|
| Fix 1 | 传递 rtf_baseline 和 penalty_factor | 已实施 |
| Fix 2 | 改用 schedule_batch() 批量调度 | 已实施 |
| Fix 3 | 服务器注册时 RTF 基准探测 | 待实施 |
| Fix 4 | RTF 校准数据持久化到数据库 | 待实施 |

**Fix 1 代码变更** (`task_runner.py`)：

```python
ServerProfile(
    ...,
    rtf_baseline=srv.rtf_baseline,      # 新增
    penalty_factor=srv.penalty_factor,   # 新增
)
```

**Fix 2 代码变更** (`task_runner.py`)：

将逐任务 `assign_single_task()` 循环替换为先收集可调度任务，再一次性调用 `schedule_batch()`。

### 3.4 调度模拟验证

创建了 `4-tests/scripts/analysis/test_scheduler_simulation.py`，模拟 4 种场景：

| 场景 | 预估 Makespan | 对比 |
|------|--------------|------|
| 当前（单任务 + 统一 RTF） | ~925s | 基线 |
| 批量 + 统一 RTF | ~603s | -35% |
| 批量 + RTF-aware | ~545s | **-41%** |
| 理论最优 | ~545s | -41% |

RTF-aware 批量调度理论改善 **41%**。

### 3.5 FunASR 线程模型研究

在诊断过程中，用户询问了 FunASR C++ WebSocket 服务器的线程参数是否影响单文件转写速度。Claude Code 直接从 GitHub 读取了源码：

- `runtime/websocket/bin/funasr-wss-server.cpp`
- `runtime/websocket/bin/websocket-server.h`
- `runtime/websocket/bin/websocket-server.cpp`

**结论**：三个参数只增加并发处理能力，不影响单文件速度：

| 参数 | 作用 | 影响单文件？ |
|------|------|:---:|
| `--io-thread-num` | WebSocket 网络 I/O | 否 |
| `--decoder-thread-num` | 解码工作线程池大小 | 否 |
| `--model-thread-num` | ONNX Runtime 推理线程数（默认=1） | 否 |

源码证据：每个音频文件由 1 个 decoder 线程处理，调用一次 `FunOfflineInferBuffer()`。

### 3.6 提交

所有变更以一个 commit 提交：

```
feat: RTF-aware capacity scheduling + VAD并行转写方案研究
```

19 个文件变更，含：
- 调度器修复（scheduler.py, task_runner.py）
- 新增 RTF 基准测试和调度模拟脚本
- VAD 并行转写方案研究文档
- 早期调研文档归档

---

## 四、阶段三：量化课程批量转写测试

### 4.1 任务输入

用户提供测试素材路径：

```
7-data/assets/2-测试量化课程/
```

Claude Code 探测后发现 8 个 MP4 视频文件（非用户说的 10 个），总计约 370 MB：

| 文件 | 大小 |
|------|------|
| 第1集 不知量价，不懂主力.mp4 | 14 MB |
| 第2集 成交量的五大作用.mp4 | 37 MB |
| 第3集 形神分析法.mp4 | 41 MB |
| 第4集 整体法和个体法.mp4 | 83 MB |
| 第5集 温和放量.mp4 | 49 MB |
| 第6集 脉冲放量.mp4 | 64 MB |
| 第7集 持续放量.mp4 | 51 MB |
| 第8集 长期放量.mp4 | 33 MB |

### 4.2 方案选择

Claude Code 提出了两种执行方案：

| 方案 | 说明 | 优缺点 |
|------|------|--------|
| A: API 直接调用 | 通过 curl 调用后端 REST API | 快速可靠，无需前端 |
| B: Playwright 自动化 | 按现有 E2E skill 模式走浏览器 | 更贴近真实用户，但更慢 |

用户确认：*"立即开始，方案 A，一次性发给 task manager 让它自己调度"*

### 4.3 环境检查

```
后端状态：UP（运行 15h26m）
funasr-10095：ONLINE | RTF=0.124 | max_concurrency=4
funasr-10096：ONLINE | RTF=0.737 | max_concurrency=4
funasr-10097：ONLINE | RTF=0.656 | max_concurrency=4
当前运行任务：0
```

服务器空闲，无需等待 5 分钟，立即开始。

### 4.4 执行步骤

**Step 1：上传文件**（08:33 ~ 08:39）

通过 `POST /api/v1/files/upload` 并行上传 8 个文件。第一批 4 个文件并行上传（2-5 集），第二批 3 个文件并行上传（6-8 集），第 1 集单独先上传。

**Step 2：创建任务**（08:34 ~ 08:39）

第 1 集先创建任务（08:34:19），剩余 7 集通过一次 `POST /api/v1/tasks` 批量创建（08:39:53），传入 7 个 file_id。

**Step 3：调度器自动分配**

Task manager 的调度器自动将 8 个任务分配到 3 台服务器：
- funasr-10095（最快，RTF=0.124）分到 4 个任务（含最大的第 4 集 83MB）
- funasr-10096（最慢，RTF=0.737）分到 4 个任务
- funasr-10097（中等，RTF=0.656）分到 0 个任务（见 4.6 分析）

**Step 4：轮询等待**（08:40 ~ 08:43）

通过 `GET /api/v1/tasks` 每 15 秒轮询一次，约 4 分钟后 8/8 全部 SUCCEEDED。

**Step 5：结果归档**

下载所有转写文本，保存到标准 E2E 输出目录：
```
4-tests/batch-testing/outputs/e2e/20260328-083340/
├── run-summary.md
├── run-summary.json
└── results/
    ├── 第1集-不知量价不懂主力.txt (3.6 KB)
    ├── 第2集-成交量的五大作用.txt (9.0 KB)
    ├── 第3集-形神分析法.txt (9.2 KB)
    ├── 第4集-整体法和个体法.txt (17 KB)
    ├── 第5集-温和放量.txt (11 KB)
    ├── 第6集-脉冲放量.txt (14 KB)
    ├── 第7集-持续放量.txt (9.9 KB)
    └── 第8集-长期放量.txt (6.2 KB)
```

### 4.5 测试结果

| 文件 | 服务器 | 耗时 | 转写字数 |
|------|--------|------|---------|
| 第1集 不知量价，不懂主力.mp4 | funasr-10095 | 41s | 1,219 |
| 第2集 成交量的五大作用.mp4 | funasr-10095 | 60s | 3,058 |
| 第3集 形神分析法.mp4 | funasr-10095 | 64s | 3,103 |
| 第4集 整体法和个体法.mp4 | funasr-10095 | 139s | 5,736 |
| 第5集 温和放量.mp4 | funasr-10096 | 157s | 3,543 |
| 第6集 脉冲放量.mp4 | funasr-10096 | 237s | 4,693 |
| 第7集 持续放量.mp4 | funasr-10096 | 99s | 3,333 |
| 第8集 长期放量.mp4 | funasr-10096 | 54s | 2,133 |

- **通过率**：8/8（100%）
- **总转写字数**：26,918 字
- **批处理耗时**：237 秒（受限于 10096 上的第 6 集）

### 4.6 调度分配分析

本次测试的调度分配：

```
funasr-10095 (RTF=0.124, 快): 4 任务 ─ 第1~4集
funasr-10096 (RTF=0.737, 慢): 4 任务 ─ 第5~8集
funasr-10097 (RTF=0.656, 中): 0 任务 ─ 未参与
```

**funasr-10097 未被使用的原因**：第 1 集在批量任务创建之前就已单独创建并分配到 10095（占 1/4 slot）。剩余 7 个任务通过 `schedule_batch()` 调度时：
- 10095 有 3 个空闲 slot
- 10096 有 4 个空闲 slot
- 10097 有 4 个空闲 slot
- 3 + 4 = 7 个 slot 恰好容纳 7 个任务

调度器按 RTF 排序优先填满最快服务器的 slot，然后溢出到次快的服务器。因为 10095 + 10096 的空闲 slot 已经足够，所以 10097 没有被使用。

**优化空间**：如果充分利用 3 台服务器，理论上可以缩短到 ~170 秒。这印证了 VAD 分段并行方案的必要性 — 通过将大文件拆分成子任务，可以更均匀地分布到所有可用服务器。

---

## 五、Claude Code 能力观察

### 优势

1. **代码阅读与理解**：能准确阅读 Python/C++ 源码，定位 bug 和理解架构
2. **多源信息整合**：同时分析本地代码、GitHub 源码、E2E 测试数据
3. **自动化执行**：直接通过 API 上传文件、创建任务、轮询结果，无需人工干预
4. **方案比较**：提出多种方案并用数据对比（模拟 vs 实际）
5. **文档生成**：自动归档测试结果，生成结构化报告

### 局限

1. **GitHub 源码检索受限**：FunASR 的 C++ 源码在 GitHub 上路径有变化，多次 404，最终通过 `git tree` API + raw URL 组合才找到正确路径
2. **Web 搜索不稳定**：多个搜索查询（FunASR threading、中文搜索等）返回空结果
3. **会话上下文长度**：长会话中上下文被压缩，部分早期细节需要重新读取文件确认

---

## 六、后续计划

基于本次测试发现的问题和讨论，建议的后续工作：

1. **实现 Fix 3/4**：服务器注册时 RTF 基准探测 + RTF 校准数据持久化
2. **VAD 分段并行**：对超大音视频文件在客户端侧用 VAD 拆分，分配到多台服务器并行转写，最后拼接结果
3. **调度器均衡性优化**：确保所有在线服务器都被利用，避免某台服务器闲置
4. **commit 归档**：将本次测试结果提交到 git

---

*文档生成时间：2026-03-28*
