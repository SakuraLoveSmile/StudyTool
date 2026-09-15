# RAG Windows 桌面版与百度 OCR 接入执行记录

目标：提供 Windows 10/11 x64 桌面安装包，用户无需配置开发环境即可独立使用；支持在软件内安全配置私有 LLM、Embedding 与百度 PaddleOCR API；支持备份恢复与保留数据的覆盖升级；接入 Feedback 反馈。

| 任务 | 状态 | 目标与当前缺口 |
| --- | --- | --- |
| **T1：安装、启动与本机数据** | 待体验 | 集中启动入口直传应用实例、原子维护屏障在途计数与 running 漏算修正、独立连接 WAL checkpoint、停机租约校验与 30s 超时安全中止（支持“继续等待”与“强制退出”循环且不自动误杀）均已修复；自动化回归全绿（含真实子进程一次启动验证）；待在干净 Windows 10/11 x64 实机环境执行安装与初次启动体验 |
| **T2：配置模型与识别扫描件** | 待体验 | 统一凭据保存/替换/清除与就绪探针失败自动回滚旧配置、配置就绪探针失败自动清理残留进程、SQLite `system_metadata` 表持久化向量锁状态并在检索与后台任务中拦截向量追加、前端向量锁提示与无缝重建索引流程、safeStorage 禁明文降级、百度 OCR 429 熔断与多页物理页码对齐均已实现；自动化单测全绿；待在实机配置真实百度 OCR Token 进行扫描真题体验 |
| **T3：备份、Feedback 与发行验证** | 待体验 | 离线备份显式传入实际数据目录与模型元数据、离线恢复结构化 JSON 输出严格校验（ok、restored_to、pre_restore_snapshot）、恢复后启动失败多级安全回滚与现场保留（`data-failed-restore-<timestamp>`）、构建脚本硬门禁（必须 Python 3.12 64-bit、清洗旧产物、冻结后台启动/首页/安全退出检查、缺失 NSIS .exe 立即报错）均已修复；Windows Actions 已生成并发布 1.0.1 安装包；待在干净 Windows 实机验证安装、覆盖升级、真实百度 OCR 与 Feedback 提交 |
| **T4：自动更新** | 待体验 | 已接入 `electron-updater`、GitHub Releases 发布配置、Windows GitHub Actions 构建工作流、手动检查/定时检查、用户确认下载、取消下载、完整性校验后重启安装及更新状态 IPC；1.0.1 的 `latest.yml`、ASCII 安装包、blockmap、manifest 与 SHA-256 已发布并完成远端同名及校验核对；待实机验证旧版到新版更新、断网/损坏包/任务繁忙与资料密钥保留 |

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
  - **自动更新外部验收缺口**：已公开发布 1.0.1 资产，但尚未在干净 Windows 实机完成旧版到新版的真实更新链路，以及断网、损坏包、任务繁忙和资料密钥保留验证。
  - **Actions 发布证据**：运行 `34932850914`（提交 `e42c22d`）在 Windows 2022 runner 全部通过，包含冻结后台启动、首页加载和安全退出门禁；公开 Release 为 `SakuraLoveSmile/StudyTool-Releases@desktop-v1.0.1`，包含安装包、`latest.yml`、blockmap、`RELEASE_MANIFEST.json`、`SHA256SUMS.txt`，下载后 SHA-256 校验通过。此前有问题的 `desktop-v1.0.0` Release 及源仓库标签已撤销。
- 状态判定：本地自动化与回归全绿，已满足将软件移交朋友体验的质量标准，T1、T2、T3 本轮状态统一推进为“**待体验**”；待完成上述实机体验后最终转为“已验收”。

## 测试版日志查看与 Windows Feedback 补齐计划与实施证据

> 编号说明：本节 `T1`–`T4` 是本轮「日志查看 + Windows Feedback 补齐」的内部编号，与上文桌面发行 T1–T4 **不是同一组任务**，引用时须带「本轮」前缀。

目标：测试用户能在软件内查看诊断日志、导出排查材料，并在 Windows 客户端使用 Feedback 提交问题。日志默认仅保存诊断元数据，不记录资料原文、提示词、模型回答或密钥。

| 任务 | 状态 | 目标与当前缺口 |
| --- | --- | --- |
| **本轮 T1：启动失败也能留下日志** | 实现中 | 桌面主进程需在启动后台前初始化持久日志；后端需在业务模块导入前完成桌面配置与日志初始化（`backend/app/main.py` 第 57–66 行先导入 `database`/`services`/`routers`/`desktop`，`setup_diagnostics` 到第 68 行才导入、第 70 行才调用）；`desktop/src/main.ts:837-857` 目前只有 `desktop:openLogDirectory`（拼接并 `mkdir` 日志目录），主进程无任何持久日志写入，启动失败弹窗只有重试与退出，缺「打开日志目录 / 导出诊断包」 |
| **本轮 T2：统一格式与安全存储** | 实现中 | `backend/app/diagnostics.py` 已有 JSONL 结构化日志、`StructuredJsonFormatter`、`SafeRotatingFileHandler` 与白名单脱敏，可直接复用；缺口是桌面/后台/前端三类文件分离、单文件 5 MiB（现为 `LOG_MAX_BYTES = 10485760`，即 10 MiB）、超过 7 天历史清理、前端上报限流与重复聚合、旧格式兼容读取 |
| **本轮 T3：前端直接查看与导出** | 实现中 | 前端没有日志页面与路由（`frontend/src/router/index.ts:15-59` 共 9 条路由、`frontend/src/views/` 共 9 个视图，均无日志页；`frontend/src/components/Navbar.vue:16-34` 导航为固定的 7 个 `nav-tab`），只有 `SettingsView.vue:456-468` 的「打开运行日志目录」按钮；主进程 `desktop:openLogDirectory` 通道可复用；缺口是受限 IPC 查询/上报/导出（ZIP）与前端筛选分页 |
| **本轮 T4：让 Windows Feedback 实际可用** | 实现中 | 反馈配置只来自构建期 `import.meta.env.VITE_FEEDBACK_API_BASE / VITE_FEEDBACK_APP_ID`（`frontend/src/composables/useFeedbackWidget.ts:72-87`），开发机由被 git 忽略的 `frontend/.env.local` 提供；`.github/workflows/windows-desktop-release.yml` 与 `desktop/scripts/build-release.py` 均未注入这两个变量，故当前 Windows 发布包不会挂载反馈组件。需在发布构建显式注入 `https://feedback.xn--fhqths51enha.cn` 与 `appId=rag`，缺失或非法即构建失败 |

### 现状与复用基座（已按文件核对，非假设）

| 事实 | 证据 |
| --- | --- |
| 后端已有 JSONL 结构化日志、轮转与脱敏 | `backend/app/diagnostics.py`（`StructuredJsonFormatter`、`SafeRotatingFileHandler`、`backend.jsonl`） |
| 后端日志参数可注入 | `backend/app/config.py:21-24`（`LOG_LEVEL` / `LOG_DIR` / `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`），`log_path` 属性在桌面模式下解析到 `desktop_root_path / "logs"` |
| 一次启动的关联编号已存在 | `backend/app/diagnostics.py` 的 `INSTANCE_ID` 与 `task_id` / `unit_id` / `call_id` contextvar |
| 主进程已有打开日志目录通道 | `desktop/src/main.ts:837-857`、`desktop/src/preload.ts:36`、`frontend/src/views/SettingsView.vue:456-468` |
| 后端诊断日志已有测试基座 | `backend/tests/test_diagnostics.py`（可在此之上扩展桌面/后台/前端三类文件分离、轮转、限流与脱敏用例） |
| 反馈包版本已锁定 | `frontend/package.json:12` → `"@feedback/web": "file:vendor/feedback-web-0.2.0.tgz"`（0.2.0 已随 `frontend/vendor/` 入库） |
| 发布工作流未注入反馈变量 | `.github/workflows/windows-desktop-release.yml:17-18` 顶层 `env:` 仅含 `RELEASE_REPO`，构建步骤无 `VITE_FEEDBACK_*`；`desktop/scripts/build-release.py` 同样未注入 |
| 反馈服务来源登记待核对 | 本机同级仓库 `Feedback/docs/closeout.md:306` 记录 `appId=rag` 的允许来源为 `http://127.0.0.1:5174`（Web 开发入口）；桌面端实际来源为 `http://127.0.0.1:18764`，故本轮 T4 必须在服务端管理端核对生产登记，且仅在缺失时补齐该来源、保留已有来源 |
| 计划轮次的工作区状态 | 计划落盘前 `git status --short` 为空（HEAD `f8456c2`）。该轮仅改本文件；随后按本计划实施，改动清单与证据见下节《本轮实施与验证证据》 |

> 复核记录（2026-09-15，HEAD `f8456c2`）：上表已逐条对照当前工作区源码重新核对，成立；相较首稿修正了 2 处行号（`desktop/src/main.ts` 由 839-856 更正为 837-857，`backend/app/main.py` 明确第 68 行导入、第 70 行调用）与 1 处已过期的「工作区无未提交改动」描述。该复核轮次为纯计划更新；实施产出见下一节。

### 本轮 T1：启动失败也能留下日志

- 桌面主进程在启动后台前初始化持久日志；后端在业务模块导入前完成桌面配置与日志初始化。
- 记录启动阶段、后台退出、就绪超时、窗口加载、渲染进程退出、更新和模型调用错误。
- 收集 Vue 错误与未处理 Promise 拒绝，不全局收集任意 console 内容。
- 一次启动共用 `session_id`，保留现有任务、执行单元与模型调用关联编号。
- 启动失败原生弹窗提供打开日志目录、导出诊断包和退出；导出不依赖后台或前端页面。

### 本轮 T2：统一格式与安全存储

- 复用后端 JSONL 实现，按桌面、后台、前端分别存文件，统一时间、级别、事件、来源、版本和关联字段。
- 默认 INFO，每类单文件 5 MiB、保留 5 个历史文件；清理超过 7 天的历史文件，兼容读取旧格式。
- 写入和前端上报有界、限流并聚合重复错误；日志故障不能阻断业务。
- 白名单字段递归脱敏，过滤认证信息、URL 参数、用户路径和文件名；写入与导出均检查，禁止记录业务正文。

### 本轮 T3：前端直接查看与导出

- 测试版主导航增加默认可见的「日志」入口，设置页保留入口；不要求打开开发者工具或填写隐藏配置。
- 默认显示最近 24 小时，按时间、级别、来源、会话、任务编号和关键词筛选；每页 200 条，支持详情、复制和手动刷新。
- 自动刷新可选，每 3 秒更新，离开页面停止；默认只读，不提供删除日志按钮。
- 主进程通过受限 IPC 提供查询、前端事件上报及导出，校验发送方与参数，不开放公共 HTTP 日志接口或任意路径读取。
- ZIP 包含选定时段的脱敏日志和版本、平台、校验值清单；默认 24 小时、最多 7 天、原始日志上限 20 MiB，超限保留最新记录并注明截断。
- 原生保存对话框选择目标，取消不留半成品；不打包数据库、资料或凭据。

### 本轮 T4：让 Windows Feedback 实际可用

- Windows 发布构建明确注入现有服务地址 `https://feedback.xn--fhqths51enha.cn` 和 `appId=rag`，不依赖开发机 `.env.local`；发布配置缺失或地址无效时构建失败。
- 保持随应用固定打包 `@feedback/web` 0.2.0，沿用组件独立登录，不传递 RAG 凭据。
- 核对服务端允许来源包含 `http://127.0.0.1:18764`；如缺失，仅补齐 RAG 应用来源配置，保留已有来源。
- 验证反馈入口、独立登录、截图遮罩及外部链接打开。日志详情区域默认截图遮罩。
- 本轮日志仍以手动导出为交付，不自动上传或自动附加给 Feedback。

### 验证与交付

- 推进顺序：按本轮 T1 → T2 → T3 顺序实施，T4 可独立完成。
- 隔离数据验证：缺失模块、后台异常退出、就绪超时、前端异常、写盘失败、轮转、限流、脱敏与导出取消。
- 前端验证：日志入口、筛选分页、详情复制与导出；Windows 打包环境验证中文路径、启动失败导出与关闭重开。
- 发布门禁增加 Feedback 配置与实际挂载检查；真实反馈提交需使用明确授权的测试内容，分别验证文本与截图成功归档，不能用「组件文件存在」代替。
- 交付约束：保持用户资料与既有改动，进度写入本发布记录。
- 状态流转：必要验证未完成为「实现中」，通过后为「待体验」，用户确认后为「已验收」。**计划轮次未修改代码或服务配置；实施与验证证据见下节，当前四项状态为「实现中」。**

### 本轮实施与验证证据（2026-09-15，状态：实现中）

> 计划四项任务已全部落地代码与自动化验证；「实现中」而非「待体验」的原因集中在最后一小节。

#### 交付物

| 层 | 新增 | 修改 |
| --- | --- | --- |
| 后端 | `backend/tests/test_diagnostics_policy.py`、`backend/tests/test_release_feedback_config.py` | `app/main.py`（导入顺序 + 启动阶段）、`app/diagnostics.py`（统一信封/白名单/轮转/限流/聚合）、`app/config.py`（5 MiB + 7 天保留） |
| 桌面 | `desktop/src/diagnostics.ts`、`log-store.ts`、`zip.ts`、`diagnostic-export.ts`、4 个测试文件 | `src/main.ts`（日志初始化、启动失败弹窗、4 个受限 IPC）、`src/preload.ts`、`src/backend-manager.ts`（生命周期事件 + session/版本透传） |
| 前端 | `frontend/src/utils/diagnostics.ts`、`frontend/src/views/LogsView.vue` | `main.ts`（错误采集安装）、`components/Navbar.vue`、`router/index.ts`、`composables/useDesktop.ts`、`views/SettingsView.vue` |
| 发布 | — | `.github/workflows/windows-desktop-release.yml`、`desktop/scripts/build-release.py` |

#### 已实现的策略数值（与计划一致）

- 三类文件：`desktop.jsonl` / `backend.jsonl` / `frontend.jsonl`，统一字段 `timestamp / level / source / event / message / session_id / app_version` 及任务、执行单元、调用关联编号。
- 单文件 5 MiB、保留 5 个历史、清理超 7 天；默认 INFO；写盘失败降级为内存环形缓冲（不阻断业务）。
- 导出 ZIP：默认 24 小时、最多 7 天、原始日志上限 20 MiB（超限保留最新并在清单标注）、临时文件 + 原子改名（取消不留半成品）。
- 前端：主导航默认可见「日志」入口、每页 200 条、默认 24 小时、自动刷新 3 秒且离开页面停止、只读无删除按钮。
- 受限 IPC 共 4 条（`getLogMeta` / `queryLogs` / `reportLog` / `exportDiagnostics`），全部校验发送方并夹取参数；**没有** 删除通道、公共 HTTP 日志接口或任意路径读取。
- 转发到反馈截图的日志详情区域已加 `data-feedback-capture-mask`，默认遮罩。

#### 自动化验证结果

| 套件 | 命令 | 结果 |
| --- | --- | --- |
| 后端全量 | `python -m pytest backend/tests/` | **206 passed**（本轮新增 42 项：15 项日志策略 + 27 项发布门禁） |
| 桌面全量 | `npm --prefix desktop run build && node --test desktop/dist/tests/*.test.js` | **38 passed**（本轮新增 24 项） |
| 前端构建 | `VITE_FEEDBACK_*=<发行值> npm --prefix frontend run build` | vue-tsc + Vite 零报错 |
| 发布契约 | PyYAML 解析工作流 + 校验函数矩阵 + 真实产物门禁 | 通过 |

覆盖的隔离验证项：旋转/保留清理、限流丢弃与单次告警、重复错误聚合、白名单递归脱敏（含嵌套栈帧）、写盘失败降级、旧格式与非 JSON 行兼容、分页与筛选、导出截断与校验值、原子写入、路径穿越拒绝、取消不留残留。

#### 端到端启动验证（真实子进程，非模拟）

按 `desktop/src/backend-manager.ts` 的真实调用方式启动后端（`python app/main.py --desktop`，stdin 递送 bootstrap），隔离数据目录与日志目录，探针 `/api/desktop/status` 通过后核对 `backend.jsonl`：

- 记录 12 条，启动阶段齐全：`startup.diagnostics.ready` → `startup.imports.ok` → `startup.uvicorn.begin` → `startup.lifespan.begin` → `startup.database.ready` → `startup.ready`；
- 全部 `source=backend`、共享单一 `session_id`、`app_version=1.0.1`。

#### 本轮独立复核发现并修复的缺陷

1. **`sk-` 秘钥规则误伤业务标识**：`task-recon-999` 被脱敏成 `task-reco***`，任务关联编号被静默破坏（递归脱敏后才暴露）。已加词边界锚定，真实密钥仍完整掩码。
2. **文件日志在 uvicorn 启动后静默失效**：`uvicorn.run()` 的 `logging.config.dictConfig` 会关闭所有既有 handler 但保留其挂载，幂等判断因此早退，启动阶段一条都不落盘。只有真实子进程启动才暴露，已改为「handler 仍可写」判定并重新挂载，并补子进程回归测试。
3. **发布产物门禁引号假设错误**：真实产物内联为反引号 ``r.appId=`rag` ``，原门禁只认双/单引号，会误杀真实发布构建。已支持三种引号 + 词边界回退，并对真实 `frontend/dist` 复跑通过。
4. **`CHECKSUMS.txt` 与实际字节不一致**：校验值原本算在未换行的字符串上，清单无法真正校验。已改为对写入归档的确切字节取摘要，测试逐行回验。
5. **后端开发态无法启动**：`python <repo>/backend/app/main.py`（即桌面开发态启动方式）因 `sys.path` 只含 `app/` 而 `ModuleNotFoundError`。已显式加入后端根目录；打包（PyInstaller）路径不受影响。
6. **后端 formatter 硬编码字段表**：白名单放行的 `phase`/`frames`/`dropped` 等字段被第二张硬编码表拦掉，导致阶段与栈帧丢失。已改为直接由白名单驱动透传。

#### 1.0.2 发布记录与 Windows 冻结构建排查（2026-09-15）

**发布结果**：`SakuraLoveSmile/StudyTool-Releases@desktop-v1.0.2`，已是 Latest；源仓库 tag `desktop-v1.0.2`（附注标签）。工作流运行 `34948879659` 在 Windows 2022 runner 上成功，包含反馈配置校验、前端产物门禁、PyInstaller onedir、冻结后台冒烟与发布。

| 资产 | 大小 | 校验 |
| --- | --- | --- |
| `rag-study-assistant-desktop-setup-1.0.2.exe` | 143,028,638 B | SHA-256 与 `SHA256SUMS.txt` 一致；SHA-512 与 `latest.yml` 一致 |
| `latest.yml` | 383 B | 声明 `version: 1.0.2`，更新器可识别 1.0.1 → 1.0.2 |
| `rag-study-assistant-desktop-setup-1.0.2.exe.blockmap` | 150,418 B | 增量更新用 |
| `RELEASE_MANIFEST.json` | 407 B | 版本 1.0.2 与安装包 SHA-256 一致 |
| `SHA256SUMS.txt` | 110 B | 与下载产物逐字节核对通过 |

**排查过程**：前三次发布运行（`34945723776` / `34946747650` / `34948112777`）均在冻结后台冒烟处以「就绪超时」失败，且发布步骤被跳过（未产生任何公开资产）。逐轮收窄证据：

1. 冒烟脚本原先只回显 stderr 尾部，看不到子进程 stdout 与后台日志；先补齐三份证据（日志尾部含阶段事件、stdout 尾部、stderr 尾部），一次失败即自证。
2. 首轮定位到非 UTF-8 控制台编码（cp1252 runner）令中文日志触发 `UnicodeEncodeError`；已让 stdout/stderr 以 `errors="replace"` 降级并使用故障安全控制台 handler，噪声归零（该问题在英文 Windows 上对真实用户同样成立）。
3. 随后日志只剩一条记录 `startup.diagnostics.ready`、stderr 全空 —— 说明是**阻塞而非异常**。加入逐模块导入阶段记录后，锁定在 `app.services.task_worker` 导入链。
4. 用 `faulthandler` 在启动窗口内导出各线程栈，得到确证：主线程阻塞在 **numpy 原生扩展（`numpy/_core/overrides.py` → `create_module`）的动态库加载**上；对照 1.0.1 成功运行，其整个冒烟（启动、就绪、首页、drain、关停）仅耗时 **2.3 秒**，故排除「运行环境整体变慢」。

**修复**：本次提交同时移除两处仅存在于本轮、且在导入阶段生效的差异——

- 把 stdin EOF 看门狗线程从「导入前」推迟到「导入完成后」启动（它只用于感知 Electron 父进程退出，无需在原生扩展加载期间存在）；
- 在 PyInstaller 冻结构建中不再改写 `sys.path`（冻结包已暴露 bundle root，改写只可能干扰其导入器）。

修复后同一工作流一次通过。**诚实说明：究竟是上述两项中的哪一项导致阻塞，尚未被隔离验证**——两者都在同一提交中移除，且无法在 macOS 上复现冻结构建。若后续再次出现同类阻塞，逐模块阶段记录与 `faulthandler` 栈导出已内建，可直接定位。

**这次发布证明了什么、没证明什么**：

- 已证明：Windows 真实构建中反馈配置校验与产物门禁通过；冻结后台在真实 Windows 上能就绪、提供首页并干净退出；启动阶段日志（bootstrap → import → uvicorn → lifespan → database → ready，含逐模块导入步骤）在冻结构建中真实落盘。
- 未证明：**反馈组件是否真正挂载**（构建期门禁只证明配置注入，安装包内实际挂载仍需启动应用观察）；**生产反馈服务是否已登记 `http://127.0.0.1:18764`**（需管理员凭据核对）；日志页在桌面客户端中的实际交互（筛选、分页、详情复制、原生保存对话框取消路径）以及中文路径下的安装与首次启动。

#### 1.0.3 修复发布与 IPC 序列化缺陷（2026-09-15）

**缺陷**：日志页在真实桌面客户端中无法加载任何记录。`useDesktop` 的查询参数直接传入了 `ref([])`——Vue 的响应式数组是 Proxy，而 **Electron 的结构化克隆拒绝序列化 Proxy**，因此每次 `desktop:queryLogs` 调用在渲染进程侧就抛错，后台处理器根本没被调用。

**为什么自动化没能拦住**：后端 208 项、桌面 38 项测试与本地后端子进程冒烟全部通过——它们从未启动 Electron，因而**任何 IPC 边界上的序列化问题都不在覆盖范围内**。该缺陷是用户在真实客户端试用手工发现的。修复：跨 IPC 前把筛选项复制为普通数组（`[...levels.value]`）。

**同类缺陷排查**：逐一核对前端全部 15 处 `desktopAPI` 调用点的入参构造——`saveCredentials(payload)` 的 `payload` 是普通对象字面量（值取自 `reactive` 表单但均为原始类型）、`testConnection` 传入新建对象字面量、其余均为字符串/无参；确认**不存在第二处**把响应式对象直接跨 IPC 传递的写法。

**发布结果**：`SakuraLoveSmile/StudyTool-Releases@desktop-v1.0.3`，已是 Latest；工作流运行 `34951869888` 在 Windows 2022 一次通过。

| 资产 | 大小 | 校验 |
| --- | --- | --- |
| `rag-study-assistant-desktop-setup-1.0.3.exe` | 143,027,968 B | `shasum -a 256 -c SHA256SUMS.txt` → OK；SHA-512 与 `latest.yml` 一致 |
| `latest.yml` | 383 B | 声明 `version: 1.0.3`，可覆盖更新 1.0.1 / 1.0.2 |
| `RELEASE_MANIFEST.json` / `SHA256SUMS.txt` / `.blockmap` | 407 / 110 / 150,386 B | 版本与安装包摘要一致 |

发布说明已改写为修复导向（本版修复三项：日志页加载、非 UTF-8 控制台中文日志、冻结后台启动阻塞），并列出该版本包含的日志能力与已知限制。

#### 未完成项与阻塞（因此状态仍是「实现中」）

- **Windows 实机安装未做**：1.0.2 已在 Windows runner 上完成构建与冻结后台冒烟（就绪/首页/干净退出），但「在干净 Windows 10/11 x64 机器上安装并运行」仍未做——中文路径、启动失败弹窗导出、关闭重开、覆盖升级保留资料，均不得以 CI 结果替代。
- **生产反馈服务来源登记未核对**：`http://127.0.0.1:18764` 是否已在 `appId=rag` 的允许来源中，需用生产管理员凭据在 `https://feedback.xn--fhqths51enha.cn/admin`（应用页）核对；如缺失，仅追加该来源并保留既有来源。此为本轮唯一需要线上配置变更的操作，须由用户授权后执行，本轮未改动任何服务端配置。
- **反馈组件实际挂载未验证**：构建期注入与产物门禁只证明「配置已注入」，不证明组件挂载；挂载需在打包后的应用中观察（登录完成、外壳可见时才会加载）。
- **桌面端 UI 路径未实跑**：日志页筛选/分页/详情复制/自动刷新、原生保存对话框取消路径、前端错误上报的实际落盘，均需在真实桌面客户端里跑一遍。1.0.2 的日志页 IPC 缺陷（响应式 Proxy 无法克隆）已证明这类问题不会被现有自动化覆盖，故此项**不可省略**；1.0.3 修复后仍需在真实客户端复核一次。

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
| **测试版日志门禁**（本轮 T1/T2） | 启动失败留存日志 | 后台缺失模块、异常退出、就绪超时三种启动失败均写入日志，弹窗可打开日志目录并导出诊断包，导出不依赖后台与前端页面 | 实现中（后端已端到端验证；弹窗与导出待实机） |
| | 格式、轮转与脱敏 | 桌面/后台/前端三类文件字段统一，单文件 5 MiB、保留 5 个历史、清理超 7 天；认证信息、URL 参数、用户路径与文件名递归脱敏；写盘失败不阻断业务 | 已通过（本地自动化） |
| | 限流与聚合 | 前端上报与写入有界，重复错误聚合，旧格式日志可读 | 已通过（本地自动化） |
| **测试版日志门禁**（本轮 T3） | 前端查看与筛选 | 主导航默认可见「日志」入口；默认最近 24 小时，按时间/级别/来源/会话/任务编号/关键词筛选，每页 200 条，支持详情、复制与手动刷新 | 实现中（页面与接口已实现，待桌面端实跑） |
| | 导出与取消 | 原生保存对话框导出 ZIP（脱敏日志 + 版本/平台/校验值清单），默认 24 小时、最多 7 天、原始日志上限 20 MiB 且截断有标注；取消不留半成品，不打包数据库、资料或凭据 | 实现中（打包、截断与原子写入已测；原生对话框取消路径待实机） |
| **Feedback 发布配置门禁**（本轮 T4） | 发布构建注入反馈配置 | Windows 发布构建显式注入 `https://feedback.xn--fhqths51enha.cn` 与 `appId=rag`，不依赖 `.env.local`；缺失或地址无效即构建失败 | 已通过（2026-09-15 Windows CI 实跑：校验步骤通过、构建通过；单测证明缺失/非法即失败） |
| | 实际挂载与截图遮罩 | 安装包内反馈入口可见、独立登录可用、日志详情区域默认截图遮罩、外部链接走系统浏览器；不得以组件文件存在替代实际挂载验证 | 实现中（日志详情已加截图遮罩；**实际挂载尚未验证**） |

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
