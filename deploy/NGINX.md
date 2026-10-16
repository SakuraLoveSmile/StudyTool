# Docker Compose 部署（已有宿主机 Nginx）

推荐在已安装 Nginx 的 Linux x64 服务器上运行一个 `app` 容器。容器同时提供 Vue 页面和 API，只监听宿主机 `127.0.0.1:8088`；现有宿主机 Nginx 继续负责 HTTPS。镜像从 GHCR 拉取，不需要 Dockerfile、源码构建或 GHCR 登录。

## 下载并配置

从公开的 [StudyTool-Releases](https://github.com/SakuraLoveSmile/StudyTool-Releases/releases) 选择联网版 Release，在附件中下载 `compose.yaml` 和 `app.env.example`，放进服务器部署目录。也可以在服务器直接下载，例如 `connected-v0.1.0`：

```bash
mkdir -p /srv/studytool
cd /srv/studytool
RELEASE=https://github.com/SakuraLoveSmile/StudyTool-Releases/releases/download/connected-v0.1.0
curl -fL "$RELEASE/compose.yaml" -o compose.yaml
curl -fL "$RELEASE/app.env.example" -o app.env.example
cp app.env.example .env
chmod 600 .env
nano .env
```

设置 `PUBLIC_ORIGIN` 为完整 HTTPS origin，例如 `https://learn.example.com`，不要带路径或结尾斜杠。`RAG_DATA_DIR` 必须指向本实例专属目录。填写 LLM、向量模型和 OCR 的服务地址、模型名及密钥。`DOMAIN` 变量已不再使用。

章节资料通过后台任务生成，提交后可在“任务”中查看进度、重试与执行日志，刷新页面不会中断。`LLM_TIMEOUT=180` 控制每次模型请求的等待时限；已有部署若仍使用默认 60 秒，可在原 `.env` 添加这一项，再执行 `docker compose --env-file .env up -d --force-recreate app`。它与 Nginx 的 `proxy_read_timeout` 独立，延长等待不能保证模型一定成功。OCR 连通性测试等同步请求仍需要下面的代理超时配置。

## 切换到百度 PaddleOCR

在原 `.env` 中添加或修改以下三项，保留其他模型、端口和数据目录配置：

```dotenv
OCR_MODE=paddleocr_api
BAIDU_OCR_API_URL=https://paddleocr.aistudio-app.com/api/v2/ocr/jobs
BAIDU_OCR_TOKEN=在服务器本地填写百度控制台的Token
```

Token 从 [百度 PaddleOCR 控制台](https://aistudio.baidu.com/paddleocr) 获取；控制台页面地址不能直接填作 API 地址。Token 属于百度，与硅基流动 API Key 不通用。原 `OCR_BASE_URL`、`OCR_API_KEY` 和 `OCR_VISION_MODEL` 可保留，`paddleocr_api` 模式不会使用它们。LLM 与向量模型保持原配置。

**旧 Compose 还需修改一行**：如果 `environment` 下写的是 `OCR_MODE: vision_llm`，请替换成 `OCR_MODE: ${OCR_MODE:-vision_llm}`，或改用本次 Release 的 `compose.yaml`；保留原端口、挂载和其余配置。Compose 的固定 `environment` 值会覆盖 `.env`。未设置 `OCR_MODE` 的旧部署继续使用视觉接口，避免仅更新镜像就无意切换供应商。

```bash
docker compose --env-file .env config --quiet
docker compose --env-file .env up -d --force-recreate app
docker compose --env-file .env exec -T app python -c 'from app.config import settings; from app.services.parser.ocr_service import OCRService; s=OCRService.configuration_status(); print("OCR模式:",s["mode"]); print("服务:",s["provider"]); print("配置完整:",s["configured"]); print("接口:",s["base_url"])'
```

应显示 `paddleocr_api`、`百度 PaddleOCR` 和 `配置完整: True`，命令不输出 Token。随后刷新网页，在设置页测试 OCR；测试会提交合成图片到百度并消耗相应额度。现有资料不会自动重新 OCR；需要时手动重试识别失败的任务。

## 数据目录

创建全新的数据目录时：

```bash
sudo install -d -m 0750 -o 10001 -g 10001 /srv/rag-data
```

如果该目录已有资料，保留现有文件和权限，不要重新创建、清空或递归改权限；确认 `.env` 中的 `RAG_DATA_DIR` 指向原目录。`.env` 只保存在服务器并限制为当前管理员可读。

## 启动容器

```bash
docker compose --env-file .env -f compose.yaml config --quiet
docker compose --env-file .env -f compose.yaml pull
docker compose --env-file .env -f compose.yaml up -d
docker compose --env-file .env -f compose.yaml ps
```

Compose 默认拉取 `ghcr.io/sakuralovesmile/studytool:latest`。镜像只提供 `linux/amd64`。要固定版本，可在 `.env` 中设置 `APP_IMAGE=ghcr.io/sakuralovesmile/studytool:0.1.0`；`APP_IMAGE` 会覆盖 Compose 默认值。本机健康与日志可这样查看：

```bash
curl -fsS http://127.0.0.1:8088/health
docker compose --env-file .env -f compose.yaml logs --tail=100 app
```

## 配置现有 Nginx

将下面的 `location` 放在该域名已有的 HTTPS `server` 块中，保留你现有的 `listen 443 ssl`、证书和 HTTP 到 HTTPS 跳转配置。此示例假设 Nginx 在宿主机运行，Docker 将应用端口绑定在 loopback：

```nginx
client_max_body_size 105m;

location / {
    proxy_pass http://127.0.0.1:8088;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 600s;
    proxy_buffering off;
}
```

`X-Forwarded-For` 使用 `$remote_addr` 覆盖客户端传入的同名头，避免转发伪造的客户端地址。检查并加载配置：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

若 Nginx 本身运行在容器中，这里的 `127.0.0.1` 不会指向宿主机上的应用容器；容器网络与上游地址需按实际 Nginx 部署方式另行配置。

## 创建账号

```bash
docker compose --env-file .env -f compose.yaml exec app \
  python -m app.manage_users create alice
```

按提示设置密码。浏览器访问 `https://你的域名/library`；Android 和 Windows 客户端填写同一个 HTTPS origin，例如 `https://learn.example.com`。

## 日常更新与回滚

`latest` 跟随发布流程更新。更新前记录当前镜像 digest，便于精确回滚：

```bash
docker image inspect ghcr.io/sakuralovesmile/studytool:latest \
  --format '{{index .RepoDigests 0}}'
```

再拉取并重建容器：

```bash
docker compose --env-file .env -f compose.yaml pull
docker compose --env-file .env -f compose.yaml up -d
docker compose --env-file .env -f compose.yaml ps
```

若需固定版本，在 `.env` 设置 `APP_IMAGE=ghcr.io/sakuralovesmile/studytool:0.1.0`（替换为需要的版本标签），再运行 `pull` 和 `up -d`。回滚时把 `APP_IMAGE` 改为更新前记录的 `ghcr.io/sakuralovesmile/studytool@sha256:...` digest 或旧版本标签，执行同样的 `pull`、`up -d`。先确认旧版本与当前数据格式兼容；不要删除或替换 `RAG_DATA_DIR`。升级前备份数据，回滚镜像不会自动回滚数据库内容。

## 从旧双容器部署升级

先按现有备份流程备份 `/data` 对应的 `RAG_DATA_DIR`，并保留当前 `.env`、Compose 文件和证书卷。不要改用新目录，也不要删除旧数据或证书。为后续核对保留旧 Compose 文件，再用它停止旧前后端，避免旧 Caddy 继续占用 80/443：

```bash
cp compose.yaml compose.caddy.yaml
docker compose --env-file .env -f compose.caddy.yaml stop frontend backend
```

下载新文件后，以新 `compose.yaml` 覆盖部署入口；保留原 `.env` 中的模型凭据，并按新模板设置 `PUBLIC_ORIGIN`、`RAG_DATA_DIR`、LLM、Embedding 和 OCR 参数。`DOMAIN` 不再使用。确认新配置仍绑定旧部署的同一个 `RAG_DATA_DIR`，然后执行上面的 `config`、`pull` 和 `up -d` 命令。账号和资料沿用该数据目录；本步骤不会删除旧证书卷或旧数据目录。

新反代链路、公网 DNS/TLS、真实模型与 OCR 服务以及 Android/Windows 设备流程，均须在目标服务器上实际验证后才能视为完成。
