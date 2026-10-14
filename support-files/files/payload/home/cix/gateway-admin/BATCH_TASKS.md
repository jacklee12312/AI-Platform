# OCR / ASR 批量任务

管理台 `/batches` 提供多文件提交、后台队列、批次与文件进度、取消、文本预览和 ZIP 下载。
OCR 使用 `glm-ocr-0.9b`，ASR 使用 `sensevoice`。所有推理都经过现有网关，使用独立管理身份，遵守全局、模型和资源池并发限制；不使用客户 API Key，不占用客户 Key 的每日额度。

## 处理与取消

- 每类任务一个处理器，OCR 与 ASR 可同时运行；文件解码与转码共用一个处理槽。Uvicorn 明确配置单进程，不能增加 worker 或共享同一数据卷运行多个管理台副本。
- 文件先上传到草稿，全部上传成功后再提交。文件 ID 和任务 ID 都是客户端 UUID；重试同一上传或提交不会新增重复文件或批次。同一文件 ID 的内容不可覆盖。
- 浏览器关闭不影响已提交任务。上传尚未完成时关闭页面会留下草稿；可提交已上传文件，或删除草稿重新选择文件。
- 取消立即停止排队项；已发出的单次推理不强制中断，完成后保存结果，再停止后续页面和文件。取消中的任务不能删除，须等当前请求收尾。
- 网关返回 429 或模型未激活 / 不可用时，任务等待资源后再尝试；已保存的 PDF 页面不重复处理。资源持续不可用时会一直等待，可手动取消。
- 单个坏文件失败不阻止其他文件。未知网络结果和正在处理时发生的管理服务重启标为“执行中断”，不自动重复推理；未开始的文件继续排队。
- PDF 以不超过 2048 像素长边渲染。OCR 输出达到 2048 Token 上限时显示“可能不完整”的警告，警告也包含在导出文件中。

## 限制与存储

| 项目 | 限制 |
| --- | --- |
| 每批文件 / 原文件大小 | 50 个 / 250 MB |
| 图片 | PNG、JPEG、WebP；单张 ≤10 MB、2000 万像素 |
| PDF | 单份 ≤40 MB、50 页；每批 OCR 合计 ≤200 页 |
| 音频 | WAV、MP3、M4A、FLAC、OGG、AAC；单份 ≤50 MB、30 分钟 |
| 待处理批次 / 历史批次 | 20 / 200 |
| 全部原文件存储 | 2 GB；上传前至少预留 150 MB 磁盘空间 |
| 识别结果 JSON | 单文件 1 MB / 每批 8 MB / 所有任务 128 MB |

结果超过限制时保留此前已保存的部分内容并报错，不静默丢弃。原文件与结果不会自动过期；下载后可在页面删除任务，释放原文件空间。SQLite 可复用已删除结果占用的页，数据库文件不一定立即缩小。

数据位于 admin-data 卷：`/data/batch-jobs.db`、`/data/batches/<UUID>/*.source`。目录和文件均使用验证后的 UUID，原始文件名仅作为元数据。SQLite WAL 保存状态与每页结果。转码子进程只处理本地文件并限制执行时间（120 秒），不接受音频播放列表或任意远程 URL。

重启时清理未完成上传和临时转码文件；已进入推理的文件标为中断，已排队文件保留。备份应在没有上传、运行任务时暂停管理服务，将数据库与 `batches/` 一起备份，或用 SQLite backup API 获取一致数据库快照后配合稳定的文件目录备份。不要仅复制正在写入的 `.db` 而遗漏 WAL。

## HTTP API

所有接口要求管理员会话，写接口另需 `X-CSRF-Token`；没有匿名任务下载入口。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/batches` | 历史、队列状态、原文件用量 |
| POST | `/api/batches` | 创建草稿：`id`, `name`, `kind: ocr/asr`, `language` |
| PUT | `/api/batches/{id}/files/{file_id}?filename=...&size=...` | 原始文件字节流，`Content-Type: application/octet-stream` |
| POST | `/api/batches/{id}/submit` | 将草稿加入队列；重复提交幂等 |
| GET | `/api/batches/{id}` | 批次进度、逐文件阶段、页数、错误和 Request ID |
| POST | `/api/batches/{id}/cancel` | 取消未开始的工作，保留已完成结果 |
| GET | `/api/batches/{id}/files/{file_id}/result` | 单文件 JSON 和文本预览 |
| GET | `/api/batches/{id}/results.zip` | 下载当前结果快照，包含部分结果 |
| DELETE | `/api/batches/{id}` | 删除草稿或已结束任务及原文件、结果 |

ZIP 包含 `manifest.json`（状态、页进度、错误、警告、Request ID）、UTF-8 BOM `summary.csv`、逐文件 TXT 和 JSON。重名文件以前缀序号区分；CSV 对公式开头的文本进行转义。`complete` 表示全部文件执行成功，`has_warnings` 单独标明质量警告。

任务创建、提交、取消、删除、下载和逐文件完成写入现有管理员审计；文件推理的 Request ID 可用于查找网关请求日志。批次 ID 不进入设备操作代理的任务追踪器。

## 验证

在已安装原有管理台测试依赖和 `requirements-batch.txt` 的环境中：

```text
python -m unittest discover -s gateway-admin/tests
```

新增 `test_batches.py` 覆盖上传幂等与边界、身份 / CSRF、并发认领、取消、PDF 页面恢复、服务重启、资源等待、未知网络结果、损坏文件隔离、转码与结果包。

浏览器验收使用 `serve_audit_fixture.py` 的本地推理替身，媒体转换、队列、HTTP、文件和数据库均为真实实现。构建前端后启动 fixture，再设置 `MODELDECK_AUDIT_FIXTURE=1`、`MODELDECK_BASE_URL=http://127.0.0.1:18780`，运行 `npx playwright test tests-e2e/batch-local.spec.ts --workers=1`。fixture 严禁部署到盒子。

`batch-live-readonly.spec.ts` 用于核查盒子已有验收任务的 UI 与下载；需要显式设置 `MODELDECK_LIVE_READONLY=1`、盒子地址和测试登录密码。真实推理验收记录见 [2026-09-07 报告](../docs/reports/OCR_ASR批量任务验收_2026-09-07.md)。
