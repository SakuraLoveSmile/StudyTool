<# Run only on a disposable Windows CI runner. Does not connect to a service. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstallerPath,
  [Parameter(Mandatory = $true)][string]$EvidenceDir,
  [ValidateRange(20, 300)][int]$TimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
# Establish evidence before preflight so a rejected or failed run also has a report.
New-Item -Path $EvidenceDir -ItemType Directory -Force | Out-Null
$evidence = (Resolve-Path -LiteralPath $EvidenceDir).Path
$report = Join-Path $evidence 'smoke-summary.json'
$product = 'StudyTool Connected'
$token = [Guid]::NewGuid().ToString('N')
$installDir = Join-Path ([IO.Path]::GetTempPath()) "studytool-connected-install-$token"
$profileDir = Join-Path ([IO.Path]::GetTempPath()) "studytool-connected-smoke-$token"
$exe = Join-Path $installDir "$product.exe"
$uninstallRoots = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
$script:roots = @()
$script:currentStage = $null
$script:snapshotNumber = 0
$script:killNumber = 0
$script:client = $null
$installedByThisRun = $false
$pathsOwnedByThisRun = $false
$summary = [ordered]@{
  verdict = 'FAILED'; started_at = (Get-Date).ToString('o'); finished_at = $null
  installer_path = $InstallerPath; install_dir = $installDir; profile_dir = $profileDir
  install_exit = $null; installed_exe = $exe; installed_exe_exists = $false
  settings_title = ''; startup_pid = $null; startup_exit = $null; graceful_exit = $false
  uninstall_exit = $null; uninstalled_exe_absent = $false; uninstall_registry_absent = $false
  error = ''; failures = @(); stages = @(); process_snapshots = @(); window_snapshots = @(); remaining_processes = @()
}

function Initialize-WindowProbe {
  Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
namespace StudyToolSmoke {
  public class WindowInfo {
    public long Handle; public int Pid; public bool Visible; public string ClassName; public string Text;
    public List<WindowInfo> Children = new List<WindowInfo>();
  }
  public static class WindowProbe {
    private delegate bool EnumProc(IntPtr hwnd, IntPtr param);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumProc callback, IntPtr param);
    [DllImport("user32.dll")] private static extern bool EnumChildWindows(IntPtr parent, EnumProc callback, IntPtr param);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, StringBuilder name, int count);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, UIntPtr count, StringBuilder text, uint flags, uint timeout, out UIntPtr result);
    private static WindowInfo Read(IntPtr hwnd) {
      uint pid; GetWindowThreadProcessId(hwnd, out pid);
      var name = new StringBuilder(256); GetClassName(hwnd, name, name.Capacity);
      var text = new StringBuilder(4096); UIntPtr result;
      SendMessageTimeout(hwnd, 0x000D, new UIntPtr((uint)text.Capacity), text, 2, 200, out result);
      return new WindowInfo { Handle = hwnd.ToInt64(), Pid = (int)pid, Visible = IsWindowVisible(hwnd), ClassName = name.ToString(), Text = text.ToString() };
    }
    public static List<WindowInfo> Capture(int[] pids) {
      var owned = new HashSet<int>(pids); var windows = new List<WindowInfo>();
      EnumWindows(delegate(IntPtr hwnd, IntPtr unused) {
        uint pid; GetWindowThreadProcessId(hwnd, out pid);
        if (!owned.Contains((int)pid)) return true;
        var window = Read(hwnd);
        EnumChildWindows(hwnd, delegate(IntPtr child, IntPtr ignored) {
          if (window.Children.Count >= 40) return false;
          window.Children.Add(Read(child)); return true;
        }, IntPtr.Zero);
        windows.Add(window); return windows.Count < 40;
      }, IntPtr.Zero);
      return windows;
    }
  }
}
'@
}
function Write-SmokeSummary {
  $summary | ConvertTo-Json -Depth 9 | Set-Content -LiteralPath $report -Encoding UTF8
}
function Start-SmokeStage([string]$Name) {
  $script:currentStage = [ordered]@{ name = $Name; started_at = (Get-Date).ToString('o'); finished_at = $null; duration_seconds = $null; status = 'RUNNING'; error = ''; pid = $null; exit_code = $null }
  $summary.stages += $script:currentStage
  Write-Host "[smoke] Starting $Name"
  Write-SmokeSummary
}
function Finish-SmokeStage([string]$Status = 'SUCCESS', [string]$ErrorMessage = '') {
  if (-not $script:currentStage) { return }
  $script:currentStage.finished_at = (Get-Date).ToString('o')
  $script:currentStage.duration_seconds = [Math]::Round(((Get-Date) - [datetime]$script:currentStage.started_at).TotalSeconds, 3)
  $script:currentStage.status = $Status
  $script:currentStage.error = $ErrorMessage
  Write-Host "[smoke] $($script:currentStage.name): $Status ($($script:currentStage.duration_seconds)s) $ErrorMessage"
  Write-SmokeSummary
}
function Add-SmokeFailure([string]$Stage, [string]$Message) {
  if (-not $summary.error) { $summary.error = "$Stage`: $Message" }
  $summary.failures += [ordered]@{ stage = $Stage; message = $Message }
  $summary.verdict = 'FAILED'
  Write-Host "[smoke] FAILED $Stage`: $Message"
}
function Get-ConnectedRegistrations {
  foreach ($registryRoot in $uninstallRoots) {
    foreach ($key in (Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
      $entry = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
      if ($entry.DisplayName -match '^StudyTool Connected(?:\s|$)') {
        [pscustomobject]@{ key = $key.PSPath; name = $entry.DisplayName; icon = $entry.DisplayIcon }
      }
    }
  }
}
function Get-OwnedProcesses {
  # The generated paths contain this run's GUID. Never kill a process by product name alone.
  $all = @(Get-CimInstance Win32_Process -OperationTimeoutSec 10 -ErrorAction Stop)
  $selected = @{}
  foreach ($item in $all) {
    $ownedPath = $item.ExecutablePath -and $item.ExecutablePath.StartsWith($installDir + '\', [StringComparison]::OrdinalIgnoreCase)
    $ownedArguments = $item.CommandLine -and ($item.CommandLine.Contains($installDir) -or $item.CommandLine.Contains($profileDir))
    $ownedRoot = $false
    foreach ($root in $script:roots) {
      if ($item.ProcessId -eq $root.id -and [Math]::Abs(([datetime]$item.CreationDate - $root.started).TotalSeconds) -lt 2) { $ownedRoot = $true }
    }
    if ($ownedPath -or $ownedArguments -or $ownedRoot) { $selected[[int]$item.ProcessId] = $item }
  }
  do {
    $added = $false
    foreach ($item in $all) {
      if (-not $selected.ContainsKey([int]$item.ProcessId) -and $selected.ContainsKey([int]$item.ParentProcessId)) {
        $parent = $selected[[int]$item.ParentProcessId]
        if ([datetime]$item.CreationDate -ge [datetime]$parent.CreationDate) { $selected[[int]$item.ProcessId] = $item; $added = $true }
      }
    }
  } while ($added)
  foreach ($item in $selected.Values) {
    [pscustomobject]@{ pid = [int]$item.ProcessId; parent_pid = [int]$item.ParentProcessId; name = $item.Name; executable = $item.ExecutablePath; command_line = $item.CommandLine; created_at = ([datetime]$item.CreationDate).ToString('o') }
  }
}
function Save-ProcessSnapshot([string]$Label) {
  $script:snapshotNumber++
  $file = "process-$($script:snapshotNumber)-$Label.json"
  $processes = @(Get-OwnedProcesses)
  ConvertTo-Json -InputObject $processes -Depth 5 | Set-Content -LiteralPath (Join-Path $evidence $file) -Encoding UTF8
  $summary.process_snapshots += $file
  if ($Label -eq 'first-window-result' -or $Label -eq 'before-cleanup' -or $Label -like '*timeout') {
    if ('StudyToolSmoke.WindowProbe' -as [type]) {
      $windowFile = "windows-$($script:snapshotNumber)-$Label.json"
      $windows = @([StudyToolSmoke.WindowProbe]::Capture([int[]]@($processes | ForEach-Object { $_.pid })))
      ConvertTo-Json -InputObject $windows -Depth 7 | Set-Content -LiteralPath (Join-Path $evidence $windowFile) -Encoding UTF8
      $summary.window_snapshots += $windowFile
    }
  }
  Write-SmokeSummary
  return $processes
}
function Start-SmokeProcess([string]$FilePath, [string]$Arguments, [string]$Label) {
  $stdout = Join-Path $evidence "$Label-stdout.log"
  $stderr = Join-Path $evidence "$Label-stderr.log"
  # An explicit neutral working directory also prevents a child holding the install directory as its cwd.
  $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $evidence -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
  $null = $process.Handle # Cache the handle before a short-lived NSIS launcher exits.
  $script:roots += @{ id = $process.Id; started = $process.StartTime }
  $script:currentStage.pid = $process.Id
  $script:currentStage.stdout = [IO.Path]::GetFileName($stdout)
  $script:currentStage.stderr = [IO.Path]::GetFileName($stderr)
  Write-SmokeSummary
  return $process
}
function Wait-SmokeProcess([System.Diagnostics.Process]$Process, [string]$Stage, [int]$Seconds = $TimeoutSeconds) {
  if (-not $Process.WaitForExit($Seconds * 1000)) {
    $null = Save-ProcessSnapshot "$Stage-timeout"
    throw "$Stage process $($Process.Id) timed out after $Seconds seconds."
  }
  $script:currentStage.exit_code = $Process.ExitCode
  return $Process.ExitCode
}
function Stop-OwnedProcesses {
  foreach ($item in @(Get-OwnedProcesses)) {
    # Recheck identity immediately before taskkill, including start time to avoid a reused PID.
    $live = Get-Process -Id $item.pid -ErrorAction SilentlyContinue
    if ($live -and [Math]::Abs(($live.StartTime - [datetime]$item.created_at).TotalSeconds) -lt 2) {
      # A root may exit between the snapshot and taskkill. Record the tool result,
      # then enforce actual process absence below. Forced cleanup never passes a failed normal-exit gate.
      $script:killNumber++
      $label = "cleanup-taskkill-$($script:killNumber)-$($item.pid)"
      $killer = Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" -ArgumentList "/PID $($item.pid) /T /F" -WorkingDirectory $evidence -RedirectStandardOutput (Join-Path $evidence "$label-stdout.log") -RedirectStandardError (Join-Path $evidence "$label-stderr.log") -PassThru
      $null = $killer.Handle
      if (-not $killer.WaitForExit(15000)) {
        Stop-Process -Id $killer.Id -Force -ErrorAction SilentlyContinue
        throw "Cleanup taskkill for PID $($item.pid) timed out after 15 seconds."
      }
      "PID $($item.pid) taskkill exit=$($killer.ExitCode)" | Out-File -LiteralPath (Join-Path $evidence 'cleanup-taskkill.log') -Append -Encoding UTF8
    }
  }
  # taskkill can return before Win32_Process drops the terminated entry. Wait for
  # actual tree disappearance, bounded independently from the normal-exit gate.
  $exitClock = [Diagnostics.Stopwatch]::StartNew()
  $remaining = @(Get-OwnedProcesses)
  while ($remaining.Count -and $exitClock.Elapsed.TotalSeconds -lt 10) {
    Start-Sleep -Milliseconds 100
    $remaining = @(Get-OwnedProcesses)
  }
  if ($remaining.Count) { throw "Owned processes still running: $($remaining.pid -join ', ')" }
}

try {
  Start-SmokeStage 'preflight'
  if ($env:CI -ne 'true' -and $env:GITHUB_ACTIONS -ne 'true') { throw 'Installer smoke is restricted to a disposable CI environment.' }
  Initialize-WindowProbe
  $summary.runtime_context = @{ powershell = $PSVersionTable.PSVersion.ToString(); session_id = (Get-Process -Id $PID).SessionId; interactive = [Environment]::UserInteractive }
  $installer = (Resolve-Path -LiteralPath $InstallerPath).Path
  $summary.installer_path = $installer
  if (@(Get-ConnectedRegistrations).Count) { throw 'An existing Connected installation was found. Refusing to install or uninstall it.' }
  if (Get-Process -Name $product -ErrorAction SilentlyContinue) { throw 'An existing Connected process was found. Refusing to touch it.' }
  foreach ($candidate in @((Join-Path $env:LOCALAPPDATA "Programs\$product"), (Join-Path ([Environment]::GetFolderPath('Desktop')) "$product.lnk"), (Join-Path ([Environment]::GetFolderPath('Programs')) "$product.lnk"), $installDir, $profileDir)) {
    if (Test-Path -LiteralPath $candidate) { throw "Existing Connected files found: $candidate" }
  }
  $pathsOwnedByThisRun = $true
  Finish-SmokeStage

  Start-SmokeStage 'install'
  # NSIS /D must be last and unquoted. Assisted /S does not auto-run without --force-run.
  $install = Start-SmokeProcess $installer "/S /currentuser /D=$installDir" 'install'
  $installedByThisRun = $true
  $null = Save-ProcessSnapshot 'install-started'
  $summary.install_exit = Wait-SmokeProcess $install 'install'
  $summary.installed_exe_exists = Test-Path -LiteralPath $exe
  if ($summary.install_exit -ne 0 -or -not $summary.installed_exe_exists) { throw 'Installation did not produce the expected executable.' }
  $summary.install_registry = @(Get-ConnectedRegistrations)
  if (-not $summary.install_registry.Count) { throw 'Installation did not create its uninstall registration.' }
  Finish-SmokeStage

  Start-SmokeStage 'first-window'
  $summary.profile_existed_before_launch = Test-Path -LiteralPath $profileDir
  $electronLog = Join-Path $evidence 'electron.log'
  $script:client = Start-SmokeProcess $exe "--smoke-test-profile=`"$profileDir`" --enable-logging --log-file=`"$electronLog`"" 'client'
  $summary.startup_pid = $script:client.Id
  $null = Save-ProcessSnapshot 'client-started'
  $clock = [Diagnostics.Stopwatch]::StartNew()
  while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
    $script:client.Refresh()
    if ($script:client.HasExited) { $summary.startup_exit = $script:client.ExitCode; throw "Client exited before showing settings (code $($script:client.ExitCode))." }
    if ($script:client.MainWindowHandle -ne [IntPtr]::Zero -and $script:client.MainWindowTitle.Contains($product)) { $summary.settings_title = $script:client.MainWindowTitle; break }
    Start-Sleep -Milliseconds 250
  }
  $null = Save-ProcessSnapshot 'first-window-result'
  if (-not $summary.settings_title) { throw "Client did not show its first-run settings window within $TimeoutSeconds seconds." }
  if (-not (Test-Path -LiteralPath $profileDir)) { throw 'Isolated profile was not created.' }
  if (Test-Path -LiteralPath (Join-Path $profileDir 'service.json')) { throw 'Smoke unexpectedly acquired a configured service.' }
  Finish-SmokeStage

  Start-SmokeStage 'normal-exit'
  if (-not $script:client.CloseMainWindow()) { throw 'Could not request a normal client exit.' }
  $summary.startup_exit = Wait-SmokeProcess $script:client 'normal-exit' 15
  $summary.graceful_exit = $true
  if ($summary.startup_exit -ne 0) { throw "Client exit code was $($summary.startup_exit)." }
  if (@(Get-OwnedProcesses).Count) { throw 'Client descendants remain after its main process exited.' }
  Finish-SmokeStage
} catch {
  $failedStage = if ($script:currentStage) { $script:currentStage.name } else { 'initialization' }
  Add-SmokeFailure $failedStage $_.Exception.Message
  Finish-SmokeStage 'FAILED' $_.Exception.Message
} finally {
  # Cleanup errors are additional failures, never replacements for the original error.
  try {
    if ($installedByThisRun) {
      Start-SmokeStage 'stop-leftover-processes'
      $null = Save-ProcessSnapshot 'before-cleanup'
      Stop-OwnedProcesses
      Finish-SmokeStage

      Start-SmokeStage 'uninstall'
      $uninstaller = Join-Path $installDir "Uninstall $product.exe"
      if (-not (Test-Path -LiteralPath $uninstaller)) { throw 'Temporary installation uninstaller was not found.' }
      # NSIS normally self-copies and exits its outer process early. _?= runs in place, so
      # WaitForExit observes the actual uninstaller; the caller removes its leftover file.
      $uninstall = Start-SmokeProcess $uninstaller "/S /currentuser _?=$installDir" 'uninstall'
      $null = Save-ProcessSnapshot 'uninstall-started'
      $summary.uninstall_exit = Wait-SmokeProcess $uninstall 'uninstall'
      $summary.uninstalled_exe_absent = -not (Test-Path -LiteralPath $exe)
      $summary.uninstall_registry_remaining = @(Get-ConnectedRegistrations)
      $summary.uninstall_registry_absent = $summary.uninstall_registry_remaining.Count -eq 0
      if ($summary.uninstall_exit -ne 0 -or -not $summary.uninstalled_exe_absent -or -not $summary.uninstall_registry_absent) { throw 'Uninstaller did not remove the executable and uninstall registration successfully.' }
      if (@(Get-OwnedProcesses).Count) { throw 'Owned processes remain after uninstall completed.' }
      Finish-SmokeStage
    }
  } catch {
    Add-SmokeFailure $script:currentStage.name $_.Exception.Message
    Finish-SmokeStage 'FAILED' $_.Exception.Message
  } finally {
    try {
      Start-SmokeStage 'temporary-files'
      # Only unique paths generated above may be removed, and only after their processes exit.
      if ($installedByThisRun) { Stop-OwnedProcesses }
      if ($pathsOwnedByThisRun) {
        foreach ($directory in @($profileDir, $installDir)) {
          if (Test-Path -LiteralPath $directory) { Remove-Item -LiteralPath $directory -Recurse -Force }
        }
      }
      Finish-SmokeStage
    } catch {
      Add-SmokeFailure 'temporary-files' $_.Exception.Message
      Finish-SmokeStage 'FAILED' $_.Exception.Message
    } finally {
      try {
        $startupTrace = "$profileDir.startup.jsonl"
        if (Test-Path -LiteralPath $startupTrace) {
          Copy-Item -LiteralPath $startupTrace -Destination (Join-Path $evidence 'startup-trace.jsonl')
          Remove-Item -LiteralPath $startupTrace -Force
        }
      } catch { Add-SmokeFailure 'startup-trace-copy' $_.Exception.Message }
      try { $summary.remaining_processes = @(Save-ProcessSnapshot 'final') } catch { Add-SmokeFailure 'final-process-snapshot' $_.Exception.Message }
      $summary.finished_at = (Get-Date).ToString('o')
      if (-not $summary.failures.Count -and $summary.graceful_exit -and $summary.uninstalled_exe_absent -and $summary.uninstall_registry_absent) { $summary.verdict = 'SUCCESS' }
      Write-SmokeSummary
    }
  }
}
if ($summary.verdict -ne 'SUCCESS') { throw $summary.error }
Write-Host 'Connected installer/startup/normal-exit/uninstall smoke passed (no remote-service test).'
