# 5+1 ASR 校对 Skill 全流程测试复盘报告

> 日期：2026-04-30
> 测试范围：asr-llm-refine-5plus1 Skill 初始化 → 转写 → 子 Agent 校对全流程
> 参与模型：
> - 模型 A（候选生成）：deepseek/deepseek-v4-flash
> - 模型 B（评分选优）：deepseek/deepseek-v4-pro

---

## 一、时间线总览

| 时间 | 事件 | 结果 |
|------|------|------|
| 18:07 | 用户发起："执行一下这个skill的初始化 asr-llm-refine-5plus1" | 开始 |
| 18:08 | 模型发现脚本执行，保存 .model_cache.json | ✅ 23 个模型 |
| 18:09 | 用户提出实验方案：两个视频 → 转写 → 校对 | 确认流程 |
| 18:12 | 收到两个视频文件（第4集 83MB、第11集 68MB） | ✅ |
| 18:18 | 尝试通过 drive API 下载飞书文件 | ❌ 权限不足 |
| 18:20 | 改用 im/v1/messages API 下载 | ✅ 两个文件下载成功 |
| 18:20 | 上传到 ASR 后端并提交转写任务 | ✅ 2 个任务 |
| 18:26 | 转写完成，保存 transcript_1.txt / transcript_2.txt | ✅ |
| 18:26 | 用户指定模型 A/B：deepseek-v4-flash / deepseek-v4-pro | 确认 |
| 18:27 | 检查 DeepSeek API Key 配置 | 发现问题 |
| 18:30 | 尝试内联长文本启动子 Agent | ❌ Gateway 超时 |
| 18:32 | Gateway 重启 | 中断 |
| 18:39 | 改用文件路径传递方案，重新启动 5 个子 Agent | ✅ |
| 18:45 | 第4集 5 份候选全部生成 | ✅ |
| 18:45 | 启动模型 B 评分（deepseek-v4-pro） | ✅ |
| 18:54 | 第4集评分完成，候选1获胜（8.8分） | ✅ |
| 18:55 | 启动第11集 5 个子 Agent | ✅ |
| 19:00 | 第11集 5 份候选全部生成 | ✅ |
| 19:00 | 启动模型 B 评分 | ✅ |
| 19:06 | 第11集评分完成，候选2获胜（8.3分） | ✅ |

---

## 二、详细操作记录

### 2.1 Skill 初始化（模型发现）

**操作**：执行 `model_discovery.py --config openclaw.json --json --save .model_cache.json`

**发现的模型列表**（23 个）：

| Provider | 模型名 | 推荐角色 | 原因 |
|----------|--------|---------|------|
| zai | GLM-5.1 | model_a_or_b | 通用 |
| zai | GLM-5 | model_a_or_b | 通用 |
| zai | GLM-5 Turbo | model_a | 含 turbo |
| zai | GLM-4.7 Flash | model_a | 含 flash |
| zai | GLM-4.7 FlashX | model_a | 含 flash |
| zai | GLM-4.5 Flash | model_a | 含 flash |
| deepseek | DeepSeek V4 Flash | model_a | 含 flash |
| deepseek | DeepSeek Reasoner | model_a_or_b | ⚠️ 未匹配 reasoning |
| infini-ai-com | glm-5.1 | model_b | 含 5.1 |
| xiaomi-coding | mimo-v2.5-pro | model_b | 含 pro |
| aliyuncs-coding | qwen3.6-plus | model_b | 含 plus |

**问题发现**：
- `DeepSeek Reasoner` 未被正确推断为 model_b，因为脚本检查 `reasoning` 但模型名是 `reasoner`（不包含 `reasoning` 子串）。这是 `model_discovery.py` 的角色推断 bug。

### 2.2 飞书文件下载

**第一次尝试**（drive API）：
```bash
curl -s -o "$OUTPUT_PATH" \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  "https://open.feishu.cn/open-apis/drive/v1/files/$FILE_KEY/download"
```
**结果**：HTTP 403，错误码 `99991672`，缺少 `drive:file` 权限。

**第二次尝试**（im API，参考 channel-intake skill）：
```bash
curl -s -o "$OUTPUT_PATH" \
  -H "Authorization: Bearer $TENANT_TOKEN" \
  "https://open.feishu.cn/open-apis/im/v1/messages/$MESSAGE_ID/resources/$FILE_KEY?type=file"
```
**结果**：✅ 下载成功（第4集 83MB，第11集 68MB）。

**教训**：飞书对话中的文件附件需要用 `im/v1/messages/{message_id}/resources/{file_key}` 接口，不是 drive API。

### 2.3 ASR 转写

**文件上传**：
```bash
POST /api/v1/files/upload -F "file=@video.mp4"
```
- 第4集：`file_id=01KQEYFWX207200CQG0JBM4R29`（82MB）
- 第11集：`file_id=01KQEYFX34571NHYXKEE64YNM9`（67MB）

**任务创建**：
```json
POST /api/v1/tasks
{
  "items": [
    {"file_id": "01KQEYFWX207200CQG0JBM4R29", "language": "zh"},
    {"file_id": "01KQEYFX34571NHYXKEE64YNM9", "language": "zh"}
  ],
  "segment_level": "10m"
}
```

**转写耗时**：
- 第4集（23.4分钟视频）：约 80 秒完成
- 第11集（16.2分钟视频）：约 80 秒完成

**输出文件**：
- `transcript_1.txt`：17,012 字符
- `transcript_2.txt`：13,171 字符

### 2.4 子 Agent 校对（核心实验）

#### 2.4.1 第一次尝试（失败）

**方案**：内联长文本到 task 参数
```python
sessions_spawn(
    task="... (5000+ 字符的 ASR 原文内联) ...",
    model="deepseek:deepseek-v4-flash"
)
```

**结果**：5 个子 Agent 全部 `gateway timeout after 10000ms`

**原因分析**：
1. Gateway 刚好在重启过程中
2. 内联长文本可能超出 task 参数限制

#### 2.4.2 第二次尝试（成功）

**方案改进**：
1. ASR 原文保存到文件 `refine_task/input_1.txt`
2. 子 Agent task 参数只包含文件路径和简短指令
3. 子 Agent 读取文件 → 校对 → 保存结果到文件

**启动命令**：
```python
sessions_spawn(
    task="读取文件 refine_task/input_1.txt，校对后保存到 refine_task/result_N.txt",
    model="deepseek/deepseek-v4-flash",
    context="isolated",
    label="refine-4k-N"
)
```

**模型格式关键发现**：
- ❌ `deepseek:deepseek-v4-flash` → "model not allowed: xiaomi-coding/deepseek:deepseek-v4-flash"
- ✅ `deepseek/deepseek-v4-flash` → accepted

OpenClaw 子 Agent 的 model 参数使用 `provider/model-id` 格式，不是 `provider:model-id`。

**第4集结果**：

| 候选 | 评分 | 关键修正 |
|------|------|---------|
| 候选1 ✅ | 8.8 | 形成分析法→形态分析法，阻力→主力 |
| 候选5 | 8.3 | 段落划分最佳，但遗漏关键术语 |
| 候选2 | 8.0 | 改动保守，术语修正不彻底 |
| 候选3 | 7.2 | 保留"形成分析法"错误 |
| 候选4 | 6.8 | 两大术语错误未修正 |

**第11集结果**：

| 候选 | 评分 | 关键修正 |
|------|------|---------|
| 候选2 ✅ | 8.3 | 黄金窝→黄金坑，对导→对倒，消息→消失 |
| 候选5 | 7.6 | 段落良好，但对导→对敲（错误） |
| 候选1 | 7.5 | 遗漏黄金窝→黄金坑 |
| 候选4 | 6.8 | 对敲错误 |
| 候选3 | 6.2 | 无段落划分，全文一面墙 |

---

## 三、问题与发现

### 3.1 模型发现脚本 bug

**问题**：`model_discovery.py` 的角色推断逻辑检查 `reasoning` 子串，但 `DeepSeek Reasoner` 小写后是 `deepseek reasoner`，不包含 `reasoning`。

**修复建议**：在关键字列表中增加 `reasoner`：
```python
B_KEYWORDS = ['pro', 'reasoning', 'reasoner', 'plus', 'opus', 'thinking']
```

### 3.2 飞书文件下载权限

**问题**：飞书应用只有 `im:message` 等基础权限，缺少 `drive:file:readonly`。

**实际解决方案**：对话中的文件附件用 `im/v1/messages/{message_id}/resources/{file_key}` 接口，不需要 drive 权限。

### 3.3 子 Agent 模型格式

**发现**：OpenClaw 子 Agent 的 model 参数使用 `provider/model-id` 格式（如 `deepseek/deepseek-v4-flash`），不是 `provider:model-id`。

### 3.4 内联文本 vs 文件传递

**发现**：子 Agent 的 task 参数有长度限制，长文本（>5000 字符）应通过文件路径传递。

### 3.5 API 方案 vs 子 Agent 方案

**对比**：

| 维度 | API 方案（脚本） | 子 Agent 方案 |
|------|----------------|--------------|
| 配置需求 | 需要 .env 中配置 API Key | 直接用 OpenClaw 已有配置 |
| 模型限制 | 仅支持 deepseek/openai/gemini/anthropic/siliconflow | 支持 OpenClaw 配置的所有模型 |
| 执行方式 | Python 脚本并行调用 | OpenClaw sessions_spawn |
| 结果收集 | 脚本写入本地文件 | 子 Agent 写入文件 |
| 可行性 | ⚠️ 需要手动配置 Key | ✅ 开箱即用 |

**结论**：子 Agent 编排层是更可行的路径。

---

## 四、输出文件清单

```
refine_task/
├── input_1.txt          # 第4集 ASR 原文
├── input_2.txt          # 第11集 ASR 原文
├── result_1.txt ~ result_5.txt   # 第4集 5 份候选
├── result_best.txt      # 第4集 最优结果（候选1）
├── evaluation.json      # 第4集 评分详情
├── evaluation.txt       # 第4集 评分摘要
├── summary.json         # 第4集 执行摘要
├── model_b_input.txt    # 第4集 模型B输入
├── result2_1.txt ~ result2_5.txt  # 第11集 5 份候选
├── result2_best.txt     # 第11集 最优结果（候选2）
├── evaluation_2.json    # 第11集 评分详情
└── model_b_input_2.txt  # 第11集 模型B输入
```

---

## 五、待改进项

1. **model_discovery.py bug**：修复 `reasoner` 关键字匹配
2. **Skill 文档**：子 Agent 编排层的模型格式示例应更新为 `provider/model-id`
3. **飞书权限文档**：channel-intake skill 应明确区分 drive API 和 im API 的使用场景
4. **summary.json 完善**：当前 time_ms 未填充实际耗时
5. **prompt_hashes**：当前为占位值，应读取实际 prompt 文件计算

---

## 六、成功要素

1. ✅ 子 Agent 文件传递方案可行，5 路并行稳定
2. ✅ deepseek-v4-flash 作为模型 A 生成质量不错
3. ✅ deepseek-v4-pro 作为模型 B 评分合理，能识别关键术语错误
4. ✅ 转写 → 校对全流程打通
5. ✅ 飞书 im API 下载方案有效
