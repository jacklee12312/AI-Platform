# Kokoro 与本机 NPU 运行库的适配

本文记录 Debian 修订 6 的处理过程。修订 7 已将库随包交付并支持自动补装、
损坏修复和兼容性选择，见 [NPU 离线运行环境](npu-offline-runtime.md)。

2026-10-16 在 CIX 盒子实测：内核 6.6.89、AIPU KMD 6.0.1，
`kokoro-cix:0.2.0` 的 libnoe 2.0.0 / libnoe.so.0.6.0 初始化报
`query capability fail`，特权容器同样失败。盒子配套的 cix-noe-umd 3.1.2、
Python 3.11 binding 和 cix-npu-onnxruntime 中的 libaipu_driver.so 可成功
加载全部五个 CIX 子图。实际中文语音接口返回 24 kHz、2.15 秒 WAV。

安装器将已通过 dpkg 完整性检查的主机运行库复制到 root 管理的只读快照，
为本机生成 Compose 配置，设置 LD_LIBRARY_PATH / AIPU_LIB_PATH。
这属于主机驱动配置：签名资源目录、模型权重、镜像 ID、公钥和资源发行 ID
保持原字节。原 Compose 留存于 private/local-configs；本机配置及运行库
摘要记录在 private/kokoro-runtime.json。继续安装时逐一校验，未知修改拒绝覆盖。
ModelDeck 后续使用的仍是相同已安装 Compose 路径，因此重建容器会保留适配。

仅在原镜像 NPU 初始化失败且主机安装了已适配的 3.1.2 时应用；旧环境
初始化正常则沿用原配置。未适配版本不会静默降级到 CPU。卸载主机驱动、
切换硬件或升级驱动后需重新验证；本次不是空白设备或重启验收。

同一盒子的 cix-cv-noe:1.0.0 也使用旧 NOE 库，直接初始化同样失败；
使用相同适配后，MobileNet V2 实际推理返回 1000 项有限数值。
因此也为所选视觉模型的共享 CV 服务生成相同的本机配置，单独记录
private/cv-runtime.json 和原始 Compose；原签名 CV 资源保持不变。
