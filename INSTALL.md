# 安装 KSP AI Hub

作者：**fubuki11st**。目标环境：KSP 1.12.5、Windows x64、Python 3.10+。

## 从源码构建并通过本地 CKAN 安装

从项目目录运行，修改 `$ksp` 为实际安装位置：

```powershell
$ksp = 'D:\steam\steamapps\common\Kerbal Space Program'
.\scripts\build.ps1 -KspRoot $ksp
.\scripts\install-local.ps1 -KspRoot $ksp
```

执行安装前保存并退出 KSP，正常关闭 CKAN。默认游戏路径适用时，也可以在构建后双击 `Install-KSPAIHub.cmd`。

安装脚本会：

1. 检查安装锁、游戏进程和发布包一致性。
2. 添加或复用 `KSPAIHub-local` 本地仓库，只刷新该来源。
3. 安装或升级到构建元数据指定的版本。
4. 核对 CKAN 归属和全部发布文件的 SHA-256。
5. 初始化缺失的启动配置；已有模型设置和凭据不会被覆盖。

完成时应显示 `Verified KSPAIHub 0.3.2: ...`。重新打开 CKAN，在 Installed / 已安装筛选中搜索 `KSPAIHub`。

本地 `dist` 中的 ZIP 和仓库索引需要保留，供 CKAN 读取。移动源码后应重新构建，并确认 CKAN 的本地仓库地址。

## 手工安装发布 ZIP

有 Release 安装包时，将其中 `GameData/KSPAIHub` 放入游戏的 `GameData`，使插件位于 `GameData/KSPAIHub/Plugins/KSPAIHub.dll`。

首次手工安装还需初始化启动配置：

- 有源码时运行 `scripts/configure.ps1 -KspRoot <游戏目录>`。
- 或将 `hub.example.json` 复制为 `PluginData/hub.json`，在游戏 AI Hub 的 **Settings** 中填写实际 Python 可执行文件路径和该 JSON 的完整路径，保存后启动服务。已有配置不要覆盖。

然后按 [MODELS.md](MODELS.md) 设置供应商凭据和模型。

## CKAN 导入与常见错误

**Import downloaded mods… 可能只缓存 ZIP。** 未收录到仓库索引的本地 Mod 不一定因此获得安装任务，建议使用上述安装脚本。

- **CKAN is running**：退出 CKAN，包括托盘实例；不要删除正在使用的 `registry.locked`。
- **repository points to another location**：检查已有同名本地仓库地址，再调整或重新构建。
- **payload differs**：同版本文件内容与发布包不同，应通过 CKAN 修复，或为修改后的构建使用新版本号。
- **只进入缓存、Installed 中没有条目**：使用明确的本地仓库安装流程。

`KSPAIHub-local-repository.zip` 是索引，不是 Mod 安装包。公开发布时用 `build.ps1 -DownloadUrl <HTTPS地址>` 生成可供其他机器下载的元数据；本机 `file://` 地址不是公开下载地址。
