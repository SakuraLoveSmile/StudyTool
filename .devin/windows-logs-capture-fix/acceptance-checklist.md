# T4 Windows 验收可行性与检查清单

创建：2026-09-18 · Tester（勘察与夹具准备，不动产品代码）
配套 spec：`e2e/specs/windows-acceptance.spec.ts`（7 个可跑用例 + 5 个 test.fixme 占位）

## 0. 一句话结论

- **本机（macOS）能验的**：导航「日志」入口门禁、日志页在 `window.desktopAPI` 桩下的
  筛选/翻页/刷新/错误态、受限按钮的 IPC 调用面、`data-feedback-capture-mask` DOM 标记、
  前端构建与 manifest 产物、desktop 单测（93 过）。**已全部跑通**。
- **本机绝不能产出的**：Windows onedir `backend.exe` 与 NSIS 安装包。
  PyInstaller 无交叉编译能力；`build-release.py` 在本机跑到 Step 6 会打印
  "非 Windows 环境绝不宣称生成 Windows 发行版" 并 `sys.exit(0)`。
- **唯一可信候选包来源**：CI `.github/workflows/windows-desktop-release.yml`
  （windows-2022）或用户本机 Windows 环境跑同一脚本。

## 1. e2e 能力勘察结论（可验证 / 不可验证矩阵）

现有资产：`playwright.config.ts` 四个 project（unconfigured@5274、theme@5274、
configured@5275、component@fixture:5276）；`helpers/host-page.ts` 提供宿主 `/api/*`
mock + 组件探针；`helpers/feedback-mock.ts` 拦截跨 origin 反馈服务请求；
`fixtures/widget-fixture.html` 直挂 UMD 组件。

| T4 验收项 | Playwright 可验证性 | 落点 |
|---|---|---|
| 「日志」导航入口门禁（desktopAPI 在 → 显示；不在 → 隐藏） | ✅ 真实验证（addInitScript 桩） | 本 spec 用例 1、2 |
| /logs 路由到达（点击导航而非直接 goto） | ✅ 真实验证 | 本 spec 用例 1 |
| 日志页筛选（关键词防抖/级别/会话/时间范围参数编码） | ✅ 真实验证（桩断言 IPC 参数） | 本 spec 用例 3 |
| 翻页/手动刷新/自动刷新 | ✅ 真实验证 | 本 spec 用例 3、6 |
| 错误结构 → 错误提示而非零条记录 | ✅ 真实验证（冻结失败结构 ok:false+code） | 本 spec 用例 5 |
| 「打开日志目录」「导出诊断包」调用受限通道 + 取消回执 | ✅ 调用面与提示文案可验；真实目录弹窗/落盘不可 | 本 spec 用例 4 |
| `data-feedback-capture-mask` 存在（导航账号区、日志详情子树） | ✅ DOM 级断言 | 本 spec 用例 7 |
| 截图遮罩**像素级**正确性 | ❌ 归 Worker A 修复包 masking 回归 + Windows 实机 | 不重复（fixme） |
| 反馈附件真实字节（getFeedbackLogs→logProvider→multipart） | ❌ 归 Worker C/D + Windows 包 | fixme（浏览器侧归 Worker D spec） |
| 干净安装 / 1.0.5 升级 / 四档缩放 | ❌ 必须 Windows 候选包 | fixme |
| 授权服务提交 | ❌ 必须测试服务 + 专用构建 | fixme |

**注意**：`window.desktopAPI` 在真实 Electron 里由 preload `contextBridge` 注入；
浏览器桩只模拟 IPC 边界形状（`useDesktop.ts` 的 TypeScript 声明），不能证明
preload/IPC 通道本身健康——那部分只能由 Windows 包验收。

## 2. Windows 验收清单（逐项标注）

| # | 验收项 | 本机可验证 | 需 CI/Windows | 需授权测试服务 | 当前阻塞 |
|---|---|---|---|---|---|
| W1 | 干净安装 → 首启 → 日志页读真实 jsonl | — | ✅ | — | 需候选包 |
| W2 | 1.0.5 → 候选版升级，数据/日志目录保留 | — | ✅ | — | 需 1.0.5 已装环境 + 候选包 |
| W3 | 日志页读取/交互/异常提示（真 IPC） | 桩下已验 ✅ | ✅（真 Worker/真文件） | — | 需候选包 |
| W4 | 打开日志目录弹出真实目录；导出诊断包产出 zip | — | ✅ | — | 需候选包 |
| W5 | 四档缩放截图（OS 缩放 × Electron 截图管线） | — | ✅ | — | 需候选包 + 缩放档定义 |
| W6 | 隐私遮罩像素级遮挡（账号区/日志详情） | DOM 级已验 ✅ | ✅（像素） | — | 需候选包 |
| W7 | 日志附件：3 来源、30min、本会话、≤256KiB/文件、无空文件、BUSY/TIMEOUT 提示 | — | ✅ | 提交闭环需测试服务 | Worker C 已落地（feedback-logs.ts/log-worker 三通道）；`desktop:getFeedbackLogs` main.ts handler 与 Worker D logProvider 未集成 |
| W8 | 授权测试服务端到端提交 | — | ✅ | ✅ | 需测试服务地址 + 指向它的专用构建（发行门禁锁死生产地址，候选发行包不可用于提交验证） |

**用户/主代理需提供**：候选安装包（CI artifact 或本机 Windows 构建产物）、
已装 1.0.5 的 Windows 机器（或允许 CI 产物在测试机上安装）、授权测试反馈服务地址
（可选：仅 W7 提交闭环/W8 需要）、四档缩放的具体档位定义（建议 OS 显示缩放
100%/125%/150%/175%）。

## 3. CI 路径核实（`.github/workflows/windows-desktop-release.yml`）

- **触发**：`push` tag `desktop-v*`；或 `workflow_dispatch` 必填 `version`
  （必须与 `desktop/package.json` 版本一致，当前 1.0.5，否则 Validate 步骤抛错）。
- **Runner**：`windows-2022`；Node 22 + Python 3.12 x64；`npm ci`（frontend/desktop）+
  `pip install --require-hashes -r backend/requirements.lock` + `pyinstaller==6.16.0`。
- **门禁**：`VITE_FEEDBACK_API_BASE=https://feedback.xn--fhqths51enha.cn` /
  `VITE_FEEDBACK_APP_ID=studytool` 硬校验（与 build-release.py 常量一致）后才构建。
- **产物形态**：`desktop/release/` 下 NSIS `rag-study-assistant-desktop-setup-<ver>.exe`
  + `*.blockmap` + `latest*.yml` + `RELEASE_MANIFEST.json` + `SHA256SUMS.txt`；
  上传为 workflow artifact `rag-study-assistant-windows-<ver>`，
  并发布到 `SakuraLoveSmile/StudyTool-Releases` 的 `desktop-v<ver>` release
  （需 secret `RELEASE_REPO_TOKEN`）。
- **验收取包建议**：在集成完成后的提交上 `workflow_dispatch` 触发（version=候选版本），
  下载 artifact zip 给 Windows 测试机；或建 `desktop-v<ver>` tag 走正式发布路径
  （会写 Releases 仓库，谨慎）。

## 4. `desktop/scripts/build-release.py` 在 macOS 上的可行性（逐阶段）

| 阶段 | 本机能否跑 | 说明 |
|---|---|---|
| 0 环境检查 | ✅（仅告警） | 非 win32 且无 STRICT_RELEASE_ENV 时只 warning（本机 Python 3.10.10 → 告警但继续） |
| 0b 反馈配置门禁 | ✅ | 纯函数注入常量；环境里有漂移的 VITE_FEEDBACK_* 会 fail |
| 1 清理 | ✅ | 会删 `frontend/dist`、`desktop/build`、`desktop/release` 等 |
| 2 前端构建 | ✅ | `npm run build`（vue-tsc + vite）本机可跑 |
| 2b 产物文本门禁 | ✅ | 扫描 dist/assets/*.js 找 host+appId 字面量 |
| 3 PyInstaller onedir | ⚠️ 能跑但产物无用 | **产出 macOS Mach-O `desktop/build/backend/backend`（非 backend.exe）**。PyInstaller 不支持交叉编译；spec `name='backend'`，Windows 后缀只在 win32 加 |
| 3b verify-windows-backend | 跳过 | `sys.platform=="win32" or FORCE_BUILD_WIN` 才跑 |
| 4 desktop tsc | ✅ | |
| 5 desktop 单测 | ✅ | 本机已跑：93 pass / 0 fail |
| 6 electron-builder NSIS | ❌ 跳过 | 非 win32 → 打印 Notice + `sys.exit(0)`，**绝不产出安装包** |
| 7 SHA256/清单 | 不可达 | |

**FORCE_BUILD_WIN=1 也不能救**：会让 3b 去跑 `backend_out/backend.exe`（不存在，直接失败）；
即便绕过，electron-builder 会把 macOS Mach-O 二进制当 backend 打包——
`backend-manager.ts:65` 在 win32 下找 `resources/backend/backend.exe`，
包里没有/格式不对，装了也启动不了。**结论：macOS 上产出的任何 NSIS 都是残次品，不得用于验收。**

## 5. 构建门禁勘察（为集成准备）

### 5.1 Vite manifest（已在工作区启用）

`frontend/vite.config.ts` 已加 `build.manifest: true`（未提交改动）。
本机实测 `npx vite build`（Vite 8.2.2）产出 `dist/.vite/manifest.json`，结构：

```jsonc
{
  "index.html": { "file": "assets/index-*.js", "isEntry": true,
                  "dynamicImports": ["src/views/LogsView.vue", "…"],
                  "css": ["assets/index-*.css"] },
  "src/views/LogsView.vue": { "file": "assets/LogsView-*.js",
                  "isDynamicEntry": true, "imports": ["_refresh-cw-*.js", "index.html"] },
  "_refresh-cw-*.js": { "file": "assets/refresh-cw-*.js", "imports": ["index.html"] }
}
```

检查算法（最小脚本思路）：读 manifest → 对每个条目的 `file`、递归 `imports`/
`dynamicImports`、以及 `css[]`/`assets[]` 收集目标路径 → 全部必须存在于 dist
且可被服务。懒加载路由（/logs、/settings、feedback-web、html2canvas）就是
`isDynamicEntry` 条目——它们缺失会在安装包里表现为「点击无反应」。

### 5.2 冻结后端资源完整性检查（对跑起来的 backend 做 HTTP 核对）

`backend/app/desktop.py` 服务链：
- `app.mount("/assets", StaticFiles(assets_dir))`（desktop.py:1080）——
  /assets 下文件直接服务，**缺失即 404，不会落到 catch-all**（Mount 先匹配）。
- `@app.get("/{full_path:path}")` SPA catch-all（desktop.py:1084-1099）——
  非 /api 路径：真实文件 → FileResponse（按扩展名推 MIME）；不存在 → index.html。
- `frontend_dist` 经 `backend.spec` datas 整树进 onedir（`_MEIPASS/frontend_dist`）。

最小脚本思路（对活后端 `http://127.0.0.1:<port>`，带 `X-RAG-Instance-Token` 非必需——
非 /api 路径 middleware 放行）：

1. `GET /.vite/manifest.json` → 200 + JSON（catch-all 会把它当真实文件返回；
   若 404/回 index.html 说明 manifest 没进 dist 或没进 datas）。
2. 递归收集所有 `file`/`css`/`assets` 路径 → `GET /<path>`：
   - 必须 200；
   - `.js` → `text/javascript` 或 `application/javascript`；`.css` → `text/css`；
   - **响应体不得以 `<!doctype html`/`<html` 开头**（index.html 冒充 = 服务链坏或文件缺失）。
3. 负向控制：`GET /assets/__missing_<rand>.js` → 必须 404（证明 /assets Mount 生效、
   缺失 chunk 不会被 index.html 吞掉）。
4. `GET /logs` → 200 + text/html + 是 index.html（SPA 路由回退正确）。

### 5.3 electron-builder asarUnpack 与 Worker 入口（`desktop/electron-builder.json5`）

- `asarUnpack: ["dist/**/*"]`——整个编译目录解包；`files` 同时含 `dist/**/*`，
  即产物在 `resources/app.asar.unpacked/dist/`。
- `defaultWorkerPath()`（log-workers.ts:63-65）把 `app.asar` 重写为
  `app.asar.unpacked`；`unpackedWorkerPath` 有单测覆盖（log-workers.test.ts:251-268，
  本机已跑过）。
- **检查点（候选包上验）**：
  - `resources/app.asar.unpacked/dist/log-worker.js` 存在（Worker 不能从 asar 里构造）；
  - `resources/app.asar.unpacked/dist/feedback-logs.js`、`log-store.js`、
    `diagnostic-export.js`、`diagnostics.js` 同在（worker require 链全在 asar 外）；
  - Worker C 落地后 worker 入口仍是单文件 `log-worker.js`（query/export/feedback 三种 job），
    asarUnpack 覆盖不变——但新文件（feedback-logs.js）必须确认进了 `dist/`，
    即 `npm run build`（tsc）编译输出包含它；
  - `extraResources: build/backend → resources/backend`：Windows 包内必须有
    `backend.exe`（win32 下 backend-manager.ts:65 按名查找）。
- 已确认现状（勘察期间其他代理陆续落地）：
  - Worker C：`desktop/src/feedback-logs.ts` + `tests/feedback-logs.test.ts` 已落地；
    `log-worker-protocol.ts` 增 `kind:'feedback'` + `FEEDBACK_ERROR_CODES`；
    `log-workers.ts` 增独立 feedback 通道（rejectWhenBusy→LOG_FEEDBACK_BUSY，
    错误码归一化 LOG_FEEDBACK_*）；`preload.ts` 已暴露 `getFeedbackLogs`。
  - Worker B：`frontend/src/composables/useLogNavigation.ts` +
    `components/LogNavNotice.vue` 已落地（点击日志入口 → pending/slow/failed
    外壳提示 + nav-<id> 诊断编号）；Navbar 入口加了 @click 埋点，
    **本 spec 的点击→/logs 断言在新代码下仍全部通过（复跑确认）**。
  - Worker D：`frontend/src/utils/feedbackLogs.ts`、`e2e/specs/feedback-logs.spec.ts` 已落地。
  - Worker A：`frontend/vendor/feedback-web-0.4.0.tgz` 已被删除、
    `feedback-web-0.4.1-studytool.1.tgz` 已放入 vendor。
  - **缺口**：`main.ts` 尚无 `ipcMain.handle('desktop:getFeedbackLogs')`（主代理接线中）；
    `useFeedbackWidget.ts` 的 logProvider 接线未确认。

### 集成风险提示（新发现，转主代理）

- `e2e/package.json` 依赖 `file:../frontend/vendor/feedback-web-0.4.0.tgz`——
  该 tarball **已被 Worker A 删除**。`npm ci`/`npm install` 会失败；
  现有 `e2e/node_modules` 残留可跑，但集成时必须把 e2e 依赖指向 0.4.1 新包。
- `e2e/specs/component-level.spec.ts` 硬编码 0.4.0 tarball 路径与 sha256
  门禁，同样会随换包失效——归主代理接线时一并处理（非 Tester 文件）。
- `e2e/specs/feedback-logs.spec.ts`（Worker D）与我的 spec 一样需要
  config 项目接线；建议主代理为两者统一建 desktop-stub project。

## 6. 跑过的命令与结果

| 命令 | 退出码 | 结果 |
|---|---|---|
| `npx vite build`（frontend/） | 0 | 1853 modules；产出 dist/.vite/manifest.json（结构已核验） |
| `npm test --prefix desktop` | 0 | 93 pass / 0 fail |
| `npx playwright test specs/windows-acceptance.spec.ts`（e2e/，官方 config） | 1 | "No tests found"——现有 testMatch 均按文件名锁定，不覆盖新文件（预期；接线归主代理） |
| `npx playwright test --config ../.devin/windows-logs-capture-fix/playwright.acceptance.config.mjs` | 0 | **7 passed / 5 skipped（test.fixme）** |

临时 config（`.devin/…/playwright.acceptance.config.mjs`）只为本机验证，用 5280 端口、
未配置形态（VITE_FEEDBACK_* 空）；spec 与反馈配置无关，任意 dev-server 形态都能跑。

## 7. 集成后需要主代理提供

1. `playwright.config.ts` 新增 project（建议）：
   ```ts
   { name: 'windows-acceptance',
     use: { ...devices['Desktop Chrome'], baseURL: 'http://127.0.0.1:5274' },
     testMatch: /specs\/windows-acceptance\.spec\.ts/ }
   ```
   （5274/5275 均可；5274 未配置形态更干净。也可与 Worker D 的 feedback-logs.spec.ts
   共用一个 desktop-stub project，但 testMatch 需分别写或按前缀匹配。）
2. 候选安装包路径：CI `workflow_dispatch` 产物的下载方式/版本号，
   或用户 Windows 机自建的 `desktop/release/*.exe` 路径。
3. 授权测试反馈服务地址（若要做 W7 提交闭环/W8）与对应专用构建方式。
4. 四档缩放的具体档位定义（建议 OS 显示缩放 100%/125%/150%/175%，待确认）。
5. main.ts `desktop:getFeedbackLogs` handler 落地后，W7 的 Windows 包验收才可启动。

## 8. 不需要动独占清单外文件

本次仅新增：`e2e/specs/windows-acceptance.spec.ts`、
`.devin/windows-logs-capture-fix/playwright.acceptance.config.mjs`、
`.devin/windows-logs-capture-fix/acceptance-checklist.md`（本文件）。
未改任何产品代码、helpers、playwright.config.ts、依赖清单。
`frontend/dist/` 为 gitignore 构建产物，`npx vite build` 刷新过属正常副作用。
