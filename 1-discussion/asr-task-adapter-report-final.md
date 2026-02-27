# FunASR 批量任务调度适配层 - 整合方案 C（终稿）

> **项目代号**: funasr-adapter-v1-plan  
> **文档版本**: v1.0-final (审核优化版)  
> **创建时间**: 2026-02-16  
> **整合来源**: 方案A (Kimi 集中式) + 方案B (MiniMax 分布式)  
> **状态**: ✅ 审核完成，可交付实施

---

## 目录

1. [整合方案概述](#1-整合方案概述)
2. [架构设计](#2-架构设计)
3. [核心模块详细设计](#3-核心模块详细设计)
4. [双模式任务分发](#4-双模式任务分发)
5. [可插拔队列系统](#5-可插拔队列系统)
6. [负载均衡策略](#6-负载均衡策略)
7. [容错与恢复机制](#7-容错与恢复机制)
8. [数据模型设计](#8-数据模型设计)
9. [接口设计](#9-接口设计)
10. [Skill 封装设计](#10-skill-封装设计)
11. [配置管理](#11-配置管理)
12. [监控与可观测性](#12-监控与可观测性)
13. [测试策略](#13-测试策略)
14. [部署指南](#14-部署指南)
15. [实施计划](#15-实施计划)
16. [风险与对策](#16-风险与对策)

---

## 1. 整合方案概述

### 1.1 整合理念

本方案整合方案A（集中式调度）和方案B（分布式事件驱动）的核心优势，采用**"分层混合架构"**：

- **核心调度层**：保持集中式（方案A），确保可控性和可观测性
- **任务分发层**：支持 Push/Pull 双模式（融合方案A+B）
- **队列层**：可插拔设计，内存队列（方案A）+ Redis Streams（方案B）
- **节点协调**：心跳检测（方案A）+ Gossip协议（方案B，可选）
- **可观测性**：统一监控指标 + 分布式追踪（新增）

### 1.2 整合对比表

| 维度 | 方案A (Kimi) | 方案B (MiniMax) | **整合方案C** |
|------|-------------|-----------------|--------------|
| **架构模式** | 集中式调度器 | 分布式事件驱动 | **分层混合** - 核心集中，分发灵活 |
| **任务分发** | Push (调度器推送) | Pull (节点拉取) | **双模式** - 自适应选择 |
| **消息队列** | 内存队列 | Redis Streams | **可插拔** - 内存/Redis/自定义 |
| **状态管理** | 中心状态机 | 事件溯源 | **混合** - 内存状态 + 可选事件日志 |
| **节点协调** | 心跳中心检测 | Gossip协议 | **可配置** - 心跳默认，Gossip可选 |
| **可观测性** | 基础日志 | 事件追踪 | **全面** - 指标+日志+追踪 |
| **部署复杂度** | 低 | 中高 | **中** - 渐进式复杂度 |
| **扩展性** | 中 | 高 | **高** - 按需扩展 |
| **可靠性** | 断路器 | 幂等重放 | **双重保障** - 断路器 + 幂等 |

### 1.3 核心设计原则

1. **渐进式复杂** - 基础功能简单，高级功能可选
2. **向后兼容** - 从方案A平滑升级到方案B特性
3. **配置驱动** - 通过配置切换不同模式，无需改代码
4. **双模式并存** - Push/Pull 同时支持，智能选择
5. **可观测优先** - 内置监控指标，便于问题排查

### 1.4 架构演进路径

```
阶段1: 简单模式 (Week 1)
├── 内存队列
├── Push调度
├── 轮询负载均衡
└── 基础日志

阶段2: 增强模式 (Week 2-3)
├── Redis队列 (可选)
├── Pull调度 (可选)
├── 多种负载均衡策略
└── 断路器+重试

阶段3: 高级模式 (Week 4)
├── Hybrid自适应
├── Gossip协议 (可选)
├── 事件溯源 (可选)
└── 完整监控体系
```

---

## 2. 架构设计

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        FunASR Batch Adapter (整合方案C)                       │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        Skill API 接口层                              │    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │    │
│  │  │ submitBatch │  │ queryStatus │  │ cancelTask  │  │ getResults  │ │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘ │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                         │
│                                    ▼                                         │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                        核心调度层 (集中式)                           │    │
│  │                                                                      │    │
│  │   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐     │    │
│  │   │  Task Queue  │◄────►│  Scheduler   │◄────►│Load Balancer │     │    │
│  │   │  (可插拔)     │      │   调度器      │      │   负载均衡    │     │    │
│  │   └──────────────┘      └──────────────┘      └──────────────┘     │    │
│  │           │                    │                       │            │    │
│  │           ▼                    ▼                       ▼            │    │
│  │   ┌──────────────┐      ┌──────────────┐      ┌──────────────┐     │    │
│  │   │ Task Tracker │      │    Retry     │      │Node Registry │     │    │
│  │   │  状态跟踪     │      │   重试机制    │      │  节点注册中心 │     │    │
│  │   └──────────────┘      └──────────────┘      └──────────────┘     │    │
│  │                                                                     │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                    │                                         │
│           ┌────────────────────────┼────────────────────────┐                │
│           │                        │                        │                │
│           ▼                        ▼                        ▼                │
│  ┌─────────────┐          ┌─────────────┐          ┌─────────────┐          │
│  │  Push Mode  │          │  Pull Mode  │          │  Event Log  │          │
│  │  (方案A)     │          │  (方案B)     │          │  (可选)      │          │
│  │             │          │             │          │             │          │
│  │ 调度器主动   │          │ 节点自主     │          │ 事件溯源     │          │
│  │ 推送任务     │          │ 拉取任务     │          │ 状态重建     │          │
│  └──────┬──────┘          └──────┬──────┘          └─────────────┘          │
│         │                        │                                           │
└─────────┼────────────────────────┼───────────────────────────────────────────┘
          │                        │
          ▼                        ▼
   ┌─────────────┐          ┌─────────────┐
   │  FunASR     │          │  FunASR     │
   │   Node A    │          │   Node B    │
   │  :10095     │          │  :10096     │
   └─────────────┘          └─────────────┘
```

### 2.2 分层架构说明

| 层级 | 组件 | 职责 | 模式 |
|------|------|------|------|
| **API层** | Skill Interface | 对外接口，参数校验 | 统一 |
| **调度层** | Scheduler, TaskQueue, LoadBalancer | 任务调度、队列管理 | 集中式 |
| **分发层** | PushDispatcher, PullWorker | 任务分发到节点 | Push/Pull 双模式 |
| **执行层** | FunASRClient, NodeWorker | 与FunASR节点通信 | 统一 |
| **存储层** | ResultRecorder, EventStore | 结果记录、事件日志 | 可选事件溯源 |
| **监控层** | MetricsCollector, HealthChecker | 指标采集、健康检查 | 统一 |

### 2.3 双模式切换逻辑

```typescript
// 根据任务特征自动选择分发模式
class DispatchModeSelector {
  selectMode(batchSize: number, taskComplexity: number, priority: number): DispatchMode {
    // 高优先级 (1-3) 或超高复杂度 (>8) -> Push模式 (低延迟)
    if (priority <= 3 || taskComplexity > 8) {
      return 'push';
    }
    
    // 小批量 (< 10) -> Push模式 (低延迟)
    if (batchSize < 10) {
      return 'push';
    }
    
    // 大批量 (> 100) -> Pull模式 (高吞吐)
    if (batchSize > 100) {
      return 'pull';
    }
    
    // 中等批量 -> 混合模式 (智能分配)
    return 'hybrid';
  }
  
  // 复杂度估算：基于音频时长、格式等因素
  private estimateComplexity(payload: TaskPayload): number {
    let complexity = 5; // 基础复杂度
    
    // 音频时长因素 (假设超过10分钟增加复杂度)
    if (payload.duration && payload.duration > 600) {
      complexity += 2;
    }
    
    // 格式因素 (非WAV格式需要转码)
    if (payload.audioFormat && payload.audioFormat !== 'wav') {
      complexity += 1;
    }
    
    // 热词数量因素
    if (payload.hotwords && payload.hotwords.length > 10) {
      complexity += 1;
    }
    
    return Math.min(complexity, 10);
  }
}
```

---

## 3. 核心模块详细设计

### 3.1 可插拔队列系统 (Unified Queue)

整合方案A的内存队列和方案B的Redis Streams，提供统一接口。

```typescript
// 队列接口定义
interface ITaskQueue {
  submit(task: Task): Promise<Task>;
  submitBatch(tasks: Task[]): Promise<Task[]>;
  dequeue(workerId?: string): Promise<Task | null>;
  peek(): Promise<Task | null>;
  updateStatus(taskId: string, status: TaskStatus, result?: TaskResult): Promise<void>;
  cancel(taskId: string): Promise<boolean>;
  getTask(taskId: string): Promise<Task | null>;
  getBatchTasks(batchId: string): Promise<Task[]>;
  getStats(): Promise<QueueStats>;
  subscribe(callback: (event: QueueEvent) => void): () => void;
  pause(): Promise<void>;
  resume(): Promise<void>;
}

// 队列事件类型
interface QueueEvent {
  type: 'taskSubmitted' | 'taskStarted' | 'taskCompleted' | 'taskFailed' | 'taskCancelled';
  taskId: string;
  timestamp: number;
  data?: any;
}

// 队列统计信息
interface QueueStats {
  pending: number;
  running: number;
  completed: number;
  failed: number;
  cancelled: number;
  total: number;
}

// 队列提供者类型
enum QueueProvider {
  MEMORY = 'memory',      // 方案A: 内存队列
  REDIS = 'redis',        // 方案B: Redis Streams
  CUSTOM = 'custom'       // 自定义实现
}

// 队列工厂
class QueueFactory {
  static create(provider: QueueProvider, config: QueueConfig): ITaskQueue {
    switch (provider) {
      case QueueProvider.MEMORY:
        return new MemoryTaskQueue(config.memory);
      case QueueProvider.REDIS:
        return new RedisTaskQueue(config.redis!);
      case QueueProvider.CUSTOM:
        return config.customProvider!;
      default:
        throw new Error(`Unknown queue provider: ${provider}`);
    }
  }
}
```

#### 3.1.1 内存队列实现 (方案A)

```typescript
class MemoryTaskQueue implements ITaskQueue {
  private queue: PriorityQueue<Task>;
  private tasks: Map<string, Task> = new Map();
  private eventEmitter: EventEmitter;
  private paused: boolean = false;
  private maxSize: number;
  
  constructor(config: MemoryQueueConfig) {
    this.maxSize = config.maxSize || 10000;
    this.queue = new PriorityQueue<Task>((a, b) => a.priority - b.priority);
    this.eventEmitter = new EventEmitter();
  }
  
  async submit(task: Omit<Task, 'id' | 'createdAt' | 'updatedAt'>): Promise<Task> {
    if (this.tasks.size >= this.maxSize) {
      throw new Error(`Queue is full (max size: ${this.maxSize})`);
    }
    
    const enrichedTask: Task = {
      ...task,
      id: generateId(),
      createdAt: Date.now(),
      updatedAt: Date.now(),
      status: 'pending'
    };
    
    this.tasks.set(enrichedTask.id, enrichedTask);
    this.queue.enqueue(enrichedTask, enrichedTask.priority);
    
    this.emit('taskSubmitted', enrichedTask);
    return enrichedTask;
  }
  
  async submitBatch(tasks: Omit<Task, 'id' | 'createdAt' | 'updatedAt'>[]): Promise<Task[]> {
    return Promise.all(tasks.map(task => this.submit(task)));
  }
  
  async dequeue(): Promise<Task | null> {
    if (this.paused) return null;
    
    const task = this.queue.dequeue();
    if (task) {
      task.status = 'running';
      task.startedAt = Date.now();
      task.updatedAt = Date.now();
      this.emit('taskStarted', task);
    }
    return task;
  }
  
  async peek(): Promise<Task | null> {
    return this.queue.peek();
  }
  
  async updateStatus(taskId: string, status: TaskStatus, result?: TaskResult): Promise<void> {
    const task = this.tasks.get(taskId);
    if (task) {
      task.status = status;
      task.updatedAt = Date.now();
      
      if (result) {
        task.result = result;
      }
      
      if (status === 'completed') {
        task.completedAt = Date.now();
      }
    }
  }
  
  async cancel(taskId: string): Promise<boolean> {
    const task = this.tasks.get(taskId);
    if (task && ['pending', 'queued'].includes(task.status)) {
      task.status = 'cancelled';
      task.updatedAt = Date.now();
      return true;
    }
    return false;
  }
  
  async cancelBatch(batchId: string): Promise<number> {
    const tasks = Array.from(this.tasks.values()).filter(
      t => t.batchId === batchId && ['pending', 'queued'].includes(t.status)
    );
    tasks.forEach(t => {
      t.status = 'cancelled';
      t.updatedAt = Date.now();
    });
    return tasks.length;
  }
  
  async getTask(taskId: string): Promise<Task | null> {
    return this.tasks.get(taskId) || null;
  }
  
  async getBatchTasks(batchId: string): Promise<Task[]> {
    return Array.from(this.tasks.values()).filter(t => t.batchId === batchId);
  }
  
  async getStats(): Promise<QueueStats> {
    const tasks = Array.from(this.tasks.values());
    return {
      pending: tasks.filter(t => t.status === 'pending').length,
      running: tasks.filter(t => t.status === 'running').length,
      completed: tasks.filter(t => t.status === 'completed').length,
      failed: tasks.filter(t => t.status === 'failed').length,
      cancelled: tasks.filter(t => t.status === 'cancelled').length,
      total: tasks.length
    };
  }
  
  subscribe(callback: (event: QueueEvent) => void): () => void {
    this.eventEmitter.on('queueEvent', callback);
    return () => this.eventEmitter.off('queueEvent', callback);
  }
  
  async pause(): Promise<void> {
    this.paused = true;
  }
  
  async resume(): Promise<void> {
    this.paused = false;
  }
  
  private emit(type: QueueEvent['type'], data: any): void {
    this.eventEmitter.emit('queueEvent', { type, ...data, timestamp: Date.now() });
  }
}
```

#### 3.1.2 Redis Streams 实现 (方案B)

```typescript
class RedisTaskQueue implements ITaskQueue {
  private redis: Redis;
  private consumerGroup: string;
  private paused: boolean = false;
  private taskKeyPrefix: string;
  
  constructor(config: RedisQueueConfig) {
    this.redis = new Redis(config.url);
    this.consumerGroup = config.consumerGroup || 'funasr-workers';
    this.taskKeyPrefix = config.taskKeyPrefix || 'funasr:task:';
    this.initializeConsumerGroup();
  }
  
  private async initializeConsumerGroup(): Promise<void> {
    try {
      await this.redis.xgroup('CREATE', 'funasr:tasks', this.consumerGroup, '$', 'MKSTREAM');
    } catch (error: any) {
      if (!error.message.includes('already exists')) {
        throw error;
      }
    }
  }
  
  async submit(task: Omit<Task, 'id' | 'createdAt'>): Promise<Task> {
    const enrichedTask: Task = {
      ...task,
      id: generateId(),
      createdAt: Date.now(),
      status: 'pending'
    };
    
    await this.redis.hset(
      `${this.taskKeyPrefix}${enrichedTask.id}`,
      'data', JSON.stringify(enrichedTask)
    );
    
    await this.redis.xadd(
      'funasr:tasks',
      '*',
      'taskId', enrichedTask.id,
      'priority', enrichedTask.priority.toString()
    );
    
    return enrichedTask;
  }
  
  async submitBatch(tasks: Omit<Task, 'id' | 'createdAt'>[]): Promise<Task[]> {
    const pipeline = this.redis.pipeline();
    const enrichedTasks: Task[] = [];
    
    for (const task of tasks) {
      const enrichedTask: Task = {
        ...task,
        id: generateId(),
        createdAt: Date.now(),
        status: 'pending'
      };
      enrichedTasks.push(enrichedTask);
      
      pipeline.hset(
        `${this.taskKeyPrefix}${enrichedTask.id}`,
        'data', JSON.stringify(enrichedTask)
      );
      pipeline.xadd(
        'funasr:tasks',
        '*',
        'taskId', enrichedTask.id,
        'priority', enrichedTask.priority.toString()
      );
    }
    
    await pipeline.exec();
    return enrichedTasks;
  }
  
  async dequeue(workerId: string): Promise<Task | null> {
    if (this.paused) return null;
    
    const messages = await this.redis.xreadgroup(
      'GROUP', this.consumerGroup, workerId,
      'COUNT', 1,
      'BLOCK', 5000,
      'STREAMS', 'funasr:tasks', '>'
    );
    
    if (!messages || messages.length === 0) return null;
    
    const [, entries] = messages[0];
    if (!entries || entries.length === 0) return null;
    
    const [id, fields] = entries[0];
    const taskId = fields[1];
    
    const taskData = await this.redis.hget(`${this.taskKeyPrefix}${taskId}`, 'data');
    if (!taskData) {
      await this.redis.xack('funasr:tasks', this.consumerGroup, id);
      return null;
    }
    
    const task: Task = JSON.parse(taskData);
    task._redisId = id;
    task.status = 'running';
    task.startedAt = Date.now();
    
    await this.redis.hset(
      `${this.taskKeyPrefix}${taskId}`,
      'data', JSON.stringify(task)
    );
    
    return task;
  }
  
  async acknowledge(task: Task): Promise<void> {
    if (task._redisId) {
      await this.redis.xack('funasr:tasks', this.consumerGroup, task._redisId);
    }
  }
  
  async updateStatus(taskId: string, status: TaskStatus, result?: TaskResult): Promise<void> {
    const taskData = await this.redis.hget(`${this.taskKeyPrefix}${taskId}`, 'data');
    if (!taskData) return;
    
    const task: Task = JSON.parse(taskData);
    task.status = status;
    task.updatedAt = Date.now();
    
    if (status === 'completed') {
      task.completedAt = Date.now();
      if (result) task.result = result;
      if (task._redisId) {
        await this.redis.xack('funasr:tasks', this.consumerGroup, task._redisId);
      }
    }
    
    await this.redis.hset(
      `${this.taskKeyPrefix}${taskId}`,
      'data', JSON.stringify(task)
    );
  }
  
  async getTask(taskId: string): Promise<Task | null> {
    const taskData = await this.redis.hget(`${this.taskKeyPrefix}${taskId}`, 'data');
    return taskData ? JSON.parse(taskData) : null;
  }
  
  async getBatchTasks(batchId: string): Promise<Task[]> {
    const keys = await this.redis.keys(`${this.taskKeyPrefix}*`);
    const tasks: Task[] = [];
    
    for (const key of keys) {
      const taskData = await this.redis.hget(key, 'data');
      if (taskData) {
        const task: Task = JSON.parse(taskData);
        if (task.batchId === batchId) {
          tasks.push(task);
        }
      }
    }
    
    return tasks;
  }
  
  async getStats(): Promise<QueueStats> {
    const keys = await this.redis.keys(`${this.taskKeyPrefix}*`);
    const tasks: Task[] = [];
    
    for (const key of keys.slice(0, 1000)) {
      const taskData = await this.redis.hget(key, 'data');
      if (taskData) {
        tasks.push(JSON.parse(taskData));
      }
    }
    
    return {
      pending: tasks.filter(t => t.status === 'pending').length,
      running: tasks.filter(t => t.status === 'running').length,
      completed: tasks.filter(t => t.status === 'completed').length,
      failed: tasks.filter(t => t.status === 'failed').length,
      cancelled: tasks.filter(t => t.status === 'cancelled').length,
      total: tasks.length
    };
  }
  
  async cancel(taskId: string): Promise<boolean> {
    const task = await this.getTask(taskId);
    if (task && ['pending', 'queued'].includes(task.status)) {
      task.status = 'cancelled';
      task.updatedAt = Date.now();
      await this.redis.hset(
        `${this.taskKeyPrefix}${taskId}`,
        'data', JSON.stringify(task)
      );
      return true;
    }
    return false;
  }
  
  async cancelBatch(batchId: string): Promise<number> {
    const tasks = await this.getBatchTasks(batchId);
    let cancelled = 0;
    
    for (const task of tasks) {
      if (['pending', 'queued'].includes(task.status)) {
        task.status = 'cancelled';
        task.updatedAt = Date.now();
        await this.redis.hset(
          `${this.taskKeyPrefix}${task.id}`,
          'data', JSON.stringify(task)
        );
        cancelled++;
      }
    }
    
    return cancelled;
  }
  
  async pause(): Promise<void> {
    this.paused = true;
  }
  
  async resume(): Promise<void> {
    this.paused = false;
  }
  
  async peek(): Promise<Task | null> {
    return null;
  }
  
  subscribe(callback: (event: QueueEvent) => void): () => void {
    const interval = setInterval(async () => {}, 1000);
    return () => clearInterval(interval);
  }
}
```

### 3.2 双模式调度器 (Dual-Mode Scheduler)

```typescript
interface SchedulerConfig {
  mode: 'push' | 'pull' | 'hybrid' | 'auto';
  maxConcurrentTasks: number;
  pollIntervalMs: number;
  enableBatchDispatch: boolean;
  batchSize: number;
  pushConfig?: PushSchedulerConfig;
  pullConfig?: PullSchedulerConfig;
  hybridThreshold?: {
    pushMaxTasks: number;
    pullMinTasks: number;
  };
}

class DualModeScheduler implements ITaskScheduler {
  private pushScheduler?: PushScheduler;
  private pullScheduler?: PullScheduler;
  private modeSelector: DispatchModeSelector;
  private currentMode: DispatchMode;
  private running: boolean = false;
  private paused: boolean = false;
  
  constructor(
    private taskQueue: ITaskQueue,
    private nodeRegistry: INodeRegistry,
    private loadBalancer: ILoadBalancer,
    private config: SchedulerConfig
  ) {
    this.modeSelector = new DispatchModeSelector();
    this.currentMode = config.mode === 'auto' ? 'push' : config.mode;
    
    if (config.mode === 'push' || config.mode === 'hybrid' || config.mode === 'auto') {
      this.pushScheduler = new PushScheduler(taskQueue, nodeRegistry, loadBalancer, config.pushConfig);
    }
    
    if (config.mode === 'pull' || config.mode === 'hybrid' || config.mode === 'auto') {
      this.pullScheduler = new PullScheduler(taskQueue, nodeRegistry, config.pullConfig);
    }
  }
  
  async start(): Promise<void> {
    this.running = true;
    await this.pushScheduler?.start();
    await this.pullScheduler?.start();
  }
  
  async stop(): Promise<void> {
    this.running = false;
    await this.pushScheduler?.stop();
    await this.pullScheduler?.stop();
  }
  
  async pause(): Promise<void> {
    this.paused = true;
    await this.pushScheduler?.pause();
    await this.pullScheduler?.pause();
  }
  
  async resume(): Promise<void> {
    this.paused = false;
    await this.pushScheduler?.resume();
    await this.pullScheduler?.resume();
  }
  
  isRunning(): boolean {
    return this.running && !this.paused;
  }
  
  async scheduleBatch(tasks: Task[]): Promise<ScheduleResult> {
    const mode = this.config.mode === 'auto' 
      ? this.modeSelector.selectMode(tasks.length, this.estimateComplexity(tasks), tasks[0]?.priority || 5)
      : this.config.mode;
    
    this.currentMode = mode;
    
    switch (mode) {
      case 'push':
        if (!this.pushScheduler) throw new Error('Push scheduler not initialized');
        return this.pushScheduler.schedule(tasks);
      case 'pull':
        if (!this.pullScheduler) throw new Error('Pull scheduler not initialized');
        return this.pullScheduler.schedule(tasks);
      case 'hybrid':
        return this.scheduleHybrid(tasks);
      default:
        throw new Error(`Unknown mode: ${mode}`);
    }
  }
  
  private async scheduleHybrid(tasks: Task[]): Promise<ScheduleResult> {
    const { pushMaxTasks, pullMinTasks } = this.config.hybridThreshold || { pushMaxTasks: 10, pullMinTasks: 100 };
    
    const pushTasks = tasks.filter(t => t.priority <= 3 || tasks.length < pushMaxTasks);
    const pullTasks = tasks.filter(t => t.priority > 3 && tasks.length >= pullMinTasks);
    
    const [pushResult, pullResult] = await Promise.all([
      pushTasks.length > 0 && this.pushScheduler ? this.pushScheduler.schedule(pushTasks) : Promise.resolve(null),
      pullTasks.length > 0 && this.pullScheduler ? this.pullScheduler.schedule(pullTasks) : Promise.resolve(null),
    ]);
    
    return this.mergeResults(pushResult, pullResult);
  }
  
  async setMode(mode: DispatchMode): Promise<void> {
    if (mode === this.currentMode) return;
    await this.pause();
    await this.waitForRunningTasks(30000);
    this.currentMode = mode;
    await this.resume();
  }
  
  getMode(): DispatchMode {
    return this.currentMode;
  }
  
  getCurrentLoad(): number {
    const pushLoad = this.pushScheduler?.getCurrentLoad() || 0;
    const pullLoad = this.pullScheduler?.getCurrentLoad() || 0;
    return pushLoad + pullLoad;
  }
  
  getMaxLoad(): number {
    return this.config.maxConcurrentTasks;
  }
  
  private estimateComplexity(tasks: Task[]): number {
    return tasks.reduce((sum, t) => sum + (t.complexity || 5), 0) / tasks.length;
  }
  
  private mergeResults(pushResult: ScheduleResult | null, pullResult: ScheduleResult | null): ScheduleResult {
    if (!pushResult) return pullResult!;
    if (!pullResult) return pushResult;
    
    return {
      batchId: pushResult.batchId,
      taskIds: [...pushResult.taskIds, ...pullResult.taskIds],
      mode: 'hybrid',
      message: `Hybrid: ${pushResult.taskIds.length} push + ${pullResult.taskIds.length} pull`
    };
  }
  
  private async waitForRunningTasks(timeoutMs: number): Promise<void> {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      if (this.getCurrentLoad() === 0) return;
      await sleep(100);
    }
  }
}
```

#### 3.2.1 Push 调度器 (方案A)

```typescript
interface PushSchedulerConfig {
  maxConcurrent: number;
  pollIntervalMs: number;
  batchDispatchSize: number;
  enableBatchDispatch: boolean;
}

class PushScheduler implements ITaskScheduler {
  private runningTasks: Map<string, RunningTask> = new Map();
  private maxConcurrent: number;
  private pollInterval: number;
  private timer?: NodeJS.Timer;
  private paused: boolean = false;
  private running: boolean = false;
  private errorHandler: ErrorHandler;
  private eventEmitter: EventEmitter;
  
  constructor(
    private taskQueue: ITaskQueue,
    private nodeRegistry: INodeRegistry,
    private loadBalancer: ILoadBalancer,
    config?: PushSchedulerConfig
  ) {
    this.maxConcurrent = config?.maxConcurrent || 10;
    this.pollInterval = config?.pollIntervalMs || 100;
    this.errorHandler = new ErrorHandler();
    this.eventEmitter = new EventEmitter();
  }
  
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    this.timer = setInterval(() => this.pollAndDispatch(), this.pollInterval);
  }
  
  async stop(): Promise<void> {
    this.running = false;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }
  
  async pause(): Promise<void> {
    this.paused = true;
  }
  
  async resume(): Promise<void> {
    this.paused = false;
  }
  
  isRunning(): boolean {
    return this.running && !this.paused;
  }
  
  getCurrentLoad(): number {
    return this.runningTasks.size;
  }
  
  getMaxLoad(): number {
    return this.maxConcurrent;
  }
  
  async schedule(tasks: Task[]): Promise<ScheduleResult> {
    return {
      batchId: tasks[0]?.batchId || generateBatchId(),
      taskIds: tasks.map(t => t.id),
      mode: 'push',
      message: `Scheduled ${tasks.length} tasks in push mode`
    };
  }
  
  setMode(mode: DispatchMode): void {}
  
  getMode(): DispatchMode {
    return 'push';
  }
  
  on(event: string, listener: (...args: any[]) => void): void {
    this.eventEmitter.on(event, listener);
  }
  
  private async pollAndDispatch(): Promise<void> {
    if (this.paused || !this.running) return;
    
    if (this.runningTasks.size >= this.maxConcurrent) {
      return;
    }
    
    const availableSlots = this.maxConcurrent - this.runningTasks.size;
    const tasks: Task[] = [];
    
    for (let i = 0; i < availableSlots; i++) {
      const task = await this.taskQueue.dequeue();
      if (task) tasks.push(task);
      else break;
    }
    
    for (const task of tasks) {
      this.dispatchTask(task).catch(error => {
        console.error(`Failed to dispatch task ${task.id}:`, error);
      });
    }
  }
  
  private async dispatchTask(task: Task): Promise<void> {
    const node = this.loadBalancer.selectNode();
    if (!node) {
      await this.taskQueue.updateStatus(task.id, 'pending');
      return;
    }
    
    await this.taskQueue.updateStatus(task.id, 'assigning');
    
    const runningTask = new RunningTask(task, node);
    this.runningTasks.set(task.id, runningTask);
    this.nodeRegistry.incrementConnections(node.id);
    
    try {
      await this.executeOnNode(task, node);
      await this.taskQueue.updateStatus(task.id, 'completed');
      this.eventEmitter.emit('taskComplete', task.id);
    } catch (error) {
      await this.handleTaskFailure(task, error as Error);
    } finally {
      this.runningTasks.delete(task.id);
      this.nodeRegistry.decrementConnections(node.id);
    }
  }
  
  private async executeOnNode(task: Task, node: NodeInfo): Promise<void> {
    const client = new FunASRClient(node);
    const result = await client.transcribe(task.payload.audioFile);
    
    await this.taskQueue.updateStatus(task.id, 'completed', {
      taskId: task.id,
      batchId: task.batchId,
      status: 'success',
      text: result.text,
      timestamps: result.timestamps,
      processingTime: Date.now() - (task.startedAt || Date.now()),
      nodeId: node.id,
      createdAt: Date.now()
    });
  }
  
  private async handleTaskFailure(task: Task, error: Error): Promise<void> {
    const shouldRetry = task.retryCount < task.maxRetries;
    
    if (shouldRetry) {
      task.retryCount++;
      task.status = 'retrying';
      task.error = error.message;
      
      const delay = calculateExponentialBackoff(task.retryCount);
      await sleep(delay);
      
      await this.taskQueue.updateStatus(task.id, 'pending');
    } else {
      await this.taskQueue.updateStatus(task.id, 'failed');
      this.eventEmitter.emit('taskFailed', task.id);
    }
  }
}
```

#### 3.2.2 Pull 调度器 (方案B)

```typescript
interface PullSchedulerConfig {
  workerPollIntervalMs: number;
  maxWorkerIdleTimeMs: number;
}

class PullScheduler implements ITaskScheduler {
  private workers: Map<string, NodeWorker> = new Map();
  private running: boolean = false;
  private paused: boolean = false;
  private eventEmitter: EventEmitter;
  private eventStore?: IEventStore;
  
  constructor(
    private taskQueue: ITaskQueue,
    private nodeRegistry: INodeRegistry,
    private config?: PullSchedulerConfig,
    eventStore?: IEventStore
  ) {
    this.eventEmitter = new EventEmitter();
    this.eventStore = eventStore;
  }
  
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    
    const nodes = this.nodeRegistry.getAvailableNodes();
    for (const node of nodes) {
      await this.startWorker(node);
    }
    
    this.nodeRegistry.on('nodeUp', (node) => this.startWorker(node));
    this.nodeRegistry.on('nodeDown', (node) => this.stopWorker(node.id));
  }
  
  async stop(): Promise<void> {
    this.running = false;
    
    for (const [nodeId, worker] of this.workers) {
      await worker.stop();
    }
    this.workers.clear();
  }
  
  async pause(): Promise<void> {
    this.paused = true;
    for (const worker of this.workers.values()) {
      await worker.pause();
    }
  }
  
  async resume(): Promise<void> {
    this.paused = false;
    for (const worker of this.workers.values()) {
      await worker.resume();
    }
  }
  
  isRunning(): boolean {
    return this.running && !this.paused;
  }
  
  getCurrentLoad(): number {
    return Array.from(this.workers.values()).reduce(
      (sum, w) => sum + (w.isBusy() ? 1 : 0), 0
    );
  }
  
  getMaxLoad(): number {
    return this.workers.size;
  }
  
  async schedule(tasks: Task[]): Promise<ScheduleResult> {
    return {
      batchId: tasks[0]?.batchId || generateBatchId(),
      taskIds: tasks.map(t => t.id),
      mode: 'pull',
      message: `Queued ${tasks.length} tasks for pull mode`
    };
  }
  
  setMode(mode: DispatchMode): void {}
  
  getMode(): DispatchMode {
    return 'pull';
  }
  
  on(event: string, listener: (...args: any[]) => void): void {
    this.eventEmitter.on(event, listener);
  }
  
  private async startWorker(node: NodeInfo): Promise<void> {
    if (this.workers.has(node.id)) return;
    
    const worker = new NodeWorker(node, this.taskQueue, this.eventStore, this.config);
    
    this.workers.set(node.id, worker);
    
    worker.on('taskComplete', (taskId: string) => {
      this.eventEmitter.emit('taskComplete', taskId);
    });
    
    worker.on('taskFailed', (taskId: string) => {
      this.eventEmitter.emit('taskFailed', taskId);
    });
    
    await worker.start();
  }
  
  private async stopWorker(nodeId: string): Promise<void> {
    const worker = this.workers.get(nodeId);
    if (worker) {
      await worker.stop();
      this.workers.delete(nodeId);
    }
  }
}

// 节点Worker (Pull模式核心)
class NodeWorker extends EventEmitter {
  private running: boolean = false;
  private paused: boolean = false;
  private currentTask?: Task;
  private idempotencyGuard?: IdempotencyGuard;
  
  constructor(
    private node: NodeInfo,
    private taskQueue: ITaskQueue,
    private eventStore?: IEventStore,
    private config?: PullSchedulerConfig
  ) {
    super();
  }
  
  async start(): Promise<void> {
    if (this.running) return;
    this.running = true;
    
    while (this.running) {
      try {
        await this.processNextTask();
      } catch (error) {
        console.error(`Worker ${this.node.id} error:`, error);
        await sleep(5000);
      }
    }
  }
  
  async stop(): Promise<void> {
    this.running = false;
  }
  
  async pause(): Promise<void> {
    this.paused = true;
  }
  
  async resume(): Promise<void> {
    this.paused = false;
  }
  
  isBusy(): boolean {
    return !!this.currentTask;
  }
  
  private async processNextTask(): Promise<void> {
    if (this.paused) {
      await sleep(1000);
      return;
    }
    
    const task = await this.taskQueue.dequeue(this.node.id);
    if (!task) {
      const pollInterval = this.config?.workerPollIntervalMs || 1000;
      await sleep(pollInterval);
      return;
    }
    
    this.currentTask = task;
    
    try {
      await this.eventStore?.record({
        type: 'TASK_PICKED',
        taskId: task.id,
        nodeId: this.node.id,
        timestamp: Date.now()
      });
      
      const result = await this.executeWithIdempotency(task);
      
      if ('acknowledge' in this.taskQueue) {
        await (this.taskQueue as RedisTaskQueue).acknowledge(task);
      }
      
      await this.taskQueue.updateStatus(task.id, 'completed', result);
      this.emit('taskComplete', task.id);
      
    } catch (error) {
      await this.handleError(task, error as Error);
    } finally {
      this.currentTask = undefined;
    }
  }
  
  private async executeWithIdempotency(task: Task): Promise<TaskResult> {
    if (!this.idempotencyGuard) {
      return this.executeTask(task);
    }
    
    return this.idempotencyGuard.execute(task.id, () => this.executeTask(task));
  }
  
  private async executeTask(task: Task): Promise<TaskResult> {
    const client = new FunASRClient(this.node);
    const startTime = Date.now();
    
    const transcriptionResult = await client.transcribe(task.payload.audioFile);
    
    return {
      taskId: task.id,
      batchId: task.batchId,
      status: 'success',
      text: transcriptionResult.text,
      timestamps: transcriptionResult.timestamps,
      duration: transcriptionResult.duration,
      processingTime: Date.now() - startTime,
      nodeId: this.node.id,
      createdAt: Date.now()
    };
  }
  
  private async handleError(task: Task, error: Error): Promise<void> {
    await this.eventStore?.record({
      type: 'TASK_FAILED',
      taskId: task.id,
      nodeId: this.node.id,
      error: error.message,
      timestamp: Date.now()
    });
    
    await this.taskQueue.updateStatus(task.id, 'failed');
    this.emit('taskFailed', task.id);
  }
}
```

### 3.3 节点注册中心 (增强版)

```typescript
interface NodeRegistryConfig {
  healthCheck: {
    mode: 'heartbeat' | 'gossip' | 'both';
    intervalMs: number;
    timeoutMs: number;
    failureThreshold?: number;
  };
  gossip?: {
    port: number;
    peers: string[];
    syncIntervalMs: number;
  };
}

class UnifiedNodeRegistry implements INodeRegistry {
  private nodes: Map<string, NodeInfo> = new Map();
  private statuses: Map<string, NodeStatus> = new Map();
  private circuitBreakers: Map<string, CircuitBreaker> = new Map();
  private heartbeatChecker?: HeartbeatChecker;
  private gossipProtocol?: GossipProtocol;
  private eventEmitter: EventEmitter;
  
  constructor(private config: NodeRegistryConfig) {
    this.eventEmitter = new EventEmitter();
    
    if (config.healthCheck.mode === 'heartbeat' || config.healthCheck.mode === 'both') {
      this.heartbeatChecker = new HeartbeatChecker(this, config.healthCheck);
    }
    
    if (config.healthCheck.mode === 'gossip' || config.healthCheck.mode === 'both') {
      this.gossipProtocol = new GossipProtocol(config.gossip!);
    }
  }
  
  register(node: NodeInfo): void {
    this.nodes.set(node.id, node);
    this.statuses.set(node.id, {
      nodeId: node.id,
      state: 'online',
      currentConnections: 0,
      lastHeartbeat: Date.now(),
      totalRequests: 0,
      failedRequests: 0,
      avgResponseTime: 0,
      circuitBreakerState: 'closed'
    });
    
    this.circuitBreakers.set(node.id, new CircuitBreaker(node.id, {
      failureThreshold: 5,
      resetTimeoutMs: 60000,
      halfOpenMaxCalls: 3
    }));
    
    this.eventEmitter.emit('nodeRegistered', node);
    this.eventEmitter.emit('nodeUp', node);
    
    this.gossipProtocol?.broadcast({
      type: 'NODE_JOINED',
      nodeId: node.id,
      nodeInfo: node,
      timestamp: Date.now()
    });
  }
  
  unregister(nodeId: string): boolean {
    const node = this.nodes.get(nodeId);
    if (!node) return false;
    
    this.nodes.delete(nodeId);
    this.statuses.delete(nodeId);
    this.circuitBreakers.delete(nodeId);
    
    this.eventEmitter.emit('nodeDown', node);
    
    this.gossipProtocol?.broadcast({
      type: 'NODE_LEFT',
      nodeId,
      timestamp: Date.now()
    });
    
    return true;
  }
  
  getNode(nodeId: string): NodeInfo | null {
    return this.nodes.get(nodeId) || null;
  }
  
  getAllNodes(): NodeInfo[] {
    return Array.from(this.nodes.values());
  }
  
  getAvailableNodes(): NodeInfo[] {
    return Array.from(this.nodes.values()).filter(node => {
      const status = this.statuses.get(node.id);
      const circuitBreaker = this.circuitBreakers.get(node.id);
      
      return status && 
             (status.state === 'online' || status.state === 'busy') &&
             circuitBreaker?.canExecute() !== false;
    });
  }
  
  updateStatus(nodeId: string, statusUpdate: Partial<NodeStatus>): void {
    const current = this.statuses.get(nodeId);
    if (current) {
      Object.assign(current, statusUpdate);
      current.lastHeartbeat = Date.now();
      
      this.gossipProtocol?.broadcast({
        type: 'STATUS_UPDATE',
        nodeId,
        status: current,
        timestamp: Date.now()
      });
    }
  }
  
  incrementConnections(nodeId: string): void {
    const status = this.statuses.get(nodeId);
    if (status) {
      status.currentConnections++;
      if (status.currentConnections >= (this.nodes.get(nodeId)?.maxConnections || 10)) {
        status.state = 'busy';
      }
    }
  }
  
  decrementConnections(nodeId: string): void {
    const status = this.statuses.get(nodeId);
    if (status) {
      status.currentConnections = Math.max(0, status.currentConnections - 1);
      if (status.state === 'busy' && status.currentConnections < (this.nodes.get(nodeId)?.maxConnections || 10)) {
        status.state = 'online';
      }
    }
  }
  
  getNodeStatus(nodeId: string): NodeStatus | null {
    return this.statuses.get(nodeId) || null;
  }
  
  getConnectionCount(nodeId: string): number {
    return this.statuses.get(nodeId)?.currentConnections || 0;
  }
  
  getLastHeartbeat(nodeId: string): number {
    return this.statuses.get(nodeId)?.lastHeartbeat || 0;
  }
  
  getCircuitBreaker(nodeId: string): CircuitBreaker | null {
    return this.circuitBreakers.get(nodeId) || null;
  }
  
  recordSuccess(nodeId: string, responseTime: number): void {
    const status = this.statuses.get(nodeId);
    if (status) {
      status.totalRequests++;
      status.avgResponseTime = status.avgResponseTime * 0.9 + responseTime * 0.1;
    }
    
    this.circuitBreakers.get(nodeId)?.recordSuccess();
  }
  
  recordFailure(nodeId: string): void {
    const status = this.statuses.get(nodeId);
    if (status) {
      status.totalRequests++;
      status.failedRequests++;
    }
    
    this.circuitBreakers.get(nodeId)?.recordFailure();
  }
  
  getStats(): NodeStats {
    const statuses = Array.from(this.statuses.values());
    return {
      total: this.nodes.size,
      online: statuses.filter(s => s.state === 'online').length,
      offline: statuses.filter(s => s.state === 'offline').length,
      busy: statuses.filter(s => s.state === 'busy').length,
      unhealthy: statuses.filter(s => s.state === 'unhealthy').length
    };
  }
  
  startHealthCheck(): void {
    this.heartbeatChecker?.start();
    this.gossipProtocol?.start();
  }
  
  stopHealthCheck(): void {
    this.heartbeatChecker?.stop();
    this.gossipProtocol?.stop();
  }
  
  on(event: 'nodeUp' | 'nodeDown' | 'nodeRegistered', listener: (node: NodeInfo) => void): void {
    this.eventEmitter.on(event, listener);
  }
}
```

### 3.4 FunASR WebSocket 客户端

```typescript
interface FunASRClientConfig {
  host: string;
  port: number;
  ssl?: boolean;
  timeoutMs?: number;
  maxReconnectAttempts?: number;
}

interface TranscriptionResult {
  text: string;
  timestamps?: TimestampSegment[];
  duration?: number;
  confidence?: number;
}

class FunASRClient {
  private ws?: WebSocket;
  private config: FunASRClientConfig;
  private reconnectAttempts: number = 0;
  
  constructor(node: NodeInfo) {
    this.config = {
      host: node.host,
      port: node.port,
      ssl: node.ssl,
      timeoutMs: 300000,
      maxReconnectAttempts: 3
    };
  }
  
  async transcribe(audioFile: string): Promise<TranscriptionResult> {
    return new Promise((resolve, reject) => {
      const protocol = this.config.ssl ? 'wss' : 'ws';
      const url = `${protocol}://${this.config.host}:${this.config.port}`;
      
      try {
        this.ws = new WebSocket(url);
        
        const timeout = setTimeout(() => {
          this.ws?.close();
          reject(new Error('Transcription timeout'));
        }, this.config.timeoutMs);
        
        this.ws.on('open', () => {
          this.sendAudioFile(audioFile);
        });
        
        this.ws.on('message', (data: WebSocket.Data) => {
          const message = JSON.parse(data.toString());
          
          if (message.mode === '2pass-offline' || message.is_final) {
            clearTimeout(timeout);
            this.ws?.close();
            
            resolve({
              text: message.text || '',
              timestamps: message.timestamp,
              duration: message.duration,
              confidence: message.confidence
            });
          }
        });
        
        this.ws.on('error', (error) => {
          clearTimeout(timeout);
          reject(error);
        });
        
        this.ws.on('close', () => {
          clearTimeout(timeout);
        });
        
      } catch (error) {
        reject(error);
      }
    });
  }
  
  private async sendAudioFile(audioFile: string): Promise<void> {
    const audioData = await fs.promises.readFile(audioFile);
    
    const configMessage = {
      mode: '2pass-offline',
      wav_format: path.extname(audioFile).slice(1),
      wav_name: path.basename(audioFile)
    };
    
    this.ws?.send(JSON.stringify(configMessage));
    
    const chunkSize = 4096;
    for (let i = 0; i < audioData.length; i += chunkSize) {
      const chunk = audioData.slice(i, i + chunkSize);
      this.ws?.send(chunk);
      await sleep(10);
    }
    
    const endMessage = { is_speaking: false };
    this.ws?.send(JSON.stringify(endMessage));
  }
  
  close(): void {
    this.ws?.close();
  }
}
```

### 3.5 事件存储 (可选组件)

```typescript
interface EventStore {
  record(event: TaskEvent): Promise<void>;
  recordBatch(events: TaskEvent[]): Promise<void>;
  query(taskId: string): Promise<TaskEvent[]>;
  queryByTimeRange(start: number, end: number): Promise<TaskEvent[]>;
  queryByBatch(batchId: string): Promise<TaskEvent[]>;
  rebuildState(taskId: string): Promise<Task | null>;
  snapshot(taskId: string, state: Task): Promise<void>;
  subscribe(callback: (event: TaskEvent) => void): () => void;
}

class RedisEventStore implements EventStore {
  private redis: Redis;
  private subscribers: Set<(event: TaskEvent) => void> = new Set();
  
  constructor(redisUrl: string) {
    this.redis = new Redis(redisUrl);
  }
  
  async record(event: TaskEvent): Promise<void> {
    await this.redis.xadd(
      'funasr:events',
      '*',
      'type', event.type,
      'taskId', event.taskId,
      'data', JSON.stringify(event)
    );
    
    this.subscribers.forEach(callback => {
      try {
        callback(event);
      } catch (error) {
        console.error('Event subscriber error:', error);
      }
    });
  }
  
  async recordBatch(events: TaskEvent[]): Promise<void> {
    const pipeline = this.redis.pipeline();
    
    for (const event of events) {
      pipeline.xadd(
        'funasr:events',
        '*',
        'type', event.type,
        'taskId', event.taskId,
        'data', JSON.stringify(event)
      );
    }
    
    await pipeline.exec();
  }
  
  async query(taskId: string): Promise<TaskEvent[]> {
    const events: TaskEvent[] = [];
    let lastId = '0';
    
    while (true) {
      const messages = await this.redis.xread(
        'COUNT', 100,
        'STREAMS', 'funasr:events', lastId
      );
      
      if (!messages || messages.length === 0) break;
      
      const [, entries] = messages[0];
      for (const [id, fields] of entries) {
        lastId = id;
        const eventData = JSON.parse(fields[3]);
        if (eventData.taskId === taskId) {
          events.push(eventData);
        }
      }
      
      if (entries.length < 100) break;
    }
    
    return events.sort((a, b) => a.timestamp - b.timestamp);
  }
  
  async queryByTimeRange(start: number, end: number): Promise<TaskEvent[]> {
    const events = await this.redis.xrange(
      'funasr:events',
      start.toString(),
      end.toString(),
      'COUNT', 1000
    );
    
    return events.map(([, fields]) => JSON.parse(fields[3]));
  }
  
  async queryByBatch(batchId: string): Promise<TaskEvent[]> {
    const allEvents = await this.queryByTimeRange(0, Date.now());
    return allEvents.filter(e => e.batchId === batchId);
  }
  
  async rebuildState(taskId: string): Promise<Task | null> {
    const events = await this.query(taskId);
    if (events.length === 0) return null;
    
    let state = this.initialState();
    for (const event of events) {
      state = this.applyEvent(state, event);
    }
    
    return state;
  }
  
  async snapshot(taskId: string, state: Task): Promise<void> {
    await this.redis.set(
      `funasr:snapshot:${taskId}`,
      JSON.stringify(state)
    );
  }
  
  subscribe(callback: (event: TaskEvent) => void): () => void {
    this.subscribers.add(callback);
    return () => this.subscribers.delete(callback);
  }
  
  private initialState(): Task {
    return {
      id: '',
      batchId: '',
      type: 'recognition',
      priority: 5,
      status: 'pending',
      payload: {} as TaskPayload,
      retryCount: 0,
      maxRetries: 3,
      createdAt: Date.now(),
      updatedAt: Date.now()
    };
  }
  
  private applyEvent(state: Task, event: TaskEvent): Task {
    switch (event.type) {
      case 'TASK_SUBMITTED':
        return { ...state, ...event.payload, status: 'pending', id: event.taskId };
      case 'TASK_PICKED':
        return { ...state, status: 'assigning', assignedNode: event.nodeId };
      case 'TASK_STARTED':
        return { ...state, status: 'running', startedAt: event.timestamp };
      case 'TASK_COMPLETED':
        return { ...state, status: 'completed', completedAt: event.timestamp, result: event.result };
      case 'TASK_FAILED':
        return { ...state, status: 'failed', error: event.error };
      case 'TASK_RETRY':
        return { ...state, status: 'retrying', retryCount: event.attempt };
      default:
        return state;
    }
  }
}
```

---

## 4.

### 4 双模式任务分发.1 模式选择策略

```typescript
class AdaptiveModeSelector implements ModeSelectionStrategy {
  select(tasks: Task[], nodes: NodeInfo[]): DispatchMode {
    const taskCount = tasks.length;
    const avgPriority = tasks.reduce((sum, t) => sum + t.priority, 0) / taskCount;
    const availableNodes = nodes.length;
    const hasHighComplexity = tasks.some(t => (t.complexity || 5) > 7);
    
    // 场景1: 小批量高优先级 -> Push
    if (taskCount < 10 && avgPriority <= 3) {
      return 'push';
    }
    
    // 场景2: 超高复杂度任务 -> Push
    if (hasHighComplexity && taskCount < 20) {
      return 'push';
    }
    
    // 场景3: 大批量 -> Pull
    if (taskCount > 100) {
      return 'pull';
    }
    
    // 场景4: 节点数多 -> Pull
    if (availableNodes > 5 && taskCount > 20) {
      return 'pull';
    }
    
    // 场景5: 混合模式
    return 'hybrid';
  }
}
```

### 4.2 模式对比与适用场景

| 特性 | Push 模式 | Pull 模式 | Hybrid 模式 |
|------|----------|----------|-------------|
| **适用场景** | 小批量、低延迟、高优先级 | 大批量、高吞吐、后台任务 | 复杂混合场景、动态负载 |
| **延迟** | 低 (< 100ms) | 中 (100-500ms) | 自适应 |
| **吞吐** | 中 (10-50 TPS/节点) | 高 (50-100 TPS/节点) | 高 |
| **资源消耗** | 调度器CPU密集型 | 节点CPU密集型 | 均衡 |
| **复杂度** | 低 | 中 | 中 |
| **容错** | 调度器重试 | 节点自治 + 幂等 | 双重保障 |
| **扩展性** | 受限于调度器 | 线性扩展 | 灵活扩展 |
| **适用数据量** | < 1000 任务/批次 | > 1000 任务/批次 | 任意 |

### 4.3 运行时模式切换

```typescript
class RuntimeModeSwitcher {
  constructor(private scheduler: DualModeScheduler) {}
  
  async switchMode(newMode: DispatchMode, options: SwitchModeOptions = {}): Promise<void> {
    const { waitForCompletion = true, timeoutMs = 30000 } = options;
    
    await this.scheduler.pause();
    
    if (waitForCompletion) {
      const startTime = Date.now();
      while (this.scheduler.getCurrentLoad() > 0) {
        if (Date.now() - startTime > timeoutMs) break;
        await sleep(100);
      }
    }
    
    await this.scheduler.setMode(newMode);
    await this.scheduler.resume();
  }
  
  async autoSwitch(): Promise<void> {
    const stats = await this.scheduler.getStats();
    const currentMode = this.scheduler.getMode();
    
    if (stats.pending > 100 && currentMode === 'push') {
      await this.switchMode('pull');
      return;
    }
    
    if (stats.pending < 10 && currentMode === 'pull') {
      await this.switchMode('push');
    }
  }
}
```

---

## 5. 负载均衡策略

### 6.1 策略实现

```typescript
interface LoadBalanceStrategy {
  selectNode(nodes: NodeInfo[], statuses: Map<string, NodeStatus>): NodeInfo | null;
  getName(): string;
  getDescription(): string;
}

// 轮询策略
class RoundRobinStrategy implements LoadBalanceStrategy {
  private currentIndex: number = 0;
  
  selectNode(nodes: NodeInfo[]): NodeInfo | null {
    if (nodes.length === 0) return null;
    const node = nodes[this.currentIndex % nodes.length];
    this.currentIndex++;
    return node;
  }
  
  getName(): string { return 'round-robin'; }
  getDescription(): string { return '按顺序轮询选择节点'; }
}

// 加权轮询策略
class WeightedRoundRobinStrategy implements LoadBalanceStrategy {
  private currentWeights: Map<string, number> = new Map();
  
  selectNode(nodes: NodeInfo[]): NodeInfo | null {
    if (nodes.length === 0) return null;
    
    let selectedNode: NodeInfo | null = null;
    let maxWeight = -Infinity;
    let totalWeight = 0;
    
    nodes.forEach(node => {
      const currentWeight = (this.currentWeights.get(node.id) || 0) + node.weight;
      this.currentWeights.set(node.id, currentWeight);
      totalWeight += node.weight;
      
      if (currentWeight > maxWeight) {
        maxWeight = currentWeight;
        selectedNode = node;
      }
    });
    
    if (selectedNode) {
      const current = this.currentWeights.get(selectedNode.id) || 0;
      this.currentWeights.set(selectedNode.id, current - totalWeight);
    }
    
    return selectedNode;
  }
  
  getName(): string { return 'weighted-round-robin'; }
  getDescription(): string { return '按权重比例轮询选择节点'; }
}

// 最少连接策略
class LeastConnectionsStrategy implements LoadBalanceStrategy {
  selectNode(nodes: NodeInfo[], statuses: Map<string, NodeStatus>): NodeInfo | null {
    if (nodes.length === 0) return null;
    
    return nodes.reduce((minNode, currentNode) => {
      const minConn = statuses.get(minNode.id)?.currentConnections || 0;
      const currentConn = statuses.get(currentNode.id)?.currentConnections || 0;
      return currentConn < minConn ? currentNode : minNode;
    });
  }
  
  getName(): string { return 'least-connections'; }
  getDescription(): string { return '选择当前连接数最少的节点'; }
}

// 响应时间加权策略
class ResponseTimeStrategy implements LoadBalanceStrategy {
  selectNode(nodes: NodeInfo[], statuses: Map<string, NodeStatus>): NodeInfo | null {
    if (nodes.length === 0) return null;
    
    return nodes.reduce((fastest, current) => {
      const fastestTime = statuses.get(fastest.id)?.avgResponseTime || Infinity;
      const currentTime = statuses.get(current.id)?.avgResponseTime || Infinity;
      return currentTime < fastestTime ? current : fastest;
    });
  }
  
  getName(): string { return 'response-time'; }
  getDescription(): string { return '选择平均响应时间最短的节点'; }
}

// 能力感知策略
class CapacityAwareStrategy implements LoadBalanceStrategy {
  selectNode(nodes: NodeInfo[], statuses: Map<string, NodeStatus>): NodeInfo | null {
    if (nodes.length === 0) return null;
    
    const scores = nodes.map(node => {
      const status = statuses.get(node.id);
      const currentLoad = status?.currentConnections || 0;
      const capacity = node.maxConnections;
      const remainingRatio = (capacity - currentLoad) / capacity;
      
      return {
        node,
        score: remainingRatio * node.weight
      };
    });
    
    scores.sort((a, b) => b.score - a.score);
    return scores[0]?.node || null;
  }
  
  getName(): string { return 'capacity-aware'; }
  getDescription(): string { return '基于节点剩余容量和权重选择节点'; }
}

// 一致性哈希策略
class ConsistentHashStrategy implements LoadBalanceStrategy {
  private ring: Map<number, NodeInfo> = new Map();
  private virtualNodes: number = 150;
  
  constructor(nodes: NodeInfo[]) {
    this.buildRing(nodes);
  }
  
  selectNode(nodes: NodeInfo[], statuses: Map<string, NodeStatus>, taskId?: string): NodeInfo | null {
    if (nodes.length === 0) return null;
    if (!taskId) return new RoundRobinStrategy().selectNode(nodes);
    
    const hash = this.hash(taskId);
    const sortedKeys = Array.from(this.ring.keys()).sort((a, b) => a - b);
    
    for (const key of sortedKeys) {
      if (key >= hash) {
        const node = this.ring.get(key);
        if (node && statuses.get(node.id)?.state !== 'offline') {
          return node;
        }
      }
    }
    
    const firstKey = sortedKeys[0];
    return this.ring.get(firstKey) || null;
  }
  
  getName(): string { return 'consistent-hash'; }
  getDescription(): string { return '使用一致性哈希分配任务，保证相同任务路由到相同节点'; }
  
  private buildRing(nodes: NodeInfo[]): void {
    this.ring.clear();
    for (const node of nodes) {
      for (let i = 0; i < this.virtualNodes; i++) {
        const hash = this.hash(`${node.id}:${i}`);
        this.ring.set(hash, node);
      }
    }
  }
  
  private hash(key: string): number {
    let hash = 0;
    for (let i = 0; i < key.length; i++) {
      const char = key.charCodeAt(i);
      hash = ((hash << 5) - hash) + char;
      hash = hash & hash;
    }
    return Math.abs(hash);
  }
}

class LoadBalanceStrategyFactory {
  static create(strategyName: string): LoadBalanceStrategy {
    switch (strategyName) {
      case 'round-robin': return new RoundRobinStrategy();
      case 'weighted-round-robin': return new WeightedRoundRobinStrategy();
      case 'least-connections': return new LeastConnectionsStrategy();
      case 'response-time': return new ResponseTimeStrategy();
      case 'capacity-aware': return new CapacityAwareStrategy();
      case 'consistent-hash': return new ConsistentHashStrategy([]);
      default: throw new Error(`Unknown strategy: ${strategyName}`);
    }
  }
  
  static getAvailableStrategies(): Array<{name: string, description: string}> {
    return [
      { name: 'round-robin', description: '按顺序轮询选择节点' },
      { name: 'weighted-round-robin', description: '按权重比例轮询选择节点' },
      { name: 'least-connections', description: '选择当前连接数最少的节点' },
      { name: 'response-time', description: '选择平均响应时间最短的节点' },
      { name: 'capacity-aware', description: '基于节点剩余容量和权重选择节点' },
      { name: 'consistent-hash', description: '使用一致性哈希分配任务' }
    ];
  }
}
```

---

## 7. 容错与恢复机制

### 7.1 双重保障机制

```typescript
interface FaultToleranceConfig {
  circuitBreaker: {
    enabled: boolean;
    failureThreshold: number;
    resetTimeoutMs: number;
    halfOpenMaxCalls: number;
  };
  retry: {
    maxRetries: number;
    baseDelayMs: number;
    maxDelayMs: number;
    backoffMultiplier: number;
    jitter: boolean;
  };
  idempotency: {
    enabled: boolean;
    keyPrefix: string;
    lockTimeoutMs: number;
    resultTtlSeconds: number;
  };
  eventSourcing: {
    enabled: boolean;
    snapshotInterval: number;
  };
}

// 指数退避
class ExponentialBackoff {
  constructor(
    private baseDelayMs: number = 1000,
    private maxDelayMs: number = 60000,
    private multiplier: number = 2,
    private jitter: boolean = true
  ) {}
  
  calculateDelay(retryCount: number): number {
    let delay = this.baseDelayMs * Math.pow(this.multiplier, retryCount);
    delay = Math.min(delay, this.maxDelayMs);
    
    if (this.jitter) {
      const jitterAmount = delay * 0.25;
      delay = delay + (Math.random() * 2 - 1) * jitterAmount;
    }
    
    return Math.floor(delay);
  }
}

function calculateExponentialBackoff(retryCount: number): number {
  const backoff = new ExponentialBackoff();
  return backoff.calculateDelay(retryCount);
}
```

### 7.2 断路器模式

```typescript
enum CircuitState {
  CLOSED = 'closed',
  OPEN = 'open',
  HALF_OPEN = 'half-open',
}

class CircuitBreaker {
  private state: CircuitState = CircuitState.CLOSED;
  private failureCount: number = 0;
  private successCount: number = 0;
  private nextAttempt: number = 0;
  
  constructor(
    private nodeId: string,
    private config: CircuitBreakerConfig
  ) {}
  
  canExecute(): boolean {
    if (this.state === CircuitState.CLOSED) return true;
    
    if (this.state === CircuitState.OPEN) {
      if (Date.now() >= this.nextAttempt) {
        this.state = CircuitState.HALF_OPEN;
        this.successCount = 0;
        return true;
      }
      return false;
    }
    
    return this.successCount < this.config.halfOpenMaxCalls;
  }
  
  recordSuccess(): void {
    this.failureCount = 0;
    
    if (this.state === CircuitState.HALF_OPEN) {
      this.successCount++;
      if (this.successCount >= this.config.halfOpenMaxCalls) {
        this.state = CircuitState.CLOSED;
      }
    }
  }
  
  recordFailure(): void {
    this.failureCount++;
    
    if (this.state === CircuitState.HALF_OPEN) {
      this.state = CircuitState.OPEN;
      this.nextAttempt = Date.now() + this.config.resetTimeoutMs;
    } else if (this.failureCount >= this.config.failureThreshold) {
      this.state = CircuitState.OPEN;
      this.nextAttempt = Date.now() + this.config.resetTimeoutMs;
    }
  }
  
  getState(): CircuitState {
    return this.state;
  }
}
```

### 7.3 幂等性保证

```typescript
class IdempotencyGuard {
  constructor(private redis: Redis, private config: IdempotencyConfig) {}
  
  async execute<T>(
    taskId: string,
    processor: () => Promise<T>
  ): Promise<T> {
    const lockKey = `${this.config.keyPrefix}:lock:${taskId}`;
    const resultKey = `${this.config.keyPrefix}:result:${taskId}`;
    
    const existingResult = await this.redis.get(resultKey);
    if (existingResult) {
      return JSON.parse(existingResult);
    }
    
    const lockValue = generateId();
    const acquired = await this.redis.set(
      lockKey, lockValue, 'NX', 'EX',
      Math.floor(this.config.lockTimeoutMs / 1000)
    );
    
    if (!acquired) {
      const result = await this.waitForResult(resultKey);
      if (result) return JSON.parse(result);
      throw new Error('Failed to acquire lock and no result found');
    }
    
    try {
      const doubleCheckResult = await this.redis.get(resultKey);
      if (doubleCheckResult) return JSON.parse(doubleCheckResult);
      
      const result = await processor();
      
      await this.redis.setex(
        resultKey, this.config.resultTtlSeconds, JSON.stringify(result)
      );
      
      return result;
    } finally {
      const luaScript = `
        if redis.call("get", KEYS[1]) == ARGV[1] then
          return redis.call("del", KEYS[1])
        else return 0 end
      `;
      await this.redis.eval(luaScript, 1, lockKey, lockValue);
    }
  }
  
  private async waitForResult(resultKey: string, maxWaitMs: number = 30000): Promise<string | null> {
    const interval = 100;
    const maxAttempts = maxWaitMs / interval;
    
    for (let i = 0; i < maxAttempts; i++) {
      const result = await this.redis.get(resultKey);
      if (result) return result;
      await sleep(interval);
    }
    
    return null;
  }
}
```

---

## 8. 数据模型设计

### 8.1 核心类型定义

```typescript
// 节点相关类型
interface NodeInfo {
  id: string;
  host: string;
  port: number;
  weight: number;
  maxConnections: number;
  capabilities: NodeCapability;
  ssl: boolean;
  metadata?: Record<string, any>;
}

interface NodeCapability {
  maxAudioDuration: number;
  supportedFormats: string[];
  supportsHotwords: boolean;
  supportsTimestamps: boolean;
  tps?: number;
}

interface NodeStatus {
  nodeId: string;
  state: NodeState;
  currentConnections: number;
  lastHeartbeat: number;
  totalRequests: number;
  failedRequests: number;
  avgResponseTime: number;
  circuitBreakerState: CircuitState;
}

type NodeState = 'online' | 'offline' | 'busy' | 'unhealthy';
type CircuitState = 'closed' | 'open' | 'half-open';

// 任务相关类型
interface Task {
  id: string;
  batchId: string;
  type: TaskType;
  priority: number;
  status: TaskStatus;
  payload: TaskPayload;
  dispatchMode?: DispatchMode;
  complexity?: number;
  createdAt: number;
  updatedAt: number;
  startedAt?: number;
  completedAt?: number;
  retryCount: number;
  maxRetries: number;
  assignedNode?: string;
  result?: TaskResult;
  error?: string;
  _redisId?: string;
}

type TaskType = 'recognition';
type TaskStatus = 'pending' | 'queued' | 'assigning' | 'running' | 'completed' | 'failed' | 'retrying' | 'cancelled';
type DispatchMode = 'push' | 'pull' | 'hybrid';

interface TaskPayload {
  audioFile: string;
  audioFormat?: AudioFormat;
  duration?: number;
  language?: string;
  hotwords?: string[];
  outputFormat?: OutputFormat;
  customParams?: Record<string, any>;
}

type AudioFormat = 'wav' | 'mp3' | 'm4a' | 'ogg' | 'flac';
type OutputFormat = 'json' | 'text' | 'srt' | 'vtt';

// 批次相关类型
interface BatchJob {
  id: string;
  status: BatchStatus;
  totalTasks: number;
  completedTasks: number;
  failedTasks: number;
  dispatchMode: DispatchMode;
  createdAt: number;
  startedAt?: number;
  completedAt?: number;
  taskIds: string[];
  config: BatchConfig;
}

type BatchStatus = 'created' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

interface BatchConfig {
  priority: number;
  maxRetries: number;
  retryDelayMs: number;
  timeoutMs: number;
  dispatchMode?: DispatchMode;
  callbackUrl?: string;
}

// 结果相关类型
interface TaskResult {
  taskId: string;
  batchId: string;
  status: 'success' | 'failed';
  text?: string;
  timestamps?: TimestampSegment[];
  duration?: number;
  processingTime: number;
  nodeId: string;
  error?: string;
  errorCode?: string;
  createdAt: number;
}

interface TimestampSegment {
  start: number;
  end: number;
  text: string;
  speaker?: string;
}

interface QualityMetrics {
  totalTasks: number;
  successCount: number;
  failedCount: number;
  successRate: number;
  avgProcessingTime: number;
  minProcessingTime: number;
  maxProcessingTime: number;
  totalAudioDuration: number;
  totalProcessingTime: number;
  throughput: number;
  avgTextLength: number;
}

// 事件相关类型
type TaskEvent =
  | TaskSubmittedEvent
  | TaskPickedEvent
  | TaskStartedEvent
  | TaskCompletedEvent
  | TaskFailedEvent
  | TaskRetryEvent;

interface TaskSubmittedEvent {
  type: 'TASK_SUBMITTED';
  taskId: string;
  batchId: string;
  payload: TaskPayload;
  timestamp: number;
}

interface TaskPickedEvent {
  type: 'TASK_PICKED';
  taskId: string;
  nodeId: string;
  timestamp: number;
}

interface TaskStartedEvent {
  type: 'TASK_STARTED';
  taskId: string;
  nodeId: string;
  timestamp: number;
}

interface TaskCompletedEvent {
  type: 'TASK_COMPLETED';
  taskId: string;
  batchId: string;
  result: TaskResult;
  timestamp: number;
}

interface TaskFailedEvent {
  type: 'TASK_FAILED';
  taskId: string;
  nodeId: string;
  error: string;
  retryCount: number;
  timestamp: number;
}

interface TaskRetryEvent {
  type: 'TASK_RETRY';
  taskId: string;
  attempt: number;
  timestamp: number;
}
```

---

## 9. 接口设计

### 9.1 Skill 对外接口

```typescript
// 批量提交
interface SubmitBatchRequest {
  audioFiles: string[];
  options?: {
    priority?: number;
    language?: string;
    hotwords?: string[];
    outputFormat?: 'json' | 'text' | 'srt';
    maxRetries?: number;
    timeoutMs?: number;
    dispatchMode?: 'push' | 'pull' | 'hybrid' | 'auto';
    callbackUrl?: string;
  };
}

interface SubmitBatchResponse {
  batchId: string;
  totalTasks: number;
  status: 'created' | 'queued';
  dispatchMode: DispatchMode;
  estimatedTimeMs?: number;
  taskIds: string[];
}

// 查询批次
interface QueryBatchRequest {
  batchId: string;
  includeResults?: boolean;
}

interface QueryBatchResponse {
  batchId: string;
  status: BatchStatus;
  dispatchMode: DispatchMode;
  progress: {
    total: number;
    completed: number;
    failed: number;
    pending: number;
    running: number;
  };
  results?: Array<{taskId: string; status: TaskStatus; result?: TaskResult; error?: string}>;
  metrics?: QualityMetrics;
}

// 查询任务
interface QueryTaskRequest { taskId: string; }
interface QueryTaskResponse {
  taskId: string;
  batchId: string;
  status: TaskStatus;
  dispatchMode?: DispatchMode;
  result?: TaskResult;
  error?: string;
  retryCount: number;
  events?: TaskEvent[];
  processingTime?: number;
}

// 取消批次
interface CancelBatchRequest { batchId: string; reason?: string; }
interface CancelBatchResponse { batchId: string; cancelled: boolean; cancelledTasks: number; message: string; }

// 获取结果
interface GetResultsRequest {
  batchId: string;
  format?: 'json' | 'csv' | 'txt';
  includeTimestamps?: boolean;
}

interface GetResultsResponse {
  batchId: string;
  format: string;
  totalResults: number;
  content: string;
  metrics: QualityMetrics;
}

// 节点管理
interface RegisterNodeRequest {
  host: string;
  port: number;
  weight?: number;
  maxConnections?: number;
  ssl?: boolean;
  capabilities?: Partial<NodeCapability>;
}

interface RegisterNodeResponse { nodeId: string; registered: boolean; message: string; }
interface ListNodesResponse {
  nodes: Array<{
    id: string;
    host: string;
    port: number;
    status: NodeState;
    currentConnections: number;
    lastHeartbeat: number;
    circuitState?: CircuitState;
    avgResponseTime: number;
  }>;
}

// 系统统计
interface GetStatsResponse {
  queues: { pending: number; running: number; completed: number; failed: number; provider: string; };
  nodes: { total: number; online: number; offline: number; busy: number; healthCheckMode: string; };
  scheduler: { mode: DispatchMode; currentLoad: number; maxLoad: number; uptime: number; };
  throughput: { tasksPerMinute: number; avgProcessingTime: number; successRate: number; };
}
```

### 9.2 内部模块接口

```typescript
interface ITaskQueue {
  submit(task: Omit<Task, 'id' | 'createdAt' | 'updatedAt'>): Promise<Task>;
  submitBatch(tasks: Omit<Task, 'id' | 'createdAt' | 'updatedAt'>[]): Promise<Task[]>;
  dequeue(workerId?: string): Promise<Task | null>;
  peek(): Promise<Task | null>;
  updateStatus(taskId: string, status: TaskStatus, result?: TaskResult): Promise<void>;
  cancel(taskId: string): Promise<boolean>;
  cancelBatch(batchId: string): Promise<number>;
  getTask(taskId: string): Promise<Task | null>;
  getBatchTasks(batchId: string): Promise<Task[]>;
  getStats(): Promise<QueueStats>;
  subscribe(callback: (event: QueueEvent) => void): () => void;
  pause(): Promise<void>;
  resume(): Promise<void>;
}

interface INodeRegistry {
  register(node: NodeInfo): void;
  unregister(nodeId: string): boolean;
  getNode(nodeId: string): NodeInfo | null;
  getAllNodes(): NodeInfo[];
  getAvailableNodes(): NodeInfo[];
  updateStatus(nodeId: string, status: Partial<NodeStatus>): void;
  incrementConnections(nodeId: string): void;
  decrementConnections(nodeId: string): void;
  getNodeStatus(nodeId: string): NodeStatus | null;
  getConnectionCount(nodeId: string): number;
  getCircuitBreaker(nodeId: string): CircuitBreaker | null;
  recordSuccess(nodeId: string, responseTime: number): void;
  recordFailure(nodeId: string): void;
  getStats(): NodeStats;
  startHealthCheck(): void;
  stopHealthCheck(): void;
  on(event: 'nodeUp' | 'nodeDown' | 'nodeRegistered', listener: (node: NodeInfo) => void): void;
}

interface ITaskScheduler {
  start(): Promise<void>;
  stop(): Promise<void>;
  pause(): Promise<void>;
  resume(): Promise<void>;
  isRunning(): boolean;
  getCurrentLoad(): number;
  getMaxLoad(): number;
  setMode(mode: DispatchMode): Promise<void>;
  getMode(): DispatchMode;
  on(event: 'taskComplete' | 'taskFailed', listener: (taskId: string) => void): void;
}

interface ILoadBalancer {
  selectNode(): NodeInfo | null;
  registerStrategy(name: string, strategy: LoadBalanceStrategy): void;
  setStrategy(name: string): void;
  getCurrentStrategy(): string;
  getAvailableStrategies(): Array<{name: string, description: string}>;
  updateNodeStatus(nodeId: string, status: NodeStatus): void;
}
```

---

## 10. Skill 封装设计

### 10.1 Skill 结构

```
skills/funasr-batch-adapter/
├── SKILL.md
├── package.json
├── tsconfig.json
├── src/
│   ├── index.ts
│   ├── types/
│   │   ├── index.ts
│   │   ├── node.ts
│   │   ├── task.ts
│   │   ├── events.ts
│   │   ├── config.ts
│   │   └── api.ts
│   ├── core/
│   │   ├── NodeRegistry.ts
│   │   ├── TaskQueue.ts
│   │   ├── Scheduler.ts
│   │   ├── LoadBalancer.ts
│   │   ├── ResultRecorder.ts
│   │   ├── ErrorHandler.ts
│   │   └── MetricsCollector.ts
│   ├── queue/
│   │   ├── MemoryTaskQueue.ts
│   │   ├── RedisTaskQueue.ts
│   │   ├── QueueFactory.ts
│   │   └── QueueMigrator.ts
│   ├── scheduler/
│   │   ├── DualModeScheduler.ts
│   │   ├── PushScheduler.ts
│   │   ├── PullScheduler.ts
│   │   └── ModeSelector.ts
│   ├── strategies/
│   │   ├── RoundRobin.ts
│   │   ├── WeightedRoundRobin.ts
│   │   ├── LeastConnections.ts
│   │   ├── ResponseTime.ts
│   │   ├── CapacityAware.ts
│   │   └── ConsistentHash.ts
│   ├── health/
│   │   ├── HeartbeatChecker.ts
│   │   └── GossipProtocol.ts
│   ├── events/
│   │   ├── EventStore.ts
│   │   └── RedisEventStore.ts
│   ├── fault/
│   │   ├── CircuitBreaker.ts
│   │   ├── ExponentialBackoff.ts
│   │   └── IdempotencyGuard.ts
│   ├── client/
│   │   └── FunASRClient.ts
│   ├── skill/
│   │   ├── index.ts
│   │   ├── handler.ts
│   │   └── config.ts
│   └── utils/
│       ├── logger.ts
│       └── helpers.ts
├── config/
│   ├── default.json
│   ├── advanced.json
│   └── production.json
├── monitoring/
│   ├── metrics.json
│   └── dashboards/
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

### 10.2 Skill 入口

```typescript
export class FunASRBatchAdapter {
  private nodeRegistry: UnifiedNodeRegistry;
  private taskQueue: ITaskQueue;
  private scheduler: DualModeScheduler;
  private loadBalancer: LoadBalancer;
  private resultRecorder: ResultRecorder;
  private errorHandler: ErrorHandler;
  private metricsCollector: MetricsCollector;
  private eventStore?: RedisEventStore;
  private handler: SkillHandler;
  private initialized: boolean = false;
  private startTime: number = 0;
  
  async initialize(): Promise<void> {
    if (this.initialized) return;
    
    console.log('[FunASR Batch Adapter] Initializing...');
    
    const config = loadConfig(this.configPath);
    this.metricsCollector = new MetricsCollector(config.monitoring);
    
    if (config.faultTolerance.eventSourcing?.enabled && config.queue.provider === QueueProvider.REDIS) {
      this.eventStore = new RedisEventStore(config.queue.redis!.url);
    }
    
    this.nodeRegistry = new UnifiedNodeRegistry(config.loadBalance);
    
    for (const node of config.nodes) {
      this.nodeRegistry.register(node);
    }
    
    this.taskQueue = QueueFactory.create(config.queue.provider, config.queue);
    this.loadBalancer = new LoadBalancer(this.nodeRegistry);
    this.loadBalancer.setStrategy(config.loadBalance.strategy);
    
    this.errorHandler = new ErrorHandler(config.faultTolerance);
    this.resultRecorder = new ResultRecorder(config.recorder);
    
    this.scheduler = new DualModeScheduler(
      this.taskQueue, this.nodeRegistry, this.loadBalancer, config.scheduler
    );
    
    this.handler = new SkillHandler(
      this.taskQueue, this.scheduler, this.resultRecorder,
      this.nodeRegistry, this.eventStore, this.metricsCollector
    );
    
    this.nodeRegistry.startHealthCheck();
    await this.scheduler.start();
    
    this.startTime = Date.now();
    this.initialized = true;
    
    console.log('[FunASR Batch Adapter] Initialized successfully');
  }
  
  async handleToolCall(toolName: string, params: any): Promise<any> {
    if (!this.initialized) await this.initialize();
    
    const startTime = Date.now();
    try {
      const result = await this.handler.handle(toolName, params);
      this.metricsCollector?.recordApiCall(toolName, Date.now() - startTime, true);
      return result;
    } catch (error) {
      this.metricsCollector?.recordApiCall(toolName, Date.now() - startTime, false);
      throw error;
    }
  }
}

let instance: FunASRBatchAdapter | null = null;

export function getAdapter(configPath?: string): FunASRBatchAdapter {
  if (!instance) instance = new FunASRBatchAdapter(configPath);
  return instance;
}

export async function handleToolCall(toolName: string, params: any): Promise<any> {
  return getAdapter().handleToolCall(toolName, params);
}
```

---

## 11. 配置管理

### 11.1 配置文件

```json
{
  "nodes": [
    {
      "id": "node-1",
      "host": "100.116.250.20",
      "port": 10095,
      "weight": 2,
      "maxConnections": 10,
      "ssl": true,
      "capabilities": {
        "maxAudioDuration": 3600,
        "supportedFormats": ["wav", "mp3", "m4a", "ogg"],
        "supportsHotwords": true,
        "supportsTimestamps": true,
        "tps": 10
      }
    }
  ],
  
  "queue": {
    "provider": "memory",
    "memory": {
      "maxSize": 10000,
      "persistence": {
        "enabled": false,
        "path": "./data/queue",
        "intervalMs": 60000
      }
    },
    "redis": {
      "url": "redis://localhost:6379",
      "consumerGroup": "funasr-workers",
      "taskKeyPrefix": "funasr:task:",
      "streams": {
        "tasks": "funasr:tasks",
        "events": "funasr:events"
      }
    }
  },
  
  "scheduler": {
    "mode": "auto",
    "maxConcurrentTasks": 20,
    "pollIntervalMs": 100,
    "enableBatchDispatch": true,
    "batchSize": 5,
    "hybridThreshold": {
      "pushMaxTasks": 10,
      "pullMinTasks": 100
    }
  },
  
  "loadBalance": {
    "strategy": "round-robin",
    "healthCheck": {
      "mode": "heartbeat",
      "intervalMs": 30000,
      "timeoutMs": 10000
    },
    "gossip": {
      "port": 7946,
      "peers": [],
      "syncIntervalMs": 1000
    }
  },
  
  "faultTolerance": {
    "circuitBreaker": {
      "enabled": true,
      "failureThreshold": 5,
      "resetTimeoutMs": 60000,
      "halfOpenMaxCalls": 3
    },
    "retry": {
      "maxRetries": 3,
      "baseDelayMs": 1000,
      "maxDelayMs": 60000,
      "backoffMultiplier": 2,
      "jitter": true
    },
    "idempotency": {
      "enabled": false,
      "keyPrefix": "funasr:idempotent",
      "lockTimeoutMs": 300000,
      "resultTtlSeconds": 3600
    },
    "eventSourcing": {
      "enabled": false,
      "snapshotInterval": 100
    }
  },
  
  "recorder": {
    "storageType": "memory",
    "storagePath": "./data/results",
    "retentionDays": 30
  },
  
  "monitoring": {
    "enabled": true,
    "metricsPort": 9090,
    "logLevel": "info"
  }
}
```

---

## 12. 监控与可观测性

### 12.1 指标定义

```typescript
interface AdapterMetrics {
  // 任务指标
  tasksSubmitted: Counter;
  tasksCompleted: Counter;
  tasksFailed: Counter;
  taskDuration: Histogram;
  
  // 队列指标
  queuePending: Gauge;
  queueRunning: Gauge;
  queueCompleted: Gauge;
  
  // 节点指标
  nodeOnline: Gauge;
  nodeBusy: Gauge;
  nodeResponseTime: Histogram;
  
  // 调度指标
  schedulerLoad: Gauge;
  dispatchMode: Gauge;
  
  // API指标
  apiCallsTotal: Counter;
  apiCallDuration: Histogram;
}

class MetricsCollector {
  private metrics: AdapterMetrics;
  
  constructor(config?: MonitoringConfig) {
    this.metrics = this.initMetrics(config);
  }
  
  recordApiCall(toolName: string, durationMs: number, success: boolean): void {
    this.metrics.apiCallsTotal.inc({ toolName, success: success.toString() });
    this.metrics.apiCallDuration.observe({ toolName }, durationMs);
  }
  
  recordTaskComplete(durationMs: number): void {
    this.metrics.tasksCompleted.inc();
    this.metrics.taskDuration.observe(durationMs);
  }
  
  recordTaskFail(): void {
    this.metrics.tasksFailed.inc();
  }
  
  getThroughputStats(): { tasksPerMinute: number; avgProcessingTime: number; successRate: number } {
    return {
      tasksPerMinute: 0,
      avgProcessingTime: 0,
      successRate: 0
    };
  }
}
```

### 12.2 健康检查

```typescript
interface HealthCheckResult {
  status: 'healthy' | 'degraded' | 'unhealthy';
  checks: {
    queue: { status: string; message?: string };
    nodes: { status: string; online: number; total: number };
    scheduler: { status: string; load: number };
  };
}

class HealthChecker {
  async check(): Promise<HealthCheckResult> {
    const queue = await this.checkQueue();
    const nodes = this.checkNodes();
    const scheduler = this.checkScheduler();
    
    const allHealthy = [queue, nodes, scheduler].every(c => c.status === 'ok');
    const anyDegraded = [queue, nodes, scheduler].some(c => c.status === 'degraded');
    
    return {
      status: allHealthy ? 'healthy' : anyDegraded ? 'degraded' : 'unhealthy',
      checks: { queue, nodes, scheduler }
    };
  }
  
  private checkQueue(): any {
    return { status: 'ok' };
  }
  
  private checkNodes(): any {
    const stats = this.nodeRegistry.getStats();
    return {
      status: stats.online > 0 ? 'ok' : 'error',
      online: stats.online,
      total: stats.total
    };
  }
  
  private checkScheduler(): any {
    const load = this.scheduler.getCurrentLoad();
    const max = this.scheduler.getMaxLoad();
    return {
      status: load < max * 0.9 ? 'ok' : 'degraded',
      load,
      max
    };
  }
}
```

---

## 13. 测试策略

### 13.1 测试金字塔

```
        /\
       /  \
      / E2E \         端到端测试 (5%)
     /─────────\
    /  Integration \   集成测试 (25%)
   /─────────────────\
  /     Unit Tests     \  单元测试 (70%)
 /─────────────────────────\
```

### 13.2 测试计划

| 级别 | 覆盖模块 | 测试数量预估 |
|------|----------|-------------|
| 单元测试 | 队列、调度器、负载均衡、断路器 | 100+ |
| 集成测试 | 完整流程、模式切换、故障转移 | 30+ |
| E2E测试 | 实际批量转写、压力测试 | 10+ |

---

## 14. 部署指南

### 14.1 环境要求

- **Node.js**: >= 18.0.0
- **Redis**: >= 6.0 (如使用Redis队列)
- **FunASR节点**: 已部署并运行

### 14.2 部署步骤

1. 安装依赖: `npm install`
2. 配置: 编辑 `config/default.json`
3. 构建: `npm run build`
4. 运行: `npm start`

### 14.3 Docker 部署

```dockerfile
FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install --production
COPY dist/ ./dist/
COPY config/ ./config/
EXPOSE 9090
CMD ["node", "dist/index.js"]
```

---

## 15. 实施计划

### 15.1 开发阶段

| 阶段 | 时间 | 交付物 |
|------|------|--------|
| Phase 1: 基础设施 | Week 1 | 可插拔队列 |
| Phase 2: 核心调度 | Week 2 | Push/Pull调度器 |
| Phase 3: 容错机制 | Week 3 | 断路器+幂等+事件溯源 |
| Phase 4: Skill封装 | Week 4 | 完整Skill+文档 |

### 15.2 里程碑

| 里程碑 | 时间 | 验收标准 |
|--------|------|----------|
| M1 | Week 1 结束 | 队列系统可用 |
| M2 | Week 2 结束 | 双模式调度可用 |
| M3 | Week 3 结束 | 容错机制可用 |
| M4 | Week 4 结束 | 完整交付 |

---

## 16. 风险与对策

### 16.1 技术风险

| 风险 | 概率 | 影响 | 对策 |
|------|------|------|------|
| Redis引入复杂性 | 中 | 中 | 内存队列默认，Redis可选 |
| 双模式切换复杂 | 中 | 中 | 提供手动模式，auto为可选 |
| Gossip协议复杂 | 中 | 中 | 心跳检测默认，Gossip可选 |
| 事件存储膨胀 | 中 | 中 | 定期快照+清理，默认关闭 |
| 配置复杂性增加 | 高 | 低 | 提供预设模板 |

### 16.2 应急预案

```typescript
// 队列故障自动回退
async function emergencyFallback(): Promise<void> {
  logger.error('Queue failed, falling back to memory queue');
  const memoryQueue = QueueFactory.create(QueueProvider.MEMORY, {});
  await adapter.switchQueue(memoryQueue);
}

// 模式切换失败保持当前模式
async function safeModeSwitch(newMode: DispatchMode): Promise<void> {
  const currentMode = scheduler.getMode();
  try {
    await scheduler.setMode(newMode);
  } catch (error) {
    logger.error(`Failed to switch to ${newMode}, staying in ${currentMode}`);
  }
}
```

---

## 附录

### A. 术语表

| 术语 | 说明 |
|------|------|
| **FunASR** | 阿里达摩院开源的语音识别工具包 |
| **Batch** | 批次，一组相关的任务集合 |
| **Task** | 任务，单个转写请求 |
| **Node** | 节点，运行FunASR服务的服务器 |
| **Push Mode** | 推送模式，调度器主动分发任务 |
| **Pull Mode** | 拉取模式，节点自主拉取任务 |
| **Hybrid Mode** | 混合模式，智能选择Push/Pull |
| **Scheduler** | 调度器，负责任务分发 |
| **Load Balancer** | 负载均衡器，负责选择执行节点 |
| **Circuit Breaker** | 断路器，防止故障扩散的保护机制 |
| **Idempotency** | 幂等性，同一操作多次执行结果相同 |
| **Event Sourcing** | 事件溯源，通过事件重放重建状态 |

### B. 方案对比总结

| 特性 | 方案A (Kimi) | 方案B (MiniMax) | **方案C (整合)** |
|------|-------------|-----------------|-----------------|
| **架构** | 集中式 | 分布式 | **分层混合** |
| **队列** | 内存 | Redis Streams | **可插拔** |
| **分发** | Push | Pull | **双模式** |
| **状态** | 中心状态 | 事件溯源 | **混合** |
| **协调** | 心跳 | Gossip | **可配置** |
| **容错** | 断路器 | 幂等重放 | **双重保障** |
| **复杂度** | 低 | 高 | **渐进式** |
| **适用规模** | 中小 | 大 | **全规模** |

### C. 版本历史

| 版本 | 日期 | 修改内容 |
|------|------|----------|
| v1.0-final | 2026-02-16 | 审核优化版，整合方案A和方案B |

---

**文档结束**
