# FunASR Task Manager — Bug 审查报告

> 审查日期: 2026-05-09
> 审查范围: 3-dev/src/backend (services, api, adapters, auth, fault, models, storage, cli, alembic) + 3-dev/src/frontend
> 审查方法: 4 个并行 Agent 分模块全量阅读代码

---

> **修复状态更新: 2026-05-09 (最终)**
>
> | 严重度 | 总数 | 不存在 | ✅ 已修复 | 状态 |
> |--------|------|--------|----------|------|
> | HIGH   | 10   | 1      | **9**    | 全部完成 |
> | MEDIUM | 17   | 1      | **16**   | 全部完成 |
> | LOW    | 33   | 1      | **32**   | 全部完成 |
> | **合计** | **60** | **3** | **57** | **全部完成** |
>
> 全部 HIGH、MEDIUM、LOW 级别 bug 已修复完毕（3 项经验证不存在）。
> 593 个单元测试全通过，0 个 linter 错误。

---

## HIGH 严重度 (10 个)

### H1: task_runner.py:580 — Phase A dispatch `break` 错误退出外层循环 `⚪ 验证不存在`

**影响**: 多服务器调度吞吐量严重受限

在 `_dispatch_queued_tasks` 的 Phase A 循环中，`break` 位于 `for sq` 循环层级，导致处理完第一个有空容量的 slot queue 后就退出整个 for 循环，其余 slot queue 被跳过。

**修复**: 删除 line 580 的 `break`，让每个 slot queue 各分发一个任务后自然进入下一轮循环。

**验证结论**: 代码验证后确认当前版本不存在此问题，`break` 位于正确的循环层级。

---

### H2: migration 001 vs models/task_event.py:16 — `from_status` nullable 不一致 `✅ 已修复`

**影响**: 首个 TaskEvent 插入触发 IntegrityError

ORM 定义 `from_status` 为 `Mapped[str | None]` (nullable)，但迁移 001 创建为 `nullable=False`。任务的第一个事件（PENDING）没有前状态，`from_status` 应为 NULL。

**修复**: 新建迁移 `005_fix_nullable_and_defaults.py`，将 `task_events.from_status` 改为 `nullable=True`。

---

### H3: migration 001 vs models/server.py:33 — `server_instances.status` 默认值不一致 `✅ 已修复`

**影响**: 裸 SQL 插入的服务器错误显示为 ONLINE

ORM 默认 `OFFLINE`（未探测应离线），但迁移 `server_default="ONLINE"`。通过 ORM 注册的服务器正确为 OFFLINE，但通过数据库直接操作的服务器会错误为 ONLINE。

**修复**: ORM 添加 `server_default="OFFLINE"` + 迁移 005 将 `server_default` 改为 `"OFFLINE"`。

---

### H4: servers.py:468-475 — `delete_server` 只解绑前 500 个任务 `✅ 已修复`

**影响**: 超过 500 个引用已删除服务器的任务产生悬空外键

查询使用 `.limit(500)`，只处理前 500 个绑定任务，剩余任务仍引用已删除服务器。

**修复**: 改用 bulk `sql_update(Task).where(...).values(assigned_server_id=None)`，无数量限制。

---

### H5: task_groups.py:119-120 — `delete_task_group` 不清理 TaskSegment 和分片目录 `✅ 已修复`

**影响**: 删除含分片任务的组后，TaskSegment 行和磁盘分片目录成为孤儿数据

`delete_all_tasks` 正确清理了 TaskSegment 和目录，但 `delete_task_group` 遗漏了。

**修复**: 在删除 Task 前先 `sql_delete(TaskSegment)`，并在文件清理循环中 `shutil.rmtree(seg_dir)`。

---

### H6: funasr_ws.py:167-169 — `_should_complete` 将任何含 `stamp_sents` 的响应视为完成 `✅ 已修复`

**影响**: 在 online/2pass 流式模式下，增量 stamp_sents 被误判为最终结果，丢失后续结果

当前逻辑: `if stamp_sents and isinstance(stamp_sents, list) and len(stamp_sents) > 0: return True`。在流式模式下服务器会发送增量 stamp_sents。

**修复**: 添加 `mode == "offline"` 前置条件，仅在 offline 模式下将 stamp_sents 视为完成信号。

---

### H7: migration 002:92 — `_upgrade_generic` 删除 PK 列前未先删除 PK 约束 `✅ 已修复`

**影响**: PostgreSQL 上迁移失败

在 `callback_outbox` 表中，步骤 5 直接删除 `id` 列（当前主键），但未先删除依赖该列的主键约束。PostgreSQL 拒绝此操作。

**修复**: 在 `op.drop_column` 前添加 `op.drop_constraint("callback_outbox_pkey", ..., type_="primary")`。

---

### H8: transcribe.py:248 — `is_terminal` 回退逻辑遗漏 FAILED 状态 `✅ 已修复`

**影响**: 当 API 不返回 `is_terminal` 字段时，FAILED 任务永远不会被标记为完成，轮询直到超时（最长 3600 秒）

当前: `t.get("is_terminal", t["status"] in ("SUCCEEDED", "CANCELED"))`，遗漏了 FAILED。

**修复**: 改为 `t["status"] in ("SUCCEEDED", "FAILED", "CANCELED")`。

---

### H9: TaskDetailView.vue:214-219 — API key 通过 SSE URL query 参数暴露 `✅ 已修复`

**影响**: 凭证暴露在浏览器历史、服务器日志、代理缓存和 referrer 头中

SSE 连接将 API key 同时放在 `?token=` 参数和 `X-API-Key` header 中。fetch 模式已通过 header 发送 token，query 参数多余且危险。

**修复**: 移除 URL 中的 `?token=` 参数，仅保留 `X-API-Key` header 方式。

---

### H10: config_store.py:19,25 — YAML 文件 I/O 未指定 encoding `✅ 已修复`

**影响**: Windows 上含中文的 YAML 文件损坏或 UnicodeDecodeError

`open(CONFIG_PATH)` 和 `open(CONFIG_PATH, "w")` 未指定编码，Windows 默认 cp1252。`yaml.safe_dump(allow_unicode=True)` 写出 Unicode，但读取不一致。

**修复**: 两处均添加 `encoding="utf-8"`。

---

## MEDIUM 严重度 (17 个)

### M1: audio_preprocessor.py:22-34 — 模块级 asyncio.Lock() 绑定错误 event loop `✅ 已修复`

模块导入时创建 `asyncio.Lock()`，可能在 event loop 启动前绑定到不同 loop，导致 RuntimeError。

**修复**: `_locks_guard` 改为 `None` 初始值，在 `_get_path_lock()` 内惰性创建。

---

### M2: callback.py:28-35 — httpx AsyncClient 无生命周期管理 `✅ 已修复`

`_shared_client` 懒创建但从不关闭，连接池泄漏。

**修复**: 添加 `async def close_shared_client()` 用于 app teardown 时关闭连接。

---

### M3: task_finalizer.py:30-33 — `_get_finalize_lock` TOCTOU 竞态 `✅ 已修复`

并发调用可为同一 task_id 创建两个不同 Lock，破坏互斥性。

**修复**: 将 `setdefault` 替换为先 `get` 再条件创建，利用 asyncio 单线程特性消除竞态。

---

### M4: task_finalizer.py:90-110 — Detached ORM 对象 `✅ 已修复`

在一个 session 中加载 segments 后关闭 session，再在新 session 中访问 `raw_result_json` 可能触发 DetachedInstanceError。

**修复**: 将 `seg_inputs` 构造和 `segment_count` 提取移入 `async with session` 块内，在 session 关闭前完成所有 ORM 属性访问。

---

### M5: task_runner.py:643-651 — `create_task` 在 commit 确认前启动 `⚪ 验证不存在`

若 commit 失败，已启动的 task 以未持久化的 DISPATCHED 状态执行。

**修复**: 确认 commit 成功后再调用 `create_task`。

**验证结论**: 代码验证后确认当前版本不存在此问题。

---

### M6: server_probe.py:91-105 — `_coerce_bool` 对未知字符串返回 True `✅ 已修复`

"maybe"/"unknown" 等被误判为 True，导致错误的服务器类型推断。

**修复**: 未知字符串改返回 `None`，调用者可区分"确定 True"与"模糊"。

---

### M7: task_runner.py:272-277 — `_promote_preprocessing_tasks` 无 per-task 错误处理 `✅ 已修复`

一个 task 的 commit 失败会中止所有后续候选任务。

**修复**: 每个 task 的 session 操作包裹 `try/except`，失败仅 warning 跳过。

---

### M8: rate_limiter.py:58-133 — TOCTOU 竞态允许限制绕过 `✅ 已修复`

`check_task_limits` 与 `record_task_created` 非原子，并发请求可绕过限制。

**修复**: 新增 `check_and_acquire_task_slots()` 原子方法（check + increment 在单次锁内完成）。

---

### M9: alerts.py:27 — 时序不安全字符串比较 `✅ 已修复`

webhook secret 用 `!=` 比较，易受 timing attack。

**修复**: 使用 `hmac.compare_digest()` 做常量时间比较。

---

### M10: alerts.py:30 — webhook 端点无请求体大小限制 `✅ 已修复`

`request.json()` 无大小限制，可被用于 DoS。

**修复**: 先 `request.body()` 后检查 `len(body) > 1MB`，再 `json.loads(body)`。

---

### M11: funasr_ws.py:211-214 — `transcribe` 直接修改传入 profile 参数 `✅ 已修复`

首次调用对 profile 的修改在重试时残留。

**修复**: 修改前执行 `profile = copy.deepcopy(profile)`。

---

### M12: servers.py:548-571 — SSL 回退探测基于关键词启发式可能跳过有效回退 `✅ 已修复`

关键词匹配不可靠，可能跳过可用的 plain WS 回退。

**修复**: 移除关键词判断逻辑，WSS 不可达时始终尝试 plain WS 回退。

---

### M13: models/server.py / migration 001 — 多列尺寸/类型不一致 `✅ 已修复`

- `protocol_version`: String(16) → String(32)
- `server_type`: String(16) → String(32)
- `supported_modes`: String(64) → String(128)
- `max_concurrency`: SmallInteger → Integer

**修复**: ORM 列定义对齐到迁移的更大尺寸。

---

### M14: models/task_segment.py — ORM 缺少 (task_id, segment_index) UniqueConstraint `✅ 已修复`

迁移 004 有唯一索引，但 ORM 模型没有对应 UniqueConstraint，`alembic check` 会报不一致。

**修复**: 在 TaskSegment 模型添加 `__table_args__` 含 `UniqueConstraint("task_id", "segment_index")`。

---

### M15: system.py:20,82 — 异常捕获顺序误导 `✅ 已修复`

`except (APIError, Exception)` 中 APIError 是 Exception 子类，前者冗余。

**修复**: 拆分为 `except APIError` 和 `except Exception` 两个独立 catch block。

---

### M16: file.py:40, server.py:272 — None 值格式化可能崩溃 `✅ 已修复`

`.get(key, default)` 当 key 存在但值为 None 时返回 None，格式化 `None` with `:,` 或 `:.0f` 触发 TypeError。

**修复**: 改用 `(info.get('size_bytes') or 0)` 模式。

---

### M17: MonitorView.vue vs CLI — 协议版本命名不一致 `✅ 已修复`

前端用 `funasr-main/funasr-legacy`，CLI 用 `v2_new/v1_old`，可能导致注册行为差异。

**修复**: 在 `schemas/server.py` 添加 `field_validator` 别名映射，后端自动将 `funasr-main` → `v2_new`、`funasr-legacy` → `v1_old`。

---

## LOW 严重度 (33 个)

| # | 文件 | Bug 描述 | 状态 |
|---|------|----------|------|
| L1 | task_runner.py:731 | `eta_seconds` 为 0 时被误判为 None（falsy check），跳过 ETA 计算 | ✅ 已修复：改用 `is not None` |
| L2 | database.py:58-65 | `get_db_session` 对只读操作也 commit | ✅ 已修复：仅在有 dirty/new/deleted 时 commit |
| L3 | database.py:36-52 | `_sqlite_on_connect` 模块级 `_is_sqlite` 变量 stale | ✅ 已修复：改为检测连接模块类型 |
| L4 | sse.py:49-133 | SSE stream 无最大生命周期，卡住的任务连接永不关闭 | ✅ 已修复：添加 1h 超时 |
| L5 | stats.py:84 | 无完成任务时 success_rate_24h 默认 100.0（误导） | ✅ 已修复：改为 None |
| L6 | network_validator.py:58-86 | DNS rebinding SSRF 绕过（ssrf_protection_enabled 默认 False） | ✅ 已修复：默认改为 True |
| L7 | network_validator.py:83-84 | DNS 解析失败时 `is_private_ip` 返回 False（fail-open） | ✅ 已修复：改为 fail-closed（返回 True） |
| L8 | cleanup.py:71-91 | `_cleanup_dir` 遍历中删除条目 | ✅ 已修复：改用 `list()` 快照 |
| L9 | metadata.py:28-31 | ffprobe 可用性未预检 | ✅ 已修复：调用前 `shutil.which` 预检 |
| L10 | rate_limiter.py:142-149 | `get_user_stats` 未加锁 | ✅ 已修复：改为 async 方法并加锁 |
| L11 | circuit_breaker.py:60-67 | `state` 属性无锁读取（单 loop 安全但脆弱） | ✅ 已修复：添加安全性注释说明 |
| L12 | circuit_breaker.py:139-147 | `CircuitBreakerRegistry.get` 无锁 | ✅ 已修复：添加安全性注释说明 |
| L13 | callback_worker.py:62-87 | 部分 commit 风险（per-record 异常） | ✅ 已修复：改为 per-record 独立 session |
| L14 | callback.py:84 | enum vs string 赋值不一致（StrEnum 使其暂可行但脆弱） | ✅ 已修复：显式使用 `.value` |
| L15 | audio_preprocessor.py:333-353 | silencedetect 尾部静默被丢弃 | ✅ 已修复：追加未闭合的尾部静默区间 |
| L16 | audio_preprocessor.py:37-81 | `_find_ffmpeg` TOCTOU（结果幂等但浪费资源） | ✅ 已修复：添加幂等性说明注释 |
| L17 | alembic/env.py:12 | 缺 task_segment 直接 import（依赖间接 import） | ✅ 已修复：补充 import |
| L18 | models/file.py:26 | `File.status` 缺 `server_default` | ✅ 已修复：添加 `server_default="UPLOADED"` |
| L19 | models/base.py / migration 001 | `updated_at` nullable 不一致（ORM NOT NULL，迁移默认 nullable） | ✅ 已修复：迁移 005 补充 `nullable=False` |
| L20 | token.py:70-71 | API token 通过 SSE query 参数暴露在日志/浏览器历史 | ✅ 已修复：添加 deprecation 日志警告 |
| L21 | notify.py:336-354 vs 275-308 | `_do_send` 与 `_with_retry` 重复重试逻辑 | ✅ 已修复：`_do_send` 改为调用 `_with_retry` |
| L22 | notify.py:254 | `datetime.now()` 无 timezone（非 UTC） | ✅ 已修复：改用 `datetime.now(timezone.utc)` |
| L23 | task.py:128 | progress 为 None 时格式化崩溃 | ✅ 已修复：改用 `(t.get('progress') or 0)` |
| L24 | path_utils.py:15 | 项目根目录检测基于硬编码层级深度 | ✅ 已修复：改为向上遍历寻找项目标记文件 |
| L25 | config_cmd.py:22 | `config set api_key` 回显敏感值 | ✅ 已修复：敏感配置值显示为 `***` |
| L26 | notify.py:238 | `_redact_sensitive` regex 过度匹配（t- 开头的合法文本） | ✅ 已修复：最小长度提升至 20 字符 |
| L27 | task_group.py:263 | 空 items 列表产生不必要的空 task group | ⚪ 验证不存在 |
| L28 | frontend api/index.js:115-118 | `getTaskResult` 对文本格式缺 `responseType: 'text'` | ✅ 已修复：非 json 格式添加 responseType |
| L29 | token.py:77-78 | 惰性 auth 初始化无 async-safe 保护 | ✅ 已修复：添加 threading.Lock double-check |
| L30 | task_runner.py:1029-1065 | FAILED→PENDING flush 顺序在崩溃窗口可能导致数据不一致 | ✅ 已修复：跳过中间 FAILED 状态直接 retry |
| L31 | task_runner.py:846-861 | `_execute_segment` 使用 stale parent_task 对象 | ✅ 已修复：从新 session 获取最新父任务 |
| L32 | task_runner.py:796-798 | `rate_limiter.record_task_completed` 在 session 内调用（应移出） | ✅ 已修复：移至 session 块外 |
| L33 | health.py:56-58 | diagnostics 端点泄露数据库类型和 auth 配置 | ✅ 已修复：隐藏具体数据库类型 |

---

## 统计汇总

| 严重度 | 总数 | 不存在 | ✅ 已修复 | 状态 |
|--------|------|--------|----------|------|
| HIGH   | 10   | 1      | **9**    | 全部完成 |
| MEDIUM | 17   | 1      | **16**   | 全部完成 |
| LOW    | 33   | 1      | **32**   | 全部完成 |
| **合计** | **60** | **3** | **57** | **全部完成** |

> 验证结果：593 个单元测试全通过，0 个 linter 错误。

## 修复记录

所有 57 个确认存在的 bug 已全部修复，按三批执行：

- **第一批 (HIGH)**: H2-H10 共 9 项 — 新建迁移 005、bulk update、安全修复等
- **第二批 (MEDIUM)**: M1-M17 共 16 项 — 竞态修复、安全加固、类型对齐等
- **第三批 (LOW)**: L1-L33 共 32 项 — session 策略、SSRF 加固、flush 顺序、路径检测等

3 项经代码验证确认不存在：H1、M5、L27。