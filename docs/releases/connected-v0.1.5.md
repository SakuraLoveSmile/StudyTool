# StudyTool 联网服务 0.1.5

修复 0.1.4 切换百度 OCR 后公网服务无法启动的问题。此前 Compose 已允许设置 `OCR_MODE=paddleocr_api`，但后端启动校验仍只允许视觉接口，导致报错 `Public mode requires real LLM, vision OCR and remote embeddings` 并反复重启。

- 公网模式正确接受 `vision_llm` 和 `paddleocr_api` 两种正式 OCR。
- 演示模式、非法 OCR 模式和非远程向量模式仍被拒绝；配置错误会分别指明对应配置项。
- 发布检查实际依次启动视觉接口模式与百度模式，并检查百度模式下的 HTTPS 登录、所选 OCR 服务及账号和教材持久化，避免只解析 Compose 却漏过启动失败。

## 已配置百度的服务器更新

保留现有 `.env` 中的百度 jobs API 地址、Token 和 `OCR_MODE=paddleocr_api`，保留原 Compose 的 `OCR_MODE: ${OCR_MODE:-vision_llm}`，执行：

```bash
docker compose --env-file .env pull
docker compose --env-file .env up -d --force-recreate app
docker compose --env-file .env logs --since=2m --tail=60 app
```

默认 `latest` 已更新；固定版本部署请设 `APP_IMAGE=ghcr.io/sakuralovesmile/studytool:0.1.5`。启动日志应出现 `Application startup complete`，刷新设置页核对“百度 PaddleOCR”。更新不会调用 OCR 或重新处理已有资料；需要时手动测试识别。

## 验证

先以真实 lifespan 测试复现百度启动失败，修复后 49 项启动、公网隔离与静态服务检查通过。本地从源码重建 Linux amd64 镜像，在隔离 Docker 网络及临时数据卷内验证两种 OCR 模式启动、HTTPS 登录、账号与教材持久化和资源清理。测试未调用百度或其他真实付费模型，未部署用户服务器。
