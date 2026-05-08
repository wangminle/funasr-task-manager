# 超长音视频隐式分段并行方案（单任务外显）

**摘要**

- 目标是缩短 `20 分钟以上` 大音频/大视频的总完成时间，外部仍保持“一个上传文件 = 一个任务”。
- 首版采用 `ffmpeg silencedetect` 做客户端侧静音切段，不引入额外 VAD 模型；切段和合并都只在后端内部发生。
- 外部 API / UI 继续只展示父任务；内部新增段级 work item、段级调度、段级重试、最终统一合并。
- 服务端能力分析继续沿用现有 `probe/benchmark + rtf_baseline + max_concurrency`，并把 `CPU 绑核 / decoder 线程数` 作为标准化诊断标签纳入服务器元数据。

**实现方案**

- 预处理链路改为：上传文件 → 统一转成 `16kHz 单声道 WAV` → 若时长 `< 1200s` 走原整文件路径 → 若时长 `>= 1200s` 进入切段流程。
- 切段默认参数固定为：`silencedetect=n=-35dB:d=0.8`，目标段长 `480s`，最小段长 `120s`，硬上限 `600s`，切点两侧保留 `400ms` 重叠。
- 切段策略固定为：优先选最近的静音点；若在当前窗口内找不到可用静音点，则允许在 `600s` 处硬切，并保留 `400ms` 重叠兜底。
- 大视频不单独设计一套逻辑，统一先抽音频到 canonical WAV，再复用同一套切段和合并流程。
- 新增内部表 `task_segments`，字段至少包括：`segment_id`、`task_id`、`segment_index`、`source_start_ms`、`source_end_ms`、`keep_start_ms`、`keep_end_ms`、`storage_path`、`status`、`assigned_server_id`、`retry_count`、`raw_result_path`、`error_message`、`created_at`、`started_at`、`completed_at`。
- 父任务状态机不新增外部状态，仍使用现有 `PREPROCESSING/QUEUED/DISPATCHED/TRANSCRIBING/SUCCEEDED/FAILED`；语义改为聚合状态。父任务 `progress` 按“已完成段的有效音频时长 / 总有效音频时长”聚合，并映射回现有进度区间。
- `TaskResponse` 增加可选诊断字段，保持兼容：`internal_split_enabled`、`internal_segments_total`、`internal_segments_completed`、`internal_assigned_server_ids`、`internal_merge_status`。现有消费者可忽略；分段任务的 `assigned_server_id` 保持 `null`，避免误导成单机执行。
- `task_runner` 从“整文件直接 dispatch”改为“父任务创建段清单后，按段调度到服务器”；段级失败独立重试，父任务仅在某段耗尽重试或最终合并失败时转 `FAILED`。
- 调度器改成双层约束：服务器硬容量继续使用 `max_concurrency`；单个父任务的活跃段并行数上限固定为 `min(在线服务器数, 3)`，避免一个超长文件吃满整个集群、拖慢短任务。
- `result_formatter` 扩展为支持段级合并：按 `segment_index` 顺序处理，先用段内时间戳过滤掉落在重叠区之外的句子，再把保留句子的时间戳按 `source_start_ms` 回写到全局时间线上，最终输出统一的 `json/txt/srt`。
- 合并规则固定为：以带时间戳的句级结果为主；若某段只有纯文本无可用时间戳，则仍参与文本拼接，但把该父任务标记为 `internal_merge_status=TEXT_ONLY_FALLBACK`，便于排障。
- 服务器能力分析不新造一套调度字段，继续以 `max_concurrency + rtf_baseline` 为调度真值；把 `cpu_set`、`decoder_threads`、`container_name`、`benchmark_profile` 规范写入现有 `labels_json`，CPU 绑核部署后通过现有 `/api/v1/servers/benchmark` 刷新基线。

**测试与验收**

- 单元测试覆盖：静音检测日志解析、切段规划、硬切兜底、段元数据生成、段级进度聚合、时间戳回写、重叠区去重、段级公平调度。
- 集成测试覆盖：一个父任务拆成多段、三台服务器并发执行、单段失败后重试迁移、全部段完成后合并写回最终结果。
- 回归测试保持原有短文件链路不变，确保 `< 20 分钟` 文件继续走整文件路径，不引入额外时延。
- 新增一条长文件验收基准：同一份 `20+ 分钟` 素材在三台服务器集群上，分段并行的 wall-clock 相比整文件基线至少下降 `25%`；最终文本顺序稳定、无明显重复段、SRT 时间戳单调递增。
- 浏览器 E2E 不改用户交互，只增加对“单任务外显 + 诊断字段可读 + 最终结果可下载”的校验；重型长文件对比测试优先放在后端集成/基准脚本，而不是常规前端 E2E。

**假设与默认值**

- 默认前提是部署环境已经有可用 `ffmpeg`，且大视频允许先抽音再转写。
- 首版不引入 FSMN-VAD，也不把内部子段暴露成用户可见任务。
- 首版允许硬切兜底，但通过 `400ms` 重叠和时间戳过滤降低边界风险；若后续实测边界误差仍明显，再升级到模型级 VAD。
- 服务器 CPU 绑核后的容量变化不靠猜测，统一通过更新 `labels_json` 和重新 benchmark 来校准；调度器不直接解析 Docker 配置。
