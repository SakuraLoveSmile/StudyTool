# 使用 GHCR 镜像部署

本方式从公开 GitHub Release 下载 Compose 文件和环境变量模板，然后由 Docker Compose 自动拉取 GHCR 镜像。配置目标为 Linux x64（`linux/amd64`）。Release 部署文件固定镜像 digest，具体版本和验证结果见同 Release 的 `GHCR_MANIFEST.json`、`GHCR_SMOKE.json`。

## 准备服务器

准备安装了 Docker Engine 和 Docker Compose 插件的 Linux x64 服务器。建议起步配置为 2 核、4 GB 内存和至少 40 GB 磁盘。将域名的 DNS A 记录指向服务器，并确保公网可访问 TCP 80 和 443 端口，供 Caddy 申请及续期 HTTPS 证书。

## 下载部署文件

下面在服务器的 root 终端执行：

```bash
mkdir -p /srv/studytool
cd /srv/studytool

RELEASE=https://github.com/SakuraLoveSmile/StudyTool-Releases/releases/download/connected-v0.1.0
curl -fL "$RELEASE/compose.yaml" -o compose.yaml
curl -fL "$RELEASE/ghcr.env.example" -o .env.example
curl -fLO "$RELEASE/GHCR_SHA256SUMS.txt"
cp .env.example ghcr.env.example
sha256sum --ignore-missing -c GHCR_SHA256SUMS.txt
cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

```bash
nano .env
```

将 `DOMAIN` 改成实际域名，只填写主机名，例如 `study.example.com`，不要加 `https://` 或路径。`RAG_DATA_DIR` 必须指向本部署专用的数据目录，不要复用其他 RAG/StudyTool 实例或备份目录。填写问答（`LLM_*`）、向量（`EMBEDDING_*`）和 OCR（`OCR_*`）服务的地址、模型名称及密钥。密钥只放在服务器的 `.env` 中。

## 创建数据目录并启动

```bash
install -d -m 0750 -o 10001 -g 10001 /srv/rag-data
docker compose --env-file .env -f compose.yaml config --quiet
docker compose --env-file .env -f compose.yaml pull
docker compose --env-file .env -f compose.yaml up -d
docker compose --env-file .env -f compose.yaml ps
```

`pull` 会从公开 GHCR 拉取镜像；无需 `docker load`、源码构建或登录 GHCR。首次启动时 Caddy 会申请证书，DNS 与 80/443 端口需已就绪。

## 创建账号并登录

```bash
docker compose --env-file .env -f compose.yaml exec backend \
  python -m app.manage_users create alice
```

按提示设置至少 12 字符的密码，首次登录需修改。浏览器访问 `https://你的域名/library`。Android 和 Windows 客户端的服务地址填写相同 HTTPS origin，例如 `https://study.example.com`，然后使用同一账号登录。

## 边界

当前目标平台为 Linux x64。当前不提供已部署的公共服务。此前 SiliconFlow PaddleOCR-VL 的连通性问题仍未排查；真实模型服务与客户端最终验收需要在部署后另行确认。数据保存在 `RAG_DATA_DIR`，证书保存在 Compose 命名卷；已有离线包部署请在原 Compose 项目目录中升级，并沿用原数据目录、账号和证书卷，先备份再升级。
