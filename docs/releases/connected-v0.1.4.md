# StudyTool 联网服务 0.1.4

支持通过 `.env` 切回百度 PaddleOCR，修复部署 Compose 将 OCR 固定为视觉接口、覆盖 `OCR_MODE` 的问题。

- 新部署模板提供百度 jobs API 地址与 Token 配置项；现有通用 OCR 接入保留。
- 未设置 `OCR_MODE` 的已有部署继续使用原视觉接口，不会仅因拉取新镜像切换服务商。
- LLM、向量模型、现有原文件、解析正文及向量索引不变，不自动重新识别资料。

## 已有部署切换

保留原 Compose 的端口和数据挂载，将 `environment` 下固定的 `OCR_MODE: vision_llm` 改为：

```yaml
OCR_MODE: ${OCR_MODE:-vision_llm}
```

在原 `.env` 中设置：

```dotenv
OCR_MODE=paddleocr_api
BAIDU_OCR_API_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
BAIDU_OCR_TOKEN=在服务器本地填写百度Token
```

Token 在 https://aistudio.baidu.com/paddleocr 获取；此控制台页面地址不能当作 API 地址，硅基流动密钥也不能用于百度。原通用 OCR 配置可保留，百度模式不使用它们。

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env pull
docker compose --env-file .env up -d --force-recreate app
```

仅 `pull` 无法替换服务器已有的 Compose 文件。刷新设置页确认服务为百度，再按需测试识别；测试将使用百度额度。配置状态检查与详细说明见附件 `NGINX.md`。

## 验证

77 项隔离 OCR 回归通过，覆盖百度 jobs 协议、所选服务的连通性入口及通用视觉接口。使用 Docker Compose 实际解析验证百度模式、未设置模式的旧部署及显式视觉模式；未调用真实百度或硅基流动服务，未修改用户服务器。
