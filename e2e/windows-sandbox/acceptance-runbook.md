# 1.0.6-beta.6 Windows 沙盒与安装兼容性验收执行手册

适用候选包：`rag-study-assistant-desktop-setup-1.0.6-beta.6.exe`（CI `workflow_dispatch` 构建产物）  
核心背景：彻底防范发布未经安装验证的安装包。此前 beta.4 在 Windows 11 最新构建（如 build 26200）上曾出现过 NSIS 启动时 `System.dll` 崩溃（异常代码 `0xc0000005`）的问题。本次 beta.6 针对 NSIS 打包插件与安装向导进行了专项加固，特制定本手册用于全方位验收安装稳定性与系统兼容性。  
执行环境要求：
1. **原故障电脑专项验收**：Windows 11 x64（推荐 build 26200 或更新的 Canary/Dev/Release 预览分支）；
2. **多场景兼容性验收**：Windows 10 x64（标准企业版/专业版，版本 21H2/22H2）；
3. **隔离执行方式**：在一次性测试机、Windows Sandbox、虚拟机，或使用配套 `sandbox-setup.ps1` 创建的临时隔离账号（如 `st-beta6-sandbox`）中操作。

配套脚本：
- `desktop/scripts/smoke-windows-installer.ps1`（自动化安装冒烟与崩溃取证脚本）
- `e2e/windows-sandbox/sandbox-setup.ps1`（沙盒临时隔离账号创建）
- `e2e/windows-sandbox/sandbox-teardown.ps1`（收尾清理，**强制执行**）

> 填写规范：本手册采用逐项勾选的回填模式。每步均列出「操作步骤 → 预期现象 → 需回传证据」，验收人在完成测试后，请在末尾表格直接填写结果（通过 / 失败 + 现象描述）并归档截图。截图统一命名 `ev-<编号>-<简述>.png`。

---

## 0. 三条红线（动手前必须通读）

1. **严禁触碰真实数据，全流程使用合成数据（Mock 数据）**：
   - 严禁在测试机的主账号个人目录或生产数据库中直接进行测试；
   - 验收过程中的备考材料、用户配置一律使用合成测试数据（mock 资料），严禁导入个人私密文档或真实业务数据；
   - 本机若已安装正在使用的真实应用，绝不可在同一用户目录下混跑，必须切入沙盒或临时测试账号。
2. **严禁向生产接口提交数据**：
   - 反馈面板与外部接口测试只验证到界面渲染正常、附件成功抓取与提交按钮显示；
   - 严禁点击生产环境的最终提交按钮（出现提交按钮即达标停手），避免污染后端线上服务。
3. **严格执行测试前后隔离与收尾清理**：
   - 运行任何冒烟脚本或手动测试前，必须确保目标测试环境处于「全新干净」状态；
   - 测试结束后，必须执行 `sandbox-teardown.ps1` 彻底清理临时账号、注册表残留及文件系统缓存。

---

## 1. 前置准备与环境初始化

### 1.1 候选包完整性校验（主账号或任意会话）

- 取得 CI 构建产物 `rag-study-assistant-desktop-setup-1.0.6-beta.6.exe` 与对应的 `SHA256SUMS.txt`。
- 在 PowerShell 中执行哈希计算：

  ```powershell
  certutil -hashfile .\rag-study-assistant-desktop-setup-1.0.6-beta.6.exe SHA256
  Get-Content .\SHA256SUMS.txt
  ```

- **预期**：certutil 输出的 SHA256 哈希值与 `SHA256SUMS.txt` 中的记录完全一致。
- **证据**：`ev-PRE-01-sha256.png`。

### 1.2 创建沙盒隔离账号（若在物理机/宿主机测试）

若使用 Windows Sandbox 或一次性虚拟机，可直接跳过本小节；若在日常物理测试机上，必须创建独立临时账号：

```powershell
# 以管理员身份打开 PowerShell
cd <项目根目录>\e2e\windows-sandbox
powershell -ExecutionPolicy Bypass -File .\sandbox-setup.ps1 -UserName st-beta6-sandbox
```

- 记录脚本生成的临时密码；
- 按照指引生成用户 Profile（`runas /user:st-beta6-sandbox "cmd /c whoami"`）；
- 锁屏切换至 `st-beta6-sandbox` 用户登录。
- **证据**：`ev-PRE-02-setup.png`。

### 1.3 隔离基线验证（安装前检查）

在测试会话（临时账号或沙盒）中执行：

```powershell
whoami                                            # 预期: <计算机名>\<测试账号名>
echo $env:LOCALAPPDATA                            # 预期测试账号的独立 AppData 路径
Test-Path "$env:LOCALAPPDATA\Programs\RAG备考助手" # 预期: False（未安装）
Test-Path "$env:LOCALAPPDATA\RAGStudyAssistant"   # 预期: False（数据与日志未生成）
Get-Process | Where-Object { $_.Name -match 'RAG|backend' } # 预期: 无任何输出
```

- **预期**：所有路径均为全新未初始化状态，无残留进程。
- **证据**：`ev-PRE-03-isolation.png`。

---

## 2. 自动化冒烟脚本运行指南

在进入复杂的手工交互测试之前，必须先在干净的一次性测试环境运行项目提供的安装冒烟脚本。

### 2.1 脚本功能与机制
脚本文件：`desktop/scripts/smoke-windows-installer.ps1`
- **环境预检（防误触）**：检查注册表与安装目录，如果发现已有安装或残留进程，**立即中止并拒绝触碰**，保护非沙盒环境；
- **静默安装执行**：调用 `/S /currentuser` 进行安装，支持指定超时限制（默认 180 秒）；
- **崩溃日志提取**：无论测试结果如何，均会自动扫描系统最近 10 分钟的 Application 与 System 事件日志，定向过滤 `System.dll`、`0xc0000005`、`Application Error` 等崩溃记录；
- **完整性校验**：自动校验安装进程退出码、主程序可执行文件大小与存在性、注册表卸载信息。

### 2.2 运行方式
在测试会话中打开 PowerShell，运行以下命令：

```powershell
# 准备输出目录
$EvidenceDir = "C:\TestEvidence\beta6-smoke"

# 执行自动化冒烟脚本
powershell -ExecutionPolicy Bypass -File .\desktop\scripts\smoke-windows-installer.ps1 `
  -InstallerPath ".\release\rag-study-assistant-desktop-setup-1.0.6-beta.6.exe" `
  -EvidenceDir $EvidenceDir `
  -TimeoutSeconds 180
```

### 2.3 证据检查与合格判定
运行结束后，进入 `$EvidenceDir` 检查以下产物：
1. `smoke-summary.json`：
   - `verdict` 必须为 `"SUCCESS"`；
   - `installer_exit_code` 必须为 `0`；
   - `installed_exe_exists` 必须为 `true`；
   - `uninstall_reg_exists` 必须为 `true`；
   - `critical_system_dll_crash` 必须为 `false`。
2. `installer-crash-events.txt`：
   - 查看文本内容，确认包含 `【正常】未检索到任何相关的错误或崩溃日志事件`，或者确认无任何与 `System.dll`、`0xc0000005` 相关的错误事件。
- **证据**：`ev-SMOKE-01-summary.png`（或保存 json/txt 文本）。

---

## 3. 核心验收用例清单

### 【第 I 部分：原故障电脑专项验收（Windows 11 build 26200）】
> 目标：复核并确保在曾发生 `System.dll` 崩溃的高版本 Windows 11 环境中，安装向导生命周期彻底稳定，无任何隐式崩溃。

#### 用例 W11-1 · 连续 3 次打开安装向导并取消（防崩溃核心验证）
- **背景**：beta.4 在向导解压初始化及取消退出时，由于 NSIS 模板内插件调用时序问题，曾触发 0xc0000005 访问违规。
- **操作步骤**：
  1. 双击 `rag-study-assistant-desktop-setup-1.0.6-beta.6.exe` 启动安装向导；
  2. 出现向导欢迎界面后，不点击下一步，直接点击窗口上的「取消」按钮，并在确认弹窗中选择「是」退出向导；
  3. **重复执行上述步骤 1~2 连续 3 次**。
- **预期结果**：
  - 3 次均能顺畅打开向导，点击取消后向导平稳关闭；
  - 无任何错误提示弹窗，进程正常销毁；
  - 检查系统事件日志，确认未产生任何 `Application Error` 或 `System.dll` 记录。
- **证据文件**：`ev-W11-1-cancel-3x.png`（第 3 次取消操作截图）。

#### 用例 W11-2 · 第 4 次完整向导安装
- **操作步骤**：
  1. 在完成 W11-1 的 3 次取消后，第 4 次双击打开安装包；
  2. 正常走完安装向导：点击「下一步」→ 查看并保持默认安装目录 → 点击「安装」；
  3. 等待安装进度条走完，到达「正在完成 RAG 备考助手 安装向导」页面；
  4. 勾选「运行 RAG 备考助手」，点击「完成」。
- **预期结果**：
  - 安装向导各页面渲染正常，进度条流畅无卡死；
  - 顺利完成安装并成功拉起主程序，展示初始化或登录界面。
- **证据文件**：`ev-W11-2-wizard-finish.png`。

#### 用例 W11-3 · 桌面与开始菜单快捷方式启动验证
- **操作步骤**：
  1. 完全退出当前运行的应用（右上角关闭，确认进程退出）；
  2. 检查 Windows 桌面，双击桌面快捷方式「RAG专业课备考助手」启动应用；
  3. 退出应用；
  4. 打开 Windows 开始菜单，在所有应用中找到「RAG专业课备考助手」，点击启动应用。
- **预期结果**：
  - 桌面与开始菜单快捷方式图标正常显示；
  - 两种方式均能正常秒级唤起主程序，前端页面与 backend 进程正常运转。
- **证据文件**：`ev-W11-3-desktop-start.png`、`ev-W11-3-startmenu-start.png`。

#### 用例 W11-4 · 进程彻底退出与冷重启验证
- **操作步骤**：
  1. 在应用主界面点击关闭按钮退出；
  2. 打开任务管理器（`Taskmgr`）或 PowerShell 执行 `Get-Process | ? { $_.Name -match 'RAG|backend' }`，核查进程树；
  3. 等待 3 秒，再次双击快捷方式启动应用（冷重启）。
- **预期结果**：
  - 应用关闭后，主进程 `RAG备考助手.exe` 与后端子进程 `backend.exe` 在 2 秒内干净退出，无后台僵尸进程残留；
  - 二次冷重启正常，各功能就绪。
- **证据文件**：`ev-W11-4-clean-exit.png`、`ev-W11-4-cold-restart.png`。

#### 用例 W11-5 · Windows 事件查看器深度审查
- **操作步骤**：
  1. 按 `Win + R` 输入 `eventvwr.msc` 打开事件查看器；
  2. 导航至「Windows 日志」→「应用程序」；
  3. 在右侧点击「筛选当前日志...」，事件级别勾选「关键」「错误」「警告」，事件来源筛选 `Application Error` 与 `Windows Error Reporting`；
  4. 查看最近 30 分钟内与安装包、`RAG备考助手.exe` 相关的记录。
- **预期结果**：
  - 事件列表中**绝无**涉及 `System.dll` 或错误代码 `0xc0000005` 的故障条目；
  - 无与安装向导有关的致命异常。
- **证据文件**：`ev-W11-5-eventvwr-clean.png`。

---

### 【第 II 部分：Windows 10 x64 兼容性验收】
> 目标：验证安装包在标准 Windows 10 x64 平台上的路径兼容性、升级稳定性及数据保留策略。

#### 用例 W10-1 · 默认路径安装
- **操作步骤**：
  1. 在干净的 Windows 10 环境中运行安装包；
  2. 选择默认路径（`%LOCALAPPDATA%\Programs\RAG备考助手`）安装完成。
- **预期结果**：
  - 安装成功，主可执行文件生成在默认目录，extraResources (backend) 完整解压释放；
  - 启动后应用主界面与后台通讯正常。
- **证据文件**：`ev-W10-1-default-path.png`。

#### 用例 W10-2 · 中文路径安装验证
- **操作步骤**：
  1. 在干净的测试会话中，运行安装包进入向导；
  2. 在「选择安装位置」页面，点击「浏览」，手动修改为含中文字符的路径，例如：
     `C:\测试 目录\StudyTool`；
  3. 执行安装并启动。
- **预期结果**：
  - NSIS 安装向导无中文乱码，文件正常释放至目标目录；
  - 主程序启动无白屏；
  - Python backend 能够正确解析含中文字符的工作目录与日志路径，正常提供本地服务。
- **证据文件**：`ev-W10-2-chinese-path.png`。

#### 用例 W10-3 · 带空格路径安装验证
- **操作步骤**：
  1. 运行安装包，自定义安装目录为带空格的路径，例如：
     `C:\My Custom Apps\RAG Study Assistant`；
  2. 执行安装直至完成，通过生成的桌面快捷方式启动。
- **预期结果**：
  - 安装顺利完成，快捷方式属性中的可执行路径带正确的双引号转义；
  - 主程序拉起 backend 进程时，命令行参数路径正确包裹转义，无截断或解析失败异常。
- **证据文件**：`ev-W10-3-space-path.png`。

#### 用例 W10-4 · 覆盖升级与 AppData 数据保留（合成 Mock 数据）
- **操作步骤**：
  1. 在测试机上预先安装历史版本（如 1.0.5 或 beta.3）；
  2. 启动旧版本，生成合成的 mock 备考材料和学习配置（生成测试科目、导入一段 mock 文本）；
  3. 查看数据目录 `%LOCALAPPDATA%\RAGStudyAssistant\data` 与 `logs`，记录文件快照：
     `Get-ChildItem -Recurse "$env:LOCALAPPDATA\RAGStudyAssistant" | Select-Object FullName`
  4. 不卸载旧版本，直接双击运行 `rag-study-assistant-desktop-setup-1.0.6-beta.6.exe` 执行覆盖安装；
  5. 安装完成后启动应用。
- **预期结果**：
  - 安装向导平稳覆盖旧二进制程序，无文件占用报错；
  - 启动后，此前生成的 mock 备考科目、历史记录完好无损；
  - `%LOCALAPPDATA%\RAGStudyAssistant` 目录下的用户数据库与配置完全保留。
- **证据文件**：`ev-W10-4-upgrade-preserve.png`。

#### 用例 W10-5 · 卸载后重装验证（契约：deleteAppDataOnUninstall = false）
- **操作步骤**：
  1. 在存在 mock 数据的环境中，进入「Windows 设置」→「应用」→「已安装的应用」（或控制面板「程序和功能」）；
  2. 找到「RAG备考助手」，点击「卸载」，按向导完成卸载；
  3. 检查程序目录：`Test-Path "$env:LOCALAPPDATA\Programs\RAG备考助手"`；
  4. 检查用户数据目录：`Test-Path "$env:LOCALAPPDATA\RAGStudyAssistant"`；
  5. 再次运行 1.0.6-beta.6 安装包完成全新安装并启动。
- **预期结果**：
  - 卸载后，程序文件目录被完全清理（输出 False）；
  - **用户数据目录 `%LOCALAPPDATA%\RAGStudyAssistant` 完整保留（输出 True）**，契约 `deleteAppDataOnUninstall: false` 严格生效；
  - 重新安装后启动，原有 mock 资料依然存在，无需重新配置。
- **证据文件**：`ev-W10-5-uninstall-retain.png`、`ev-W10-5-reinstall-ok.png`。

---

### 【第 III 部分：既有核心功能与沙盒回归】
> 目标：回归确认升级与安装加固未对 beta.3 验收冻结的核心功能（反馈面板、日志附件采集、ACL 容灾等）造成破坏。

#### 用例 REG-1 · 反馈面板日志附件自动采集
- **操作步骤**：启动应用，点击顶部「日志」导航（产生前端日志），点击右侧反馈悬浮球打开反馈面板。
- **预期**：
  - 自动采集出现 `studytool-desktop.jsonl`、`studytool-backend.jsonl`、`studytool-frontend.jsonl` 附件卡片；
  - 徽标显示「自动」，计数显示 `(3/3)`（或根据实际活跃来源计数）；
  - 严禁出现空文件。
- **证据文件**：`ev-REG-1-attachments.png`。

#### 用例 REG-2 · 附件预览元数据核对与移除
- **操作步骤**：点击任一附件「预览」，核对首行 metadata；关闭预览，点击另一附件「移除」。
- **预期**：
  - 首行 metadata 包含 `"kind":"feedback-log-collection"`、`"app_version":"1.0.6-beta.6"`、`"from"/"to"` 等字段；
  - 移除后卡片消失，计数相应减少。
- **证据文件**：`ev-REG-2-preview.png`。

#### 用例 REG-3 · 权限拒绝 (ACL) 容灾回归
- **操作步骤**：
  1. 退出应用，在沙盒账号下执行：
     `icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs" /deny "$env:USERNAME:(OI)(CI)(R)" /T`
  2. 启动应用并打开反馈面板。
- **预期**：
  - 应用仍能正常启动运行（日志目录异常不阻断主程序）；
  - 反馈面板明确提示「日志不可用」，提示文案友好脱敏；
  - 恢复 ACL（`icacls ... /remove:d ...`）后重新打开面板，日志附件恢复正常采集。
- **证据文件**：`ev-REG-3-acl-denied.png`、`ev-REG-3-acl-recovered.png`。

---

## 4. 收尾清理（强制执行）

验收完成后，必须按顺序执行收尾，杜绝残留测试账户与脏配置：

1. 确认演示期间添加的任何 ACL 限制均已恢复：
   ```powershell
   icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs"
   ```
2. 登出当前测试账号会话，切换回主管理员账号；
3. 打开管理员 PowerShell，执行 teardown 脚本：
   ```powershell
   cd <项目根目录>\e2e\windows-sandbox
   powershell -ExecutionPolicy Bypass -File .\sandbox-teardown.ps1 -UserName st-beta6-sandbox -UninstallApp
   ```
4. 核对 teardown 输出，末尾三项核对必须全部为 `PASS`：
   - `CHECK 账号不存在: PASS`
   - `CHECK profile 已清除: PASS`
   - `CHECK 无遗留 RAG 进程: PASS`
- **证据文件**：`ev-Z1-teardown-pass.png`。

---

## 5. 验收结果记录与回填模板

### 5.1 测试环境元数据表

| 配置项 | 详细信息回填 |
|---|---|
| **操作系统版本** | 例: Windows 11 Pro 24H2 (OS Build 26200.5000) / Win 10 22H2 |
| **测试硬件/架构** | x86_64 / 物理机 / Windows Sandbox / 虚拟机 |
| **源码 Commit ID** | 例: `f27aa23` (或对应的 Git SHA) |
| **安装包文件名** | `rag-study-assistant-desktop-setup-1.0.6-beta.6.exe` |
| **安装包 SHA256** |  |
| **测试执行人** |  |
| **执行日期** | YYYY-MM-DD |

---

### 5.2 用例结论与证据汇总表

| 编号 | 测试用例分类与名称 | 执行结论 (通过 / 失败) | 关联证据文件 | 现象描述与备注 |
|---|---|---|---|---|
| **PRE-1** | 候选包 SHA256 完整性核对 |  | `ev-PRE-01-sha256.png` |  |
| **PRE-2** | 沙盒账号环境创建 |  | `ev-PRE-02-setup.png` |  |
| **PRE-3** | 隔离基线验证（全新干净） |  | `ev-PRE-03-isolation.png` |  |
| **SMOKE** | 自动化安装冒烟与日志取证 |  | `ev-SMOKE-01-summary.png` | `smoke-summary.json` 结果 |
| **W11-1** | 原故障机：连续 3 次打开向导并取消 |  | `ev-W11-1-cancel-3x.png` | 必须无 System.dll 崩溃 |
| **W11-2** | 原故障机：第 4 次完整向导安装 |  | `ev-W11-2-wizard-finish.png` | 安装完成并拉起应用 |
| **W11-3** | 原故障机：桌面与开始菜单快捷方式启动 |  | `ev-W11-3-desktop-start.png` | 两种途径均正常拉起 |
| **W11-4** | 原故障机：进程彻底退出与冷重启 |  | `ev-W11-4-clean-exit.png` | 无僵尸进程残留 |
| **W11-5** | 原故障机：事件查看器 Application 日志 |  | `ev-W11-5-eventvwr-clean.png` | 无 0xc0000005 崩溃条目 |
| **W10-1** | Win10 兼容性：默认路径安装 |  | `ev-W10-1-default-path.png` | 运行正常 |
| **W10-2** | Win10 兼容性：含中文路径安装 |  | `ev-W10-2-chinese-path.png` | 路径无乱码，backend 正常 |
| **W10-3** | Win10 兼容性：含空格路径安装 |  | `ev-W10-3-space-path.png` | 快捷方式与参数正确包裹 |
| **W10-4** | Win10 兼容性：覆盖安装数据保留 |  | `ev-W10-4-upgrade-preserve.png` | Mock 用户数据完好保留 |
| **W10-5** | Win10 兼容性：卸载重装数据保留 |  | `ev-W10-5-uninstall-retain.png` | AppData 未被清空 |
| **REG-1** | 既有功能回归：反馈日志自动抓取 |  | `ev-REG-1-attachments.png` |  |
| **REG-2** | 既有功能回归：日志预览与元数据 |  | `ev-REG-2-preview.png` |  |
| **REG-3** | 既有功能回归：ACL 拒绝容灾与恢复 |  | `ev-REG-3-acl-denied.png` |  |
| **POST-1**| 收尾清理（teardown 验证） |  | `ev-Z1-teardown-pass.png` | 三项核对必须全 PASS |

---

### 5.3 归档打包规范
所有测试截图与生成的证据日志（`smoke-summary.json`、`installer-crash-events.txt`）请统一存放在一个证据文件夹中，并压缩为：  
`beta6-acceptance-evidence-<操作系统分支>-<日期>.zip` （例如 `beta6-acceptance-evidence-win11-26200-20250228.zip`），与本回填手册一同提交归档。
