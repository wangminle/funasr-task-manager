# 结果解读与汇报模板

## 单服务器 Benchmark 结果

```
✅ Benchmark 完成: {server_id}

┌───────────────────────────────────────┐
│ 单线程 RTF     │ {single_rtf}        │
│ 吞吐量 RTF     │ {throughput_rtf}     │
│ 推荐并发数      │ {recommended_concurrency} │
│ 测试样本       │ {sample_file}        │
│ 耗时           │ {elapsed}            │
├───────────────────────────────────────┤
│ 并发梯度详情                           │
│  N=1: tp_rtf={}, wall={}s            │
│  N=2: tp_rtf={}, wall={}s            │
│  N=4: tp_rtf={}, wall={}s ← 推荐     │
│  N=8: ⚠ 退化 (improvement < 10%)     │
├───────────────────────────────────────┤
│ 调度影响                               │
│  rtf_baseline ← {single_rtf}（影响 ETA）│
│  max_concurrency ← {rec_conc}（影响 slot 数）│
│  throughput_rtf ← {tp_rtf}（容量对比用）│
│  get_throughput_speed() = {rec_conc} / {single_rtf}│
│   = {speed}（配额速度）              │
└───────────────────────────────────────┘
```

## Benchmark 失败

```
❌ Benchmark 失败: {server_id}

  错误: {error_message}
  建议: {suggestion}
```

常见失败原因和建议：

| 错误 | 建议 |
|------|------|
| 服务器不可达 | 检查网络连接和 FunASR 服务是否运行 |
| 超时 | 检查服务器负载和网络延迟 |
| 音频文件不存在 | 检查 benchmark 样本文件配置 |
| SSL 握手失败 | 检查证书配置或使用 `--no-ssl` |

## 全量 Benchmark 汇总

```
✅ 全量 Benchmark 完成

服务器: {completed}/{total} 成功

┌─────────────────────────────────────────────────────┐
│ 服务器     │ 单线程 RTF │ 吞吐量 RTF │ 推荐并发 │ 容量占比 │
│────────────┼───────────┼───────────┼─────────┼────────│
│ asr-10095  │ 0.1234    │ 0.0358    │ 4       │ 65%    │
│ asr-10096  │ 0.1876    │ 0.0912    │ 2       │ 35%    │
└─────────────────────────────────────────────────────┘

调度基线已更新。
```

如有失败节点：

```
⚠ 部分服务器 Benchmark 失败:

  ❌ asr-10097: 服务器不可达（已标记为 OFFLINE）
  ✅ asr-10095: 正常 (single_rtf=0.1234)
  ✅ asr-10096: 正常 (single_rtf=0.1876)
```

## 退化检测说明

当某个并发梯度级别的性能提升低于 10% 时，benchmark 会检测到退化并停止更高级别测试：

```
⚠ 退化检测: N={concurrency}

  N={prev}: throughput_rtf = {prev_rtf}
  N={curr}: throughput_rtf = {curr_rtf}
  提升仅 {improvement}%（阈值 10%）

  推荐并发数: {recommended}（退化前的最佳级别）

  可能原因:
  - GPU 显存不足，更高并发导致频繁换入换出
  - CPU 核心已满载
  - 网络带宽瓶颈（尤其 WAN 场景）
  - FunASR 服务内部队列积压

  建议:
  - 使用推荐并发数 {recommended}
  - 如为 WAN 部署，检查网络延迟
  - 检查 GPU 显存利用率（nvidia-smi）
```

## 闲时校准汇总

闲时校准场景使用更简洁的汇总格式：

```
🔄 闲时校准完成

  校准时间: {timestamp}
  服务器数: {count}
  
  结果:
  ├─ asr-10095: rtf_baseline 0.1234 → 0.1198 (↓3%)  ✅ 正常
  ├─ asr-10096: rtf_baseline 0.1876 → 0.2301 (↑23%) ⚠ 性能异常，需运维检查
  └─ asr-10097: 跳过（OFFLINE）

  异常: 1 台服务器性能显著下降，已通知运维。
```

## RTF 偏差超标说明

当闲时校准发现 RTF 偏差 > 20% 时：

```
⚠ 性能异常: {server_id}

  上次基线: rtf_baseline = {old_rtf}
  本次结果: rtf_baseline = {new_rtf}
  偏差: {deviation}%（阈值 20%）

  ⚠ 当前后端不支持通过 API 自动标记 DEGRADED，请运维手动检查。

  可能原因:
  - 服务器硬件故障或资源争抢
  - 模型版本变更
  - 网络条件显著变化
  - Docker 容器资源限制变更

  建议:
  - 检查服务器资源使用情况
  - 确认 FunASR Docker 容器状态
  - 排除干扰后重新 benchmark
```
