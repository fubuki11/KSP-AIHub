# 0.2.0：多供应商与实时模型目录

## 游戏内操作

1. 打开 **AI Hub**，点击 **Refresh** 连接服务。
2. 已有配置档直接选择；没有则点击 **Add provider / compatible API**。
3. 选择 OpenAI、Anthropic、DeepSeek、Xiaomi MiMo、Google Gemini 或 OpenCode Zen，填写一个未占用的配置档 ID，点击 **Create connection**。服务地址和协议由预设填好。
4. 填写 API Key，点击 **Store API key and fetch models**。密钥加密保存后，面板自动请求该服务的模型目录。
5. 用 **Search models** 搜索，点击模型，再点击 **Apply global model**。列表获取本身不改变当前模型，也不发起推理。
6. **Refresh models (from provider)** 强制重新获取；普通自动获取可复用五分钟缓存，面板显示目录抓取时间。

默认操作全局模型。需要个别 Mod 专用模型时才勾选消费端覆盖；AutoCraft 仍需使用 `examples/autocraft.gateway.json` 才会跟随 Hub。升级不会把原来的直连配置自动改成网关。

## 实际接口

| 服务 | 默认模型列表 | 推理协议/认证差异 |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1/models` | Responses；Bearer API Key |
| Anthropic | `https://api.anthropic.com/v1/models` | Messages；`x-api-key` 和版本头；处理 `has_more` / `after_id` |
| DeepSeek | `https://api.deepseek.com/v1/models` | OpenAI 兼容 Chat Completions；Bearer API Key |
| Xiaomi MiMo | `https://api.xiaomimimo.com/v1/models` | OpenAI 兼容 Chat Completions；`api-key` 请求头 |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/models` | 目录使用 `x-goog-api-key`，处理 `nextPageToken`；选取支持 `generateContent` 的条目并去掉 `models/` 前缀；推理走 `/v1beta/openai/chat/completions` |
| OpenCode Zen | `https://opencode.ai/zen/v1/models` | 根据模型系列选择 Responses / Messages / Chat Completions |

目录名称来自这些接口的响应，不是内置的固定模型清单。按当前 Zen 官方端点表，GPT/o1/o3/o4、Grok、Muse 系列使用 Responses，Claude、Qwen 系列使用 Messages，其他文本系列使用 Chat Completions；Jev/System One 不是本版支持的文本生成协议，目录中排除。Zen 列表本身不提供协议字段，自动路由是经官方端点表核对的系列规则；后续新增不同协议的系列时，可在创建 Zen 连接时选显式协议覆盖，或更新适配规则。

列表表示供应商发布的目录，不保证每个条目都有当前账户的推理权限、余额或 Hub 所需文本/JSON 能力。OpenAI 等目录也可能返回图像、语音和嵌入模型；请按用途选择文本生成模型。Gemini 目录中明确不支持 `generateContent` 的项目会过滤。

## 第三方兼容 API 和手填模型

选择 **Custom compatible API**，填写 Base URL（例如以 `/v1` 结尾，不包含 `/chat/completions`），选择 `chat_completions`、`responses` 或 `messages`。

- 默认尝试该 Base URL 下的 `/models`。
- 没有模型列表接口时，关闭 **Fetch /models**，直接填写 **Model ID**。
- 无论是预设还是自定义连接，**Model ID 始终可以手动填写**，不要求 ID 出现在列表中。
- HTTP 401/403/404、网络故障、格式不兼容或刷新失败都会明确显示状态；不会清除已选模型或自动切换供应商。

高级配置仍可在配置档指定 `modelsUrl`、`modelsFormat`（`openai` / `anthropic` / `gemini` / `none`）。模型目录地址必须和推理地址同源。API Key 凭据支持 `keyHeader`：`auto`、`authorization`、`api-key`、`x-api-key`、`x-goog-api-key`。密钥不放入 URL 或配置 JSON。

## 升级与存储

- 0.1.x 的 `hub.json`、DPAPI 凭据及全局/消费端选择继续使用，不需要替换现有配置。
- 游戏内新增连接保存到 `PluginData/Private/configured-profiles.json`，密钥继续单独加密存储；无需重启服务即可添加。
- 该文件只记录连接信息，不包含密钥。它和 `hub.json` 中的配置档/凭据 ID 必须唯一，冲突会明确报错，不覆盖任意一份。
- 手工修改 `hub.json` 或受管配置文件时，仍需重启 Hub 服务。更改模型、保存 Key、游戏内添加连接不需要重启。
- 模型目录缓存仅在内存中保留五分钟；保存新 Key 会失效缓存。目录请求限制大小、分页数和并发数，拒绝重定向，沿用凭据来源绑定。

本次新增的是各供应商 API Key 连接与目录发现。OpenCode Zen Key 与 OpenCode 内已有 OpenAI OAuth 是不同凭据；OAuth 迁移和独立 OAuth 的原有行为继续保留。订阅 OAuth 的推理入口不一定提供 `/models`，此时仍可手填模型。

## 参考接口文档

- DeepSeek：<https://api-docs.deepseek.com/api/list-models>
- OpenAI：<https://developers.openai.com/api/reference/resources/models/methods/list>
- Gemini：<https://ai.google.dev/api/models>
- Zen：<https://opencode.ai/docs/zen/>
- MiMo：<https://platform.xiaomimimo.com>
