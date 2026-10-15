# StudyTool Connected（Windows 联网版）

独立的联网客户端。首次打开填写 `https://learn.example.com` 形式的服务地址，随后进入该服务的 `/library`。先在服务端创建账号，再在页面中登录、上传 PDF / DOCX、使用知识库与 AI 问答。模型与 OCR 配置由服务端统一保存。

这份客户端不会启动本地 Python 后端。它与原有 `desktop/` 本地版分别安装，使用独立的应用标识、快捷方式和数据目录，不会迁移原本地版的教材与学习数据。

## 开发与验证

需要 Node.js 22.12.0 或更新版本；在本目录执行：

```sh
npm ci
npm run build
npm test
npm start
```

`start` 先通过 `install-electron` 安装 / 复用本机运行时（Electron 44 不再通过 npm postinstall 自动下载）；也可单独运行 `npm run setup:runtime`。Windows 打包由 electron-builder 下载对应 Windows 运行时。

`build` 检查脚本语法；`test` 包括地址校验、导航与新窗口策略、本地设置 IPC 的窗口 / 主 frame / URL 校验、会话清理、配置持久化、单实例保护及 NSIS 模板安全补丁的哈希门禁。

如需隔离开发体验，可在未打包运行时设置绝对路径 `STUDYTOOL_CONNECTED_DEV_PROFILE`。例如 macOS / Linux：

```sh
STUDYTOOL_CONNECTED_DEV_PROFILE=/tmp/studytool-connected-preview npm start
```

该环境变量在安装包中不生效。开发模式也只接受 HTTPS，不绕过证书错误。

## 构建 Windows 安装包

在 Windows x64 环境运行：

```powershell
npm ci
npm test
npm run pack
```

生成 `release/studytool-connected-setup-0.1.0-x64.exe`；版本变化后文件名随 `package.json` 更新。`npm run pack:dir` 可生成免安装的 `win-unpacked` 构建目录。两条命令均显式设置 `--publish never`，不会创建 GitHub Release 或上传产物。

新联网版固定 Electron 44.7.2（本次核对的 npm 稳定版），沿用现有桌面版的 electron-builder 25.1.8 和 NSIS `multiUser.nsh` 安全补丁模式。打包前校验版本及模板哈希，未知漂移直接失败。模板补丁只修改当前包自己的 `node_modules`，不依赖修改原 `desktop/`。

默认未配置代码签名，Windows 可能显示未知发布者或 SmartScreen 提示。正式签名需另行提供签名配置；构建通过不代表已签名、已发布或已在 Windows 安装验收。本客户端目前没有自动更新机制。

在一次性 Windows CI runner 中可运行安装启动冒烟：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/smoke-installer.ps1 -InstallerPath release/studytool-connected-setup-0.1.0-x64.exe -EvidenceDir release/smoke
```

脚本要求 `CI=true` 或 `GITHUB_ACTIONS=true`，检测已有联网版安装或进程时拒绝执行。仅安装到本次创建的临时目录，使用新临时 profile 检查首开设置窗口、正常退出并卸载，输出 `smoke-summary.json`；不访问外部 AI 服务。专用 `--smoke-test-profile=` 参数仅接受系统 temp 的全新直接子目录，名称必须以 `studytool-connected-smoke-` 开头，不能覆盖任意已有数据目录。此检查不能代替 Windows 上的登录、上传和知识库真实体验。

## 使用与数据边界

- 在菜单“学习服务 → 连接设置 / 切换服务”修改地址；“重试连接”重新加载学习页。连接失败会返回本地设置页，可重试或改地址。查看教材原文件后，可通过菜单“后退”（Alt+Left）或“返回知识库”继续学习。
- 地址只能包含 HTTPS 域名和可选端口，可带末尾 `/`；不接受账号、路径、查询参数或片段。需要先部署服务器及有效 HTTPS 证书。
- 切换为不同地址时，先清除本客户端原服务的 cookie、站点存储、HTTP 认证和缓存，再保存新地址；需要在新服务重新登录。原服务上的资料不受影响。
- Windows 用户数据目录为 `%APPDATA%\StudyTool Connected`，`service.json` 仅保存服务地址；会话保存在独立 Electron 分区。卸载默认保留该目录。
- 远程页面无 preload、Node.js、`desktopAPI` 或本地设置 IPC；登录和 CSRF 均沿用服务端网页。教材上传使用标准文件选择器。
- 学习页的主导航只允许当前同源地址。外部 HTTP / HTTPS 链接交给系统浏览器，危险协议被拒绝，不创建任意新窗口；设备权限默认拒绝。TLS 校验保持启用。

## 本次证据边界

本次已在 macOS 使用经官方校验的 Windows Electron 运行时交叉构建 NSIS 安装包，并检查 ASAR 中的源码与工作区一致，构建证据位于忽略目录 `release/build-verification.json`。这与 Windows 安装、真实服务连通是不同验证：安装启动冒烟仍需 Windows 构建工作流或 Windows 主机；公网服务部署、账号登录及真实知识库上传需在服务可用后继续验收。
