# StudyTool 联网服务部署

## 推荐：已有宿主机 Nginx 部署

优先参考 [NGINX.md](NGINX.md)：从联网版 Release 下载 `compose.yaml` 和 `app.env.example`，复制环境变量模板为 `.env`，再由 Docker Compose 拉取默认 `latest` 单应用 GHCR 镜像。固定版本时通过 `APP_IMAGE` 指定版本标签。升级旧的 backend/frontend+Caddy 部署时，先备份并停止旧容器，沿用原 `RAG_DATA_DIR`；不删除旧数据或证书卷。旧版双容器说明与离线镜像流程保留在下文供兼容使用。

## 旧版双镜像 GHCR 部署

旧版双镜像 Caddy 部署说明见 [GHCR.md](GHCR.md)，仅用于兼容已有实例。当前单应用部署附件及其更新、回滚步骤见 [NGINX.md](NGINX.md)。离线镜像部署方式保留如下。

## 服务器配置与镜像交付

少量用户起步建议 Linux **2 核、4 GB 内存、40～60 GB 磁盘**，不需要 GPU。LLM、OCR、向量模型通过远程接口运行；本机负责解析、检索、账号和资料存储。扫描 PDF 解析时会有内存峰值，先用少量页面验证；资料、日志、备份增加后需扩容。这是起步建议，尚未进行并发容量测试。若在服务器上编译镜像，需要额外内存和磁盘；推荐加载已构建镜像。

在有 Docker 的开发机运行（Python 3.10+，不读取本机 `.env`）：

```sh
python3 deploy/build_bundle.py --platform linux/amd64 --tag connected-candidate
python3 deploy/smoke_images.py --bundle artifacts/connected-delivery/server
```

输出 `artifacts/connected-delivery/server/` 包含前后端镜像 `studytool-images.tar.gz`、Compose、部署脚本、空凭据配置示例、构建信息和 SHA-256 校验。ARM 服务器改为 `--platform linux/arm64`，两种架构不能混用。默认不推送镜像仓库。构建脚本不会覆盖非空输出目录；新一轮请指定新的 `--output`。

`.github/workflows/connected-builds.yml` 同时构建安卓候选 APK、Windows x64 安装包及 Linux x64 服务端镜像包，结果保存在 Actions artifacts。它不发布 Release、不推送注册表、不部署。旧的 Windows 本地版仍使用原独立工作流。

下载并解压服务端 artifact 后，把整个目录复制到 `/srv/rag`，先核对校验和并加载镜像：

```sh
cd /srv/rag
sha256sum -c SHA256SUMS.txt
docker image load -i studytool-images.tar.gz
```

然后按下面步骤创建数据目录、填写配置和人工开户。使用下载的镜像包时，将下面的 `build --pull` 换成 `up -d --no-build --pull never`；镜像包没有业务源码，不能在服务器上执行 build。首次打开安卓/Windows 联网版填写相同 HTTPS origin（例如 `https://learn.example.com`），不填 `/library`。

目前没有服务器和域名，不能直接将 `learn.example.com` 当作可用地址。DNS、HTTPS 配好并启动服务后，客户端才能登录。

The production data directory must be dedicated and outside this source tree. These commands assume the checkout is `/srv/rag`, Docker Compose is installed, and DNS for `DOMAIN` points at the server. Do not copy `backend/data` into production.

## Install

```sh
sudo install -d -m 0750 -o 10001 -g 10001 /srv/rag-data
sudo install -d -m 0700 /srv/rag-backups
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
$EDITOR deploy/.env
```

Set a real `DOMAIN`, `RAG_DATA_DIR=/srv/rag-data`, and the remote model credentials in `deploy/.env`. Keep `LLM_API_KEY` and other secrets only in that file. Check the resolved configuration before starting:

```sh
docker compose --env-file deploy/.env config --quiet
docker compose --env-file deploy/.env build --pull
docker compose --env-file deploy/.env up -d
docker compose --env-file deploy/.env ps
```

使用已加载镜像包的完整启动命令为：

```sh
docker compose --env-file deploy/.env config --quiet
docker compose --env-file deploy/.env up -d --no-build --pull never
docker compose --env-file deploy/.env ps
```

The backend is reachable only on the Compose network. Caddy serves HTTP on port 80 and HTTPS on port 443, obtains certificates for `DOMAIN`, proxies `/api`, and serves the built SPA. Verify `/healthz` locally through the Caddy container and verify HTTPS using the real domain after DNS has propagated.

## Backups and restore drill

Backups stop the backend, archive the account database plus each user's database and uploads, omit logs/secrets, write a SHA-256 manifest, and retain the newest seven archives. Configure a backup directory outside `/srv/rag`:

```sh
export ENV_FILE=/srv/rag/deploy/.env
export RAG_BACKUP_DIR=/srv/rag-backups
deploy/backup.sh
```

Install the daily systemd job:

```sh
sudo install -m 0644 deploy/rag-backup.service /etc/systemd/system/rag-backup.service
sudo install -m 0644 deploy/rag-backup.timer /etc/systemd/system/rag-backup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now rag-backup.timer
systemctl list-timers rag-backup.timer
```

Restore always targets a new directory and validates paths, hashes, SQLite integrity, and foreign keys before moving it into place:

```sh
export RAG_BACKUP_ARCHIVE=/srv/rag-backups/rag-YYYYMMDDTHHMMSSZ.tar.gz
export RAG_RESTORE_DIR=/srv/rag-restore-YYYYMMDD
deploy/restore.sh
sudo chown -R 10001:10001 "$RAG_RESTORE_DIR"
```

After inspection, stop the stack, move the current data directory aside for retention, set `RAG_DATA_DIR` to the restored directory in `deploy/.env`, and start the pinned release. Never overwrite the only current data directory.

## Rollback

Keep the failed release data directory intact. Set `BACKEND_IMAGE` and `FRONTEND_IMAGE` back to the previously verified tags, point `RAG_DATA_DIR` at a separately restored directory, validate Compose, then recreate the stack:

```sh
docker compose --env-file deploy/.env config --quiet
docker compose --env-file deploy/.env up -d --force-recreate
docker compose --env-file deploy/.env ps
```

The deployment is not considered open until two real browser sessions pass the login, isolation, learning, restart, and backup-restore checks.

## 人工开户与账号管理

首次启动是空库。账号命令使用服务器端配置，密码通过终端交互输入，不要写进命令参数或日志：

```sh
docker compose --env-file deploy/.env exec backend python -m app.manage_users create alice
docker compose --env-file deploy/.env exec backend python -m app.manage_users list
docker compose --env-file deploy/.env exec backend python -m app.manage_users reset-password alice
docker compose --env-file deploy/.env exec backend python -m app.manage_users disable alice
```

初始密码至少 12 字符；首次登录必须修改。开户失败保持禁用，修复环境后对同一用户名重跑 create。已经成功开户再禁用的用户不会被 create 或 reset-password 自动启用。后台在约 1 秒内发现新账号或禁用状态。首次开放限定最多 10 位人工确认用户。

## 反馈组件登记（上线时）

前端内置的反馈组件（`@feedback/web`）是否出现，由**构建期**变量决定。下面两个动作必须配套，缺少任意一个都会静默失效：

**A. 构建镜像时传入构建参数。** Vite 在打包时把这两个值静态内联进产物，容器运行时再设同名环境变量**无效**：

```sh
VITE_FEEDBACK_API_BASE=https://feedback.xn--fhqths51enha.cn \
VITE_FEEDBACK_APP_ID=studytool \
docker compose --env-file deploy/.env build frontend
```

`compose.yaml` 已把这两个变量映射为 `frontend.build.args`，默认值为空 —— **默认构建出的产物不含反馈入口**，这是刻意设计，避免误发布未配置产物。构建后可用 `docker compose --env-file deploy/.env config` 确认 `build.args` 已按预期填充。

**B. 在反馈服务后台登记上线后的真实来源。** 必须精确匹配浏览器实际 origin，**不要使用通配符**：

- `appId`：`studytool`
- 名称：学习助手
- 允许来源：`https://$DOMAIN`（即上线后的 HTTPS origin）

若还要保留本机调试，`http://127.0.0.1:5174` 必须**单独再登记一条**；登记一个不会覆盖另一个。

两者的失效方式不对称：只做 A 不做 B，灵感球会出现但提交被服务端按来源拒绝；只做 B 不做 A，产物里根本没有反馈入口（未配置时组件包也不会被加载）。

截图遮挡由反馈组件自身完成：带 `data-feedback-capture-mask` 的元素（当前为导航栏账号区）在截图中被不透明覆盖，`input[type="password"]` 自动受保护。反馈原话与截图归档在反馈服务侧（最终落到 Kaneo），**本仓库不保存反馈数据**。

## 上线前核查

1. 核对 Linux 架构、内存、磁盘空间及 80/443 端口使用者；不要覆盖其他站点。构建需要额外空间，镜像和缓存大小不能当作可用容量。
2. 核对 DOMAIN 的 DNS 指向当前主机，防火墙只公开 80/443；不要给 backend 添加 ports，也不要把不可信服务加入 rag_private 网络。后端信任该专属网络中的代理头，Caddy 负责重建客户端 IP。
3. 确认非空 LLM/Embedding 密钥和可用视觉模型。OCR 默认复用 LLM 地址和密钥，但模型必须支持图片；不能使用 mock 通过真实验收。
4. 两浏览器分别使用两个账号验证资料、任务、评分隔离；检查 HTTP 跳转、HTTPS 证书及 `/today` 深链接刷新。
5. 先执行独立恢复演练，再开通首批用户。实际结果写入项目 `RELEASE.md`，历史记录不等于本次通过。
6. 按「反馈组件登记（上线时）」完成 A（构建参数）与 B（后台登记生产 origin）；上线域名与登记的允许来源必须一致，否则球会出现但提交被拒。

账号目录及后端镜像使用 UID/GID 10001。系统备份服务以 root 运行，以便停止 Docker 服务容器并读取该专属目录；只停止本 Compose 项目的 backend。

依赖锁更新命令（需安装 uv）：

```sh
uv pip compile backend/requirements.release.in --python-version 3.12 --generate-hashes -o backend/requirements.lock
```

改动锁后，须在 Python 3.12 隔离环境及目标镜像重新跑测试。新单应用部署以 `APP_IMAGE` 选择固定版本或 digest；旧双镜像部署分别记录 `BACKEND_IMAGE` / `FRONTEND_IMAGE` 标签。
