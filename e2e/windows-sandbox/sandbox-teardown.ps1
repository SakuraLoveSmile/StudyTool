<#
.SYNOPSIS
  StudyTool 1.0.6-beta.3 · D4 Windows 沙盒验收 —— 清理脚本（必须与 sandbox-setup.ps1 配套）。

.DESCRIPTION
  收尾顺序：强制注销临时账号会话 → 结束该账号名下/目录下的 RAG 相关进程 →
  （可选 -UninstallApp）运行卸载程序 → 恢复演示期间加过的 ACL 拒绝 →
  删除本地账号 → 清除 C:\Users\<账号> profile 残余 → 打印三行核对结果供回传。

  ⚠ 仅允许在用户授权的测试机上运行；只清理 -UserName 指定的那一个账号。
  即使验收中途放弃，也应运行本脚本，避免留下账号/ACL/进程残留。

.PARAMETER UserName
  要清理的本地账号名，默认 st-beta3-sandbox（须与 setup 时一致）。

.PARAMETER UninstallApp
  可选开关：尝试运行该账号下的应用卸载程序（按用户安装，卸载程序在
  C:\Users\<账号>\AppData\Local\Programs\...\Uninstall*.exe）。
  不加此开关时跳过卸载步骤——注意：删除 profile 本身就会移除按用户安装的程序文件，
  本开关只是让「卸载」这一步留下正规记录。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sandbox-teardown.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sandbox-teardown.ps1 -UninstallApp
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\sandbox-teardown.ps1 -WhatIf   # 干跑
#>

[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [Parameter(Mandatory = $false)]
  [ValidatePattern('^[A-Za-z0-9._-]{3,20}$')]
  [string]$UserName = 'st-beta3-sandbox',

  [Parameter(Mandatory = $false)]
  [switch]$UninstallApp
)

$ErrorActionPreference = 'Stop'
$script:Failures = @()

function Emit([string]$Text) { Write-Host "[D4-TEARDOWN] $Text" }
function Fail([string]$Text) {
  Write-Host "[D4-TEARDOWN] CHECK-FAIL: $Text" -ForegroundColor Red
  $script:Failures += $Text
}

try {
  Emit '==================== StudyTool beta.3 D4 沙盒清理 ===================='
  Emit "清理目标账号: $UserName"
  if ($WhatIfPreference) { Emit '【干跑模式 -WhatIf】只打印动作，不落改动。' }

  # ---------------------------------------------------------------- 0. 管理员检查
  $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
  if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Emit '错误：需要「以管理员身份运行」的 PowerShell。'
    exit 1
  }
  Emit '管理员权限: 确认'

  $profileRoot = "C:\Users\$UserName"
  $appLocal     = "$profileRoot\AppData\Local\StudyTool"

  # ---------------------------------------------------------------- 1. 强制注销该账号的活动会话
  Emit '--- 步骤 1/6：注销该账号的活动会话 ---'
  $loggedOff = $false
  try {
    $quser = quser $UserName 2>$null
    if ($LASTEXITCODE -eq 0 -and $quser) {
      foreach ($line in ($quser | Select-Object -Skip 1)) {
        # quser 输出形如 " st-beta3-sandbox  console  2  Active ..."；会话 ID 是其中的纯数字列。
        $cols = ($line -replace '^>', ' ').Trim() -split '\s+'
        $sessionId = $cols | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1
        if ($sessionId) {
          Emit "发现会话 ID=$sessionId（$($cols[0])），执行 logoff $sessionId"
          if ($PSCmdlet.ShouldProcess("会话 $sessionId", 'logoff 强制注销')) {
            logoff $sessionId 2>$null
            $loggedOff = $true
          }
        }
      }
    } else {
      Emit '该账号无活动会话（或 quser 不可见），跳过。'
    }
  } catch {
    Emit "注销会话时出现非致命异常：$($_.Exception.Message)（继续，后面靠进程清理兜底）"
  }
  if ($loggedOff) { Start-Sleep -Seconds 2 }

  # ---------------------------------------------------------------- 2. 结束该账号下的应用相关进程
  Emit '--- 步骤 2/6：结束该账号名下/目录下的应用相关进程 ---'
  # 判定口径（宁宽勿窄，但全部限定在该账号自己的地盘）：
  #   a) 可执行路径位于 C:\Users\<账号> 之下（含按用户安装的 Programs 目录与后端 backend.exe）；
  #   b) 命令行/路径含 StudyTool（兼容更名前遗留的 RAGStudyAssistant）；
  #   c) 进程名为 学习助手 或 backend，且路径落在该账号目录内。
  $targets = Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($profileRoot, [System.StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.CommandLine -match 'StudyTool|RAGStudyAssistant') -or
    ($_.Name -match '^(学习助手|backend)\.exe$' -and $_.ExecutablePath -and $_.ExecutablePath -like "*$UserName*")
  }
  if ($targets) {
    foreach ($p in $targets) {
      Emit "结束进程: PID=$($p.ProcessId) Name=$($p.Name) Path=$($p.ExecutablePath)"
      if ($PSCmdlet.ShouldProcess("PID $($p.ProcessId) ($($p.Name))", 'Stop-Process -Force')) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
      }
    }
    Start-Sleep -Seconds 2
  } else {
    Emit '未发现该账号下的 RAG 相关进程，跳过。'
  }

  # ---------------------------------------------------------------- 3. （可选）卸载应用
  Emit '--- 步骤 3/6：卸载应用（可选） ---'
  if ($UninstallApp) {
    $uninstallers = Get-ChildItem "$profileRoot\AppData\Local\Programs" -Recurse -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue
    if ($uninstallers) {
      foreach ($u in $uninstallers) {
        Emit "运行卸载程序（静默 /S）: $($u.FullName)"
        if ($PSCmdlet.ShouldProcess($u.FullName, '运行卸载程序 /S')) {
          Start-Process -FilePath $u.FullName -ArgumentList '/S' -Wait -ErrorAction SilentlyContinue
        }
      }
      Emit '卸载程序已调用；如有残留由后续 profile 删除兜底（按用户安装，程序文件都在 profile 内）。'
    } else {
      Emit "未在 $profileRoot\AppData\Local\Programs 下找到 Uninstall*.exe。"
      Emit '  可能应用未装/已被卸载，或装到了自定义目录——若有自定义目录请手动卸载后再继续。'
    }
  } else {
    Emit '未指定 -UninstallApp，跳过卸载步骤（profile 删除会一并移除按用户安装的程序文件）。'
  }

  # ---------------------------------------------------------------- 4. 恢复演示期间加过的 ACL 拒绝
  Emit '--- 步骤 4/6：恢复 ACL（兜底，runbook 用例 G 应已恢复） ---'
  if (Test-Path $appLocal) {
    Emit "对 $appLocal 执行 /remove:d + /reset（含子项 /T）："
    if ($PSCmdlet.ShouldProcess($appLocal, "icacls /remove:d $UserName /T 与 /reset /T")) {
      icacls $appLocal /remove:d $UserName /T 2>&1 | ForEach-Object { Emit "  $_" }
      icacls $appLocal /reset /T 2>&1 | ForEach-Object { Emit "  $_" }
    }
    Emit 'ACL 兜底恢复完成（账号随后整体删除， orphaned ACE 随 profile 一并消失）。'
  } else {
    Emit "$appLocal 不存在，无需恢复 ACL。"
  }

  # ---------------------------------------------------------------- 5. 删除账号与 profile
  Emit '--- 步骤 5/6：删除本地账号与 profile ---'
  $user = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
  if ($null -ne $user) {
    if ($PSCmdlet.ShouldProcess("本地账号 $UserName", 'Remove-LocalUser')) {
      Remove-LocalUser -Name $UserName
      Emit "本地账号 $UserName 已删除。"
    }
  } else {
    Emit "本地账号 $UserName 不存在（可能已被删），跳过 Remove-LocalUser。"
  }

  # profile：先走 Win32_UserProfile（连注册表 ProfileList 一起清），再清残余目录。
  $prof = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
          Where-Object { $_.LocalPath -eq $profileRoot }
  if ($prof) {
    if ($PSCmdlet.ShouldProcess($profileRoot, 'Remove-CimInstance Win32_UserProfile')) {
      try {
        $prof | Remove-CimInstance -ErrorAction Stop
        Emit "Win32_UserProfile 记录已移除（$profileRoot）。"
      } catch {
        Emit "Win32_UserProfile 移除失败：$($_.Exception.Message)——改用目录强删兜底。"
      }
    }
  }
  if (Test-Path $profileRoot) {
    if ($PSCmdlet.ShouldProcess($profileRoot, 'Remove-Item -Recurse -Force')) {
      try {
        Remove-Item $profileRoot -Recurse -Force -ErrorAction Stop
        Emit "profile 目录已删除：$profileRoot"
      } catch {
        Emit "profile 目录删除未完全成功：$($_.Exception.Message)"
        Emit '  常见原因：会话尚未完全注销/文件句柄未释放——请重启资源管理器或重启系统后重跑本脚本。'
      }
    }
  } else {
    Emit "profile 目录 $profileRoot 不存在。"
  }

  # ---------------------------------------------------------------- 6. 最终核对（三行必须全 PASS）
  Emit '--- 步骤 6/6：最终核对 ---'
  if ($WhatIfPreference) {
    Emit '【干跑模式】核对项按“未执行改动”口径报告，不代表真实清理结果。'
  }

  $stillUser = Get-LocalUser -Name $UserName -ErrorAction SilentlyContinue
  if ($null -eq $stillUser) { Emit "CHECK 账号不存在: PASS" } else { Fail "账号 $UserName 仍存在" }

  $stillProfDir = Test-Path $profileRoot
  $stillProfRec = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
                  Where-Object { $_.LocalPath -eq $profileRoot }
  if (-not $stillProfDir -and -not $stillProfRec) { Emit "CHECK profile 已清除: PASS" } else {
    Fail "profile 残余：Dir=$stillProfDir Registry=$([bool]$stillProfRec)（$profileRoot）"
  }

  $stillProc = Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($profileRoot, [System.StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and $_.CommandLine -match 'StudyTool|RAGStudyAssistant')
  }
  if (-not $stillProc) { Emit 'CHECK 无遗留应用进程: PASS' } else {
    $stillProc | ForEach-Object { Fail "遗留进程 PID=$($_.ProcessId) $($_.Name)" }
  }

  Emit '==================== 清理结果 ===================='
  if ($script:Failures.Count -eq 0) {
    Emit '全部核对 PASS，沙盒已干净回收。可将本输出回传为 ev-Z1。'
    exit 0
  } else {
    Emit "存在 $($script:Failures.Count) 项未通过，请按上面 CHECK-FAIL 行处理后重跑本脚本。"
    exit 1
  }
} catch {
  Write-Host "[D4-TEARDOWN] 失败：$($_.Exception.Message)" -ForegroundColor Red
  Write-Host '[D4-TEARDOWN] 请保留本输出并手工核对：账号 / profile 目录 / 进程 / ACL。' -ForegroundColor Red
  exit 1
}
