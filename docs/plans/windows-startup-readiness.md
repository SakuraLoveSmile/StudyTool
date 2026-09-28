# Windows 1.0.8-beta.1 启动失败调查与修复施工计划

状态：整体实现中。T1、T2 的源码与本地测试已完成；T3 手册已补充，Windows 候选包与原故障环境尚未实测。未执行外部发布或用户数据迁移。

## 目标与本次证据

- 目标：已安装的 Windows x64 学习助手在后端冷启动较慢时仍能打开主窗口；真正的后端异常能留下可定位的阶段与原因。保持现有资料、凭据、固定本地端口和实例令牌校验。
- 源码基线：`beta/studytool-1.0.8-beta.1`，HEAD `34656f3`；调查开始时工作区干净。用户诊断包：`/Users/sakurasep/Downloads/rag-diagnostics-2026-09-27T16-50-00-867Z.zip`，ZIP 解压校验通过。包内 19 条 desktop、43 条 backend 日志，`truncated=false`、`incomplete=false`；没有 frontend 记录。
- 失败会话 `sess-847df9c2`（UTC 2026-09-27）：16:39:40.272 桌面端记录 `startup.backend.spawn`；16:40:19.366 后端才首次记录 `startup.diagnostics.ready`，相隔约 39.1 秒；16:40:23.917 记录 `Database initialized successfully ... (schema v6)`；16:40:24.310 桌面端记录 `startup.backend.ready_timeout`，`duration_ms=44038`、`timeout_config=15000ms`；16:40:24.818 才记录后端退出码 1。无 `startup.database.ready`、`startup.ready` 或窗口创建事件。
- 对照会话 `sess-44b9e4f2` 中，`1.0.6-beta.5` 同机从 spawn 到后端 ready 约 34.3 秒，之后窗口加载并完成启动。旧会话的自动更新网络错误发生在启动成功之后，不能解释本次后端就绪失败。
- 调用链：`desktop/src/main.ts` 的 `app.whenReady()` → 端口预检 → `BackendManager.start()` → 打包的 `resources/backend/backend.exe` → stdin bootstrap → `backend/app/main.py` 导入及 FastAPI lifespan → `GET /api/desktop/status` 必须返回 200 → 才创建窗口。失败已发生在窗口创建前，现有证据不指向 NSIS 向导、前端渲染或模型服务连接。

### 原因判断与证据边界

调查基线 HEAD `34656f3` 的直接失败机制是：桌面端在后端尚未完成启动时按固定 15 秒就绪预算判定失败并终止子进程。诊断日志中的超时早于进程退出，所以退出码 1 不能当成独立崩溃证据。发布时的冻结后端验证却允许 90 秒冷启动（`desktop/scripts/verify-windows-backend.py:125-149`），安装器冒烟仅验证安装，不启动 Electron 主程序（`desktop/scripts/smoke-windows-installer.ps1`）。当前工作树已实施有界等待修复；Windows 结果尚未取得。

还不能断言“把 15 秒改成 90 秒”就足够。`duration_ms=44038` 以 spawn 前计时，15 秒轮询的起点在 `spawn()` 返回后；缺少两者之间的时间戳，无法判定是同步 spawn 耗时、主进程调度延迟还是探针耗时。后端最后一条数据库日志位于 `init_db()` 的 `db_session()` 内，事务提交与关闭发生在该条日志之后（`backend/app/database.py:62-72,1029-1034`）。因此数据库提交、`HttpClientProvider.start()`、TaskWorker 尚未被排除。诊断包没有 Python traceback、Windows 事件、安装路径或原机性能数据；不能声称已证明杀毒软件、数据库损坏或特定模块是根因。

## 冻结的修复契约

1. Windows 打包版从发起进程启动到后端认证状态接口返回 HTTP 200，采用总计 **90 秒**的有界就绪预算；开发态继续 15 秒。90 秒采用现有 Windows 冻结后端发布门禁的数值，不表示每次都等待满 90 秒。任何非 200、网络错误或未响应都不视为就绪；仍用现有实例令牌和固定 `127.0.0.1:18764`。达到预算或子进程先退出即失败，不静默启动一个无法工作的窗口。
2. 分别记录 spawn 调用耗时、开始探针时刻、最终总耗时与失败类别；`timeout_config` 必须等于实际预算。单次 HTTP 探针须在约 1 秒超时后销毁请求并继续轮询，使总预算真能结束。后端先退出记录“未就绪前退出”，仅真正达到截止时间才记录“就绪超时”；主动终止后的退出码不得被解释为原发异常。
3. 后端 lifespan 的数据库初始化、HTTP client 初始化、TaskWorker 启动各有开始与完成阶段记录；异常在现有脱敏日志边界记录 `failed_stage`、`error_type`、安全的 `exception_info` 后原样抛出。`startup.ready` 仍只在全部启动步骤完成后写出，桌面端仍以认证状态接口 200 作为最终就绪依据，不以日志事件代替探针。
4. 不修改数据库 schema、迁移策略、数据根目录、端口、凭据存储或外部模型配置。不以卸载、清空 `%LOCALAPPDATA%\StudyTool`、降级旧版或关闭杀毒软件作为修复步骤。原机验收前保留可恢复的数据备份；隔离测试使用合成数据或受控副本。

## T1：慢冷启动可正常打开窗口

目标：修复桌面端过早判定就绪失败，并保留真实失败时的终止行为。

- 执行者 A 修改 `desktop/src/backend-manager.ts`、`desktop/src/diagnostics.ts` 及对应测试；主 agent 同步了后端共享诊断白名单。必读：上述冻结契约、`desktop/src/main.ts:989-1079`、`desktop/scripts/verify-windows-backend.py:84-172`。初始施工与 T2 并行；集成阶段由主 agent 核对三端字段一致。
- 在 `BackendManager.start()` 的全新启动及“已跟踪进程再次检查”两条路径复用同一打包版/开发态预算。总截止时间从 spawn 前确定；探针只用剩余预算，避免 `spawn()` 阻塞后又额外等待完整 90 秒。记录实际耗时与退出原因，处理超时与 exit/error 竞争，只结算一次。失败清理必须确认子进程退出；终止失败沿现有 `forceKill()` 语义显式报告，不触碰资料。
- 测试用短时间可控的本地假后端/HTTP 状态端点覆盖：迟到但在预算内返回认证 200、非 200、挂起探针与单次重试、子进程先退出或在 HTTP 200 后退出、截止后终止、已跟踪进程复检。断言错误分类与白名单落盘字段一致；无需让单元测试真实等待 90 秒。
- 本地结果：`npm run build` 通过；`npm test` **155 passed**。实现已改为单调时钟，从 spawn 前算总预算；bootstrap 同步/异步写入失败均走终止确认路径；请求超时会销毁请求并避免重复排队。桌面与后端白名单均保留 spawn/probe/total 耗时、剩余预算、失败原因和终止确认字段。
- 状态：实现中。Windows 打包版和原故障机启动仍未验证。

## T2：启动失败能指明真实阶段

目标：若延长等待后仍失败，诊断包可以区分数据库提交、HTTP client、TaskWorker 的错误。

- 执行者 B 独占 `backend/app/main.py`、必要的 `backend/tests/test_diagnostics_startup.py`。必读：上述冻结契约、`backend/app/database.py:62-72,682-703,1029-1034`、`backend/app/diagnostics.py:152-178,279-320`。与 T1 可并行；不得修改桌面文件。
- 在 lifespan 的每个现有启动步骤周围记录结构化阶段，尤其要在 `init_db()` **返回后**才记录数据库步骤完成；异常仅在启动阶段捕获，使用既有安全异常提取函数与字段白名单写 `startup.failed`，然后重新抛出。不要在日志中写数据库路径、资料内容、凭据或原始异常文本；不改变业务初始化顺序。
- 用隔离临时目录（桌面模式设置 `STUDYTOOL_DESKTOP_DATA_DIR`，非桌面测试按既有夹具设置 `DATA_DIR`）和失败注入验证三个步骤的完成/失败事件、无 `startup.ready` 的失败语义、脱敏与异常仍向外传播。测试既覆盖数据库提交失败，也覆盖 HTTP client/worker 抛错；复用现有诊断夹具，不新增整套日志框架。
- 本地结果：`python -m pytest tests/test_diagnostics_startup.py tests/test_diagnostics_policy.py`（工作目录 `backend/`）**27 passed**；5 条既有 PyMuPDF SWIG deprecation warnings。启动异常日志使用现有脱敏异常提取函数并重新抛出；新增桌面启动元数据已同步到后端字段白名单并有策略测试。
- 状态：实现中。Windows 冻结后端包内日志与实际失败阶段尚未验证。

## T3：Windows 候选包与原故障环境闭环

目标：证明实际安装的 Electron 程序打开主窗口，而非仅证明 backend.exe 能独立启动。
状态：实现中；当前环境没有可用 Windows 安装会话，Windows 候选包与原故障机验收未执行。

- 主 agent 独占集成核对、`e2e/windows-sandbox/acceptance-runbook.md` 中本次候选包与启动用例的必要更新，以及最终结果记录；不得与 A/B 同时写各自文件。依赖 T1/T2 合并、桌面定向测试和后端诊断测试通过。现有发布脚本由主 agent 运行，不因旧手册中混用 beta.6/1.0.7 示例而拿错候选包。
- 先在隔离 Windows x64 环境，以当前 `1.0.8-beta.1` 和受控数据副本重现原始症状，记录 `backend.exe` 路径/哈希、spawn→首条后端日志→数据库完成→认证 200/失败的时间。此取证只在隔离副本上进行，不要求在用户主账号上反复触发迁移。若已经无法取得旧候选包，标注对照缺失，继续新包验证。
- 构建新候选包后运行现有 Windows `build-release.py`（含 90 秒冻结后端验证）及安装器冒烟。再在可交互 Windows 10/11 x64 隔离环境中**实际启动安装后的学习助手.exe**：冷启动与退出重启均须出现窗口、首页加载和 `startup.completed`；测量后端认证就绪耗时不超过 90 秒，并确认退出后无残留 `backend.exe`。另用受控慢启动跨过 15 秒但低于 90 秒，证实不会误报失败；用退出/挂起夹具证实有界失败和可导出诊断包。
- 在原故障机上验证覆盖安装前先备份 `%LOCALAPPDATA%\StudyTool`，不卸载清数据；记录安装包 SHA-256、Windows 版本、首次及二次启动阶段时间、首页截图、诊断事件。只有原机上的新包确实就绪，才把本次故障列为“待体验”。CI/Windows Server 2022 冻结后端通过、NSIS 安装成功、macOS 单测通过都不能替代原机启动结果。
- 若 90 秒内后端仍无 `startup.ready`，以 T2 的 `failed_stage` 和原机新诊断包锁定具体阻塞/异常后，只修复该阶段并重跑相应验证；不得单纯继续增大超时。若测试机无法得到真实 Windows 交互会话，保留 T3 为“实现中”，明确缺少的机器和证据。

## 执行与授权边界

- 本轮已获实施授权。开始时工作区仅有此前保存的计划目录未跟踪；A/B 分别实施桌面与后端改动，主 agent 完成共享字段集成和 Windows 验收手册更新。代码内局部组织沿现有惯例，不扩大成数据库、升级器或 UI 重构。
- 本轮本地验证已运行桌面 `npm run build`、`npm test`（155 passed），以及在 `backend/` 下运行 `python -m pytest tests/test_diagnostics_startup.py tests/test_diagnostics_policy.py`（27 passed）。测试使用隔离夹具。后续 Windows `build-release.py` 与实机验收尚未运行；发布或让用户安装更新包属于后续明确授权范围，本计划不触发外部发布、真实用户数据迁移或费用。
- 同一阻塞两次不同依据尝试仍未解决时记录预期、实际、日志、尝试与所需决定；不把单独 backend 冒烟或历史 `RELEASE.md` 记录写成这次的实机验收。
