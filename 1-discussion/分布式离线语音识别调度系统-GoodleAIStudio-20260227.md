


这是一个非常典型且工程价值极高的**分布式离线重计算（Heavy-Compute）任务调度系统**。对于语音识别（ASR）这种高耗时、重资源（CPU/GPU）的离线任务，集中式管理和分布式调度的架构是目前的行业标配。

基于你的需求，我为你梳理了**最佳实践架构设计、技术栈推荐**以及**相关的开源项目参考**。

---

### 一、 核心技术栈建议（Python体系）

考虑到你们团队处于起步阶段并希望使用Python，Python在该领域（AI后台调度）拥有最成熟的生态。建议采用以下技术栈：

*   **接入层/API服务**：`FastAPI`。支持异步高并发，极其适合处理大量文件上传请求，自带Swagger文档，方便前后端分离开发。
*   **任务调度与队列**：`Celery` + `RabbitMQ`（或 `Redis`）。**Celery** 是Python界分布式任务队列的绝对标准。强烈建议消息代理（Broker）使用 **RabbitMQ**，因为它原生支持**优先级队列（Priority Queue）**，为你后期实现“高优先级插队”提供了开箱即用的支持。
*   **状态与缓存管理**：`Redis`。用于存储临时状态、计算并网关节点资源、管理实时进度以及缓存ETA（预估完成时间）。
*   **持久化数据库**：`PostgreSQL`。用于存储用户数据、文件统一编号（UUID）及其元数据（时长、音视频类型等）、任务历史和状态。
*   **文件存储**：`MinIO`（开源的S3兼容对象存储）。**强烈建议不要用本地文件系统**。作为集中式管理器，你需要对接多个分布式ASR节点，使用 MinIO 可以让各个ASR计算节点通过网络拉取文件，实现真正的分布式计算与状态解耦。
*   **音视频预处理**：`FFmpeg` + `ffmpeg-python` 或 `pydub`。在中间件层进行格式检查、时长提取，并统一转码为ASR所需的标准格式（如 16kHz, 单声道 wav），降低底层ASR服务器的适配难度。

---

### 二、 系统架构设计方案

整个架构可分为四个核心层：**用户交互层、中心控制层（中转适配层）、分布式消息队列、ASR计算节点层**。

#### 1. 用户界面与接入层 (Frontend & API)
*   **功能实现**：用户通过界面多选文件批量上传。FastAPI 接收文件后，调用 FFmpeg 极速提取时长和格式信息。
*   **进度反馈机制**：使用 **WebSocket** 或 **SSE (Server-Sent Events)** 建立前端与API的持续连接。当底层任务进度更新时，主动推送到前端；或者前端通过定时轮询 `GET /task/{id}/status` 接口获取进度。
*   **文件管理**：系统为每个文件生成唯一的 `TaskID / FileID (UUID)`，将原文件存入 MinIO，并将元数据存入 PostgreSQL。

#### 2. 中心调度与资源监测 (Scheduler & Resource Monitor)
*   **资源监测**：不需要中心节点去“主动探活”。利用 Celery 的**Pull（拉取）模型**：每个ASR服务器上部署一个 Celery Worker（监听特定的队列）。如果服务器有4个线程算力，就设置该 Worker 的并发数为4 (`concurrency=4`)。这样它最多同时处理4个任务，天然实现负载均衡。
*   **统计展示**：利用 Celery 的 `app.control.inspect().stats()` 或者使用开源监控面板 `Flower`，你可以随时在后台看到当前有几个ASR节点在线、分别在处理什么任务。
*   **ETA预估**：维护一个该ASR服务器的“实时率 (RTF, Real-Time Factor)”。比如一小时的音频需要跑10分钟 (RTF=0.16)。
    *   *ETA = (当前队列中等待的音频总时长 + 本文件时长) × RTF / 在线总可用线程数*。

#### 3. 任务队列与动态分发 (Message Queue)
*   **初期顺序并发**：所有任务统一打入一个名为 `asr_tasks_queue` 的队列，各节点抢占式消费。
*   **后期插队机制**：在 RabbitMQ 中配置 `x-max-priority`。FastAPI 下发任务时，普通任务 `priority=1`，VIP插队任务 `priority=10`。RabbitMQ 会自动将高优先级任务推到队列前端，闲置的 Worker 会优先取到它。

#### 4. ASR计算节点与适配器 (Worker & Adapter Layer)
*   这一层部署在真实的ASR服务器上或与其同网络环境下。
*   **适配器模式 (Adapter Pattern)**：在 Worker 代码中编写两个不同的执行类（`NewProtocolAdapter` 和 `OldProtocolAdapter`）。Worker 从队列拿到任务后，从 MinIO 下载音频，根据任务指定的协议类型，调用不同的ASR服务接口。
*   **进度回传**：ASR服务处理时（如果是流式或分段处理），Worker 可以在过程中不断更新 Redis 中的进度字段（如 `task_id: progress=45%`），中心 API 层读取此 Redis 键推送给前端。

---

### 三、 经典项目参考与业界文献

在 GitHub 和技术博客中，有很多类似架构的实现，你可以直接借鉴其基础设施搭建方式：

1.  **OpenAI Whisper 异步调度队列 ( matthieuml/whisper-api )**
    *   **GitHub**: `matthieuml/whisper-api`
    *   **介绍**：这是一个使用 `FastAPI` + `Celery` + `Redis/RabbitMQ` 封装本地 Whisper 模型的项目。它的架构与你需求的“多用户上传 -> API排队 -> 分布式离线处理”完全一致。你可以参考它如何通过 Celery 管理长耗时的语音转写任务并提供进度查询机制。
2.  **CodeVaani 语音助手后台架构 (学术/大厂实践)**
    *   **架构参考**：近期很多基于语音和LLM的教育或业务系统（如印度理工的 CodeVaani）均采用：`React` (前端) + `Django/FastAPI` (后端) + `PostgreSQL` + `Redis队列` + `GPU Celery Workers`。这种架构被证明可以无缝承接从端侧传来的语音请求，并在GPU计算集群上做并发调度。
3.  **Cisco pyosupgrade (Celery Flower 监控参考)**
    *   你可以参考各种引入了 `Flower`（Celery的Web监控工具）的项目。Flower 能够实时显示：有多少个 Worker (ASR节点) 存活，每个节点正在执行什么任务，队列里还有多少任务。你可以直接用它的 API 将资源状态集成到你的管理界面中。

---

### 四、 文件生命周期与后续扩展建议

1.  **文件有序管理**：
    *   定义明确的存储桶（Buckets）：`raw-audio` (原文件), `processed-audio` (统一转码后文件), `asr-results` (JSON格式的转写结果)。
    *   利用 Celery Beat（定时任务）每天凌晨执行一次 `cleanup_task`，删除超过 7 天的临时音视频文件，只保留转写后的文本结果。
2.  **为后续的大模型（LLM）摘要做准备**：
    *   采用 **Pipeline（流水线）/ Chain（链式）队列设计**。未来加入大模型摘要时，你不需要修改现有的 ASR Worker。
    *   只需要在 ASR 任务完成的最后一步，触发一个新任务发送到 `llm_summary_queue`。
    *   独立的 LLM Worker 监听该队列，拿到 ASR 的文本结果进行摘要处理。这使得系统高度解耦。

### 总结
你设计的方向非常准确。基于 **FastAPI + Celery + RabbitMQ + MinIO** 是做离线集中式 AI 任务分发的“黄金组合”。建议第一步先搭建纯环境（用睡眠函数模拟 ASR 耗时），跑通“前端上传 -> FastAPI生成ID -> 压入Celery队列 -> Worker消费 -> 更新Redis进度 -> 前端轮询进度”的整条链路，再逐步将真实的 ASR 协议和 FFmpeg 处理挂载进 Worker 中。