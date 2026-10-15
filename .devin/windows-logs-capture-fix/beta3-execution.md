# beta.3 修复执行记录（D1–D4）

创建：2026-09-18 · 主代理：Devin · 依据 `beta3-d1-d4-freeze.md` 冻结契约
状态口径：实现中 → 待体验 → 已验收（仅用户确认标「已验收」）

## 分工与交付

| 执行者 | 范围 | 交付 |
|---|---|---|
| Agent A | D1 后端诊断初始化保护 + 白名单镜像 | `backend/app/diagnostics.py`：log_path 属性访问、目录创建、文件 handler 创建/挂载全部纳入同一保护块；失败写 `[DIAGNOSTICS INIT ERROR]` 后跳过文件 handler，console handler 照常安装、`_diagnostics_initialized=True`，函数正常返回；`LOG_FIELD_WHITELIST` 镜像 `log_record_id`。新增 `tests/test_diagnostics_startup.py`（6 用例）；修复 `test_diagnostics.py`/`test_diagnostics_policy.py` 的 `temp_log_dir` fixture 既有隔离缺陷（收集期 `from app.main import app` 触发模块级 setup 污染幂等断言） |
| Agent B | D2 待写队列 | `desktop/src/diagnostics.ts`：`log_record_id`（`${sessionId}:${source}:${seq}`，writeEntry 统一铸造）；待写队列 500 条/1MiB 先到生效、丢最旧 + `droppedPending`；`recoveryIntervalMs`（默认 3000）unref 定时器恢复尝试；drain 有序遇败即停；`log.persistence.recovered` 摘要；`getPendingEntries()`/`getPersistenceState()`；`close()` 一次尽力补写 |
| Agent C | D3 导出披露 | `log-store.ts`：`errors`（全候选 `{file,stage,code}` 相对名）+ `missing`（全 ENOENT）；`collectExportEntries` 归并 磁盘→memoryFallback→pendingEntries（按 source、`log_record_id` 去重、无 id 保留）；`diagnostic-export.ts`：MANIFEST 顶层 8 新字段、`DiagnosticArchive.incomplete` |
| Agent D | D4 沙盒交付物 | `e2e/windows-sandbox/`：`sandbox-setup.ps1`（临时账号，UTF-8 BOM）、`acceptance-runbook.md`（用例 A–J + 证据点）、`sandbox-teardown.ps1`（进程/账号/profile/ACL 核对清理）。按真实组件语义纠正了「重开重采」预期：成功采集后重开不重采（`logsCollected` 只在成功置位），失败重开自动重试 |
| 主代理 | 接线与发布链 | `main.ts`：`openLogDirInShell`（mkdir+openPath 返回值双通道）、IPC 返回 `{ok,message}`、两弹窗失败提示、导出传两 logger 的 pendingEntries/droppedPendingEntries + `incomplete` 透传；`useDesktop.ts`/`LogsView.vue`/`LogNavNotice.vue`：查询错误与操作结果分区、`incomplete` 文案、打开目录失败可见；`desktop/package.json`→1.0.6-beta.3；workflow 增 `prerelease` input（create 带 `--prerelease --latest=false`） |

## 验证结果（自动）

| 项 | 结果 |
|---|---|
| desktop `npm run build`（tsc） | 零错误 |
| desktop `node --test dist/tests/*.test.js` | **129/129 通过**（B 新增 9、C 新增 10、含全部回归） |
| backend `pytest tests/` | **275 通过**（含新 startup 6 用例；修复既有收集期污染后全目录绿） |
| frontend `npm run build`（vue-tsc+vite） | 通过 |
| e2e `--project=acceptance --project=configured-logs` | **37 通过 / 5 跳过**（Windows 占位 fixme，不假通过） |
| `git diff --check` | 通过 |

## D 项状态

| 项 | 状态 | 证据 |
|---|---|---|
| D1 启动保护 | 待体验（自动验证通过，待 Windows 实机） | startup 测试覆盖普通文件占据/父级文件/权限拒绝三路径；`openLogDirectory` IPC 返回结构 + 前端分区提示 |
| D2 待写队列 | 待体验 | 单测覆盖 init 失败/运行中失败/恢复补写保序保字段/反复失败/双上限溢出/close 尽力写；runbook G 用例提供实机旁证点 |
| D3 导出披露 | 待体验 | MANIFEST 字段+CHECKSUMS 一致测试；e2e 桩下导出不清查询错误回归通过 |
| D4 附件 UI | 实现中（等用户在自有 Windows 机执行沙盒） | runbook A–J 用例 + setup/teardown 脚本已交付 |

## 集成复核（Reviewer）

只读复核按冻结契约逐字核对，结论：**无阻塞/应修问题，可进入提交构建**。

- D1 保护块覆盖路径解析/目录创建/handler 挂载；非日志启动错误照常抛出；`log_record_id` 白名单镜像一致
- D2：单线程 drain 无交错路径，写成功才出队 → 无丢无重无乱序；`unref` 定时器、close 尽力写均落实
- D3：read_errors 三阶段全候选收集、missing 语义、budget 未触不算缺失、memory/pending 统计点在字节预算后、CHECKSUMS 哈希实际 MANIFEST 字节——逐字一致
- 接线：openPath 三调用点齐备、导出快照/丢弃计数透传、前端查询错误与操作结果分区
- D4 交付物防护到位（-WhatIf、ACL 备份/恢复、teardown 三项 CHECK）

建议级观察（不阻塞，下一轮视情）：close 后 log() 不进内存环（退出阶段无观察者）；close drain 成功后 degraded 标志不复位（无观察者）；目录级 stat 失败的 file 字段取目录 basename（仍满足相对名脱敏口径）；极端抖动可写多条 recovered 摘要（语义正确略噪）。

## 提交与构建

| 项 | 值 |
|---|---|
| 分支 | `codex/windows-1.0.6-beta.3` |
| 主提交 | `5e2cae9` fix(diagnostics): survive bad log dir, replay pending writes, disclose export gaps |
| 修正提交 | `e7bd321` fix(build): keep uncommitted connection-test/OCR work out of beta.3 |
| CI | run 35340124674 因混入用户未提交工作（main.ts 引用未提交的 connection-test 导出）typecheck 失败 → 修正后已提交树经独立 worktree tsc+vite 验证 → **run 35340622477 全部通过（4m47s）** |

教训记录：整文件 `git add` 会把同文件内他人未提交改动一并带入。本次 `main.ts`/`useDesktop.ts` 已修正为「beta.2 + 仅本轮 hunk」，用户改动完整保留在工作区未提交状态。

## 发布验证（已完成）

| 核对项 | 结果 |
|---|---|
| 发布目标 | `SakuraLoveSmile/StudyTool-Releases` tag `desktop-v1.0.6-beta.3`，**Pre-release=true，Latest 仍为 1.0.5，beta.2 未受影响** |
| 附件 | `rag-study-assistant-desktop-setup-1.0.6-beta.3.exe`（143,107,751 B）+ `.blockmap` + `latest.yml` + `RELEASE_MANIFEST.json` + `SHA256SUMS.txt` |
| 匿名下载 | 无鉴权 curl 下载成功（公开 release） |
| SHA-256 四方一致 | CI 产物 = release 附件 digest = SHA256SUMS = MANIFEST = 匿名下载文件 = `6443c8292c2edde924176887e7c0611d80230caf366f2f52f48eeaaacec5f738` |

## 非本轮工作（提交时排除）

`ocr_service.py`、`test_desktop_and_ocr.py`、`test_desktop_ocr_connection_test.py`、`test_ocr_jobs_protocol.py`、`connection-test.ts`、`connection-test.test.ts`、`backend-request-timeout.test.ts`、`SettingsView.vue`，以及 `main.ts`/`useDesktop.ts` 内的连接超时/OCR helper hunk —— 工作区既有未提交改动，不属于本计划，保持原样不提交不回滚。

## 待办（发布后）

- [ ] CI `workflow_dispatch` version=1.0.6-beta.3 publish=true prerelease=true
- [ ] 匿名下载验证 + SHA256SUMS/RELEASE_MANIFEST/文件三方核对
- [ ] 交付 D4 沙盒包给用户（安装包 + e2e/windows-sandbox/ 三件套）
- [ ] beta.2 已验收项回归（缺失/空态/拒读、导航、升级保留）——runbook J 已含
