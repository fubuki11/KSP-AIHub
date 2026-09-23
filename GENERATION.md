# AI Hub 0.4.0：生成预算、流式响应与错误诊断

## 配置档的生成设置

游戏内选择配置档，打开 **Generation limits / reasoning**，填写并保存：

| 设置 | 新配置默认 | 说明 |
| --- | ---: | --- |
| Output tokens | 16000 | 普通请求的总输出预算；部分供应商的推理也消耗该额度 |
| Recovery maximum | 32000 | 消费端明确请求恢复时，预算最多为普通额度的两倍，且不超过此上限 |
| Timeout | 300 s | 可设置 1–600 秒；网络失败不会自动重放 |
| Stream | 开启 | 支持 Chat Completions、Responses、Messages 的流式接收；不支持流的兼容接口可关闭 |
| Reasoning effort | provider_default | 仅在供应商支持时选择；DeepSeek/Gemini 新预设采用 low |
| Thinking mode | provider_default | 可对支持该参数的 Chat API 选择 enabled/disabled；此时 effort 必须保持默认 |
| Repetition recovery | prompt_only | 明确重复截断后的恢复策略，详见 [DIAGNOSTICS.md](DIAGNOSTICS.md) |

**Save generation settings for this profile** 立即保存到 `PluginData/Private/generation-settings.json`。它只覆盖该配置档的生成选项，不改变当前模型、认证或 API 地址。已开始的 HTTP 请求保留自身参数快照。

原有明确设置的额度不会被服务静默覆盖；可以在新面板调整。新配置缺省值为 16000 / 32000 / 300 秒。配置文件也支持 `maxOutputTokens`、`recoveryMaxOutputTokens`、`timeout`、`stream`、`reasoningEffort`、`thinkingMode`。

OpenCode OAuth/Codex 不接受普通 API 的输出上限参数，面板明确标记 **provider-managed**；它仍受本地超时与响应大小上限约束，不能把 UI 中的 token 数视为该接口已执行的限制。

## 错误不再全部混为 invalid_model_output

- `output_truncated`：明确长度上限导致终止，附预算、终止原因和可用 token 用量。
- `invalid_json_output`：已完成的文本不是单个有效 JSON 对象。
- `empty_model_output`：已完成但没有最终正文，推理内容不算设计正文。
- `model_refusal` / `model_tools_unsupported`：拒绝/过滤或工具调用，不能当作可自动修复的构型。
- `provider_incomplete`：流未明确完成，完整的 JSON 片段也不会被当成成功。
- `provider_timeout` / `provider_connection_error`：超时或连接故障。
- `invalid_provider_response` / `unsupported_model_output`：外层响应格式或结构不支持。

错误详情仅包含受控元数据，不返回原始模型正文、推理内容、密钥或上游错误体。完整单层 Markdown JSON 围栏可以正规化；截断对象、任意正文中的括号片段不会被修补或猜测成成功。

0.3.1 将 SSE 的传输量与最终正文上限分开：协议帧/元数据最多 64 MB，每帧最多 2 MB，最终正文仍最多 2 MB，并受时间和事件数限制。高频小分片的流不会再仅因协议开销超过 2 MB 而中断。

## 与 AutoCraft 0.7.0 配合

AutoCraft 对预算、格式和明确重复截断各采用一次对应恢复策略，全部调用仍共享最多 3 次的上限。同类错误重复出现时停止。预算恢复和临时降低思考可以组合，并沿用到后续结构修正；不会改写正常请求的配置。拒绝、网络失败和真正缺少完成信号的响应不自动重放。

重试前会重新检查存档、编辑器与合同；所有成功对象仍须通过原有结构、合同和性能检查。

AutoCraft 把发给模型的部件目录改成列式数据、删除重复键并只对模型侧数字保留六位有效数字；不减少已选部件，不改变校验器读取的原始值。报告新增 `promptBytes` 与 `modelRecoveries`。

## API v1 增量

- `POST /v1/generation-settings`：`{profile,settings:{...}}`，只接受生成设置白名单字段。
- `POST /v1/generate` 新增可选 `recovery:boolean`，不会让 Hub 自行发起重试；只应用配置档允许的恢复额度。
- 错误新增 `details`，包含适用的 `protocol`、`finishReason`、`outputLimit`、`recoveryLimit`、输入/输出/推理 token 数。
- 成功响应新增 `outputLimit` 与 `providerManagedOutput`。
- UI/SDK 新增生成设置和 provider-managed 提示。

AutoCraft 客户端单次等待上限 630 秒，游戏后台任务硬上限 35 分钟；默认单个供应商请求仍是 300 秒。这些上限不意味着会无限生成或无限重试。
