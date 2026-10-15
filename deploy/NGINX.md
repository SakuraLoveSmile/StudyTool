# Docker Compose 部署（已有宿主机 Nginx）

推荐在已安装 Nginx 的 Linux x64 服务器上运行一个 `app` 容器。容器同时提供 Vue 页面和 API，只监听宿主机 `127.0.0.1:8088`；现有宿主机 Nginx 继续负责 HTTPS。镜像从 GHCR 拉取，不需要 Dockerfile、源码构建或 GHCR 登录。

## 下载并配置

从公开的 [联网版 Release](https://github.com/SakuraLoveSmile/StudyTool-Releases/releases/tag/connected-v0.1.0) 下载 `compose.app.yaml` 和 `app.env.example`，放进服务器部署目录，例如 `/srv/studytool`：

```bash
mkdir -p /srv/studytool
cd /srv/studytool
cp compose.app.yaml compose.yaml
cp app.env.example .env
chmod 600 .env
nano .env
```

设置 `PUBLIC_ORIGIN` 为完整 HTTPS origin，例如 `https://learn.example.com`，不要带路径或结尾斜杠。`RAG_DATA_DIR` 必须指向本实例专属目录。填写 LLM、向量模型和 OCR 的服务地址、模型名及密钥。`DOMAIN` 变量已不再使用。

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

Compose 会拉取 `ghcr.io/sakuralovesmile/studytool-connected-app:0.1.0`。镜像只提供 `linux/amd64`。本机健康与日志可这样查看：

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
    proxy_set_header Host $host;
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

## 从旧双容器部署升级

先按现有备份流程备份 `/data` 对应的 `RAG_DATA_DIR`，并保留当前 `.env`、Compose 文件和证书卷。不要改用新目录，也不要删除旧数据或证书。为后续核对保留旧 Compose 文件，再用它停止旧前后端，避免旧 Caddy 继续占用 80/443：

```bash
cp compose.yaml compose.caddy.yaml
docker compose --env-file .env -f compose.caddy.yaml stop frontend backend
```

下载新文件后，以新 `compose.yaml` 覆盖部署入口；保留原 `.env` 中的模型凭据，并按新模板设置 `PUBLIC_ORIGIN`、`RAG_DATA_DIR`、LLM、Embedding 和 OCR 参数。`DOMAIN` 不再使用。确认新配置仍绑定旧部署的同一个 `RAG_DATA_DIR`，然后执行上面的 `config`、`pull` 和 `up -d` 命令。账号和资料沿用该数据目录；本步骤不会删除旧证书卷或旧数据目录。

新反代链路、公网 DNS/TLS、真实模型与 OCR 服务以及 Android/Windows 设备流程，均须在目标服务器上实际验证后才能视为完成。
