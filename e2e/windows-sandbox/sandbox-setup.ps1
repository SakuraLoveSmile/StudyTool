<#
.SYNOPSIS
  StudyTool 1.0.6-beta.3 · D4 Windows 沙盒验收 —— 临时宿主账号创建脚本。

.DESCRIPTION
  在用户授权的 Windows 测试机上创建一次性本地账号（默认 st-beta3-sandbox），
  用于在「全新宿主环境」中验收反馈面板的日志附件自动采集 / 预览 / 移除。

  ⚠ 仅允许在用户授权的测试机上运行；本脚本绝不修改任何现有账号。
  验收结束后必须运行同目录 sandbox-teardown.ps1 完成清理（删账号、删 profile、核对 ACL）。

  为什么必须用独立账号做隔离（冻结契约 D4 背景，切勿改用环境变量省事）：
    - 桌面日志目录固定为 %LOCALAPPDATA%\StudyTool\logs
      （desktop/src/backend-manager.ts 的 getLogDir() 不读 STUDYTOOL_DESKTOP_DATA_DIR）；
    - 后端数据目录 %LOCALAPPDATA%\StudyTool\data 可被
      STUDYTOOL_DESKTOP_DATA_DIR 改道，但日志目录不行；
    - Electron userData 在 %APPDATA%\<productName> 下（productName=学习助手）。
    因此「全新日志/数据/userData」只能靠另一个 Windows 账号的 AppData 获得。

.PARAMETER UserName
  要创建的本地账号名，默认 st-beta3-sandbox。已存在同名账号时脚本直接报错退出，
  绝不复用/改动既有账号（可先跑 teardown 清理，或换 -UserName）。

.PARAMETER Password
  可选明文密码。省略时自动生成 20 位随机密码并只打印一次——
  请当场记录到验收记录中，离开窗口后无法再查看。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sandbox-setup.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sandbox-setup.ps1 -WhatIf   # 干跑，不落任何改动
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sandbox-setup.ps1 -UserName st-b3-a -Password '自设密码'
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [Parameter(Mandatory = $false)]
  [ValidatePattern('^[A-Za-z0-9._-]{3,20}$')]
  [string]$UserName = 'st-beta3-sandbox',

  [Parameter(Mandatory = $false)]
  [AllowEmptyString()]
  [string]$Password = ''
)

$ErrorActionPreference = 'Stop'

function Emit([string]$Text) {
  # 所有可回传证据统一带 [D4-SETUP] 前缀，方便执行者整段复制进验收记录。
  Write-Host "[D4-SETUP] $Text"
}

try {
  Emit '==================== StudyTool beta.3 D4 沙盒账号创建 ===================='
  Emit "目标账号名: $UserName"
  Emit '本脚本只新建上述账号；发现同名账号将中止而不是接管。'

  # ---------------------------------------------------------------- 0. 管理员检查
  $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Emit '错误：需要「以管理员身份运行」的 PowerShell（右键 → 以管理员身份运行）。'
    exit 1
  }
  Emit '管理员权限: 确认'

  # ---------------------------------------------------------------- 1. 同名账号存在性检查（保护既有账号）
  $existing = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
  if ($null -ne $existing) {
    Emit "错误：本地账号 '$UserName' 已存在。为避免误动既有账号，脚本中止。"
    Emit '  若这是上一轮验收残留：请先运行 sandbox-teardown.ps1 清理后再来。'
    Emit '  若想换名字：加 -UserName <新名字> 重新运行。'
    exit 1
  }
  Emit '同名账号冲突检查: 通过（不存在）'

  # ---------------------------------------------------------------- 2. 密码准备
  $generated = $false
  if ([string]::IsNullOrEmpty($Password)) {
    Add-Type -AssemblyName System.Web
    $Password = [System.Web.Security.Membership]::GeneratePassword(20, 4)
    $generated = $true
  }
  $secure = ConvertTo-SecureString $Password -AsPlainText -Force

  # ---------------------------------------------------------------- 3. 创建账号（-WhatIf 时只打印动作不落改动）
  if ($PSCmdlet.ShouldProcess("本地账号 $UserName", 'New-LocalUser（密码永不过期、账号永不过期）')) {
    New-LocalUser -Name $UserName `
                  -Password $secure `
                  -Description 'StudyTool 1.0.6-beta.3 验收临时账号；验收完由 sandbox-teardown.ps1 删除' `
                  -PasswordNeverExpires `
                  -AccountNeverExpires | Out-Null
  }

  if ($WhatIfPreference) {
    Emit '【干跑模式 -WhatIf】以上动作未实际执行；下面给出的是真实运行时会出现的检查输出与指引。'
  } else {
    # -------------------------------------------------------------- 4. 确认创建成功
    $u = Get-LocalUser -Name $UserName
    Emit "创建结果: Name=$($u.Name) Enabled=$($u.Enabled) PasswordExpires=$($u.PasswordExpires) Description='$($u.Description)'"
    Emit 'net user 复核（回传以下输出即可）:'
    net user $UserName | ForEach-Object { Emit "  $_" }
  }

  # ---------------------------------------------------------------- 5. 证据与后续指引
  $profileRoot = "C:\Users\$UserName"
  $localBase   = "$profileRoot\AppData\Local"
  $roamBase    = "$profileRoot\AppData\Roaming"
  $appLocal     = "$localBase\StudyTool"

  Emit '---------------- 步骤 2/3：生成该账号的 profile ----------------'
  Emit 'profile（C:\Users\<账号>）无法由本脚本预创建，需要先让 Windows 为该账号初始化一次。'
  Emit '任选其一（推荐 A，不必真的打开桌面）:'
  Emit "  A. 在【当前会话】执行（会提示输入密码，输入后即生成 profile）:"
  Emit "       runas /user:$UserName `"cmd /c whoami`""
  Emit "     看到输出 $env:COMPUTERNAME\$UserName 即成功，关掉那个黑窗口即可。"
  Emit '  B. 锁屏（Win+L）→「切换用户」→ 用该账号登录一次 → 出现桌面后注销回来。'
  Emit "验证 profile 已生成: Test-Path '$profileRoot' → 应输出 True"

  Emit '---------------- 步骤 3/3：登录该账号后的「必须为空」检查 ----------------'
  Emit '切到该账号会话后，安装应用【之前】执行以下检查，预期全新（回传输出）:'
  Emit "  whoami                                          → 期望: $env:COMPUTERNAME\$UserName"
  Emit "  echo `$env:LOCALAPPDATA                          → 期望: $localBase"
  Emit "  Test-Path `"`$env:LOCALAPPDATA\StudyTool`"  → 期望: False（日志/数据目录尚未生成）"
  Emit "  Get-ChildItem `"`$env:APPDATA`" -Filter '*学习助手*'  → 期望: 无输出（Electron userData 未生成）"
  Emit "  Get-Process | ? {`$_.Name -match '学习助手|backend'}  → 期望: 无输出（无遗留进程）"
  Emit ''
  Emit '该账号下验收会涉及的三个路径（写进记录，ACL 演示与核对都用得到）:'
  Emit "  桌面日志目录:   $appLocal\logs"
  Emit "  后端数据目录:   $appLocal\data   （STUDYTOOL_DESKTOP_DATA_DIR 只能改它，改不了 logs）"
  Emit "  Electron userData: $roamBase\学习助手  （productName；若不存在则看 $roamBase\studytool）"
  Emit ''
  if ($generated) {
    Emit '==================== 账号密码（仅此一次显示，立即记录） ===================='
    Write-Host "  $UserName / $Password" -ForegroundColor Yellow
    Emit '==========================================================================='
    if ($WhatIfPreference) {
      Emit '【干跑模式】上面这个密码是预生成的，但账号并未创建；正式运行时请以正式输出为准。'
    } else {
      Emit '提示：把密码记入验收记录；teardown 删除账号后密码即失效，无需另行销毁。'
    }
  } else {
    Emit '账号密码: 使用了 -Password 参数传入的自设密码（脚本不回显）。'
  }
  Emit '下一步：切到该账号会话，按 e2e/windows-sandbox/acceptance-runbook.md 逐项执行。'
  Emit '完毕。'
  exit 0
} catch {
  Write-Host "[D4-SETUP] 失败：$($_.Exception.Message)" -ForegroundColor Red
  Write-Host '[D4-SETUP] 未完成的改动请用 sandbox-teardown.ps1 核对清理。' -ForegroundColor Red
  exit 1
}
