# Ballot Reader

`ballot-reader` 是民主测评票的常驻 CPU 识别服务。它不使用 LQ50/NPU，也不切换当前
大模型模式。服务先纠正扫描方向和小角度倾斜，再定位表格、解析 6 行 × 6 维度 × 4
等级的 144 个候选格，最后按每组四选一规则输出结构化结果和低置信度复核项。

当前盒子构建复用已部署的 `weknora/ocr:arm64` 基础层，以离线获得 OpenCV、NumPy 和
PDFium；`ballot-reader` 自身不调用该镜像的 OCR 接口或模型，只使用其中的 CPU 图像运行时
和 aiohttp。Compose 会以只读方式挂载服务代码，便于当前盒子在完全离线时部署。

## 部署

```bash
cd /home/cix/ballot-reader
docker tag weknora/ocr:arm64 ballot-reader:0.1.0
docker compose up -d --no-build
```

上面是盒子的离线部署方式；如构建机能访问镜像仓库，也可以运行
`docker compose up -d --build` 生成同名镜像。

容器只加入 `houmo-ai_default` 内网，不发布宿主机端口。网关注册模型
`democratic-eval-v1` 后，通过下面的统一入口调用：

```bash
curl -H "Authorization: Bearer $HOUMO_API_KEY" \
  -H "Content-Type: application/pdf" \
  --data-binary @sample.pdf \
  "http://192.168.133.1:17700/v1/documents/analyze?model=democratic-eval-v1"
```

## 批量验收

将文件放入 `work/input` 后执行：

```bash
docker compose exec -T ballot-reader \
  python -m app.batch /work/input /work/output
```

输出包括：

- `results.json`：完整逐票、逐格结果和诊断参数；
- `details.csv`：每人每维度的等级明细；
- `summary.csv`：按人员、维度、等级聚合计数；
- `review.json`：低置信度或多选/漏选项；
- `annotated/*.jpg`：在原扫描图上框出识别选择，绿色为自动通过、橙色为待复核。

该服务针对当前固定模板。模板行列变化时会明确返回网格数量错误，不会静默套用旧版布局。
