# beta.3 Windows 沙盒验收执行手册（D4：附件 UI + 隔离宿主）

适用候选包：`rag-study-assistant-desktop-setup-1.0.6-beta.3.exe`（CI `workflow_dispatch` 产物）
执行环境：用户自有 Windows 10/11 x64 **授权测试机**，全程在临时账号 `st-beta3-sandbox` 会话内操作。
配套脚本：同目录 `sandbox-setup.ps1`（建账号）/ `sandbox-teardown.ps1`（收尾清理，**强制执行**）。

> 本手册是逐项勾选的执行记录模板：每步都写明「操作 → 预期 → 需回传证据」，
> 每用例末尾的「结论」行直接回填（通过 / 失败（现象）+ 证据链接）。
> 截图统一命名 `ev-<编号>-<简述>.png`，命令输出可纯文本保存同名 `.txt`。

## 0. 三条红线（动手前读一遍）

1. **不点生产提交**：反馈面板里出现最终「提交」按钮即截图停手（用例 I）。面板内的
   反馈服务登录可以做（独立测试账号），但提交动作一律不做。
2. **只在沙盒账号里演示 ACL**：所有 `icacls /deny` 只加在
   `st-beta3-sandbox` 自己的 `%LOCALAPPDATA%\RAGStudyAssistant\logs` 上，
   先 `/save` 备份、按本手册用 `/remove:d` 恢复；**验收结束必须跑 teardown**，
   它会把残留 ACL、进程、账号、profile 一并核对清除。
3. **不动真实账号数据**：本机主账号下的 `%LOCALAPPDATA%\RAGStudyAssistant`
   若已存在（真实使用数据），全程只读对照、绝不改名/删除/加 ACL。

## 1. 前置准备

### 1.1 候选包完整性核对（主账号或任意会话均可）

- 取得 CI artifact `rag-study-assistant-windows-1.0.6-beta.3`，解压出
  `rag-study-assistant-desktop-setup-1.0.6-beta.3.exe` 与 `SHA256SUMS.txt`。
- 在安装包所在目录执行（普通 PowerShell）：

  ```powershell
  certutil -hashfile .\rag-study-assistant-desktop-setup-1.0.6-beta.3.exe SHA256
  notepad .\SHA256SUMS.txt    # 或 Get-Content .\SHA256SUMS.txt
  ```

- 预期：certutil 输出的哈希与 SHA256SUMS 中 `setup-1.0.6-beta.3.exe` 那行完全一致。
- 证据：`ev-01-sha256.png`（命令输出与 sums 行同框）。
- 结论：通过 / 失败（现象）+ 证据：

### 1.2 创建临时账号（主账号、管理员 PowerShell）

```powershell
cd <本目录>
powershell -ExecutionPolicy Bypass -File .\sandbox-setup.ps1
# 干跑预览：加 -WhatIf
```

- 脚本会：建账号（密码永不过期）→ 打印一次性密码 → 给出 profile 生成指引与「必须为空」检查。
- 按指引用 `runas /user:st-beta3-sandbox "cmd /c whoami"` 生成 profile，然后
  **锁屏切换用户，登录 `st-beta3-sandbox`**。
- 证据：`ev-02-setup.png`（脚本输出含密码段可打码，但账号/路径行须可见）。
- 结论：通过 / 失败（现象）+ 证据：

### 1.3 隔离验证（沙盒账号内、安装前）

在 `st-beta3-sandbox` 会话里执行并回传输出：

```powershell
whoami                                            # 期望: <机器名>\st-beta3-sandbox
echo $env:LOCALAPPDATA                            # 期望: C:\Users\st-beta3-sandbox\AppData\Local
Test-Path "$env:LOCALAPPDATA\RAGStudyAssistant"   # 期望: False（日志/数据目录全新）
Get-ChildItem $env:APPDATA -Filter '*RAG*'        # 期望: 无输出（Electron userData 未生成）
Get-Process | ? { $_.Name -match 'RAG|backend' }  # 期望: 无输出
```

- 预期：三处全部「干净」——桌面日志/后端数据/Electron userData 都还没生成。
  隔离来自账号本身：`RAG_DESKTOP_DATA_DIR` 只能改 data 目录，改不了
  `logs`（`backend-manager.ts:getLogDir()` 不读它），所以必须用独立账号。
- 证据：`ev-03-isolation.png`。
- 结论：通过 / 失败（现象）+ 证据：

## 2. 用例清单

> 约定：下文「反馈球」指页面右缘的悬浮反馈入口（组件 orb）；「附件区」指面板内
> 日志附件列表（文件名 + 「自动」徽标 + `(n/3)` 计数 + 预览/移除按钮）。

### 用例 A · 安装并启动

- 操作：双击安装包 → SmartScreen「Windows 已保护你的电脑」→「更多信息」→「仍要运行」
  （未签名测试包，提示**属预期**）→ 按向导安装（按用户安装，可改目录）→ 完成并启动。
- 预期：应用启动到主界面；不出现端口占用弹窗（18764 空闲时）；无崩溃。
- 证据：`ev-A1-smartscreen.png`、`ev-A2-first-screen.png`。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 B · 打开反馈面板 → 附件自动采集

- 前置小动作：先点一次顶部「日志」导航并等 2 秒——该动作会产生 frontend 来源
  记录（`log_nav.*`/`log_query.*` 埋点上报），保证三个来源都有本会话内容。
- 操作：点击右侧反馈球打开面板，观察附件区。
- 预期：
  - 先出现「正在自动采集日志…」状态，随后附件卡片出现；
  - 附件名称为 `studytool-desktop.jsonl` / `studytool-backend.jsonl` /
    `studytool-frontend.jsonl`，徽标「自动」，计数 `(n/3)`；
  - n 正常为 3；若某来源本会话无记录则该来源**不产附件**（契约：绝不出现空文件）；
  - 若看到「采集日志超时（超过 3 秒）」：组件有 3 秒上限，属已知边界——
    关闭面板再打开会自动重试（失败不置采集标记），把现象与重试结果一并记录。
- 证据：`ev-B1-panel-attachments.png`（附件列表可见文件名与计数）。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 C · 附件预览与移除

- 操作：点任一附件「预览」→ 查看 → 关闭；再对另一附件点「移除」。
- 预期：
  - 预览显示 JSONL 文本内容；**首行为 metadata**：`"kind":"feedback-log-collection"`、
    `"source"` 为对应来源、`"from"/"to"` 恰为 30 分钟窗口、`"app_version":"1.0.6-beta.3"`；
  - 记录行的 `session_id` 全部等于本会话 ID（可对照日志页「会话」筛选里
    「本次启动」的值，或日志记录中的 session_id 字段）；
  - 单文件体积 ≤256KiB（附件卡片有大小显示/或导出文件可查）；
  - 移除后该项消失，计数变 `(n-1/3)`。
- 证据：`ev-C1-preview.png`（首行 metadata 入镜）、`ev-C2-after-remove.png`。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 D · 关闭重开 / 重新采集（按冻结语义核对）

- 操作 d1：关闭面板 → 立即重开。
  - 预期：**不重新采集**——已采集的附件原样在列，不再出现「正在自动采集日志…」
    （组件 `logsCollected` 语义：同一草稿只自动采一次）。
- 操作 d2：把剩余附件全部「移除」→ 关闭 → 重开。
  - 预期：**不补回**——用户删除是有意的，计数保持 `(0/3)`、无错误条。
- 操作 d3（重新采集的证据）：**完全退出应用 → 重新启动** → 开面板。
  - 预期：重新自动采集，附件再次出现（新会话 → 新 30 分钟窗口）。
  - 说明：宿主内「退出登录→重新登录」重建组件实例，同样触发重采，可作为替代。
- 证据：`ev-D1-reopen-same.png`、`ev-D3-restart-recollect.png`。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 E · 单来源不可读（backend 拒读）

- 前置：完全退出应用。在沙盒会话 PowerShell 执行（普通权限即可，被拒再换管理员窗口）：

  ```powershell
  # ① 先备份整个 logs 目录 ACL（出问题时可用它 /restore）
  icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs" /save "$env:USERPROFILE\Desktop\acl-logs-backup.txt" /T
  # ② 对 backend 来源全部世代加拒绝读取
  icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs\backend.jsonl*" /deny "$env:USERNAME:(R)"
  ```

- 操作：启动应用 → 点一次「日志」导航 → 打开反馈面板。
- 预期：
  - `studytool-desktop.jsonl` 与 `studytool-frontend.jsonl` 附件正常出现；
    **不出现** `studytool-backend.jsonl`；
  - 预览任一附件，首行 metadata 可见 `"unavailable":["backend"]`
    ——该来源被如实标记为不可用，而不是静默混入或整体失败；
  - 附件区**不显示**「日志不可用」错误条（部分失败 ≠ 整体失败）。
- 恢复（本用例内先不恢复，与 F 一起恢复；若单独做本用例则执行）：

  ```powershell
  icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs" /remove:d "$env:USERNAME" /T
  ```

- 证据：`ev-E1-partial-attachments.png`、`ev-E2-meta-unavailable.png`、
  两条 icacls 命令输出文本。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 F · 全来源不可读（整个 logs 目录拒读）

- 前置：完全退出应用。PowerShell：

  ```powershell
  icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs" /deny "$env:USERNAME:(OI)(CI)(R)" /T
  ```

- 操作：启动应用 → 打开反馈面板。
- 预期：
  - **应用仍能启动到主界面**——日志目录整体拒读不阻断启动
    （beta.3 D1「日志路径异常不阻断启动」的实机旁证，顺手记录）；
  - 面板附件区显示**明确的失败提示**「日志不可用」，不出现任何附件，
    也绝不是「安静地没有附件」；
  - 错误文案不含绝对路径/内部信息（脱敏口径：组件只显示简短中文）。
- 旁证（可选，若顺手）：主界面/日志页也应显示读取异常提示而非零记录。
- 证据：`ev-F1-app-started.png`、`ev-F2-log-unavailable.png`。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 G · 恢复 ACL → 重新采集正常

- 操作：保持应用运行，PowerShell 恢复：

  ```powershell
  icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs" /remove:d "$env:USERNAME" /T
  # 兜底：若上面未能完全还原，用备份整体恢复
  # icacls "$env:LOCALAPPDATA\RAGStudyAssistant" /restore "$env:USERPROFILE\Desktop\acl-logs-backup.txt"
  icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs"   # 复核：输出中不应再有本账号 DENY 行
  ```

  然后**关闭面板再打开**（上一次采集是失败的，未置采集标记，重开会自动重试）。
- 预期：
  - 附件区重新出现附件（同用例 B）；错误条消失；
  - 可选观察：日志页刷新后能看到 `log.persistence.recovered` 事件记录——
    拒读期间积压在内存待写队列的日志在 ACL 恢复后补写（D2 实机旁证，看到即记录）。
- 若重开仍未采集：完全退出应用重启再开面板（新会话必然触发）。
- 证据：`ev-G1-acl-restored.png`（icacls 复核输出）、`ev-G2-recollect.png`。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 H · 宿主退出清理 + 反馈服务独立登录

- 操作 h1：在面板草稿里随便输入几个字（不提交）→ 在宿主应用内「退出」登录。
  - 预期：反馈球/面板**一并消失**（组件实例随宿主会话销毁）；
    重新登录宿主 → 反馈球重新出现 → 开面板 → 是**全新实例**
    （重新自动采集，旧草稿不回流）。
- 操作 h2：重新登录宿主 → 开面板 → 找到反馈服务登录区。
  - 预期：组件要求**独立登录**反馈服务（自己的账号/密码输入），
    绝不自动携带宿主会话或凭据；
    - 有授权测试账号 → 登录成功，截 `ev-H3`；
    - 没有测试账号 → 只截登录界面即可，不继续。
- 证据：`ev-H1-orb-gone.png`（退出后无悬浮球）、`ev-H2-new-instance.png`、
  `ev-H3-feedback-login.png`。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 I · 提交按钮出现即停（不点生产提交）

- 操作：在已登录反馈服务的面板里填一段正文 → 让主「提交」按钮完整出现在画面里。
- 预期 / 红线：**能看到提交按钮即达标，绝不可点击**。若误触进入「提交中」状态，
  立即关闭面板并在结论里如实记录现象。
- 证据：`ev-I1-submit-visible.png`。
- 结论：通过 / 失败（现象）+ 证据：

### 用例 J · beta.2 已验收口径回归（新代码不得破坏）

> 口径来源：`.devin/windows-logs-capture-fix/log-read-error-fix.md`（beta.2 已验收）。

- **J1 · logs 目录被普通文件占据 → 启动不阻断（D1 实机验收 + 回归）**
  - 操作：退出应用 → 把 `logs` 目录改名 `logs_real` → 原位新建**同名普通文件** `logs`
    （`ni "$env:LOCALAPPDATA\RAGStudyAssistant\logs" -ItemType File`）→ 启动应用。
  - 预期：应用**正常启动**（日志目标创建失败被兜住，不崩溃）；打开日志页显示
    **明确错误提示**（红色 alert、含错误码），不是「共 0 条」假空态。
  - 恢复：退出应用 → 删除普通文件 `logs` → `logs_real` 改回 `logs` → 启动 →
    日志页刷新正常。
  - 证据：`ev-J1-occupied-error.png`、`ev-J2-restored.png`。

- **J2 · 缺失 / 空态 / 拒读三态提示与 beta.2 一致**
  - 缺失：把某来源（如 `frontend.jsonl*`）移出 logs → 日志页显示
    「未找到日志文件（可能尚未生成）：前端」，属正常空态**非错误**；
  - 空态：筛选条件下无记录 → 显示「当前筛选条件下没有日志记录」；
  - 拒读：单来源 ACL 拒绝 → 红色 alert 含来源与错误码（EACCES），**不是零记录**
    （可与用例 E 的截图复用为证据）。
  - 证据：`ev-J3-missing.png`、`ev-J4-empty.png`、`ev-J5-denied.png`。

- **J3 · 1.0.5 → beta.3 覆盖安装数据保留（仅当该机已装 1.0.5 且用户同意）**
  - 注意：按用户安装的特性决定升级测试要在**装了 1.0.5 的那个账号**里做
    （通常是主账号，不在沙盒内）；数据目录以该账号 `%LOCALAPPDATA%` 为准。
  - 操作：覆盖前 `Get-ChildItem "$env:LOCALAPPDATA\RAGStudyAssistant" -Recurse | Select FullName`
    存档 → 运行 beta.3 安装包覆盖 → 启动。
  - 预期：资料/配置/日志保留；SESSION_ID 换新；日志页正常。
  - 本机无 1.0.5 或用户不同意覆盖 → 结论填「不适用」。
  - 证据：`ev-J6-before-after.png`。

- 结论（J 整体）：通过 / 失败（现象）+ 证据：

## 3. 收尾（强制执行）

1. 确认用例 E/F/G 的 ACL 已恢复：`icacls "$env:LOCALAPPDATA\RAGStudyAssistant\logs"`
   输出中无 `st-beta3-sandbox` 的 DENY 行（teardown 会兜底再清一次，但不能只靠兜底）。
2. 退出沙盒账号会话，切回主账号。
3. 管理员 PowerShell 执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\sandbox-teardown.ps1 -UninstallApp
   ```

   （不想卸载应用、只清理账号时去掉 `-UninstallApp`。）
4. teardown 末尾三行核对必须全 PASS：账号不存在 / `C:\Users\st-beta3-sandbox`
   已清除 / 无遗留 RAG 进程。
- 证据：`ev-Z1-teardown.png`。
- 结论：通过 / 失败（现象）+ 证据：

## 4. 回填汇总表（验收记录用）

| 用例 | 结论 | 证据文件 |
|---|---|---|
| 1.1 包完整性 |  | ev-01 |
| 1.2 建账号 |  | ev-02 |
| 1.3 隔离验证 |  | ev-03 |
| A 安装启动 |  | ev-A1/A2 |
| B 自动采集 |  | ev-B1 |
| C 预览/移除 |  | ev-C1/C2 |
| D 重开/重采 |  | ev-D1/D3 |
| E 单来源拒读 |  | ev-E1/E2 |
| F 全来源拒读 |  | ev-F1/F2 |
| G 恢复重采 |  | ev-G1/G2 |
| H 退出清理/独立登录 |  | ev-H1~H3 |
| I 提交即停 |  | ev-I1 |
| J beta.2 回归 |  | ev-J1~J6 |
| 3 收尾清理 |  | ev-Z1 |

打包：全部 `ev-*` 截图/文本 + 本文件回填后 → `beta3-d4-evidence-<日期>.zip` 回传。

## 5. 已知边界与如实记录项（看到不等于失败，记录即可）

- 组件自动采集有 **3 秒超时**（「采集日志超时（超过 3 秒）」）；首次调用 Worker
  偏慢时可能触发，关开面板自动重试。若重试仍超时，记为失败并附日志页截图。
- 某来源本会话**无记录**（如 frontend 未产生任何上报）时该来源不产附件——
  这是契约行为（不产空文件），不是采集失败。
- 30 分钟窗口按「面板打开那一刻往前 30 分钟」取，超过 30 分钟前的本会话记录
  不会进附件；session_id 非本会话的记录也不会进附件。
