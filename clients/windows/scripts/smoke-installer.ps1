<# Run only on a disposable Windows CI runner. Does not connect to a service. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$InstallerPath,
  [Parameter(Mandatory = $true)][string]$EvidenceDir,
  [ValidateRange(20, 300)][int]$TimeoutSeconds = 120
)
$ErrorActionPreference = 'Stop'
if ($env:CI -ne 'true' -and $env:GITHUB_ACTIONS -ne 'true') {
  throw 'Installer smoke is restricted to a disposable CI environment.'
}
$installer = (Resolve-Path -LiteralPath $InstallerPath).Path
$product = 'StudyTool Connected'
$uninstallRoots = @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')
foreach ($registryRoot in $uninstallRoots) {
  foreach ($key in (Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
    if ((Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue).DisplayName -match '^StudyTool Connected(?:\s|$)') {
      throw 'An existing Connected installation was found. Refusing to install or uninstall it.'
    }
  }
}
if (Get-Process -Name $product -ErrorAction SilentlyContinue) {
  throw 'An existing Connected process was found. Refusing to touch it.'
}
foreach ($candidate in @(
  (Join-Path $env:LOCALAPPDATA "Programs\$product"),
  (Join-Path ([Environment]::GetFolderPath('Desktop')) "$product.lnk"),
  (Join-Path ([Environment]::GetFolderPath('Programs')) "$product.lnk")
)) {
  if (Test-Path -LiteralPath $candidate) { throw "Existing Connected files found: $candidate" }
}
New-Item -Path $EvidenceDir -ItemType Directory -Force | Out-Null
$evidence = (Resolve-Path -LiteralPath $EvidenceDir).Path
$token = [Guid]::NewGuid().ToString('N')
$installDir = Join-Path ([IO.Path]::GetTempPath()) "studytool-connected-install-$token"
$profileDir = Join-Path ([IO.Path]::GetTempPath()) "studytool-connected-smoke-$token"
if ((Test-Path -LiteralPath $installDir) -or (Test-Path -LiteralPath $profileDir)) { throw 'Smoke temp directory already exists.' }
$exe = Join-Path $installDir "$product.exe"
$summary = [ordered]@{ verdict = 'FAILED'; install_exit = $null; installed_exe = $exe; settings_title = ''; startup_pid = $null; graceful_exit = $false; uninstall_exit = $null; error = '' }
$started = $null
$installedByThisRun = $false

function Wait-SmokeProcess([System.Diagnostics.Process]$Process, [string]$Stage) {
  if (-not $Process.WaitForExit($TimeoutSeconds * 1000)) {
    & taskkill.exe /PID $Process.Id /T /F | Out-Null
    throw "$Stage timed out."
  }
  $Process.Refresh()
  return $Process.ExitCode
}

try {
  # NSIS requires /D to be last, without quotes around its value.
  $install = Start-Process -FilePath $installer -ArgumentList "/S /currentuser /D=$installDir" -PassThru
  $installedByThisRun = $true
  $summary.install_exit = Wait-SmokeProcess $install 'Install'
  if ($summary.install_exit -ne 0 -or -not (Test-Path -LiteralPath $exe)) { throw 'Installation did not produce the expected executable.' }
  $started = Start-Process -FilePath $exe -ArgumentList "--smoke-test-profile=`"$profileDir`"" -PassThru
  $summary.startup_pid = $started.Id
  $clock = [Diagnostics.Stopwatch]::StartNew()
  while ($clock.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
    $started.Refresh()
    if ($started.HasExited) { throw "Client exited before showing settings (code $($started.ExitCode))." }
    if ($started.MainWindowHandle -ne [IntPtr]::Zero -and $started.MainWindowTitle.Contains($product)) {
      $summary.settings_title = $started.MainWindowTitle
      break
    }
    Start-Sleep -Milliseconds 250
  }
  if (-not $summary.settings_title) { throw 'Client did not show its first-run connection settings window.' }
  if (-not (Test-Path -LiteralPath $profileDir)) { throw 'Isolated profile was not created.' }
  if (Test-Path -LiteralPath (Join-Path $profileDir 'service.json')) { throw 'Smoke unexpectedly acquired a configured service.' }
  if (-not $started.CloseMainWindow()) { throw 'Could not request a normal client exit.' }
  $summary.graceful_exit = $started.WaitForExit(15000)
  if (-not $summary.graceful_exit) { throw 'Client did not exit normally after closing its window.' }
  if ($started.ExitCode -ne 0) { throw "Client exit code was $($started.ExitCode)." }
  $summary.verdict = 'SUCCESS'
} catch {
  $summary.error = $_.Exception.Message
} finally {
  if ($started -and -not $started.HasExited) {
    & taskkill.exe /PID $started.Id /T /F | Out-Null
  }
  # Only uninstall this run's explicit temporary destination. Never search for an existing install.
  if ($installedByThisRun) {
    try {
      $uninstaller = Join-Path $installDir "Uninstall $product.exe"
      if (-not (Test-Path -LiteralPath $uninstaller)) { throw 'Temporary installation uninstaller was not found.' }
      $uninstall = Start-Process -FilePath $uninstaller -ArgumentList '/S /currentuser' -PassThru
      $summary.uninstall_exit = Wait-SmokeProcess $uninstall 'Uninstall'
      if ($summary.uninstall_exit -ne 0) { throw 'Temporary installation uninstall failed.' }
      $cleanupClock = [Diagnostics.Stopwatch]::StartNew()
      while ((Test-Path -LiteralPath $exe) -and $cleanupClock.Elapsed.TotalSeconds -lt 15) { Start-Sleep -Milliseconds 250 }
      if (Test-Path -LiteralPath $exe) { throw 'Temporary executable remains after uninstall.' }
    } catch {
      $summary.verdict = 'FAILED'
      $summary.error = "$($summary.error) Cleanup: $($_.Exception.Message)".Trim()
    }
  }
  # These paths are unique direct temp children generated above; no user data paths are removed.
  if (Test-Path -LiteralPath $profileDir) { Remove-Item -LiteralPath $profileDir -Recurse -Force }
  if (Test-Path -LiteralPath $installDir) { Remove-Item -LiteralPath $installDir -Recurse -Force }
  $summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $evidence 'smoke-summary.json') -Encoding UTF8
}
if ($summary.verdict -ne 'SUCCESS') { throw $summary.error }
Write-Host 'Connected installer/startup/exit/uninstall smoke passed (no remote-service test).'
