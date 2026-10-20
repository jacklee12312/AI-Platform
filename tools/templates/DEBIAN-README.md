# AI 平台安装器 Debian 包

适用厂家 Debian 12、ARM64、Python 3.11 设备。当前 Debian 包版本为 `1.3.0-9`，程序安装到 `/opt/ai-platform-installer/1.3.0/`，标准命令为 `/usr/bin/ai-platform-installer`。

1. 在桌面打开 `.deb`，点击“安装软件包”并完成系统授权；也可执行 `sudo apt install ./ai-platform-installer_1.3.0-9_arm64.deb`。
2. 软件包安装完成且安装锁释放后，自动在当前活动桌面以普通用户打开向导。已有向导时不重复打开；无活动图形桌面的 SSH 安装只安装软件，可稍后从 GNOME“活动 → 显示应用”搜索“AI 平台安装器”。安装 `.deb` 不自动开始注册探针或部署平台。
3. 选择组件、模型及来源，填写现场配置后开始。顺序为探针 → 组件库 → 模型及验收 → 翼起办及模型网关联通测试。

向导启动时自带完整系统命令搜索路径，兼容 GNOME 默认不含 `/usr/sbin` 的用户环境；无需在新设备修改桌面启动项或增加命令链接。自动打开失败时可从应用菜单重开，诊断见 `journalctl -u ai-platform-installer-open-wizard-1-3-0-9` 与 `journalctl --user -u ai-platform-installer-wizard`。

探针每次执行该阶段都从平台下载当前版本，按二进制 SHA-256 与已有探针比较。一致复用，不一致备份后替换程序和服务，保留配置、证书与设备注册信息；失败恢复旧版本。没有探针才显示注册表单并强制安装。

组件按选择拉取镜像；翼起办必需 MySQL、Redis、ParadeDB，并复用这些共享组件。Docker 配套包不同时自动替换旧运行时，保留原数据与配置并恢复容器状态。所选服务端口被旧服务占用时，先备份再接管，失败执行恢复。

模型库独立保存。可在向导中选择 `~/AI-Platform-Model-Library` 或其他模型库根目录，也可联网下载。Qwen3.6 双卡资源远端缺失，该模型未包含在交付的本地模型库中，请取消勾选。

翼起办应用资源改为从 Harbor 下载约 4.41 GB 的 ZIP，固定仓库清单摘要、ZIP 摘要及发行签名，支持断点续传。ZIP 中的 10 个 ARM64 应用镜像与 926 个模板、SQL、配置文件全部校验后才部署。模型选择本地导入仍可联网获取翼起办；如需本地提供，将 `AI-Platform-YiQiBan-Harbor-Upload-0.6.1-arm64-20261019-full.zip` 放在所选本地资源目录根部。详细来源见安装目录内 `bundle/docs/翼起办远端资源.md`。

配套 NOE 3.1.2 用户态运行库随包提供，缺失自动补齐，损坏先备份后修复。厂家内核驱动、设备节点与 XH2 SDK 前置条件仍需满足。实际 NPU 推理在模型验收阶段检查。

缓存、下载断点、模型暂存、运行时解压和翼起办资源解包自动放在可用空间最大的安全候选分区，通常为 `/opt/ai-platform-data/`。安装任务固定该路径，继续时保留断点。

更新 `.deb` 保留 `/var/lib/ai-platform-installer` 中的任务记录、所选范围、凭据及进度，所以同为 1.3.0 的未完成任务会显示“继续安装”。来源配置保存在 `${XDG_CONFIG_HOME:-$HOME/.config}/ai-platform-installer/1.3.0/sources.json`。`ai-platform-installer --check` 不启动平台部署。

移除向导可执行 `sudo apt remove ai-platform-installer`，不会清除已部署平台、模型库或业务数据。安装报告中的容器健康、模型联通与业务授权分别记录；翼起办仍需首次管理员注册及有效许可证。
