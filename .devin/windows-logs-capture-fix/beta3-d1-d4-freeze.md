# beta.3 修复冻结契约（D1–D4）

创建：2026-09-18 · 主代理：Devin · 分支 `codex/windows-1.0.6-beta.2` 起，目标版本 `1.0.6-beta.3`
本文档是 A/B/C/D 四个执行者与主代理接线的**唯一语义依据**。字段名、签名、语义逐字冻结；
实现细节（私有方法名、内部结构）由所有者自定，但对外行为不得偏离本文。

## 边界（继承用户决定）

- 保留 beta.2「日志读取异常」已验收结论；不改用户真实日志、账号、资料、权限。
- D2 只做有界内存保留 + 恢复后补写；不新增应急落盘目录；不承诺强制结束进程后仍保留。
- D4 用隔离宿主账号验证采集/预览/移除，不做生产提交；用户自有 Windows 机执行。
- `PLAN.md` 未提交改动保留；本文件与后续执行记录置于同目录。

## D1：日志路径异常不阻断启动（Agent A）

**根因（已核实）**：`backend/app/config.py` 的 `log_path` 属性在访问时执行
`p.mkdir(parents=True, exist_ok=True)`（config.py:172）；`setup_diagnostics()`
在 `app/diagnostics.py:738` 以 `getattr(settings, "log_path", None)` 触发该属性，
位于现有 try 块（753 行起）之外。`logs` 被普通文件占据或 ACL 拒绝时异常冒出，
`app/main.py:108` 模块级调用处崩溃启动。

**冻结行为**：

- 日志目标目录解析（含 `settings.log_path` 属性访问与 `DATA_DIR` 回退）、目录创建、
  文件 handler 创建/挂载，全部处于同一保护范围。任一失败 → 写一条
  `[DIAGNOSTICS INIT ERROR]` 到 `sys.stderr`（沿用现有格式），跳过文件 handler，
  继续执行控制台 handler 安装与 `_diagnostics_initialized = True`，函数正常返回。
- 保护范围**仅限日志路径与文件 handler**。`data_path`/`uploads_path`/`db_path` 等
  业务路径行为不变；数据库、凭据等启动错误照常抛出，不得吞掉。
- 不删除/替换占据 `logs` 路径的普通文件，不自动修改 ACL。
- `settings.log_path` 属性本身保持现签名（含 mkdir），不改成惰性纯路径。

**「打开日志目录」错误处理（主代理文件，语义在此冻结）**：

- `desktop:openLogDirectory` IPC 改为返回 `{ ok: boolean; message?: string }`：
  `fs.mkdirSync` 异常 → `{ok:false}` + 中文提示；`await shell.openPath(dir)` 返回
  非空字符串（Electron 以 resolve 携带错误而非 reject）→ `{ok:false}` + 提示；
  成功 `{ok:true}`。不出现未处理 Promise。
- 启动失败弹窗（main.ts ~614、~957）中 `shell.openPath` 同样 await 并检查返回串，
  失败时经 `extra` 行提示，循环继续。
- 前端 `LogsView`/`LogNavNotice` 收到 `{ok:false}` 时给出界面提示；不复用查询错误区。

## D2：DiagnosticsLogger 可恢复待写队列（Agent B）

适用 `desktop/src/diagnostics.ts` 的 `DiagnosticsLogger`（desktopLogger 与
frontendLogger 两个实例共用本类，前端记录经 `desktop:reportLog` 进入）。

### log_record_id（冻结）

- 每条进入持久化路径的记录分配 `log_record_id = "${sessionId}:${source}:${seq}"`，
  `seq` 为该 logger 实例内单调递增计数、8 位十进制零填充。
  例：`sess-875103ec:desktop:00000042`。
- 分配点：写入路径统一入口（首写、聚合摘要、限流通知等所有落盘记录各得新 id；
  聚合摘要不得沿用模板 id）。id 同时随记录进入内存环与待写队列。
- `log_record_id` 加入 `FIELD_WHITELIST`（desktop/src/diagnostics.ts）；
  `LOG_FIELD_WHITELIST` 镜像加入（backend/app/diagnostics.py，Agent A 负责）——
  后端不铸造 id，白名单镜像仅保持两边字段契约一致。

### 待写队列语义（冻结）

- 常量：`PENDING_MAX_ENTRIES = 500`、`PENDING_MAX_BYTES = 1024 * 1024`，先到者生效；
  字节按 `Buffer.byteLength(JSON.stringify(record) + '\n', 'utf8')` 计。
- 入队条件：`writeEntry` 中 (a) 已 `degraded`（含初始化即失败）且未 `closed`；
  (b) `appendFileSync` 抛错 → 置 `degraded` 并将该记录入队。
  `closed` 之后到达的记录只进内存环，不入队（写出方已终结）。
- 超限：丢弃**最旧**待写记录，`droppedPending` 单调累计（永久不清零）。
- 恢复定时器：进入 degraded 后每 `recoveryIntervalMs`（默认 3000，options 可注入，
  仅内部/测试用，不进 IPC）一次恢复尝试；定时器 `unref()`，不阻止进程退出。
- 每次恢复尝试：
  1. 若初始化从未成功，先重试等价 initialize 的探测（mkdir + 活动文件尺寸读取），
     不写任何记录；
  2. 队列非空 → 按原顺序补写；每条经正常 rotate+append 路径，遇首个失败即停，
     剩余留队。drain 成功 → `degraded=false`；
  3. 队列空且探测成功 → `degraded=false`。
- drain 完成后写一条摘要记录：`event='log.persistence.recovered'`，
  字段含 `entries`（本次补写条数）与 `dropped`（累计丢弃数）；
  该记录走正常 writeEntry 并获新 `log_record_id`。
- 导出用不可变快照：`getPendingEntries(): Record<string, unknown>[]` 返回
  待写记录的**浅拷贝数组**（原顺序）；调用方修改返回值不得影响队列。
  补写成功后才从队列移除；快照不含未入队记录。
- `getPersistenceState(): { degraded: boolean; pendingCount: number; droppedPending: number }`
  —— 只含三个字段，不携带原始异常文本或路径。
- `close()`：停恢复定时器；对剩余待写做**一次**同步尽力补写（逐条有序，失败即停），
  不阻塞、不抛出；随后 `closed=true`。
- 既有行为不变：内存环 500 条照常维护（导出兜底用）；限流、聚合、白名单、
  脱敏、`isDegraded()`/`getMemoryEntries()` 签名不变；公开方法一律不抛。

## D3：导出披露不完整原因（Agent C）

`readSource`（log-store.ts）现状只保留首个错误且导出完全忽略错误——按下列冻结修改。

### readSource 结果（冻结）

- 保留 `error`（首个错误，queryLogs 抛它，查询契约不变）；新增：
  - `errors: Array<{ file: string; stage: 'stat' | 'open' | 'read'; code: string }>`
    —— 收集**全部**候选文件的读取错误；`file` 为相对文件名（basename，
    如 `backend.jsonl.2`）；`code` 经现有 `readErrorCode` 归一化；
    不含绝对路径、原始异常文本、行内容。
  - `missing: boolean` —— 所有候选均 ENOENT（attempted>0、无任何非 ENOENT 错误、
    无成功读取）时为真；预算耗尽提前中断时按已扫描部分判定，未扫描不算缺失。

### collectExportEntries / DiagnosticArchive（冻结）

入参新增（可选，向后兼容）：

- `pendingEntries?: Record<string, unknown>[]` —— 主进程传入两个 logger 的
  `getPendingEntries()` 合并快照（记录自带 `source` 字段，按 source 归并）。
- `droppedPendingEntries?: number` —— 两个 logger `droppedPending` 之和。

归并规则（冻结）：

- 每来源合并顺序：磁盘记录 → memoryFallback（沿用现语义，仅 desktop、磁盘为空时）
  → pendingEntries（同 source）。pending 记录经 `log_record_id` 与已收记录去重：
  id 相同即丢弃 pending 副本；无 id 的 pending 记录一律保留（宁多勿丢）。
  合并后沿用现有按 timestamp_ms 新→旧排序与时段过滤。
- `memory_sources` = 最终载荷中含内存/待写来源记录的来源列表
  （memoryFallback 或 pending 贡献了至少一条即计入）。
- `buffered_entries` = 实际写入导出载荷的 pending 记录数（去重、时段过滤、
  字节预算之后）。

`ExportSelection` 与 `DiagnosticArchive` 新增字段（MANIFEST.json 顶层同名字段）：

| 字段 | 语义 |
|---|---|
| `read_errors` | `Array<{source,file,stage,code}>`，全来源全部读取错误 |
| `missing_sources` | `string[]`，所有候选均不存在的来源 |
| `skipped_lines` | 现有 skippedLines（超长行跳过数） |
| `read_truncated` | 任一来源因扫描预算未完整读取 |
| `memory_sources` | `string[]` |
| `buffered_entries` | number |
| `dropped_pending_entries` | number |
| `incomplete` | `read_errors.length>0 \|\| read_truncated \|\| truncated \|\| skipped_lines>0 \|\| memory_sources.length>0 \|\| dropped_pending_entries>0` |

- `truncated` 保持现有导出容量截断语义，不改变。
- `note` 追加对应中文摘要（沿用现有风格），不替代字段。
- `CHECKSUMS.txt` 必须覆盖含新字段的实际 MANIFEST 字节（现有实现已对
  manifestContent 哈希，保持即可）；ZIP 内文件名集合不变。
- `DiagnosticArchive` 返回值新增 `incomplete: boolean`（IPC 透传用）。

### IPC 与界面（主代理文件，语义在此冻结）

- `desktop:exportDiagnostics` 成功返回增加 `incomplete?: boolean`。
- `LogsView`：导出结果与查询错误**分区显示**（查询错误区不受影响）；
  `incomplete` 时提示「诊断包已导出，部分内容不完整或来自内存，详情见 MANIFEST.json」。
  导出动作不清除已有查询错误。
- `DesktopDiagnosticExportResult`（useDesktop.ts）加 `incomplete?: boolean`；
  `openLogDirectory` 返回类型改为 `Promise<{ok:boolean;message?:string}>`。

## D4：附件 UI 验证（Agent D，用户在自有 Windows 机执行）

交付物（全部新文件，置于 `e2e/windows-sandbox/`）：

- `sandbox-setup.ps1`：创建临时本地宿主账号（名如 `st-beta3-sandbox`）、
  验证 `%LOCALAPPDATA%\RAGStudyAssistant` 在该账号下全新；
  打印证据收集指引。注意 `RAG_DESKTOP_DATA_DIR` **不隔离**桌面日志目录
  （`backend-manager.ts:getLogDir()` 不读它）——隔离依赖独立 Windows 账号本身。
- `acceptance-runbook.md`：逐步用例——以临时账号登录 → 安装候选包 →
  打开反馈面板验证自动采集 → 附件预览 → 移除 → 关闭重开 →
  制造单来源/全来源不可读（对日志目录加 ACL 拒绝）验证 UI →
  宿主退出后确认附件与组件状态清理 → 反馈服务独立登录、不传宿主凭据。
  每步注明预期现象与需回传的证据（截图/导出文件）。**不点击生产提交**。
- `sandbox-teardown.ps1`：结束进程、删除临时账号与 profile、核对 ACL 复原、
  输出清理结果。

## 文件写入归属（每文件唯一写入者）

| 文件 | 写入者 |
|---|---|
| `backend/app/diagnostics.py`、`backend/tests/test_diagnostics*.py`（含新增） | Agent A |
| `desktop/src/diagnostics.ts`、`desktop/src/tests/diagnostics.test.ts` | Agent B |
| `desktop/src/log-store.ts`、`desktop/src/diagnostic-export.ts`、`desktop/src/tests/log-store.test.ts`、`desktop/src/tests/diagnostic-export.test.ts` | Agent C |
| `e2e/windows-sandbox/**`（新目录） | Agent D |
| `desktop/src/main.ts`、`desktop/src/preload.ts`、`frontend/src/composables/useDesktop.ts`、`frontend/src/views/LogsView.vue`、`frontend/src/components/LogNavNotice.vue`、`desktop/package.json`、`.github/workflows/windows-desktop-release.yml`、`e2e/specs/*` 既有 spec 修订、`e2e/playwright.config.ts`、`RELEASE.md`、本目录记录文件 | 主代理 |
| 一切文件（只读） | Reviewer |

依赖：B 只需本文冻结签名即可开工；C 对冻结签名编码，可用自建夹具模拟
pendingEntries，不依赖 B 的实现完成。主代理在 A/B/C 交付后接线。

## 验证门槛（继承计划，执行时逐项记录）

- A：`setup_diagnostics()` 不传 `log_dir`、默认 `logs` 被普通文件占据/权限拒绝
  → 不抛、控制台 handler 仍装、业务启动继续；正常路径回归。
- B：初始化失败 / 运行中写失败 / 恢复后无新事件仍自动补写 / 反复失败 /
  队列溢出丢弃 / close 清理 / 磁盘已有旧记录时 getPendingEntries 仍返回待写。
- C：活动代不可读但历史可读 / 中间代不可读 / 全 ENOENT 正常缺失 / 空文件 /
  扫描截断 / 超长行 / 内存+待写合并去重 / MANIFEST 字段与 CHECKSUMS 一致。
- 主代理：desktop `tsc`+全套测试、backend `pytest tests/test_diagnostics*`、
  frontend `npm run build`、e2e acceptance + configured-logs 项目。
- 构建：`workflow_dispatch` version=1.0.6-beta.3，publish=true + prerelease=true
  （workflow 新增 `prerelease` input，`gh release create --prerelease`）。
