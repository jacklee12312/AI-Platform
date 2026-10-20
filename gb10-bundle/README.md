# AI Platform GB10 候选版

独立硬件配置为 Ubuntu 24.04 ARM64 / Python 3.12 / NVIDIA GB10（SM 12.1）。
原 NPU 版源码、资源签名、公钥与历史交付保持不变。版本由工程根目录
`release-config.json` 的 `profiles.gb10` 定义。

本轮首先验收现有两套本地模型的安装、互斥切换及统一网关调用。
沿用原 AI Model Gateway、ModelDeck 前后端、会话/CSRF、API Key、用量、
请求诊断、批量任务与评测代码；NPU 操作由 GB10 控制后端隔离。
仅将两套 GB10 模型列入当前可用清单，不把原平台的 NPU 模型声明为 GPU 已适配。

## 构建

1. 按 `python-runtime.json` 与工程 `external-assets.json` 准备外置 ARM64 CPython 3.12 wheel。
2. 在原 `platform-overlays/1.2.0/modeldeck-frontend` 执行 `npm run build`。
3. 提交源码后执行 `python tools/release.py test`，测试须对应同一干净提交且无跳过。
4. 执行 `python tools/release.py check --online`，保留原资源来源检查结果；原 NPU 来源故障与 GB10 本地复用分开记录。
5. `python tools/release.py gb10 --key <工程外原受信任发布私钥> --deb-revision 2`。

产物名 `ai-platform-gb10-installer_1.3.1-2_arm64.deb`，使用独立包名、安装目录、
桌面入口与 systemd 单元。构建拒绝覆盖已有产物，包内保留提交和测试记录。
新增资源清单以原发布密钥签名，安装时校验清单、原公钥指纹及每个文件摘要。
不包含大模型权重和 34.6GB 的推理镜像，不修改 Docker/驱动或其他容器。

## 盒子安装与调用

```sh
sudo dpkg -i ai-platform-gb10-installer_1.3.1-2_arm64.deb
sudo ai-platform-gb10 check
sudo ai-platform-gb10 install \
  --model-root /home/user/gb10-bench/models \
  --cache-root /home/user/gb10-bench/cache \
  --tiktoken-root /home/user/gb10-bench/tiktoken
sudo ai-platform-gb10 switch nemotron
sudo ai-platform-gb10 switch gpt
sudo ai-platform-gb10 status
sudo ai-platform-gb10 credentials
```

安装包本身只注册安装入口；执行 `install` 才校验模型并部署平台。
首次部署校验全部官方 LFS SHA256 与 Git blob SHA1，不复制两套大权重。
重复安装仅在本次签名清单的 revision、逐文件摘要以及原校验记录的大小、inode、
纳秒 mtime 均匹配时复用原完整校验；否则重新校验。安装会重载控制服务与管理容器。
切换前检查文件未变化，等待在途请求结束；目标模型加载后必须回答实际算术问题，
才开放调用。失败尝试恢复原模式，失败任务仍报告失败。大模型启动最多等待 20 分钟。
后台安全状态过期、容器退出、实例变化或重启后未验证时，网关拒绝新推理请求。
服务恢复保留最后成功模式；系统整机重启验收须另行实测，不以单元配置代替。

管理入口 `http://<盒子IP>:17780`，OpenAI 兼容入口 `http://<盒子IP>:17700/v1`。
模型 ID 为 `gpt-oss-120b` / `nemotron-3-super`。初始管理密码和业务 Key 自动生成，
仅保存在 root 可读配置中，通过 `credentials` 现场查看，不写入交付报告。
模型服务和受限代理只监听本机回环地址。浏览器无 Docker socket 访问权限。

GPT 使用原始 MXFP4；Nemotron 使用 NVFP4 与 MTP 3；FP8 KV、TP/EP=1、
batch=4、max_num_tokens=4096、context=16384，关闭前缀复用。
两套 120B 模型互斥运行。切换期间另一模型返回 503，不按每个请求自动加载。
API Key 与公共地址保持不变。

模型后端使用独立 Docker 网络空间，仅映射到本机 `127.0.0.1:17701`，
避免 TensorRT-LLM 启动端口检查被上一模型的 `TIME_WAIT` 阻挡。
切换前将上一实例的末尾日志保存在私有 `logs/` 目录。
组件库使用原签名配方与认证检查，容器、配置和数据使用 GB10 独立前缀；
端口被原有业务占用时中止，不执行原 NPU 安装器的端口接管或 Debian 12 依赖安装。
Nginx 与 etcd 的镜像身份另行锁定于 `component-extras.json`；没有仓库摘要的
既有 etcd 镜像仅支持按镜像 ID 本地复用/导入，不将可变标签当成可信下载来源。

## 验收边界

候选包的真实设备证据单独保存于 `releases/gb10-*-device-validation.json`。
现有平台的探针注册/MQTT、CPU 组件、翼起办、媒体能力和评测具有各自独立的
依赖与验收步骤；仅通过模型网关检查不能替代这些功能的 GB10 验收。
默认不会停止盒子原有 Ollama、MaxKB 或其他业务。
