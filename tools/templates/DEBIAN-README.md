# AI 平台安装器 Debian 安装包

适用厂家 Debian 12、ARM64、Python 3.11 设备。Debian 包版本 `1.2.0-1` 表示安装器 1.2.0 的第 1 次 Debian 封装；原签名资源与原 1.2.0 交付保持一致。

1. 在图形桌面用“软件”打开 `.deb`，安装 AI 平台安装器；需要管理员授权。若系统默认用压缩软件打开，请改用“软件”，也可在终端执行 `sudo apt install ./ai-platform-installer_1.2.0-1_arm64.deb`。
2. 安装完成后，从 GNOME“活动 → 显示应用”搜索“AI 平台安装器”。不要再打开普通文件夹中的旧 `.desktop` 文件。
3. 向导仍按“识别/安装探针 → 所选组件库 → 所选模型”执行。安装 `.deb` 本身不会注册探针、启动组件或安装模型。
4. 模型库独立保存。已复制到 `~/AI-Platform-Model-Library` 的库可被发现；也可在向导的本地导入选项里选择模型库根目录。未勾选的模型以后仍可通过 ModelDeck 补装。

程序与完整基础资源安装到 `/opt/ai-platform-installer/1.2.0/`，标准命令为 `/usr/bin/ai-platform-installer`。来源配置保存在 `${XDG_CONFIG_HOME:-$HOME/.config}/ai-platform-installer/1.2.0/sources.json`，允许普通用户保存本地导入选择。`ai-platform-installer --check` 只校验安装器并准备用户配置，不启动安装。

移除安装器应用执行 `sudo apt remove ai-platform-installer`。这只移除安装器自身文件；不会清除平台、模型库、数据库或用户配置。包管理器不自动运行整个平台的安装和卸载。

包内 `debian-provenance.json` 同时记录 Debian 封装源码提交和原 1.2.0 资源源码提交。完整基础资源仍包含在包内，组件镜像按选择拉取，大模型不重复装入 `.deb`。原 1.2.0 的远端 Qwen3.6 双卡资源缺失限制保持不变，请取消勾选该模型；本地模型库已排除它。
