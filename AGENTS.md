# 工程约定

- Windows 的 PowerShell 命令使用 PowerShell 7 (`pwsh.exe`)，不使用 Windows PowerShell 5.1。
- 当前源码目录为 `installer-bundle/`。当前发布入口为 `python tools/release.py`，历史脚本仅作审计。
- 安装器版本由 `release-config.json` 定义；`installer-bundle/release.json` 的派生版本须一致。模型资源发行 ID 与安装器版本各自管理。
- 发布前运行 `python tools/release.py test` 与 `python tools/release.py check --online`。构建要求测试对应同一个干净的 Git 提交。
- `support-files/files/` 和 `installer-bundle/catalog/` 中现有签名内容必须保留精确字节。修改平台或模型内容须显式更新资源发行 ID 并使用受信任发布密钥重新签名；不要悄悄更换公钥。
- 私钥、实际凭据、模型权重、镜像、安装依赖及生成的交付包不提交 Git。外置依赖由摘要清单记录。
- 已有版本的交付文件保留，发布工具拒绝覆盖。发布标签指向包内 provenance.json 的 source_commit；发布记录可单独提交。
- 本地测试和容器模拟不能作为空白设备、硬件推理或重启验收通过的依据。
