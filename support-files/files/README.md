# ModelDeck 全量离线交付包

版本：1.4.0-dev.1-offline-20260919（development）。

请先完整阅读 docs/安装说明.md 或 docs/安装说明.pdf。

适用范围：已预装匹配厂家 Debian 12 ARM64 系统和 XH2 v1.4.0 驱动的空白 CIX P1 / 后摩 LQ50 盒子；不是系统刷机包，不覆盖已有项目。

在本目录执行：

```bash
sudo bash scripts/install.sh --preflight-only
sudo bash scripts/install.sh
```

安装不会联网。全部模型安装到本机，后摩互斥模式按需切换；默认运行 CoPaw。管理页面端口 17780，统一 API 端口 17700。初始管理密码在安装时独立随机生成。

manifests/ 包含版本、镜像 ID 锁定、模型清单、源文件来源、构建检查和全部文件 SHA-256。

当前已进行制品静态验证；未在空白同型号设备执行完整安装和重启演练。安装器会执行默认模式验收，交付前还应在维护窗口执行全模式和断网重启验收。源码盒子的验证不代替新机验证。
