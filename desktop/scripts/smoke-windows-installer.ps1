<#
.SYNOPSIS
  Windows 安装包自动化静默安装与冒烟验证脚本 (兼容 Windows PowerShell 5.1 及 PowerShell 7+)

.DESCRIPTION
  用于在干净的一次性测试环境或 Windows 沙盒环境中，对 RAG 备考助手安装包（如 1.0.6-beta.6）进行自动化静默安装测试。
  功能包括：
    1. 参数校验与目录初始化：验证安装包文件存在，创建并校验证据目录；
    2. 严格环境隔离检查：若检测到已有安装、旧版注册表或残留进程，则拒绝触碰并退出；
    3. 静默安装执行：以 /S /currentuser 参数启动安装程序，监控进程生命周期与 PID；
    4. 超时与进程守护：默认 180 秒超时控制，超时自动终止进程树；
    5. 证据与崩溃事件收集：无论成败，均采集 Application/System 事件日志及 System.dll / 0xc0000005 崩溃记录；
    6. 安装完整性校验：验证进程退出码、主程序可执行文件生成、注册表卸载项完整性。

.PARAMETER InstallerPath
  待测试的安装包路径（例如 rag-study-assistant-desktop-setup-1.0.6-beta.6.exe）。

.PARAMETER EvidenceDir
  证据输出目录。测试汇总 json、崩溃事件日志等将保存在此目录下。

.PARAMETER TimeoutSeconds
  静默安装最长等待超时时间（秒），默认 180 秒。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\desktop\scripts\smoke-windows-installer.ps1 -InstallerPath "C:\dist\rag-study-assistant-desktop-setup-1.0.6-beta.6.exe" -EvidenceDir "C:\evidence"
#>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$InstallerPath,

  [Parameter(Mandatory = $true)]
  [string]$EvidenceDir,

  [Parameter(Mandatory = $false)]
  [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'

function Write-SmokeLog {
  param(
    [ValidateSet("INFO", "WARN", "ERROR", "SUCCESS")]
    [string]$Level,
    [string]$Message
  )
  $timestamp = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
  $color = "White"
  switch ($Level) {
    "INFO"    { $color = "Cyan" }
    "WARN"    { $color = "Yellow" }
    "ERROR"   { $color = "Red" }
    "SUCCESS" { $color = "Green" }
  }
  Write-Host "[$timestamp] [SMOKE-INSTALLER] [$Level] $Message" -ForegroundColor $color
}

# -----------------------------------------------------------------------------
# 0. 基础环境与配置常量
# -----------------------------------------------------------------------------
$APP_ID = "com.rag.studyassistant"
$PRODUCT_NAME = "RAG备考助手"
$DEFAULT_INSTALL_DIR = Join-Path $env:LOCALAPPDATA "Programs\$PRODUCT_NAME"
$DEFAULT_APP_EXE = Join-Path $DEFAULT_INSTALL_DIR "$PRODUCT_NAME.exe"
$UNINSTALL_REG_PATH = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$APP_ID"
$APP_REG_PATH = "HKCU:\Software\$APP_ID"

$startTime = Get-Date
$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$installerExitCode = $null
$timedOut = $false
$failureReasons = [System.Collections.Generic.List[string]]::new()
$hasCriticalCrash = $false

$summary = [ordered]@{
  timestamp                 = $startTime.ToString("yyyy-MM-ddTHH:mm:sszzz")
  installer_path            = $null
  evidence_dir              = $null
  timeout_seconds           = $TimeoutSeconds
  duration_seconds          = 0
  timed_out                 = $false
  installer_exit_code       = $null
  precheck_clean            = $false
  conflict_details          = @()
  installed_exe_exists      = $false
  installed_exe_path        = $DEFAULT_APP_EXE
  uninstall_reg_exists      = $false
  uninstall_reg_key         = $UNINSTALL_REG_PATH
  crash_events_count        = 0
  critical_system_dll_crash = $false
  verdict                   = "PENDING"
  failure_reasons           = @()
  process_snapshot          = @()
}

# -----------------------------------------------------------------------------
# a) 参数校验与目录准备
# -----------------------------------------------------------------------------
Write-SmokeLog "INFO" "==================== 开始 Windows 安装包冒烟验证 ===================="
Write-SmokeLog "INFO" "目标安装包: $InstallerPath"
Write-SmokeLog "INFO" "证据保存目录: $EvidenceDir"
Write-SmokeLog "INFO" "执行超时设定: $TimeoutSeconds 秒"

if (-not (Test-Path -Path $InstallerPath -PathType Leaf)) {
  $err = "指定的安装包不存在或不是有效文件: $InstallerPath"
  Write-SmokeLog "ERROR" $err
  $failureReasons.Add($err)
  exit 1
}
$resolvedInstallerPath = (Resolve-Path -Path $InstallerPath).Path
$summary.installer_path = $resolvedInstallerPath

if (-not (Test-Path -Path $EvidenceDir -PathType Container)) {
  Write-SmokeLog "INFO" "证据目录不存在，正在自动创建: $EvidenceDir"
  New-Item -Path $EvidenceDir -ItemType Directory -Force | Out-Null
}
$resolvedEvidenceDir = (Resolve-Path -Path $EvidenceDir).Path
$summary.evidence_dir = $resolvedEvidenceDir

# -----------------------------------------------------------------------------
# b) 严格环境隔离检查（拒绝触碰已有安装）
# -----------------------------------------------------------------------------
Write-SmokeLog "INFO" "--- 阶段 1/4: 执行严格环境隔离检查 ---"
$conflicts = [System.Collections.Generic.List[string]]::new()

# 1. 检查 HKCU:\Software\com.rag.studyassistant
if (Test-Path -Path $APP_REG_PATH) {
  $conflicts.Add("注册表已存在软件应用配置项: $APP_REG_PATH")
}

# 2. 检查 HKCU 卸载注册表项
if (Test-Path -Path $UNINSTALL_REG_PATH) {
  $conflicts.Add("注册表已存在软件卸载项: $UNINSTALL_REG_PATH")
}

# 检查是否有其它包含 RAG备考助手 的卸载项
$uninstallBase = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall"
if (Test-Path -Path $uninstallBase) {
  try {
    $subKeys = Get-ChildItem -Path $uninstallBase -ErrorAction SilentlyContinue
    foreach ($k in $subKeys) {
      $dispName = (Get-ItemProperty -Path $k.PSPath -Name DisplayName -ErrorAction SilentlyContinue).DisplayName
      if ($dispName -and $dispName -match 'RAG.*备考助手') {
        $conflicts.Add("卸载注册表中发现同名记录: $($k.PSPath) (DisplayName: $dispName)")
      }
    }
  } catch {
    Write-SmokeLog "WARN" "遍历卸载注册表发生警告: $($_.Exception.Message)"
  }
}

# 3. 检查默认安装路径与主 exe
if (Test-Path -Path $DEFAULT_APP_EXE) {
  $conflicts.Add("默认安装路径下已存在主程序: $DEFAULT_APP_EXE")
} elseif (Test-Path -Path $DEFAULT_INSTALL_DIR) {
  $conflicts.Add("默认安装目录已存在: $DEFAULT_INSTALL_DIR")
}

# 4. 检查是否有相关后台进程正在运行
$runningProcs = Get-Process | Where-Object { $_.ProcessName -match '^(RAG备考助手|backend)$' }
if ($runningProcs) {
  foreach ($p in $runningProcs) {
    $conflicts.Add("检测到正在运行的相关进程: PID=$($p.Id), Name=$($p.ProcessName)")
  }
}

if ($conflicts.Count -gt 0) {
  $summary.precheck_clean = $false
  $summary.conflict_details = $conflicts.ToArray()
  $summary.verdict = "REJECTED_DIRTY_ENVIRONMENT"
  $summary.failure_reasons = $conflicts.ToArray()

  $summaryJsonPath = Join-Path $resolvedEvidenceDir "smoke-summary.json"
  $summary | ConvertTo-Json -Depth 5 | Set-Content -Path $summaryJsonPath -Encoding UTF8

  Write-SmokeLog "ERROR" "========================================================================="
  Write-SmokeLog "ERROR" "【严格隔离检查失败】检测到已有安装或残留进程，拒绝触碰！"
  Write-SmokeLog "ERROR" "提示：检测到已有安装，拒绝触碰，请在干净的一次性测试环境或沙盒环境执行。"
  Write-SmokeLog "ERROR" "冲突项清单:"
  foreach ($c in $conflicts) {
    Write-SmokeLog "ERROR" "  - $c"
  }
  Write-SmokeLog "ERROR" "========================================================================="

  Write-Error "检测到已有安装，拒绝触碰，请在干净的一次性测试环境或沙盒环境执行！"
  exit 2
}

$summary.precheck_clean = $true
Write-SmokeLog "SUCCESS" "隔离检查通过：目标测试环境全新干净，无任何历史残留。"

# -----------------------------------------------------------------------------
# c) 执行静默安装
# -----------------------------------------------------------------------------
Write-SmokeLog "INFO" "--- 阶段 2/4: 执行静默安装 ---"
Write-SmokeLog "INFO" "执行命令: `"$resolvedInstallerPath`" /S /currentuser"

$installerProc = $null
$installerPid = $null
try {
  $installerProc = Start-Process -FilePath $resolvedInstallerPath -ArgumentList @("/S", "/currentuser") -PassThru
  $installerPid = $installerProc.Id
  Write-SmokeLog "INFO" "安装程序已启动，进程 PID: $installerPid"
} catch {
  $err = "启动安装程序进程失败: $($_.Exception.Message)"
  Write-SmokeLog "ERROR" $err
  $failureReasons.Add($err)
}

# -----------------------------------------------------------------------------
# d) 超时与进程监控
# -----------------------------------------------------------------------------
if ($installerProc) {
  Write-SmokeLog "INFO" "正在监控安装进程运行状态 (超时限制: $TimeoutSeconds 秒)..."
  while (-not $installerProc.HasExited) {
    if ($stopwatch.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
      $timedOut = $true
      break
    }
    Start-Sleep -Milliseconds 500
    try {
      $installerProc.Refresh()
    } catch {
      break
    }
  }

  if ($timedOut) {
    Write-SmokeLog "ERROR" "安装进程执行超时（已超过 $TimeoutSeconds 秒），强行终止进程树..."
    try {
      & taskkill.exe /F /T /PID $installerPid 2>&1 | Out-Null
    } catch {
      Write-SmokeLog "WARN" "taskkill 调用异常: $($_.Exception.Message)"
    }
    if (-not $installerProc.HasExited) {
      try {
        $installerProc.Kill()
      } catch {}
    }
    $installerExitCode = -1
    $failureReasons.Add("静默安装超时（超过 ${TimeoutSeconds} 秒），已被强制杀死")
  } else {
    try {
      $installerExitCode = $installerProc.ExitCode
    } catch {
      $installerExitCode = -1
    }
    Write-SmokeLog "INFO" "安装进程已退出，退出码: $installerExitCode，耗时: $([math]::Round($stopwatch.Elapsed.TotalSeconds, 2)) 秒"
  }
}

# 缓冲 3 秒让系统注册表和磁盘 IO 最终完成落盘
Start-Sleep -Seconds 3

# -----------------------------------------------------------------------------
# e) 证据与崩溃事件收集（无论成功还是失败均收集）
# -----------------------------------------------------------------------------
Write-SmokeLog "INFO" "--- 阶段 3/4: 收集证据与崩溃事件日志 ---"
$eventSearchStart = $startTime.AddMinutes(-10)
$crashEvents = [System.Collections.Generic.List[PSObject]]::new()

# 1. 扫描 Application 日志中的 Error (Level=2) / Warning (Level=3) / Critical (Level=1)
try {
  $appFilter = @{
    LogName   = 'Application'
    StartTime = $eventSearchStart
    Level     = @(1, 2, 3)
  }
  $events = Get-WinEvent -FilterHashtable $appFilter -ErrorAction SilentlyContinue
  if ($events) {
    foreach ($ev in $events) {
      $crashEvents.Add($ev)
    }
  }
} catch {}

# 2. 扫描与 System.dll / 0xc0000005 / Application Error 关键字匹配的事件
try {
  $recentEvents = Get-WinEvent -FilterHashtable @{
    LogName   = 'Application'
    StartTime = $eventSearchStart
  } -ErrorAction SilentlyContinue

  if ($recentEvents) {
    $installerFileName = [System.IO.Path]::GetFileName($resolvedInstallerPath)
    foreach ($ev in $recentEvents) {
      $msg = "$($ev.Message)"
      $prov = "$($ev.ProviderName)"
      $matched = ($prov -match 'Application Error|Windows Error Reporting') -or
                 ($msg -match 'System\.dll') -or
                 ($msg -match '0xc0000005') -or
                 ($msg -match 'RAG备考助手') -or
                 ($msg -match [regex]::Escape($installerFileName))

      if ($matched) {
        $exists = $false
        foreach ($item in $crashEvents) {
          if ($item.RecordId -eq $ev.RecordId) {
            $exists = $true
            break
          }
        }
        if (-not $exists) {
          $crashEvents.Add($ev)
        }
      }
    }
  }
} catch {}

# 3. 扫描 System 日志中的 Error
try {
  $sysFilter = @{
    LogName   = 'System'
    StartTime = $eventSearchStart
    Level     = @(1, 2)
  }
  $sysEvents = Get-WinEvent -FilterHashtable $sysFilter -ErrorAction SilentlyContinue
  if ($sysEvents) {
    foreach ($ev in $sysEvents) {
      $crashEvents.Add($ev)
    }
  }
} catch {}

# 分析关键 System.dll / 0xc0000005 崩溃
$systemDllCrashes = [System.Collections.Generic.List[string]]::new()
foreach ($ev in $crashEvents) {
  $combined = "$($ev.ProviderName) $($ev.Message)"
  if ($combined -match 'System\.dll' -or ($combined -match '0xc0000005' -and $combined -match 'RAG|Installer|setup')) {
    $systemDllCrashes.Add("[$($ev.TimeCreated)] Provider=$($ev.ProviderName), EventID=$($ev.Id), Detail=$($ev.Message)")
  }
}

if ($systemDllCrashes.Count -gt 0) {
  $hasCriticalCrash = $true
  Write-SmokeLog "ERROR" "发现致命崩溃事件（System.dll / 0xc0000005）！"
  foreach ($crashItem in $systemDllCrashes) {
    Write-SmokeLog "ERROR" "  - $crashItem"
    $failureReasons.Add("致命崩溃: $crashItem")
  }
} else {
  Write-SmokeLog "SUCCESS" "事件日志分析：未出现 System.dll / 0xc0000005 / Application Error 崩溃记录。"
}

# 导出 installer-crash-events.txt
$crashEventsFile = Join-Path $resolvedEvidenceDir "installer-crash-events.txt"
$logBuilder = [System.Text.StringBuilder]::new()
[void]$logBuilder.AppendLine("=========================================================================")
[void]$logBuilder.AppendLine("RAG备考助手 Windows 安装冒烟 - 事件与崩溃日志取证")
[void]$logBuilder.AppendLine("生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
[void]$logBuilder.AppendLine("扫描范围: $($eventSearchStart.ToString('yyyy-MM-dd HH:mm:ss')) 至今 (近10分钟)")
[void]$logBuilder.AppendLine("发现关键 System.dll / 0xc0000005 崩溃: $hasCriticalCrash")
[void]$logBuilder.AppendLine("相关事件记录总数: $($crashEvents.Count)")
[void]$logBuilder.AppendLine("=========================================================================`n")

if ($crashEvents.Count -eq 0) {
  [void]$logBuilder.AppendLine("【正常】未检索到任何相关的错误或崩溃日志事件。")
} else {
  foreach ($ev in $crashEvents) {
    [void]$logBuilder.AppendLine("-------------------------------------------------------------------------")
    [void]$logBuilder.AppendLine("TimeCreated : $($ev.TimeCreated)")
    [void]$logBuilder.AppendLine("Provider    : $($ev.ProviderName)")
    [void]$logBuilder.AppendLine("EventId     : $($ev.Id)")
    [void]$logBuilder.AppendLine("Level       : $($ev.LevelDisplayName)")
    [void]$logBuilder.AppendLine("Message     : $($ev.Message)")
    [void]$logBuilder.AppendLine("")
  }
}

[System.IO.File]::WriteAllText($crashEventsFile, $logBuilder.ToString(), [System.Text.Encoding]::UTF8)
Write-SmokeLog "INFO" "事件取证已写入: $crashEventsFile"

# 获取当前进程快照
$procSnapshots = @()
try {
  $runningNow = Get-Process | Where-Object { $_.ProcessName -match '^(RAG备考助手|backend|rag-study-assistant)' }
  if ($runningNow) {
    foreach ($p in $runningNow) {
      $procSnapshots += [ordered]@{
        id    = $p.Id
        name  = $p.ProcessName
        ws_mb = [math]::Round($p.WorkingSet64 / 1MB, 2)
      }
    }
  }
} catch {}

# -----------------------------------------------------------------------------
# f) 安装完整性校验
# -----------------------------------------------------------------------------
Write-SmokeLog "INFO" "--- 阶段 4/4: 执行安装完整性校验 ---"

# 1. 退出码校验
if ($timedOut) {
  $failureReasons.Add("校验失败：安装超时")
} elseif ($installerExitCode -ne 0) {
  $err = "校验失败：安装进程退出码非 0 (实际: $installerExitCode)"
  Write-SmokeLog "ERROR" $err
  $failureReasons.Add($err)
} else {
  Write-SmokeLog "SUCCESS" "进程退出码正常 (0)"
}

# 2. 主执行文件存在性校验
$exeExists = Test-Path -Path $DEFAULT_APP_EXE
if (-not $exeExists) {
  $err = "校验失败：主程序可执行文件未生成: $DEFAULT_APP_EXE"
  Write-SmokeLog "ERROR" $err
  $failureReasons.Add($err)
} else {
  $exeItem = Get-Item -Path $DEFAULT_APP_EXE
  Write-SmokeLog "SUCCESS" "主程序可执行文件存在: $DEFAULT_APP_EXE ($([math]::Round($exeItem.Length / 1MB, 2)) MB)"
}

# 3. 注册表卸载信息校验
$regExists = Test-Path -Path $UNINSTALL_REG_PATH
$matchedUninstallKey = $null
if ($regExists) {
  $matchedUninstallKey = $UNINSTALL_REG_PATH
  Write-SmokeLog "SUCCESS" "卸载注册表项校验通过: $UNINSTALL_REG_PATH"
} else {
  # 遍历匹配 DisplayName
  if (Test-Path -Path $uninstallBase) {
    $keys = Get-ChildItem -Path $uninstallBase -ErrorAction SilentlyContinue
    foreach ($k in $keys) {
      $dn = (Get-ItemProperty -Path $k.PSPath -Name DisplayName -ErrorAction SilentlyContinue).DisplayName
      if ($dn -and $dn -match 'RAG.*备考助手') {
        $regExists = $true
        $matchedUninstallKey = $k.PSPath
        Write-SmokeLog "SUCCESS" "找到卸载注册表项: $($k.PSPath) (DisplayName: $dn)"
        break
      }
    }
  }
  if (-not $regExists) {
    $err = "校验失败：未在注册表中找到卸载记录 (预期: $UNINSTALL_REG_PATH)"
    Write-SmokeLog "ERROR" $err
    $failureReasons.Add($err)
  }
}

# 更新 summary 汇总对象
$summary.duration_seconds          = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
$summary.timed_out                 = $timedOut
$summary.installer_exit_code       = $installerExitCode
$summary.installed_exe_exists      = $exeExists
$summary.uninstall_reg_exists      = $regExists
$summary.uninstall_reg_key         = if ($matchedUninstallKey) { $matchedUninstallKey } else { $UNINSTALL_REG_PATH }
$summary.crash_events_count        = $crashEvents.Count
$summary.critical_system_dll_crash = $hasCriticalCrash
$summary.failure_reasons           = $failureReasons.ToArray()
$summary.process_snapshot          = $procSnapshots

$summaryJsonPath = Join-Path $resolvedEvidenceDir "smoke-summary.json"

if ($failureReasons.Count -eq 0) {
  $summary.verdict = "SUCCESS"
  $summary | ConvertTo-Json -Depth 5 | Set-Content -Path $summaryJsonPath -Encoding UTF8
  Write-SmokeLog "INFO" "冒烟结果汇总已保存至: $summaryJsonPath"
  Write-SmokeLog "SUCCESS" "========================================================================="
  Write-SmokeLog "SUCCESS" "【冒烟测试通过】安装完成且完整性校验全部通过 (SUCCESS)！"
  Write-SmokeLog "SUCCESS" "========================================================================="
  exit 0
} else {
  $summary.verdict = "FAILED"
  $summary | ConvertTo-Json -Depth 5 | Set-Content -Path $summaryJsonPath -Encoding UTF8
  Write-SmokeLog "INFO" "冒烟结果汇总已保存至: $summaryJsonPath"
  Write-SmokeLog "ERROR" "========================================================================="
  Write-SmokeLog "ERROR" "【冒烟测试未通过】存在以下失败项:"
  foreach ($failItem in $failureReasons) {
    Write-SmokeLog "ERROR" "  - $failItem"
  }
  Write-SmokeLog "ERROR" "========================================================================="
  exit 1
}
