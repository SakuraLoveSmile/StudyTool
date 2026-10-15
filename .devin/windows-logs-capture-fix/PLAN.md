# Windows 反馈修复施工记录（T1/T2/T3/T4）

创建：2026-09-18 · 主代理：Devin

## 基线与既有改动归属（T0 勘察结果）

### StudyTool 宿主仓库

- 分支 `main`，HEAD `a3ce4ab chore(release): bump desktop to 1.0.5`。
- 工作区存在**两大块与本任务无关的未提交改动**，不属于本次成果，任何人不得覆盖、回滚、重排：
  1. **主题（明暗模式）工作**：`frontend/src/composables/useTheme.ts`、
     `components/ThemeToggle.vue`、`desktop/src/theme-prefs.ts`、
     `desktop/src/tests/theme-prefs.test.ts`、`e2e/specs/theme.spec.ts`、
     `e2e/theme-shots.mjs`、`frontend/index.html`、`frontend/src/main.ts`、
     `desktop/src/preload.ts`、`desktop/src/main.ts`（主题 IPC 段）、
     `tailwind.config.js`、`style.css` 及各视图/组件的样式改动。
  2. **发布配置工作**：`desktop/scripts/build-release.py`（反馈服务
     `feedback.xn--fhqths51enha.cn` / appId `studytool` 门禁）、
     `backend/tests/test_release_feedback_config.py`、
     `.github/workflows/windows-desktop-release.yml`、`deploy/README.md`、
     `frontend/.env.example`、`e2e/*`（fixture/mock/config/host-integration 适配 0.4.0）。
- `useFeedbackWidget.ts` 中已有 +13 行未提交改动（theme 属性同步），属主题工作的一部分。
- `frontend/package.json` 依赖：`@feedback/web = file:vendor/feedback-web-0.4.0.tgz`；
  vendored 0.4.0 解包核验：`dist/index.d.ts` 已含 `logProvider`/`FeedbackLogProvider`/`FeedbackLogFile` 公开类型。
- 前端无单元测试框架；前端验证一律走 `e2e/`（Playwright 1.63.0，独立依赖树）。
- 桌面测试：`desktop/` 下 `tsc` 编译 + `node --test dist/tests/*.test.js`。

### Feedback 组件仓库（../Feedback）

- 分支 `main`，HEAD `60e55b4`（v0.4.0 之后一个部署脚本提交）。tag `v0.4.0` = `aaed517`。
- 工作区存在**大量未提交改动**：`capture.ts` 的最小修复 + T1 回归测试之外，
  还有 server_pref（设置视图）、flutter、admin、e2e shots 等其他未发布工作。
- T1 可移植资产（位于脏工作区，仅作参考来源）：
  - `packages/web/src/capture.ts`：`paintMaskRects` 的 setTransform+try/finally 修复（唯一 hunk）。
  - `packages/web/test/masking.test.ts`：新增 describe `渲染器残余变换与 DPR 缩放（T1 回归）`。
  - `packages/web/test/pixel-fixture.ts`：FakeCtx 变换跟踪/save/restore/setTransform、
    `setDocumentScroll`、`PngImage` 等支撑（仅带入 T1 测试所需部分）。
  - `packages/web/test/helpers.ts`（localStorage 清理）与 `a11y.test.ts` 的改动
    属 server_pref 工作，**不带入**。
- 组件 0.4.0 logProvider 行为（源码核验）：`open()` 时在 `!logsCollected && logs.length===0`
  自动 `collectLogs()`；组件内部 3 秒超时；MAX_LOG_FILES=3、MAX_LOG_BYTES=1MiB、
  扩展名白名单 .log/.txt/.json/.jsonl；迟到结果由 epoch/draft/opSeq/openState 检查拒收。

### 现场证据（~/Downloads）

- `desktop.jsonl`（85 行）：1.0.5/win32 两次 `diagnostics.export.ok`（17:27:21、17:27:27）
  及随后正常启动（sess-875103ec）；另有 embedding 404、启动超时、更新下载取消
  （均不在本次范围）。**无任何前端导航错误记录**。
- `backend.jsonl`（294 行）：后台运行记录，无导航相关事件。
- 结论与计划一致：附件不能证明 query IPC 正常，也不能证明 Worker 故障——T2 根因未定。

## 任务登记

| 编号 | 内容 | 状态 | 负责 |
|---|---|---|---|
| T1 | Feedback 截图遮罩修复包 0.4.1-studytool.1 | **完成**：tarball SHA `2c56eb…4bcf`，164 单测 + 52 真实浏览器遮罩断言全过；已 vendored 并接入前端/e2e | Worker A |
| T2a | 日志入口导航定位 + 外壳级错误处理 | **完成**：`useLogNavigation.ts` + `LogNavNotice.vue` + router.onError/埋点，nav-id 诊断链贯穿点击→守卫→挂载→首查 | Worker B |
| T2b | 按五项证据修复真实根因 | **实现中**：证据缺"失败阶段"与"具体错误"两项。嫌疑排序：auth 守卫挂起 ≈ 单 chunk 缺失/被杀软拦截/MIME 异常 > IPC/Worker。诊断候选版本已含取数埋点；现场复现步骤见 Worker B 交付 §7 | 主代理裁定 |
| T3a | 桌面受限日志采集（feedback Worker 通道） | **完成**：`feedback-logs.ts` + 独立通道，20/20 新测试、93/93 无回归；主进程 `desktop:getFeedbackLogs` 已接线 | Worker C |
| T3b | Feedback 面板 logProvider 接入 | **完成**：`feedbackLogs.ts` 适配 + `useFeedbackWidget` 注入，25/25 e2e 用例（真实附件字节/错误文案断言） | Worker D |
| T4 | 接线、构建门禁、E2E、Windows 候选包验收 | **进行中**：依赖已替换、e2e 项目已接线（59 通过/5 个 Windows 占位跳过）、构建门禁已加入 build-release.py（manifest 递归含 css/assets + 冻结后端 MIME + Worker unpacked 校验，含负向验证）；Reviewer 终审 R1-R3 已修。**CI 候选包已产出**：分支 `beta/windows-1.0.6-beta.1`（全部未提交工作区打包推送），workflow_dispatch run 35302981625 全部步骤通过（含 Windows 上的门禁、93 单测、NSIS），`publish=false` 发布步骤正确跳过。安装包 `rag-study-assistant-desktop-setup-1.0.6-beta.1.exe`（143 MB），SHA-256 `e7d44e24e686b8c12cdc9db93500ee7bf1630632bacc6c95e38aa06b1fbb882f`（SHA256SUMS/RELEASE_MANIFEST/下载文件三方一致）。**剩余：Windows 实机验收表 + T2b 现场取证** | 主代理 + Tester |

## 冻结 IPC 契约（T3a/T3b 唯一依据，两边逐字一致）

新增 `desktopAPI.getFeedbackLogs(): Promise<FeedbackLogsResult>`，
**不接受任何参数**（无路径、时间范围、来源、文件名或字节上限）。

```ts
type FeedbackLogSource = 'desktop' | 'backend' | 'frontend';

type FeedbackLogFilename =
  | 'studytool-desktop.jsonl'
  | 'studytool-backend.jsonl'
  | 'studytool-frontend.jsonl';

type FeedbackLogsResult =
  | {
      ok: true;
      files: Array<{
        filename: FeedbackLogFilename;
        text: string;
      }>;
      truncated: boolean;
      unavailable: FeedbackLogSource[];
      skippedLines: number;
    }
  | {
      ok: false;
      code:
        | 'LOG_FEEDBACK_BUSY'
        | 'LOG_FEEDBACK_TIMEOUT'
        | 'LOG_FEEDBACK_CANCELED'
        | 'LOG_FEEDBACK_UNAVAILABLE'
        | 'LOG_FEEDBACK_FAILED';
      message: string;
    };
```

固定响应语义：

| 情况 | 返回与宿主处理 |
|---|---|
| 有有效记录 | `ok:true`，转换为附件 |
| 部分来源缺失、其他来源有记录 | `ok:true`，返回有效附件；附件元信息说明缺失来源 |
| 所有来源可读但无符合条件记录 | `ok:true, files:[]`，不制造空文件 |
| 没有有效记录且存在不可读取来源 | `ok:false`（`LOG_FEEDBACK_UNAVAILABLE`） |
| 在途采集未结束又来新请求 | `LOG_FEEDBACK_BUSY`；不排队、不取消原请求 |
| 超时（2.5s，自接受任务起） | 终止采集 Worker，返回 `LOG_FEEDBACK_TIMEOUT` |
| Worker 崩溃或意外异常 | 归一化为 `LOG_FEEDBACK_FAILED`；不暴露原始系统路径 |
| 进程关闭 | 结束在途采集，返回 `LOG_FEEDBACK_CANCELED` |

正常失败经以上结构返回；IPC 本身断开仍可能 reject，由前端适配器处理。
不得把失败转成成功的空数组。

### Worker 任务载荷（main → worker，由主进程填值，渲染进程不可见）

```ts
interface FeedbackJobPayload {
  dir: string;        // currentLogDir()，主进程自取
  sessionId: string;  // SESSION_ID，仅本次启动
  appVersion: string; // app.getVersion()
  now: number;        // 接受请求时 Date.now()；worker 固定 to=now, from=now-30min
}
```

Worker 返回值即 `FeedbackLogsResult` 的 `ok:true` 变体
（`files`/`truncated`/`unavailable`/`skippedLines`）；
失败经调度器错误通道携带上述 `LOG_FEEDBACK_*` 码。

### 采集规则（冻结）

- 仅本次启动 `session_id`；时段 = 最近 30 分钟（开始后 from/to 固定不漂移）。
- 三个来源**分别**读取，每来源原始扫描 ≤1 MiB（合计 ≤3 MiB）。
- 每来源保留最近 ≤500 条匹配记录。
- 每输出文件 ≤256 KiB（**UTF-8 字节**，含元信息行与换行）。
- 只保留完整 JSONL 记录；不切断中文字符或 JSON。
- 超出任一限制 → `truncated:true`。
- 有记录的文件首行为采集元信息（版本、平台、采集时段、来源、记录数、
  截断标记、缺失来源、跳过行数）；不含日志目录与用户身份。
- 无匹配记录的来源不生成仅有元信息的文件。

## 文件写入归属（每文件唯一写入者）

| 文件 | 写入者 |
|---|---|
| Feedback 隔离副本内 `packages/web/src/capture.ts`、`test/masking.test.ts`、`test/pixel-fixture.ts`、`package.json` | Worker A |
| `frontend/src/components/Navbar.vue`、`router/index.ts`、`views/LogsView.vue`、新导航状态/提示模块（B 自命名） | Worker B |
| `desktop/src/log-workers.ts`、`log-worker.ts`、`log-worker-protocol.ts`、新采集模块 `desktop/src/feedback-logs.ts`、`desktop/src/tests/feedback-logs.test.ts` | Worker C |
| `frontend/src/composables/useFeedbackWidget.ts`、新适配模块 `frontend/src/utils/feedbackLogs.ts`、`e2e/specs/feedback-logs.spec.ts`（新文件） | Worker D |
| `desktop/src/main.ts`、`preload.ts`、`frontend/src/composables/useDesktop.ts`、`App.vue`、`main.ts(frontend)`、`package.json`/lock、`vite.config.ts`、`electron-builder.json5`、`build-release.py`、`e2e/playwright.config.ts`、其余共享/E2E 公共文件 | 主代理 |
| `e2e/specs/` 下 Tester 自命名的新 spec | Tester |
| 一切文件（只读） | Reviewer |

T2b 若需改 Worker C 文件：B 先交证据，主代理串行安排，不并行编辑。

## Reviewer 审查结论与修复（集成后终审）

Reviewer 对五处契约实现逐字核对一致；确认采集/Worker/适配/身份隔离/门禁语义正确。
三处应修项均已修复并回归验证：

| 编号 | 问题 | 修复 |
|---|---|---|
| R1 | `nav_id`/`trigger`/`to_name`/`elapsed_ms` 不在 `FIELD_WHITELIST`，nav 诊断字段被 scrubRecord 静默丢弃，nav-id 关联链不落盘 | 扩展 `desktop/src/diagnostics.ts` FIELD_WHITELIST + `backend/app/diagnostics.py` 镜像（四字段） |
| R2 | 已在 /logs 页再点「日志」→ `duplicated` 导航无后继收尾 → 假 pending → 10s 后误报"打开时间较长" | `useLogNavigation.ts` afterEach failure 分支对 duplicated+to=logs 执行 resetState |
| R3 | Worker 崩溃 message 未经脱敏过 IPC（可能含 unpacked 绝对路径） | `main.ts` getFeedbackLogs handler 返回前 `scrubString(message)` |

建议项处置：S1 已修（manifest 收集补 `css`/`assets` 数组）；S3 已修（e2e README sha/版本更新）；
S4 已修（feedbackLogs.ts 过时注释更正）；S2/S5 评估后可接受（sys.path 副作用仅限发布脚本进程；
asarUnpack 文本检查与 dist 文件存在性检查配套生效）。

修复后回归：desktop 93/93、backend diagnostics+release 49/49、前端 build 通过、e2e 59 通过/5 跳过。

## 与计划的偏差记录

- `.feedback-work/` 被 .gitignore 排除且不可写 → 本施工记录置于 `.devin/`。
- 组件 0.4.0 `collectLogs()` 自带 3 s 超时；桌面采集 Worker 冻结为 2.5 s
  （先于组件超时失败，错误经适配器转成中文提示，语义一致）。
- Worker C 仅能用 `log-store.ts` 已导出 API（该文件不在其独占清单）：
  按来源各调一次 `queryLogs({sources:[one], page:1, pageSize:500, maxScanBytes:1MiB, ...})`
  可满足"分来源 1 MiB / 500 条"约束。
- 前端无单元测试框架：Worker D 的测试落 `e2e/specs/feedback-logs.spec.ts`
  （Playwright，`page.addInitScript` 注入 `window.desktopAPI` 桩），
  `playwright.config.ts` 的项目接线由主代理在集成时补。
