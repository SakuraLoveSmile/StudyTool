# RAG 上线执行记录

目标：统一 HTTPS 网站，最多 10 人人工开户，独立资料及学习记录，空库上线。

| 任务 | 状态 | 结果与缺口 |
| --- | --- | --- |
| T1 登录后仅访问自己的资料、任务和学习记录 | 待体验 | 账号、会话、分库、后台隔离与浏览器登录/跨标签回归通过；尚未发布 |
| T2 实际导入、审核、背诵及练习 | 实现中 | 文字 PDF/DOCX 真实导入成功；LLM/Embedding 探测通过；OCR 模型 HTTP 404 阻塞扫描件 |
| T3 HTTPS、持久化及恢复 | 实现中 | Compose、Caddy、备份恢复已验证；完整镜像构建被 Docker 空间不足阻塞；服务器及域名待提供 |

## 开始前证据

- 2026-09-12，本目录不是 Git 仓库；未覆盖本机 `backend/data`。
- 源码快照：`/tmp/rag-source-20260912-124903.tar.gz`，排除密钥、数据、日志、依赖与构建产物。
- 本次计划阶段实测：Python 3.10.10，隔离 DATA_DIR、DEMO_MODE=false，107 passed / 5 warnings；前端 `npm run build` 通过。
- 原系统没有账号隔离及生产部署。以上旧测试不能证明新的多人实现通过。

## 执行约束

- 默认 Linux Docker Compose；真实服务器、域名、资源、已有代理需核实，未核实不发布。
- 不迁移本机资料，不开放注册，不增加共享资料、支付或个人模型配置。
- 发布门禁：A/B 越权测试、真实学习流程、HTTPS/重启、独立备份恢复全部通过。
- 状态只用未开始、实现中、待体验、已验收；已验收仅由用户确认。
- 两次不同依据的尝试仍阻塞则记录证据，继续独立工作，不降低门禁。

## 本次实现

- T1：新增 `app/tenancy.py`、`auth.py`、`access.py`、账号命令和 auth 路由。PUBLIC_MODE 缺少身份时拒绝业务存储；账号库在全局根目录，业务库/文件在用户 UUID 目录。首次改密、会话撤销、CSRF、限流、禁用账号及安全开户重试已实现。
- T1：TaskWorker facade 分派到各用户执行器；后台生命周期带租户上下文，禁用时取消并等待后台和同步长任务；Embedding 缓存按用户区分。
- T1：前端登录/改密/退出、会话代次及取消、任务清理、跨标签 BroadcastChannel 通知；浏览器存储异常不阻止安全退出。
- T2：三个同步生成/批改入口支持 Idempotency-Key、持久化结果和断连重取；正式模式不再对空模型题目伪造成功。保留既有难度、评分依据、列表和返回行为，新增防退化断言。
- T3：固定基础镜像摘要和依赖哈希；非 root、单 worker、独立网络/数据、Caddy HTTPS及SPA；停机备份、7份保留、每日systemd定时和独立恢复验证。部署步骤见 `deploy/README.md`。

## 本次验证证据（2026-09-12，macOS ARM64，未提交工作目录）

- 前端：`cd frontend && npm run build` 通过，类型检查及 Vite 构建完成。
- 最终后端回归：**137 passed / 272 warnings，21.79秒**（Python 3.12.14）；警告为SWIG、datetime/Starlette与tarfile弃用提示。完整输出 `artifacts/release/backend-tests.txt`。
- 生产依赖：在 `/tmp/rag-release-python312` 建立 Python 3.12.14 环境，通过 `uv pip sync --require-hashes backend/requirements.lock` 安装。测试完全隔离 DATA_DIR、模型密钥和宿主代理；模型专项用例自行注入测试配置/传输。
- T1 HTTP/数据库：A/B 文件与切片、任务/事件/重试、学习揭示与评分、同日相同幂等ID、禁用/会话撤销/开户失败重试均有测试。真实租户执行器测试覆盖重启取消、禁用A而B继续；10个会话读取和2人同时上传使用ASGI传输，证明本机HTTP/队列隔离，不证明公网容量。
- T1 浏览器：Chrome headless，两个独立上下文完成登录/首次改密、章节隔离、今日学习揭示/评分、刷新及退出再登录；同一浏览器两标签 A→B 切换后旧标签清空并重认证；localStorage SecurityError 不阻止退出。`artifacts/release/results.json` 为跨标签专项结果，截图为合成学习资料。
- 浏览器使用临时自签名证书并忽略证书校验，只证明 HTTPS 路由/Cookie，不证明真实域名证书；临时服务已停止。保存的浏览器脚本用于审计，需要原隔离夹具/临时入口，不是生产初始化脚本。
- 真实服务：LLM JSON 探测通过（1.89秒），Embedding 探测通过（0.23秒）。OCR 模型沿用现有配置，以非敏感测试图片请求返回 HTTP 404；未修改本机 `.env`，未擅自切换其他模型。
- 真实导入：在临时账号/数据库，通过 HTTP 上传合成文字PDF和DOCX，后台均 `completed`、各1个切片、embedding `completed`。不包含扫描PDF；生成/审核/练习的完整真实AI浏览器链路仍未验收。
- 备份：合成SQLite WAL及上传文件独立恢复通过；已测试覆盖拒绝、路径穿越、重复manifest、符号链接及错误哈希拒绝。
- Caddy：实际运行 `caddy:2.10.2-alpine`（UID10001），配置校验成功，内部health返回200，HTTPS `/today` 返回200；HTTP返回308，Location为 `https://localhost/today`，未错误带出8443。该临时容器已移除。
- Docker完整构建：`docker compose build` 在前端 npm ci 阶段因 `ENOSPC` 失败，后端构建被取消；Docker文件系统7.8GiB、可用0，宿主约70GiB可用。未删除其他项目镜像/卷/缓存，未把部分构建当成成功。最终镜像须在有空间的Docker环境重建，并验证真实绑定目录、代理客户端IP和容器重启。

## 下一步与开放门禁

1. 提供服务器连接入口、实际域名及现有反向代理信息；使用独立 `/srv/rag-data` 空目录，不迁移本机资料。
2. 配置可用的视觉OCR模型/端点（密钥只在服务器环境配置），重跑扫描件及完整真实AI学习流程。
3. 在有可用空间的 Docker 环境构建最终镜像，执行部署说明的双账号、证书、持久化重启及备份恢复检查。
4. 以上完成后才开放首批账号。目前未部署到公网，不标记已验收。
