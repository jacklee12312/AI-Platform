# 第三方组件与来源

本文件是构建来源记录，不是已完成的许可证合规审计。正式对外分发之前，应核对所有模型、厂商运行时、镜像和系统软件的使用及再分发条件。

## 图形安装器新增组件

| 组件 | 版本 / 来源记录 | 随包材料 |
| --- | --- | --- |
| PySide6 Essentials / Shiboken6 | 6.7.3，PyPI 官方发布；具体 URL 与哈希在 `gui-runtime/lock.json` | 原始 ARM64 wheel、`licenses/*.METADATA.txt` |
| Qt 库 | 随上述 PySide6 wheel 提供的 Qt 6.7.3 动态库 | `licenses/LGPL-3.0-only.txt`、`GPL-3.0-only.txt`、`Qt-GPL-exception-1.0.txt`，来源与哈希在 `licenses/sources.json` |
| Debian 图形辅助库 | Debian 12 bookworm ARM64，逐包版本、官方 URL 与 SHA 在 `gui-runtime/system-libs-lock.json` | 原始 `.deb`，其中保留各包 `/usr/share/doc/<包名>/copyright` 等文件 |
| Noto Sans CJK SC | Noto CJK Regular，具体 URL 和 SHA 在 `assets/fonts/sources.json` | 字体、原始 SIL Open Font License，见 `assets/fonts/LICENSE` |

PySide6 的发行元数据列出 LGPLv3/GPLv3 与商业许可选择，详见随包原始元数据。安装器使用未经修改的动态库，不使用 PyInstaller 封闭打包，图形安装器 Python 源码一并交付。上游源代码索引：[Qt 6.7.3](https://download.qt.io/archive/qt/6.7/6.7.3/)、[Qt for Python](https://code.qt.io/cgit/pyside/pyside-setup.git/)。安装流程本身不会访问这些链接。

随包许可文本不是 Qt 全部模块及其第三方代码的逐项合规报告；若按许可证要求分发对应源码或提供源码获取方式，应由交付方在正式发布中配齐，不应把本开发版标为已完成商业分发认证。

Debian 依赖仅提取共享库到用户私有缓存，保留原始包作为版权和版本来源，不执行这些 GUI 包的维护脚本，不替换厂家 glibc、libstdc++ 或图形驱动。

## 原完整平台资源

`resources/` 是已有全量包的独立完整副本；所有原有模型、镜像、运行时、说明、来源及清单保持不变。以该目录的清单和组件自带许可证为准。是否持有厂商软件、模型权重及其衍生制品的分发权，不能从“文件已完整打包”推断。

本次制作没有为第三方模型、厂商 SDK 或原项目新增授权声明。
