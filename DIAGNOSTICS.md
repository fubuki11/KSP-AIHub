# 0.4.0：重复截断恢复与诊断

`repetition_truncation` 是供应商明确的重复生成终止标记，不是“没有收到流完成信号”。Hub 现在将其报告为 **model_repetition**，并保留真实 `finishReason`、请求/实际响应是否流式，以及可用的 token 用量。正文为空、被截断或终止原因未知时，仍不会作为成功设计返回。

## 重复恢复策略

在 **Generation limits / reasoning → Repetition recovery** 中为配置档选择：

| 策略 | 消费端请求重复恢复时的行为 |
| --- | --- |
| disabled | 不允许自动重复恢复 |
| prompt_only | 仅由消费端精简/纠正提示，推理设置不变；兼容旧配置的默认值 |
| disable_thinking | 仅本次恢复请求发送 `thinking.type=disabled`，适用于支持该参数的 Chat Completions 接口 |
| low_effort | 仅本次恢复请求降低 `reasoning_effort` / `reasoning.effort` 为 low |

MiMo 新连接预设采用 disable_thinking；DeepSeek/Gemini 新预设采用 low_effort。旧配置可在面板设置。恢复不覆盖正常请求的推理选项，不改变流式开关、模型选择或凭据。

AutoCraft 0.7.0 对预算、格式、重复三种恢复策略各最多执行一次，**所有生成与修正合计最多三次调用**。因此“额度截断 → 重复截断 → 完整响应”可以在三次以内恢复，而同类错误反复出现时会停止。预算增加与重复恢复可组合；单独遇到重复截断不会盲目加大 token。网络失败、拒绝和未知完成状态不会自动重放。

## 诊断文件

Hub 每次已接受的生成调用保存受控元数据到：

`GameData/KSPAIHub/PluginData/Private/Diagnostics/generation-<requestId>.json`

最多保留最近 100 份。记录包含：配置档/模型、请求编号、实际使用的生成设置、恢复原因、完成/错误类型、终止标记、是否流式、用量和耗时。不会记录提示词、模型正文、思考内容、API Key、令牌或上游原始错误体。

AutoCraft 游戏内设计失败时保存：

`GameData/KSPAutoCraft/PluginData/Diagnostics/task-<id>.json`

最多保留最近 32 份，包含此次任务的 Hub 请求编号和安全错误元数据；面板显示诊断文件路径。成功的设计报告也包含 `modelRequests`，可与 Hub 记录对应。

存储失败不会覆盖原来的模型结果或异常。取消/强行终止进程的任务不保证有任务级文件；已经由 Hub 接受的调用会在结束时尝试记录。

## API v1 增量

- `repetitionRecovery` 为配置档生成选项，可由 `/v1/generation-settings` 保存。
- `/v1/generate` 接受 `recoveryReasons`，由最多三个不同的受支持错误码组成；支持 `output_truncated`、`invalid_json_output`、`empty_model_output`、`model_repetition`。
- 旧的 `recovery:true` 仍表示预算恢复，不破坏旧消费端。
- 错误详情保留安全的实际 `finishReason`，新增 `recoveryAllowed`、`requestStreaming`、`responseStreaming`、请求编号及应用后的恢复设置。
- 同一个请求编号出现在 HTTP 响应、Hub 诊断和 AutoCraft 报告中。
- `/v1/health` 声明 `recovery-reasons`、`generation-diagnostics` 能力。AutoCraft 0.7.0 需要 Hub 0.4.0+，同时检查安装和正在运行的服务。
