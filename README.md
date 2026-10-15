# AI 平台安装包工程

源码与发布记录由 Git 管理，当前工作版本由 `release-config.json` 定义。工程外层目录仍保留历史名称 `AI-Platform-1.0.0`，它不决定当前版本。

| 路径 | 用途 |
|---|---|
| `installer-bundle/` | 稳定的安装器源码、签名资源清单与来源配置 |
| `support-files/files/` | 已签名平台代码和配套资源；文本源码保留原始字节 |
| `release-config.json` | 安装器版本、资源发行 ID、构建布局 |
| `external-assets.json` | 大型外置依赖的相对路径、大小和 SHA-256 |
| `tools/release.py` | 唯一的当前发布入口 |
| `releases/` | 每版交付摘要和验证范围 |
| `delivery/` | 生成的交付目录和归档，不提交 Git |
| `build/` | 暂存、测试依赖及当前测试证据，不提交 Git |
| `validation/`、`tools/legacy/v1.0.0/` | 历史证据和原始构建脚本；不用于当前发布 |

在 Windows 使用 PowerShell 7：

```powershell
python tools/setup_test_dependencies.py  # 首次准备本机测试依赖
python tools/release.py test
python tools/release.py check --online
python tools/release.py build --online
```

Windows 上校验和打包使用本机 Python 3.11+，自动查找 Git 附带的 OpenSSL；POSIX 和 Qt 测试自动通过 WSL Python 3 运行。Linux 可直接运行全部命令。Linux 归档权限由构建脚本显式设置并验证。测试依赖准备脚本使用宿主机 pip，按 `tools/test-dependencies.json` 中固定的 SHA-256 下载 Linux x86_64 的 PySide6-Essentials 和 shiboken6 6.10.2 到 `build/test-python/`。本机测试环境为 Python 3.14。交付包仍使用既有锁定的 Linux ARM64 Qt 6.7.3，测试依赖不进入交付包。

`check --online` 只读取远端制品清单，不下载大模型。只要有远端模型不可用，它就返回退出码 2，并将明细保存到 `build/validation/preflight.json`。`build --online` 会将这些阻止项写入候选包的验证记录，绝不把它们报告为全部可安装。

构建要求当前 Git 工作区干净、全部自动化测试在该提交上通过且没有跳过；脚本校验签名、外置依赖、全部本地资源及原 1.0.0 归档。它重新封装安装器，真实执行 `.run --extract-only`，逐文件验证交付目录和归档中的内容、Linux 执行权限。已有版本的输出拒绝覆盖。

大模型、镜像、运行时归档、字体和安装依赖不进入 Git。首次克隆后，将 `external-assets.json` 对应的文件按原相对目录从可信资源库补齐：

```sh
python3 tools/release.py assets --path /path/to/asset-store
```

Git 仓库本身不包含这些依赖；当前机器的本地文件已保留。签名公钥和原签名清单纳入 Git，私钥始终留在工程及交付目录之外。

发布顺序：

1. 在分支修改源码、来源及必要测试，在 `release-config.json` 更新安装器版本及 `CHANGELOG.md`，运行 `python tools/release.py sync-version` 同步派生元数据、说明及桌面入口。
2. 提交源码，运行 `test` 和 `check --online`。资源内容变化时必须单独更新资源发行 ID、清单和受信任签名；单纯变更下载来源可复用原资源签名。
3. 运行 `build --online`；生成 `delivery/AI-Platform-版本-linux-arm64/`、完整 `.tar.gz`、归档摘要和 `releases/版本.json`。
4. 检查交付记录，将版本标签指向 `releases/版本.json` 里的 `source_commit`；再单独提交发布记录。标签与包内提交号必须相同。
5. 在兼容空白设备完成安装、推理和重启验收，再记录现场结果。候选状态不自动变成通过。

当前只建立本地 Git 管理，尚未配置远端仓库和异机备份。Git 历史支持恢复源码；设备上的跨版本升级、迁移和回滚需要单独实现。
