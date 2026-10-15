# 2026-09-22 Harbor 下载接入

来源：用户提供的《适配文件下载路径.txt》。只解析可见仓库引用，不执行文档命令。原文同时写了 23 项和 22 项可拉取，本次实际查询 23 个不同仓库，均可匿名读取清单。

服务为 `http://36.111.34.121:15436`，项目 `aistore`。没有向服务器上传、删除或修改内容。

最初 17 个原始文件及后来新增的 CosyVoice3、Spark，共 19 个单文件模型的层摘要和大小与平台签名清单完全一致，直接按摘要下载。CoPaw 层标题包含 Windows 绝对路径，安装器忽略该标题，部署路径来自原签名发行清单。

6 个压缩目录为 Qwen3-VL、GLM-OCR、Qwen3Guard、SenseVoice、Matcha、Kokoro。先验证压缩层，再验证每个所需文件，具体覆盖与核验状态见 `evidence/harbor-sources-20260922.json`。

Qwen3Guard 包未带 `LICENSE`、`README.md`、`generation_config.json`；SenseVoice 包未带 `test_wavs/zh.wav`。这 4 个小文件从原发行包复制进 `support-files`，逐个核对原摘要，不包含大型模型权重。

2026-09-23 新增的 `aistore/cosyvoice3:v1` 与原 CosyVoice3 权重的摘要和大小一致，已接入。`aistore/xunfei-spark:v1` 上传完成后也已核对，资源层大小 8,266,037,056 字节，SHA-256 与原签名 Spark 权重完全一致。两项都固定清单摘要并写入来源配置；合计 25 个仓库、60 个远端模型文件，另有 4 个随包辅助文件。原签名模型清单未改。

安装器版本为 0.2.2，资源发行 ID 仍为 `1.4.0-dev.1-modular.1-20260921`。签名资源目录和模型 SHA-256 未改。目录中的 `installer_version: 0.2.0` 表示该资源清单最初由上一版创建，不影响当前安装器版本。

当前为 HTTP 传输，没有链路加密。最终模型按既有签名清单校验；临时匿名令牌只保留内存，不保存仓库密码。正式发布应使用 HTTPS 和正式发行签名信任链。

本次增加的是模型 OCI 工件拉取，12 个容器镜像仍在独立镜像归档中，位置不变。

实现参考：[OCI Distribution 拉取规范](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)、[Registry Token Authentication](https://distribution.github.io/distribution/spec/auth/token/)。
