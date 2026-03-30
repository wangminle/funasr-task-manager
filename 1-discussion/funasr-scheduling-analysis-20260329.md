# FunASR Task Manager 调度算法分析与改进方案

> **日期**：2026-03-29
> **背景**：FunASR Task Manager 项目调度策略讨论
> **目标**：分析现有调度算法，借鉴操作系统经典调度方法，提出改进方案

---

## 一、操作系统经典调度算法回顾

操作系统进程调度是核心功能之一，决定了在多个进程竞争 CPU 时，谁能获得使用权以及使用多长时间。

### 1.1 先来先服务（FCFS - First-Come, First-Served）

**核心思想**：先到先得，谁先来谁先被服务。

**生活类比**：银行取号排队，1号先来就先办业务，办完才轮到2号。

**特点**：
- ✅ 简单公平，实现容易
- ❌ 如果前面有个"慢吞吞"的人，后面所有人都要等很久
- ❌ 对短作业不友好（"护航效应"）

**适用场景**：批处理系统，不追求响应时间

---

### 1.2 最短作业优先（SJF - Shortest Job First）

**核心思想**：谁执行时间短，谁先来。

**生活类比**：超市快速通道——只买一瓶水的人可以先结账，不用等前面装满购物车的人。

**特点**：
- ✅ 平均等待时间最短（数学上可证明）
- ❌ 需要预知进程执行时间（实际上很难）
- ❌ 长作业可能被"饿死"（一直有短作业插队）

**抢占式版本**：最短剩余时间优先（SRTN），新来的如果剩余时间更短，立即抢占 CPU

---

### 1.3 时间片轮转（RR - Round Robin）

**核心思想**：每人轮流用 CPU 一小段时间，用完就去队尾排队。

**生活类比**：打麻将，每人摸一张牌轮一圈，大家都有机会，不会有人一直霸占。

**特点**：
- ✅ 公平，响应时间短
- ✅ 适合分时系统（多用户交互）
- ❌ 时间片大小很关键：太大变 FCFS，太小频繁切换开销大
- ⚠️ 典型时间片：10ms ~ 200ms

**适用场景**：现代操作系统最常用的调度方式之一

---

### 1.4 优先级调度

**核心思想**：优先级高的先执行。

**生活类比**：医院急诊——心脏骤停的病人比扭伤脚的先看，不管谁先来。

**特点**：
- ✅ 满足实时性要求
- ❌ 低优先级进程可能"饿死"
- 🔧 解决方案：**老化机制**——等得越久，优先级慢慢提高

**可以是抢占式或非抢占式**

---

### 1.5 经典调度算法对比

| 算法 | 抢占式 | 优点 | 缺点 | 适用场景 |
|------|--------|------|------|----------|
| FCFS | ❌ | 简单公平 | 护航效应 | 批处理 |
| SJF | 可选 | 平均等待最短 | 难预测、饥饿 | 批处理 |
| RR | ✅ | 公平、响应快 | 切换开销 | 分时系统 |
| 优先级 | 可选 | 实时性好 | 饥饿 | 实时系统 |

---

## 二、FunASR Task Manager 现有调度算法分析

### 2.1 问题建模

FunASR Task Manager 的调度问题可以建模为：

```
任务度量 = 音频时长
资源容量 = benchmark 测出的 RTF × max_concurrency
调度目标 = 最大化资源利用率、最小化完成时间
```

### 2.2 核心算法：LPT + Earliest-Finish-Time + Capacity-Aware

#### 资源抽象

```
服务器 A (max_concurrency=4, running=2) → 2 个空闲 slot
服务器 B (max_concurrency=2, running=0) → 2 个空闲 slot
服务器 C (max_concurrency=8, running=5) → 3 个空闲 slot

总共 7 个"虚拟 CPU"可用
```

#### 任务预估

```python
处理时间 = 音频时长 × RTF + overhead
```

**RTF (Real-Time Factor)**：
- RTF = 0.3 → 1 分钟音频需要 0.3 分钟处理
- RTF = 0.5 → 1 分钟音频需要 0.5 分钟处理
- 不同服务器 RTF 不同（性能差异）

#### LPT 排序

```python
# 关键代码
task_estimates.sort(key=lambda x: x["estimated_duration"], reverse=True)
```

**为什么要长任务优先？**
- 目标是**最小化 makespan**（所有任务完成的总时间）
- 长任务先调度，可以"填充"服务器的空闲窗口
- 类似装箱问题：先放大件，再填小件

#### Earliest-Finish-Time 分配

```python
best_slot = min(
    slots,
    key=lambda s: s.earliest_free + processing_time
)
```

### 2.3 与操作系统调度算法对比

| 维度 | 操作系统 | FunASR Task Manager |
|------|----------|---------------------|
| **调度目标** | 公平性、响应时间 | makespan 最小化 |
| **任务顺序** | SJF（短作业优先） | LPT（长任务优先） |
| **抢占式** | RR 可抢占 | ❌ 非抢占（任务跑完才释放） |
| **资源异构** | 通常假设同构 | ✅ 异构（不同服务器 RTF 不同） |
| **动态调整** | 优先级老化 | RTF 滚动统计（P90） |

### 2.4 现有设计的亮点

#### RTF 滚动统计

```python
# 每 50 个任务统计一次 P90 RTF
RTF_WINDOW_SIZE = 50
sorted_vals = sorted(window)
idx = int(math.ceil(0.9 * len(sorted_vals))) - 1
```

类比操作系统根据历史行为调整优先级。

#### 并发惩罚因子

```python
penalty = 1.0 + server.penalty_factor * server.running_tasks
effective_rtf = base_rtf * penalty
```

服务器当前任务越多，估计时间越长，避免"热点"。

---

## 三、经典调度方法在 FunASR 场景的适用性分析

### 3.1 FCFS（先来先服务）

**适用性**：❌ 不适合

**原因**：
- 长任务先到，会阻塞后面所有短任务
- 类比：3 小时的音频先到，后面 10 个 1 分钟的音频都要等
- 不适合实时性要求

**改进空间**：如果用户要求"公平"（先到先处理），可以考虑

---

### 3.2 SJF（短作业优先）

**适用性**：⚠️ 部分适合

**优点**：
- 平均等待时间最短
- 短任务快速响应，用户体验好

**缺点**：
- 长任务可能"饿死"
- 在 FunASR 场景，长任务往往更重要（大文件），不能饿死

**当前策略是 SJF 的反向（LPT）**，追求的是 makespan 最小化

---

### 3.3 RR（时间片轮转）

**适用性**：❌ 不适用

**原因**：
- ASR 任务**不可中断**（一旦开始转写，必须跑完）
- 没有真正意义上的"时间片"

**类比**：ASR 任务更像"批处理作业"，而不是"交互式进程"

---

### 3.4 优先级调度

**适用性**：✅ 值得引入

**当前缺失**：
- 所有任务优先级相同
- 没有区分"紧急任务"和"普通任务"

**建议改进**：
```python
class TaskPriority(Enum):
    URGENT = 0      # 用户标注紧急
    HIGH = 1        # VIP 用户
    NORMAL = 2      # 普通任务
    LOW = 3         # 后台批处理
```

**调度策略**：
- 优先级队列内用 LPT + EFT
- 高优先级任务可以"插队"

---

### 3.5 多级反馈队列

**适用性**：✅✅ 强烈推荐

**设计思路**：
```
Queue 0 (高优先级): 紧急任务、短任务 (< 5分钟)
Queue 1 (中优先级): 普通任务 (5-30分钟)
Queue 2 (低优先级): 长任务 (> 30分钟)
```

**调度规则**：
1. 高优先级队列先调度
2. 同一队列内用 LPT + EFT
3. 任务可以"升级"（等太久升优先级，防止饥饿）

**类比操作系统**：
- Queue 0 = 交互式进程（响应快）
- Queue 1 = 批处理进程
- Queue 2 = 后台任务

---

## 四、当前策略的不足与改进建议

### 4.1 问题 1：LPT 对短任务不友好

**场景**：
- 100 个 1 分钟任务 + 1 个 3 小时任务
- LPT 会先调度 3 小时任务，其他任务等 3 小时

**改进**：引入"短任务快速通道"
```python
if audio_duration < SHORT_TASK_THRESHOLD:
    # 跳过 LPT 排序，直接分配
```

---

### 4.2 问题 2：没有考虑"实时性"

**场景**：用户问"我的任务还要多久？"

**当前**：只估计 makespan，不估计"单个任务的等待时间"

**改进**：引入 **ETC (Estimated Time to Completion)** 预测
```python
etc = queue_position * avg_task_duration / available_slots + my_processing_time
```

---

### 4.3 问题 3：动态资源变化

**场景**：
- 服务器可能突然下线
- 新服务器可能动态注册
- 网络波动导致 RTF 变化

**当前**：有 RTF 滚动统计，但没有"资源变化事件响应"

**改进**：引入**资源事件驱动重调度**
```
服务器下线 → 重新调度 affected tasks
RTF 大幅变化 → 重新评估调度计划
```

---

## 五、推荐的调度策略改进方案

### 5.1 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    FunASR 调度器 v2.0                    │
├─────────────────────────────────────────────────────────┤
│  1. 多级队列（按任务时长 + 用户优先级）                    │
│     - Queue 0: 紧急 + 短任务 (< 5分钟)                   │
│     - Queue 1: 普通任务 (5-30分钟)                       │
│     - Queue 2: 长任务/后台 (> 30分钟)                    │
│                                                         │
│  2. 队列内调度：LPT + EFT                                │
│     - 目标：makespan 最小化                              │
│                                                         │
│  3. 队列间调度：优先级抢占                               │
│     - 高优先级可以插队                                   │
│     - 防饥饿：老化机制                                   │
│                                                         │
│  4. 动态调整：                                           │
│     - RTF 滚动统计                                       │
│     - 资源变化响应                                       │
│     - ETC 预测                                           │
└─────────────────────────────────────────────────────────┘
```

### 5.2 数据结构设计

```python
class TaskPriority(Enum):
    URGENT = 0      # 用户标注紧急
    HIGH = 1        # VIP 用户
    NORMAL = 2      # 普通任务
    LOW = 3         # 后台批处理

class TaskQueue:
    def __init__(self, priority: TaskPriority, duration_range: tuple):
        self.priority = priority
        self.duration_range = duration_range  # (min, max) in seconds
        self.tasks: list[Task] = []
    
    def can_accept(self, task: Task) -> bool:
        """判断任务是否属于此队列"""
        dur = task.audio_duration_sec
        return self.duration_range[0] <= dur < self.duration_range[1]
    
    def enqueue(self, task: Task):
        """按 LPT 顺序插入"""
        # ... 排序逻辑

class MultiLevelFeedbackScheduler:
    def __init__(self):
        self.queues = [
            TaskQueue(TaskPriority.URGENT, (0, 300)),       # < 5分钟
            TaskQueue(TaskPriority.NORMAL, (300, 1800)),   # 5-30分钟
            TaskQueue(TaskPriority.LOW, (1800, float('inf'))),  # > 30分钟
        ]
        self.rtf_tracker = RTFTracker()
        self.aging_threshold = 600  # 10分钟老化阈值
```

### 5.3 调度流程

```python
def schedule_batch(self, tasks: list[Task], servers: list[ServerProfile]) -> list[ScheduleDecision]:
    # 1. 分类入队
    for task in tasks:
        for queue in self.queues:
            if queue.can_accept(task):
                queue.enqueue(task)
                break
    
    # 2. 老化机制检查
    self._apply_aging()
    
    # 3. 按优先级顺序调度
    decisions = []
    for queue in sorted(self.queues, key=lambda q: q.priority.value):
        if queue.tasks:
            queue_decisions = self._schedule_queue(queue, servers)
            decisions.extend(queue_decisions)
    
    return decisions

def _apply_aging(self):
    """防止长任务饥饿"""
    now = time.time()
    for queue in self.queues:
        for task in queue.tasks:
            wait_time = now - task.created_at.timestamp()
            if wait_time > self.aging_threshold:
                # 升级到更高优先级队列
                self._promote_task(task, queue)
```

### 5.4 ETC 预测

```python
def calculate_etc(self, task: Task, servers: list[ServerProfile]) -> int:
    """计算任务的预计完成时间"""
    # 找到最佳服务器
    best_server = self._find_best_server(task, servers)
    
    # 计算队列等待时间
    queue_position = self._get_queue_position(task)
    avg_task_duration = self._get_avg_task_duration()
    available_slots = sum(
        max(s.max_concurrency - s.running_tasks, 0) 
        for s in servers if s.status == "ONLINE"
    )
    queue_time = (queue_position * avg_task_duration) / max(available_slots, 1)
    
    # 计算处理时间
    rtf = self.rtf_tracker.get_p90(best_server.server_id)
    processing_time = task.audio_duration_sec * rtf + DEFAULT_OVERHEAD
    
    return int(queue_time + processing_time)
```

---

## 六、实现优先级建议

### 阶段 1：优先级调度（高优先级）

- 添加 `TaskPriority` 字段
- 支持用户标注紧急任务
- 高优先级任务插队机制

### 阶段 2：多级队列（中优先级）

- 按任务时长分层
- 队列间优先级调度
- 队列内 LPT + EFT

### 阶段 3：老化机制（低优先级）

- 防止长任务饥饿
- 动态优先级调整
- ETC 预测显示

---

## 七、操作系统调度思想对照

| 操作系统思想 | FunASR 对应 | 当前状态 | 改进建议 |
|-------------|-------------|---------|---------|
| 多级反馈队列 | 按时长分层 | ❌ 未实现 | 阶段 2 |
| 优先级调度 | 紧急任务插队 | ❌ 未实现 | 阶段 1 |
| 老化机制 | 防止长任务饥饿 | ❌ 未实现 | 阶段 3 |
| 负载均衡 | RTF + 并发惩罚 | ✅ 已实现 | 保持 |
| 动态优先级调整 | RTF 滚动统计 | ✅ 已实现 | 保持 |
| ETC 预测 | 任务完成时间预测 | ❌ 未实现 | 阶段 3 |

---

## 八、总结

FunASR Task Manager 的调度问题本质是**异构资源池 + 批处理调度**问题。

**现有策略（LPT + EFT）** 是最小化 makespan 的经典做法，优点：
- ✅ 负载均衡（RTF + 并发惩罚）
- ✅ 异构资源适配
- ✅ 动态 RTF 统计

**可借鉴的改进方向**：
1. **多级反馈队列** → 区分长短任务，提升短任务响应
2. **优先级调度** → 支持紧急任务，提升用户体验
3. **老化机制** → 防止长任务饥饿，保证公平性
4. **ETC 预测** → 提升用户体验，增加系统透明度

---

## 九、开源 ASR 项目调度算法调研

### 9.1 FunASR 官方调度策略

**来源**：[FunASR GitHub](https://github.com/modelscope/FunASR)

FunASR 本身**没有独立的任务调度器**，而是采用以下策略：

#### 实时流式处理
- **Chunk-based Processing**：音频分块处理，模拟流式解码
- `chunk_size` 参数控制延迟和实时显示粒度
- 每个增量输入独立处理，最小化延迟

#### 批处理与并发
- **Dynamic Batching**：动态批处理，支持基于样本数、总长度或 token 数
- **Multi-threading Concurrency**：多线程并发，CPU/GPU 支持上百并发流
- 最大支持 12 小时音频、2GB 文件

#### 与 vLLM 集成
当 FunASR 与 vLLM 集成时，继承 vLLM 的调度策略：
- **FCFS + Preemption**：先来先服务 + 内存不足时抢占
- **Chunked Prefilling**：长 prompt 分块处理，防止单请求垄断资源

---

### 9.2 vLLM 调度算法

**来源**：[vLLM Documentation](https://docs.vllm.ai/)

vLLM 是目前最先进的 LLM 推理引擎，其调度算法值得借鉴：

#### 核心调度策略

```
┌─────────────────────────────────────────────────────────┐
│                    vLLM 调度器                          │
├─────────────────────────────────────────────────────────┤
│  1. FCFS (First-Come, First-Served)                    │
│     - 请求按到达顺序排队                                 │
│     - 公平性保证，防止饥饿                               │
│                                                         │
│  2. Preemption (抢占)                                   │
│     - 触发条件：GPU KV cache 内存耗尽                   │
│     - 策略：抢占最新到达的请求                           │
│     - 恢复：重新计算 KV cache 或从 CPU swap             │
│                                                         │
│  3. Chunked Prefill (分块预填充)                        │
│     - 长 prompt 分块处理                                │
│     - 优先调度 decode 请求                              │
│     - 避免单请求垄断 token budget                       │
│                                                         │
│  4. PagedAttention (分页注意力)                         │
│     - KV cache 分块存储，类似虚拟内存                   │
│     - Block table 映射逻辑到物理地址                    │
│     - 支持内存共享，减少冗余拷贝                         │
└─────────────────────────────────────────────────────────┘
```

#### 与 FunASR Task Manager 的对比

| 维度 | vLLM | FunASR Task Manager |
|------|------|---------------------|
| 基础策略 | FCFS | LPT |
| 抢占机制 | ✅ 支持 | ❌ 不支持 |
| 内存管理 | PagedAttention | 无特殊管理 |
| 批处理 | Continuous Batching | Static Batching |
| 适用场景 | LLM 推理 | ASR 转写 |

**借鉴点**：
- Chunked Prefill 思想可用于长音频分块调度
- PagedAttention 可用于管理多任务的内存资源

---

### 9.3 Whisper 生态系统

**来源**：[faster-whisper](https://github.com/SYSTRAN/faster-whisper)

#### faster-whisper 性能优化

- 基于 CTranslate2，比原版快 4 倍
- 支持 8-bit 量化，降低内存占用
- GPU/CPU 双支持

#### 分布式推理方案

| 项目 | 调度策略 | 特点 |
|------|---------|------|
| **Ray Serve** | 分布式调度 | 水平扩展，支持 EKS/GCP |
| **whisper-asr-webservice** | REST API | GPU 加速，模型动态加载 |
| **faster-whisper-server** | Web Server | 实时转写，CPU 友好 |
| **Remote Faster Whisper** | 远程卸载 | 低功耗设备远程调用 GPU |

#### Ray Serve 调度机制

```python
# Ray Data 调度 GPU-based Whisper inference
# - 管理变长 batch
# - 优化资源利用率
# - 支持 GPU 分布式
```

**借鉴点**：
- Ray 的分布式调度框架可作为扩展参考
- 模型动态加载/卸载机制

---

### 9.4 Kaldi 在线解码

**来源**：[Kaldi Online Decoding](https://kaldi-asr.org/doc/online_decoding.html)

#### 核心架构

```
┌─────────────────────────────────────────────────────────┐
│                    Kaldi Online2                        │
├─────────────────────────────────────────────────────────┤
│  1. Chunk-by-Chunk Decoding                            │
│     - decoder.InitDecoding() 初始化                     │
│     - decoder.AdvanceDecoding() 每块调用               │
│     - 无依赖未来的增量处理                               │
│                                                         │
│  2. Asynchronous Communication                         │
│     - Client/Server 异步通信                           │
│     - 结果即时输出（达到置信度）                         │
│     - 4-byte chunk size header                         │
│                                                         │
│  3. Online Endpointing                                 │
│     - 基于 non-silence 检测                            │
│     - trailing silence 时长                            │
│     - decoded path cost                                │
│                                                         │
│  4. GPU Batched Online Processing                      │
│     - 音频分块（几秒）批处理                            │
│     - GPU 特征提取加速                                 │
└─────────────────────────────────────────────────────────┘
```

**借鉴点**：
- Online Endpointing 可用于任务完成检测
- 异步通信模式可用于回调机制

---

### 9.5 ESPnet 流式 ASR

**来源**：[ESPnet Streaming ASR](https://espnet.github.io/espnet/)

#### 核心技术

```python
# Blockwise Encoder 配置
block_size = 40       # 每块帧数
hop_size = 16         # 跳跃帧数
look_ahead = 16       # 前瞻帧数

# 支持的架构
- contextual_block_transformer
- contextual_block_conformer

# 流式接口
def chunk_forward(...):      # 在线解码
def reset_streaming_cache(...)  # 缓存管理
```

#### 调度特点

- **Blockwise Synchronous Beam Search**：块同步束搜索
- **Chunk-by-Chunk Decoding**：Transducer 模型支持
- **Unified Interface**：离线 + 流式统一接口
- **Latency Management**：`beam_size` 调优延迟

**借鉴点**：
- Block size/hop size/look ahead 参数设计
- 流式解码的模块化架构

---

### 9.6 EasyASR 分布式平台

**来源**：[EasyASR Paper (AAAI 2021)](https://chywang.github.io/papers/aaai2021a.pdf)

阿里 + 字节跳动联合发表的分布式 ASR 平台论文。

#### 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    EasyASR                              │
├─────────────────────────────────────────────────────────┤
│  Training Pipeline:                                     │
│  - ASR Create Dataset: 特征提取 + 音频-文本对生成       │
│  - Distributed Training: GPU 集群训练                   │
│                                                         │
│  Serving Pipeline:                                      │
│  - ASR Predict: 快速推理                               │
│  - Load Balancing: 工作负载均衡                        │
│                                                         │
│  Supported Models:                                      │
│  - Wav2Letter, Speech Transformer                      │
│  - Pre-trained ASR models                              │
└─────────────────────────────────────────────────────────┘
```

#### 关键特性

- 基于**阿里云机器学习平台 PAI**
- 支持**分布式 GPU 集群**训练和推理
- **高效学习**：优化的训练流程
- **快速推理**：低延迟服务部署

**借鉴点**：
- 云原生分布式架构设计
- 训练 + 推理一体化 pipeline

---

### 9.7 开源项目调度策略对比

| 项目 | 调度策略 | 优点 | 适用场景 | GitHub Stars |
|------|---------|------|---------|-------------|
| **FunASR** | Chunk + Dynamic Batching | 低延迟、高并发 | 实时流式 + 批处理 | 10k+ |
| **vLLM** | FCFS + Preemption + PagedAttention | 高吞吐、内存高效 | LLM 推理 | 30k+ |
| **faster-whisper** | 静态批处理 | 简单高效 | 离线转写 | 15k+ |
| **Kaldi Online** | Chunk-by-Chunk | 实时性好 | 在线解码 | 14k+ |
| **ESPnet Streaming** | Blockwise Processing | 模块化、灵活 | 流式 ASR | 9k+ |
| **EasyASR** | 分布式调度 | 可扩展性强 | 大规模部署 | 论文 |

---

### 9.8 对 FunASR Task Manager 的借鉴建议

#### 从 vLLM 借鉴

1. **Chunked Processing for Long Audio**
   ```python
   # 长音频分块调度
   if audio_duration > LONG_AUDIO_THRESHOLD:
       chunks = split_audio(audio, chunk_size=600)  # 10分钟/块
       for chunk in chunks:
           schedule_chunk(chunk, server)
   ```

2. **Memory Budget Management**
   ```python
   # 类似 PagedAttention 的内存管理
   class MemoryBudget:
       total_blocks: int
       allocated_blocks: dict[task_id, list[block]]
   ```

#### 从 Kaldi 借鉴

1. **Online Endpointing**
   ```python
   # 任务完成检测
   def check_endpoint(silence_duration, decoded_cost):
       if silence_duration > SILENCE_THRESHOLD:
           return True
       return False
   ```

#### 从 ESPnet 借鉴

1. **Blockwise Parameters**
   ```python
   # 任务分块参数
   TASK_CHUNK_CONFIG = {
       'block_size': 300,   # 5分钟/块
       'hop_size': 60,      # 1分钟跳跃
       'look_ahead': 30,    # 30秒前瞻
   }
   ```

#### 从 Ray Serve 借鉴

1. **Distributed Scheduler**
   ```python
   # Ray-based 分布式调度
   @ray.remote
   class ASRWorker:
       def transcribe(self, audio_chunk):
           return asr_engine.process(audio_chunk)
   
   # 自动负载均衡
   workers = [ASRWorker.remote() for _ in range(num_workers)]
   ```

---

## 附录：参考资料

### 操作系统调度

- [操作系统进程调度算法](https://developer.aliyun.com/article/1410789)
- [LPT 调度算法](https://en.wikipedia.org/wiki/Longest-processing-time-first_scheduling)
- [Makespan 最小化问题](https://en.wikipedia.org/wiki/Job-shop_scheduling)
- [多级反馈队列调度](https://en.wikipedia.org/wiki/Multilevel_feedback_queue)

### ASR 开源项目

- [FunASR GitHub](https://github.com/modelscope/FunASR)
- [vLLM Documentation](https://docs.vllm.ai/)
- [faster-whisper GitHub](https://github.com/SYSTRAN/faster-whisper)
- [Kaldi Online Decoding](https://kaldi-asr.org/doc/online_decoding.html)
- [ESPnet Streaming ASR](https://espnet.github.io/espnet/espnet2_tutorial.html)
- [EasyASR Paper (AAAI 2021)](https://chywang.github.io/papers/aaai2021a.pdf)
- [whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice)
- [Ray Serve for Whisper](https://docs.ray.io/en/latest/ray-overview/examples/e2e-audio/README.html)
