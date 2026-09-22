# KSP AI Hub：可复用 AI 接入框架

## 1. 定位与边界

KSPAIHub 是独立于 KSPAutoCraft 的 AI 接入层。它统一提供模型选择、认证、请求规范化和错误处理；飞船、合同、任务执行等业务仍归各 Mod。模型名称是配置数据，不在代码里固定为 GPT-6 或任何具体版本。

```text
KSPAutoCraft / 其他兼容 Mod
      │   AI Hub v1（文本 / JSON 请求）
      ├── C# SDK（KSP 1.12.5 / net472）
      └── Python / HTTP 客户端
                  │  127.0.0.1、单实例、令牌认证
             AI Hub Service（Python）
                  ├── Profile Router：Mod → 配置档 → 模型
                  ├── Credential Broker：API Key / 独立 OAuth / 迁移适配器
                  ├── Provider Adapter：Responses / Chat Completions / Messages
                  └── 请求限额、错误分类、秘密存储
```

游戏内 `KSPAIHub` Mod 负责启动/停止自己的服务、显示配置档和认证状态、切换模型，以及打开浏览器授权。外部服务负责 TLS、OAuth、密钥和提供商协议。其他 Mod 不需要保存提供商密钥，也不需要依赖 AutoCraft 的 Python 实现。

## 2. 模型、提供商、认证三者分离

- **Provider**：声明协议适配器和支持的认证方式；例如 OpenAI Responses、OpenAI 兼容 Chat Completions、Anthropic Messages。
- **Credential**：独立凭据 ID，包含认证类型、授权来源和允许发送凭据的 API origin。多个模型可共用同一凭据。
- **Profile**：给用户选择的模型配置档，绑定 provider、credential、base URL、model ID、超时和输出上限。
- **Global selection**：管理界面默认修改全局 profile/model，所有未覆盖的消费端共享。
- **Client override**：每个 Mod 可用自己的稳定 ID 设置独立覆盖；不要求预注册。清除覆盖后重新继承全局，改变一个 Mod 的覆盖不影响其他 Mod。
- **Request snapshot**：每次生成固定当时的 profile、model 与参数；切换模型只影响后续请求。

支持配置多个同类模型，例如 `fast`、`reasoning`、`claude-main`，但不通过名称猜测其能力。模型 ID 可从列表选择或手工填写；最终权限由提供商判断。

路由优先级为：**请求显式指定 > 消费端覆盖 > 运行时全局默认 > 配置文件默认**。各层选择是一组 profile/model：切换到不同 profile 时不把上一层另一个 profile 的模型名混入。省略 `clientId` 的请求直接走全局；核心服务、SDK 管理界面和通用示例不含任何特定消费 Mod 默认值。

消费端 ID 与 OAuth 客户端 ID 是不同概念。前者是路由/计量标识，后者属于提供商授权注册；修改前者不会改变密钥或登录身份。没有覆盖时，不同消费端 ID 的实际上游相同。

## 3. 认证能力矩阵

| 提供商 / 凭据 | 基础版处理 |
| --- | --- |
| OpenAI API Key | 支持 Responses / Chat Completions |
| OpenAI 独立 OAuth | 核心包含授权码 + PKCE、回调、加密保存及刷新；需要提供商允许使用的 OAuth 客户端注册信息 |
| OpenAI 已有 OpenCode OAuth | 可选迁移适配器，只读现有登录；不是独立登录的实现 |
| Anthropic API Key | 支持原生 Messages API |
| Claude 订阅 OAuth | 当前标记不可用于本框架的第三方接入，不提供可用登录按钮 |
| 自定义获授权 OAuth 服务 | 使用通用独立 OAuth 注册配置与兼容推理适配器 |

**独立登录优先**表示 OAuth 会话和刷新由 Hub 自己管理；并不意味着可以省略提供商注册、借用其他产品的 client ID，或保证每家都开放第三方 OAuth。

Anthropic 官方明确要求第三方产品使用 API Key 或受支持的云服务，不允许把 Claude.ai 订阅登录及其令牌作为第三方产品的接入方式。框架保留 OAuth 扩展接口，但不把这一入口伪装成现阶段可用功能。未来若获得官方支持，可新增提供商认证适配器，消费者协议不变。

依据：[Anthropic Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)、[Claude Code Authentication](https://code.claude.com/docs/en/authentication)。OpenAI 的独立应用注册/服务条款也应以提供商实际批准的授权方式为准；当前可复用登录的能力不等于取得独立客户端注册。

## 4. 独立 OAuth 生命周期

1. 用户在 Hub 管理界面选择凭据并点击登录。
2. 服务生成随机 state 和 PKCE verifier/challenge，将待完成会话保存在内存中并设置过期时间。
3. 浏览器前往该注册配置的 HTTPS authorization endpoint。
4. 回调只接受与当前会话匹配、未过期、一次性的 state。
5. 服务向固定 token endpoint 交换授权码，将 token 写入加密凭据存储。
6. 生成请求前按凭据锁串行检查/刷新；token 轮换原子落盘。
7. UI 仅获得 ready / login_required / expired / unsupported 等状态，不接触 access token 或 refresh token。

基础版面向 Windows，使用用户级 DPAPI 加密文件。API Key 也可由环境变量提供。跨平台版本应增加系统 Keychain/Secret Service 后端，而不是回退到明文 token。

## 5. v1 消费接口

- 统一输入：可选 `clientId`、可选 `profile`、消息数组、`format`（text/json）、可选模型覆盖。
- 统一输出：request ID、实际 provider/profile/model、文本、可选 JSON 文本、可获得的 token 使用量。
- 调用方不得传入 provider URL、Authorization 头、credential 内容或任意代码。
- JSON 结果仍需由消费 Mod 按自己的业务 Schema 校验。Hub 不会因为模型输出 JSON 就执行游戏动作。
- 不静默切换供应商，不自动重发可能产生费用的生成请求。
- 基础版先采用受限并发的同步 HTTP 请求；C# SDK 在后台线程调用，游戏主线程只处理完成结果。

完整接口见 `PROTOCOL.md`。协议版本独立于 Mod/服务版本；追加能力字段保持兼容，改变行为应增加协议版本。

## 6. 配置、凭据和信任边界

- 公共配置包含模型 ID、路由、认证类型和允许的 origin，不包含密钥。
- 推理服务只监听 loopback。请求需要 Hub 自己的 IPC token，拒绝浏览器 Origin 和重定向。
- 每个 credential 绑定允许的 API origin；切换 profile/model 不会把同一 token 发送到未批准主机。
- OAuth 回调是唯一无 IPC token 的入口，使用一次性 state 和 PKCE 保护。
- 密钥存储不在存档、设计 JSON 或公开 ZIP 中。
- KSP Mod 在同一用户、同一游戏进程中拥有相近权限；clientId 是路由/计量标识，不应宣称能隔离恶意 DLL。

## 7. KSPAutoCraft 迁移

增加 `auth: gateway` 的可选客户端配置。设计器继续调用 `generate(messages)`，合同与几何校验完全由 AutoCraft 保留。Hub 负责实际模型选择及认证，AutoCraft 不再需要知道选中的是 GPT 还是 Claude，也不需要 Hub 预设它的 ID。

现有直连配置仍可读取。迁移时先启动 Hub、配置一个可用 profile、验证 Hub，再把 AutoCraft 的模型配置改为 gateway。修改配置不需要重启游戏；下一次健康检查/设计会重新读取。

## 8. 分阶段交付

### 基础版

- 独立服务、HTTP 协议、配置档选择、三种推理协议适配器。
- API Key、获授权注册的独立 PKCE OAuth、加密存储与刷新、可选 OpenAI 旧登录迁移。
- KSP 管理 Mod 与 C# SDK、AutoCraft 可选网关接入、契约测试。
- 明确显示未配置/未开放认证方式，不伪造“所有 OAuth 均可用”。

### 后续扩展

- 异步 job、lease/取消传播、流式事件统一、断线恢复及公平队列。
- 完整模型目录发现、模型能力/上下文窗口/JSON Schema 能力探测。
- 工具调用和图像输入能力；游戏动作授权仍由消费 Mod 处理。
- 系统凭据库的跨平台后端、更多合法 OAuth/设备授权适配器。
- CKAN 公共发布与稳定 URL；兼容 Mod 依赖 `KSPAIHub`，而非依赖 `KSPAutoCraft`。
