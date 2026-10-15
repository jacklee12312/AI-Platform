# AI 平台安装包工程

源码与发布记录由 Git 管理，当前工作版本由 `release-config.json` 定义。工程外层目录仍保留历史名称 `AI-Platform-1.0.0`，它不决定当前版本。

1.1 按“探针自动识别及 MQTT → 所选组件库认证与健康检查 → 所选模型安装及验收”执行。已有兼容探针时隐藏注册表单并自动复用，没有探针时强制安装；组件库按需勾选，Milvus 自动包含 MinIO 和 etcd。现场配置、固定版本和兼容复用边界见 [1.1 安装流程](installer-bundle/docs/1.1安装流程.md)。MinIO 固定为 `dappros/minio:RELEASE.2025-09-07T16-13-09Z` 的已批准 ARM64 清单摘要；其余组件镜像同样锁定平台、版本与内容摘要。

| 路径 | 用途 |
|---|---|
| `installer-bundle/` | 稳定的安装器源码、签名资源清单与来源配置 |
| `support-files/files/` | 历史已签名资源基线；保留原始字节 |
| `platform-overlays/1.1.2/` | 本版平台修改及 ModelDeck 前端完整源码 |
| `build/resources-1.1.2/` | 从基线和版本化修改生成的新签名资源，由外置依赖清单锁定 |
| `release-config.json` | 安装器版本、资源发行 ID、构建布局 |
| `installer-bundle/catalog/workflow.json` | 受发行签名保护的探针、组件、镜像与依赖锁定清单 |
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

制作新的 1.1 前置资源时，使用 `tools/prepare_workflow_resources.py` 和 WSL 中的 `tools/prepare_workflow_debs.py`，下载与构造归档，不安装宿主机软件。首次建立锁定之后默认复用锁定内容；更新来源需显式刷新并重新签名。`tools/finalize_workflow_release.py --key 项目外的原发布密钥路径` 校对原公钥、更新资源发行身份及摘要并签名。密钥不进入 Git 或交付包。

本版先运行 `python tools/validate_component_sources.py` 核验六个组件的远端 ARM64 清单与配置，默认包不携带组件镜像。真实 ARM64 程序的开发验证入口为 WSL root 下的 `tools/validate_workflow_artifacts.py` 和 `tools/validate_component_archives.py`。前者以 QEMU 检查 ARM64 MinIO、Compose 和探针依赖，探针只访问不可用的 loopback 测试注册地址；后者仅在临时独立 Docker 中导入并校对六个离线镜像。它们不替代目标设备的真实注册、MQTT、全组件联通和重启验收。

1.1.2 的资源准备：先在 `platform-overlays/1.1.2/modeldeck-frontend/` 按 package-lock.json 安装构建依赖并执行 `npm run build`，然后执行 `python tools/prepare_slim_release.py --key <工程外原受信任私钥路径>`。工具从 v1.1.1 资源基线生成 `build/resources-1.1.2/`，只移除已退役服务的镜像标签及独占层，保留 Kokoro 共享层，并生成新的资源发行 ID、摘要和签名。后端回归依赖安装到 WSL 的 `build/test-python/`，与原 Qt 测试依赖共用目录，不进入交付包。
