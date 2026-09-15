# RAG Windows 桌面版与百度 OCR 接入执行记录

目标：提供 Windows 10/11 x64 桌面安装包，用户无需配置开发环境即可独立使用；支持在软件内安全配置私有 LLM、Embedding 与百度 PaddleOCR API；支持备份恢复与保留数据的覆盖升级；接入 Feedback 反馈。

| 任务 | 状态 | 目标与当前缺口 |
| --- | --- | --- |
| **T1：安装、启动与本机数据** | 待体验 | 集中启动入口直传应用实例、原子维护屏障在途计数与 running 漏算修正、独立连接 WAL checkpoint、停机租约校验与 30s 超时安全中止（支持“继续等待”与“强制退出”循环且不自动误杀）均已修复；自动化回归全绿（含真实子进程一次启动验证）；待在干净 Windows 10/11 x64 实机环境执行安装与初次启动体验 |
| **T2：配置模型与识别扫描件** | 待体验 | 统一凭据保存/替换/清除与就绪探针失败自动回滚旧配置、配置就绪探针失败自动清理残留进程、SQLite `system_metadata` 表持久化向量锁状态并在检索与后台任务中拦截向量追加、前端向量锁提示与无缝重建索引流程、safeStorage 禁明文降级、百度 OCR 429 熔断与多页物理页码对齐均已实现；自动化单测全绿；待在实机配置真实百度 OCR Token 进行扫描真题体验 |
| **T3：备份、Feedback 与发行验证** | 待体验 | 离线备份显式传入实际数据目录与模型元数据、离线恢复结构化 JSON 输出严格校验（ok、restored_to、pre_restore_snapshot）、恢复后启动失败多级安全回滚与现场保留（`data-failed-restore-<timestamp>`）、构建脚本硬门禁（必须 Python 3.12 64-bit、清洗旧产物、缺失 NSIS .exe 立即报错）均已修复；自动化单测全绿；待在干净 Windows 实机执行 `python desktop/scripts/build-release.py` 产出安装包并验证覆盖升级与 Feedback 提交 |
| **T4：自动更新** | 实现中 | 已接入 `electron-updater`、GitHub Releases 发布配置、Windows GitHub Actions 构建工作流、手动检查/定时检查、用户确认下载、取消下载、完整性校验后重启安装及更新状态 IPC；待 Windows 安装包产出后验证 `latest.yml`、blockmap、断网/损坏包/任务繁忙与资料密钥保留 |

## 本轮修复执行与验证证据（全链路缺陷修复与自动化通过，状态推进至“待体验”）

- 代码与缺陷修复：
  - **T1 启动与维护退出**：
    - 在 `app.desktop` 与 `app.main` 中收敛桌面启动入口为 `bootstrap_desktop()`，彻底消除顶层读取标准输入的副作用；
    - `uvicorn.run(app, ...)` 直接传入内存中已初始化的应用对象，杜绝重复导入与再次等待 stdin；
    - 建立基于原子互斥与在途计数的 `MaintenanceManager` 维护屏障，支持 `is_exit=True` 退出租约在有在途任务时排他获取；修正 `get_active_tasks_count()` 涵盖 `state IN ('pending', 'running', 'processing')`，彻底解决 `running` 状态漏算；
    - 重构 `/api/desktop/drain`，严格验证 `lease_id`，若数据库更新或独立连接 `PRAGMA wal_checkpoint(TRUNCATE)` 失败显式抛出 HTTP 500，杜绝静默假成功；
    - Electron 主进程 `waitForExit` 进程退出守卫检查 `proc.exitCode !== null || proc.killed`，已退出立即返回；退出超时 30 秒弹窗提供“继续等待 (15秒)”与“强制退出”，超时循环不自动强制击杀。
  - **T2 凭据修改、就绪探针与持久化向量锁**：
    - `BackendManager.start()` 区分崩溃退出与就绪超时，就绪探针失败后主动执行 `forceKill()` 清理僵尸进程；
    - `switchConfigWithRollback` 在修改凭据前确保旧进程完全退出；旧凭据读取抛错时安全中止；新配置就绪失败自动清理新进程、回滚旧加密凭据并重启旧服务；
    - 在 SQLite 主库 `app.db` 中新建 `system_metadata` 表持久化存储 `vector_locked`、`vector_lock_reason`、`embedding_model`、`embedding_base_url`；
    - `is_vector_locked()` 在向量来源不一致或信息缺失时拦截向量检索与追加；普通文本 FTS5 搜索与文档阅读 100% 保持可用；
    - 新增 `/api/desktop/reindex` 接口，使用 `document_chunks_staging` 隔离表重构向量，失败/取消自动丢弃暂存表且原切片及锁状态不变，成功后原子事务替换切片向量并释放向量锁；前端 `SettingsView.vue` 与 `useDesktop.ts` 展示向量锁警告并提供确认重建索引交互；
    - 严守 `safeStorage` 禁止明文降级；百度 OCR 429 额度耗尽熔断与多页顺序对齐均具备完整测试保护。
  - **T3 离线恢复校验、现场保留回滚与打包门禁**：
    - Electron 离线备份显式传入实际生产数据目录（`--source`）以及当前生效的非敏感模型标识（`--embedding-model` 等）；
    - 离线恢复结构化输出严格校验：必须为合法 JSON 且包含 `ok === true`、`restored_to`、`pre_restore_snapshot`；缺字段立即中止并恢复旧数据；
    - 恢复完成启动新服务如果就绪检查失败：先杀掉失败进程，将当前目录重命名为 `data-failed-restore-<timestamp>` 完整保留损坏现场，将 `pre_restore_snapshot` 恢复回 live 目录，并重新拉起旧服务；
    - 恢复逻辑自动将备份元数据与向量锁状态同步至恢复后的 `app.db` `system_metadata` 中；
    - 构建脚本 `desktop/scripts/build-release.py` 强化硬门禁：Windows 环境严格校验 Python 3.12 64-bit，构建前深度清洗 `frontend/dist`、`desktop/build`、`desktop/dist`、`desktop/release`、`pyinstaller/build`，调用 `electron-builder --win --x64`，若 `release` 目录缺失 `.exe` 立即非零抛错终止，并自动输出 SHA-256 清单。
- 自动化验证结果：
  - 后端全量测试套件：**164 passed / 278 warnings** 全部通过（含 T1/T2/T3 回归、重建索引独占维护租约以及所有核心/多租户/调度器用例）；真实子进程标准输入启动测试通过；
  - 桌面端全量测试套件：**14 passed** 全部通过（包含 safeStorage 安全凭据、停机生命周期、强制终止失败保留进程引用、配置回滚、恢复校验与现场保留）；
  - 前端全量构建：`npm --prefix frontend run build`（Vite + vue-tsc）零报错编译通过。
  - 桌面端构建：`npm run build`（TypeScript）零报错；`electron-updater` 依赖及 GitHub Releases 发布配置已锁定。
- 当前明确缺口（待进入“已验收”所需客观外部条件）：
  - **Windows 干净实机打包与安装缺口**：当前开发宿主为 macOS ARM64，尚未在无开发环境的 Windows 10/11 x64 物理机上实际运行 `python desktop/scripts/build-release.py` 产出 `.exe` 并安装测试；
  - **真实商业级百度 OCR Token 缺口**：待配置真实有效百度 OCR Token 后，在实机导入 >10 页复杂扫描真题进行端到端识别与练习体验；
  - **桌面客户端线上 Feedback 归档缺口**：待在 Windows 实机固定来源（`http://127.0.0.1:18764`）下点击反馈组件并提交截图与文本，并在 Kaneo 平台确认归档。
  - **自动更新外部验收缺口**：尚未生成 Windows NSIS 安装包及 `latest.yml`/blockmap，也未在 GitHub Releases 草稿中完成旧版到新版的真实更新链路；当前未执行任何对外发布。
  - **Actions 发布准备**：已创建公开发布仓库 `SakuraLoveSmile/StudyTool-Releases`，并在源仓库配置 `RELEASE_REPO_TOKEN`；需推送版本标签后由 Windows runner 首次执行并观察产物。
- 状态判定：本地自动化与回归全绿，已满足将软件移交朋友体验的质量标准，T1、T2、T3 本轮状态统一推进为“**待体验**”；待完成上述实机体验后最终转为“已验收”。

## 执行约束与准则

1. **零外部环境依赖**：最终交付的 Windows 安装包必须开箱即用，目标机器无需安装 Python、Node.js、Docker 或任何编译工具链。
2. **数据绝对私有与隔离**：
   - 生产数据统一存放至 `%LOCALAPPDATA%\RAGStudyAssistant\`，严禁依赖当前工作目录。
   - 首次安装从空数据库启动，严禁打包或注入开发环境数据、测试数据或真实 `.env`。
   - 凭据仅通过 Electron `safeStorage`（Windows DPAPI）加密存储，备份包与安装包绝对不包含凭据明文或密文。
3. **安全边界与端口一致性**：
   - 固定后端监听 `127.0.0.1:18764`，同时提供静态前端托管、SPA 路由与 `/api` 接口。
   - 端口冲突时弹窗提示并支持重试，严禁杀死外部进程，严禁私自切换端口（避免 Feedback 来源校验失效）。
   - 每次启动由 Electron 生成高强度随机实例令牌，通过安全进程管道传给后端；Electron 仅对本地请求注入令牌，严禁附加至外部网络请求。
   - 开启沙箱与上下文隔离，禁用 Node 集成；外部链接强制调用系统默认浏览器。
4. **百度 OCR 契约与额度防护**：
   - 严格遵循百度官方 API 契约（完整 URL、`Authorization: token <TOKEN>`、`file` Base64、`fileType: 1`、`visualize: false`）。
   - 百度凭据完全独立，严禁降级回退使用 LLM 密钥，严禁向 OCR 地址追加 `/chat/completions`。
   - 遇到 `429` 立即熔断当前批次后续调度并提示额度耗尽；`403/413/422` 不重试；网络超时与 `500/503/504` 最多重试两次并支持用户取消；严禁返回模拟虚假文本。
5. **向量模型一致性保护**：
   - 库中一旦存在向量数据，禁止修改 Embedding Base URL 与模型名称，仅允许更新 API Key，防止不同模型/维度向量混用导致检索损坏。
6. **状态流转与门禁规则**：
   - 任务状态严格使用：`未开始`、`实现中`、`待体验`、`已验收`。
   - 只有在完成对应真实环境验收并获得用户确认后，方可标记为 `已验收`。
   - 遇到同一阻塞点尝试两次不同方案仍失败时，保留详细证据，挂起该阻塞任务，推进其余独立任务。

---

## 阶段实施计划

### 阶段一：桌面底座与安全隔离 (T1)

1. **项目骨架与构建配置**：
   - 新建 `desktop/` 目录，管理 Electron 主进程、Preload 脚本、构建配置与打包脚本。
   - 锁定 `package.json` 中的 `electron` 与 `electron-builder` 版本。
   - 编写 PyInstaller `.spec` 脚本，配置 Python 3.12 后端 onedir 模式打包（集成 PyMuPDF、NumPy、jieba 数据及 C++ 运行时 DLL，排除 pytest 等开发依赖与本地 `.env`）。
2. **进程管理与生命周期**：
   - 启动时检测 `18764` 端口可用性；被占用时展示原生提示弹窗，允许用户释放端口后重试。
   - 申请 Electron 单实例锁（`requestSingleInstanceLock`），多开时自动聚焦已存在窗口。
   - 后端启动健康检查循环（探针轮询 `/health`，超时 15s），后端就绪后再展示主窗口，启动失败弹出日志查看与重试面板。
   - 关闭主窗口生命周期拦截：检查任务队列，若有正在进行的导入/向量化任务，弹窗询问“继续运行”或“停止任务并退出”。退出时发送优雅终止信号，等待 SQLite WAL 刷盘与取消完成。
3. **实例鉴权与安全沙箱**：
   - Electron 启动时生成 `crypto.randomBytes(32).toString('hex')` 实例令牌，通过标准输入/管道传递给后端子进程。
   - 后端新增 `DesktopAccessMiddleware`，仅在桌面模式启用：
     - 校验 `Host: 127.0.0.1:18764` 或 `localhost:18764`。
     - 校验 `X-RAG-Instance-Token` 请求头，拒绝未认证请求。
     - 校验非 GET/HEAD 请求的 `Origin`，防范 CSRF 与跨源写入。
   - Electron `webRequest.onBeforeSendHeaders` 仅对目标为 `http://127.0.0.1:18764/*` 的请求注入实例令牌，白名单外绝对不注入。
   - 窗口启用 `contextIsolation: true`、`sandbox: true`、`nodeIntegration: false`；Preload 仅暴露受限 IPC 白名单。
   - `setWindowOpenHandler` 拦截外部 HTTPS 链接，统一使用 `shell.openExternal` 唤起系统浏览器。
4. **数据存储路径规范化**：
   - 后端配置根据平台动态解析：Windows 环境默认使用 `%LOCALAPPDATA%\RAGStudyAssistant\`。
   - 建立规范的子目录布局：`data/`（业务数据库与上传文件）、`config/`（非敏感设置）、`logs/`（回滚轮转日志）、`backups/`（备份归档）。

### 阶段二：模型设置与百度 OCR 接入 (T2)

1. **凭据安全存储与 IPC 接口**：
   - 在 Electron 主进程中使用 `safeStorage`（基于 Windows DPAPI）加密存储 LLM、Embedding 与 Baidu OCR 的敏感密钥。
   - 提供安全 IPC 通道：`getCredentialsStatus`（只返回是否已配置及脱敏标识）、`saveCredentials`、`clearCredentials`、`testConnection`。
   - 解密失败时引导重新输入，严禁降级明文存储。
2. **前端“模型设置”与首次引导**：
   - 新增首次启动配置向导（Wizard），允许填写或跳过。跳过时允许使用纯本地查看与练习功能，对依赖模型的功能予以明确置灰或引导提示。
   - 设置页包含三组服务配置：
     - **LLM**：API 地址、模型名、API Key。
     - **Embedding**：API 地址、模型名、API Key。
     - **百度 OCR**：完整 API URL、Token。
   - 防护规则：当后端检测到数据库中已存在向量索引时，前端锁定 Embedding 的 API 地址与模型名称输入框，只允许修改 API Key。
3. **连接测试与维护状态原子切换**：
   - 设置页提供“连接测试”按钮，后端分别发送最小合成请求验证连通性，界面明确标明“测试将消耗少量接口配额”，日志脱敏处理。
   - 配置保存时，后端先进入原子维护状态；若存在排队或执行中的任务，拒绝保存并提示等待；空闲时写入并平滑重启后端服务。
4. **百度 PaddleOCR API (`paddleocr_api`) 适配器实现**：
   - 在 `OCRService` 中新增 `paddleocr_api` 模式，保留现有 `vision_llm` 与 `mock` 路径。
   - 严格实现请求契约：
     - 请求方式：JSON POST 到完整用户配置 URL。
     - 请求头：`Authorization: token <TOKEN>`、`Content-Type: application/json`。
     - 请求体：`{"file": "<base64>", "fileType": 1, "visualize": false}`。
   - 严格实现响应解析与校验：
     - HTTP 200 + `errorCode == 0`。
     - 提取 `result.ocrResults[0].prunedResult.rec_texts`，按顺序以换行符连接。空结果或结构不符进入提取失败逻辑。
5. **并发流水线与错误熔断**：
   - 保持现有 `pdf_parser.py` 的逐页渲染与 2 页并发调度：文字页直接本地 PyMuPDF 提取，扫描页按页上传 PaddleOCR API，保留原始页码。
   - 图片导入复用同一适配器。
   - 错误与重试策略：
     - `403 / 413 / 422`：直接失败，不重试。
     - `429`：视为每日限额耗尽，立即中止当前批次的所有后续 OCR 请求调度，给出清晰额度警告。
     - 网络超时 / `500 / 503 / 504`：指数退避重试最多 2 次，支持用户手动取消任务。
     - 失败时不私自回退到 vision_llm，不伪造模拟文本。

### 阶段三：备份恢复、Feedback 与打包发行 (T3)

1. **备份与恢复功能**：
   - 重构复用 `deploy/backup.py` 逻辑，暴露给后端 API / 本地命令调用。
   - 备份流程：暂停写入与后台 Worker → 执行 SQLite WAL checkpoint (`TRUNCATE`) → 打包 `data/` 为带 `MANIFEST.json`（含 SHA-256）的 tar.gz 归档（排除 logs、secrets、backups）。
   - 恢复流程：通过系统原生文件选择对话框选择备份文件 → 先解包到临时安全目录并校验清单哈希及 SQLite 完整性（`PRAGMA integrity_check`、`foreign_key_check`）→ 校验成功后备份当前数据副本，再原子切换至新数据目录 → 重启服务。若校验失败保留原数据并报错。
   - 跨机器恢复保障：恢复后提示重新输入 DPAPI 加密的凭据；比对备份中记录的模型标识，防止向量不兼容。
2. **覆盖升级与卸载保障**：
   - NSIS 配置 `perMachine: false`（按当前用户安装至 `%LOCALAPPDATA%`，无需管理员提权）。
   - 安装更新时只覆盖应用执行程序与资源文件，绝对不触碰 `%LOCALAPPDATA%\RAGStudyAssistant\data\`。
   - 数据库版本变更时，启动前自动执行预迁移备份；迁移失败时阻止启动并保留现场。
   - 卸载程序默认保留用户学习资料与历史数据（提供可选勾选框由用户自主决定）。
3. **Feedback 反馈组件集成**：
   - 继续锁定 `@feedback/web 0.2.0`。
   - 后台 `appId=rag` 配置精确来源 `http://127.0.0.1:18764`。
   - 验证桌面端环境下的截图遮罩逻辑（排除敏感输入）以及反馈成功后 Kaneo 外部链接通过系统浏览器打开。
4. **发行与构建验证清单**：
   - 构建流水线输出：`.exe` 安装包、版本号、对应 Git Commit SHA、SHA-256 校验和。
   - 朋友试用版未签名安全说明：说明书中明确告知未购买公信商业证书，Windows SmartScreen 提示属于正常现象，提供操作指引（“更多信息” -> “仍要运行”），绝不夸大或隐瞒提示。

---

## 门禁验收清单

| 门禁分类 | 检验项目 | 检验标准与通过条件 | 验收状态 |
| --- | --- | --- | --- |
| **自动化测试门禁** | 后端全量测试回归 | `python -m pytest backend/tests/` 163 项全部通过，无失败项 | 已通过 |
| | 前端构建与类型检查 | `npm --prefix frontend run build` 构建成功，Vite 与 vue-tsc 零错误 | 已通过 |
| | 桌面端单元测试 | `npm --prefix desktop test` 13 项全部通过（safeStorage、停机生命周期、配置回滚、恢复校验与现场保留） | 已通过 |
| **Windows 干净环境门禁** | 无开发依赖运行 | 在无 Python、Node.js、Docker 的标准 Windows 10/11 x64 环境安装运行成功 | 待验收 |
| | 中文路径兼容性 | 安装于包含中文与空格的用户名路径下（如 `C:\Users\测试用户\AppData\Local\...`）功能正常 | 待验收 |
| | 端口冲突防御 | 预先占用 18764 端口，程序弹出友好报错且允许用户释放后重试，不崩溃、不强杀进程 | 待验收 |
| | 异常退出与重启续接 | 任务执行中强杀进程后重新启动，未完成任务正确标记中断，无静默付费重试，数据无损 | 待验收 |
| **真实学习流程门禁** | 扫描件真实 OCR | 导入 >10 页真实扫描 PDF，按页上传百度 PaddleOCR 识别，页码与文本对应准确 | 待验收 |
| | 混合与复杂文档提取 | 导入文字 PDF、图片及混合文档，文字页本地提取，图片/扫描页调用 OCR | 待验收 |
| | 全链路学习与评分 | 完成导入、切片提取、审核发布、今日学习、揭示评分全流程闭环 | 待验收 |
| | 离线纯本地可用性 | 断开网络连接，已有资料查看、历史背诵卡复习与本地操作依然完全可用 | 待验收 |
| **发行与恢复门禁** | 覆盖升级保留数据 | 覆盖安装新版本，原有资料、学习进度、配置状态完全保留 | 待验收 |
| | 数据备份与异机恢复 | 导出备份文件，清空后通过备份成功恢复，数据校验一致；跨机提示重新输入密钥 | 待验收 |
| | 真实反馈归档验证 | 在桌面客户端提交 1 条文本反馈与 1 条截图反馈，Kaneo 后台成功归档 | 待验收 |

---

# 历史执行记录（公网多租户部署模式 - 归档）

目标：统一 HTTPS 网站，最多 10 人人工开户，独立资料及学习记录，空库上线。

| 任务 | 状态 | 结果与缺口 |
| --- | --- | --- |
| T1 登录后仅访问自己的资料、任务和学习记录 | 待体验 | 账号、会话、分库、后台隔离与浏览器登录/跨标签回归通过；尚未发布 |
| T2 实际导入、审核、背诵及练习 | 实现中 | 文字 PDF/DOCX 真实导入成功；LLM/Embedding 探测通过；OCR 模型 HTTP 404 阻塞扫描件 |
| T3 HTTPS、持久化及恢复 | 实现中 | Compose、Caddy、备份恢复已验证；完整镜像构建被 Docker 空间不足阻塞；服务器及域名待提供 |

### 历史实现与验证（2026-09-12，macOS ARM64）
- 后端回归：137 passed / 272 warnings，21.79秒（Python 3.12.14）。
- 前端：Vite 构建通过。
- 租户隔离：`app/tenancy.py`、`auth.py`、`access.py`。
- Docker 完整构建因空间不足挂起，原公网模式文件与用例均完好保留，不再作为桌面发行门禁。
