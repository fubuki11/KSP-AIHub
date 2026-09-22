# KSP AI Hub 0.3.2

**作者：fubuki11st** · **MIT License** · **KSP 1.12.5 / Windows x64**

独立、可复用的 AI 接入 Mod。兼容的 KSP Mod 通过统一 HTTP API 或 C# SDK 使用模型，无需自行处理各供应商的认证与协议。

## 功能

- 全局模型选择与可选的逐 Mod 覆盖；不预设固定消费端或模型名称。
- OpenAI Responses、OpenAI 兼容 Chat Completions、Anthropic Messages，支持流式接收。
- OpenAI、Anthropic、DeepSeek、MiMo、Gemini、OpenCode Zen 连接预设，以及第三方兼容地址。
- 保存 API Key 后获取模型目录，支持搜索、刷新、选择和手工输入模型 ID。
- Windows 用户绑定的 DPAPI 加密凭据、环境变量凭据、注册式 OAuth + PKCE，以及可选的已有 OpenCode OpenAI 登录适配器。
- 分配置档设置输出预算、恢复上限、超时和推理选项；区分截断、格式错误、拒绝与网络故障。
- 仅在本机监听，提供游戏内管理面板和可复用 SDK。

模型目录可读不代表每个模型都有账户权限，或都支持文本/JSON 功能。独立 OAuth 仍需供应商认可的客户端注册；示例注册默认禁用。Claude 订阅 OAuth 不是本版支持的第三方登录路径，Anthropic 请使用 API Key。

## 文档

| 文档 | 内容 |
| --- | --- |
| [安装](INSTALL.md) | 从源码构建、本地 CKAN 安装、手工安装与常见错误 |
| [模型配置](MODELS.md) | 供应商、模型目录、自定义 API 和手填模型 |
| [生成设置](GENERATION.md) | token/超时/推理参数、流式响应与有界恢复 |
| [HTTP / SDK 协议](PROTOCOL.md) | 供其他 Mod 调用的 API v1 |
| [架构](DESIGN.md) | 认证、协议、路由与消费端分层 |

## 从源码构建

需要 Git、.NET SDK、Python 3.10+ 和本机 KSP 1.12.5。DLL 目标为 .NET Framework 4.7.2；游戏/Unity 程序集仅作编译引用，不随发布包分发。

在 PowerShell 中执行，按实际情况修改 `$ksp`：

```powershell
$project = Join-Path $PWD 'KSP-AIHub'
git clone https://github.com/fubuki11/KSP-AIHub.git $project
$ksp = 'D:\steam\steamapps\common\Kerbal Space Program'
& (Join-Path $project 'scripts\build.ps1') -KspRoot $ksp
```

构建生成 `dist/KSPAIHub-0.3.2.zip`、独立 `.ckan` 和本地元数据仓库 ZIP。保存并退出 KSP、CKAN 后安装：

```powershell
& (Join-Path $project 'scripts\install-local.ps1') -KspRoot $ksp
```

安装脚本通过 CKAN 安装明确版本，核对文件内容和归属，再初始化缺失的启动配置。**Import downloaded mods… 可能只缓存未收录 Mod，不能代替完整安装。**

## 游戏内配置

1. 点击右上角 **AI Hub**，必要时点击 **Start service**。
2. 选择已有配置档，或通过 **Add provider / compatible API** 添加连接。
3. 保存 API Key 或完成该配置档支持的登录，获取模型列表。
4. 搜索/点击模型，或填写 **Model ID**，点击 **Apply global model**。
5. 在 **Generation limits / reasoning** 中调整该配置档的输出预算与超时。

默认选择作用于所有没有独立覆盖的兼容 Mod。要为单个 Mod 指定模型，勾选 **Configure an individual Mod override**，输入该 Mod 的消费端 ID；点击 **Use global default** 可恢复继承。

运行配置位于游戏中的 `GameData/KSPAIHub/PluginData`。不要把该目录、连接令牌或个人凭据放入源码仓库或发布包。

## AutoCraft 与其他 Mod

AutoCraft 0.6.0 自动发现同一游戏中的 AI Hub 0.3.0+；该项目只发布独立 Hub，不包含 AutoCraft。旧 AutoCraft 0.3.1–0.4.x 可参考 `examples/autocraft.gateway.json`，按实际游戏目录调整路径。

其他 Mod 引用 `KSPAIHub.dll`，通过 `AiHubClient.FromConnectionFile(connectionPath, consumerId)` 接入。消费端 ID 是可选的路由标签，不是 OAuth client ID 或 API Key。不要在 Unity 主线程阻塞等待网络任务，也不要未经自身验证直接执行模型输出。完整示例见 [PROTOCOL.md](PROTOCOL.md)。

## 测试

```powershell
$env:PYTHONPATH = Join-Path $project 'service'
python -m unittest discover -s (Join-Path $project 'tests') -v
```

测试使用隔离 HTTP 服务和临时目录，不需要真实模型凭据。Windows 专属测试需要 Windows；发布包检查在构建后运行。与 AutoCraft 的集成检查在缺少相邻 AutoCraft 源码时会跳过。

## 公开发布安装包

源码通过 Git 管理，安装包放在 [GitHub Releases](https://github.com/fubuki11/KSP-AIHub/releases)。准备该版本 Release 时，使用公开 HTTPS 下载地址重新构建元数据：

```powershell
& (Join-Path $project 'scripts\build.ps1') -KspRoot $ksp -DownloadUrl 'https://github.com/fubuki11/KSP-AIHub/releases/download/v0.3.2/KSPAIHub-0.3.2.zip'
```

这会同步更新独立、包内及仓库内的 CKAN 元数据；不会自动上传或创建 Release。省略 `-DownloadUrl` 时仍生成适合本机安装的 `file://` 地址。`dist/` 不进入 Git 历史。

GitHub Release 不等于 CKAN 公共索引收录；NetKAN/CKAN 收录另行办理。

## 当前边界

目前支持文本与 JSON 对象输出，不包含多模态、模型工具执行、持久化任务队列或完整的远端取消协议。目录发现不会自动切换模型；Hub 不会静默切换供应商或自动重放付费生成。消费端只能在明确错误类型和自己的尝试上限内请求恢复。
