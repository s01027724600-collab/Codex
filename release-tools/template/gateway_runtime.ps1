param(
  [ValidateSet('initialize','start','stop','status','repair','firewall','disable-autostart')]
  [string]$Command = 'status',
  [switch]$NoPause,
  [switch]$TestMode,
  [int]$PortOffset = 0,
  [string]$StateRoot = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
$Root = [IO.Path]::GetFullPath($PSScriptRoot)
if (-not $StateRoot) { $StateRoot = Join-Path $env:LOCALAPPDATA 'TeachingGateway' }
$StateRoot = [IO.Path]::GetFullPath($StateRoot)
if ($TestMode -and $StateRoot -eq (Join-Path $env:LOCALAPPDATA 'TeachingGateway')) {
  throw 'TestMode requires a separate -StateRoot.'
}
if ($PortOffset -ne 0 -and -not $TestMode) { throw 'PortOffset is only available in TestMode.' }
$AuthFile = Join-Path $StateRoot 'auth.json'
$Manifest = $null
$TranscriptStarted = $false
$Failures = [Collections.Generic.List[string]]::new()
$Checks = [ordered]@{}
$LogFile = ''
$NewPassword = ''
$PortPrograms = @{
  9090 = 'claude_gateway_agent'; 9091 = 'kill_gateway';
  7050 = 'touchpad_gateway'; 7000 = 'course_monitor'
}

function Q([string]$Value) {
  # Windows argv quoting; all managed path arguments are files/directories without a trailing slash.
  return '"' + $Value.Replace('"','\"') + '"'
}
function Write-Json([string]$Path, $Value) {
  $parent = Split-Path -Parent $Path
  [IO.Directory]::CreateDirectory($parent) | Out-Null
  $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
  [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
  if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, ($Path + '.previous')) }
  else { [IO.File]::Move($temporary, $Path) }
}
function Read-Json([string]$Path) {
  return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}
function Test-Admin {
  return [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Step([string]$Name, [scriptblock]$Action) {
  Write-Host "[$Name] ..."
  try {
    & $Action
    $Checks[$Name] = 'passed'
    Write-Host "[$Name] OK" -ForegroundColor Green
  } catch {
    $message = $_.Exception.Message
    $Checks[$Name] = $message
    $Failures.Add("${Name}: $message")
    Write-Host "[$Name] FAILED: $message" -ForegroundColor Red
  }
}
function Assert-Layout {
  if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) {
    throw 'Windows 10/11 64-bit is required.'
  }
  $script:Manifest = Read-Json (Join-Path $Root 'manifest.json')
  foreach ($entry in $Manifest.files) {
    $file = [IO.Path]::GetFullPath((Join-Path $Root $entry.path))
    if (-not $file.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid manifest path.' }
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Package file missing: $($entry.path). Extract the entire ZIP again." }
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $entry.sha256) {
      throw "Package file changed or damaged: $($entry.path). Extract the original ZIP to a new directory."
    }
  }
  $probe = Join-Path $Root ('.write-test-' + [guid]::NewGuid().ToString('N'))
  [IO.File]::WriteAllText($probe, 'test')
  [IO.File]::Delete($probe)
  if ($Root.Length -gt 150) { throw 'Extract to a shorter path, for example C:\TeachingGateway. Current path is too long.' }
}
function To-B64([byte[]]$Bytes) { return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+','-').Replace('/','_') }
function Ensure-Auth {
  if (Test-Path -LiteralPath $AuthFile) {
    $auth = Read-Json $AuthFile
    if ($auth.password_hash -notmatch '^pbkdf2_sha256\$\d+\$[^$]+\$[^$]+$' -or -not $auth.session_secret) {
      throw "Invalid authentication file: $AuthFile. Keep a backup, then rename it and rerun initialization."
    }
    return
  }
  if ($TestMode) { $password = 'Test-' + [guid]::NewGuid().ToString('N') }
  else {
    Write-Host 'Set one browser password for 7000 / 7050 / 9090 / 9091. It is NOT an API key.'
    do {
      $secure = Read-Host 'Password (at least 6 characters)' -AsSecureString
      $again = Read-Host 'Confirm password' -AsSecureString
      $password = [Net.NetworkCredential]::new('', $secure).Password
      $confirmation = [Net.NetworkCredential]::new('', $again).Password
      $valid = $password.Length -ge 6 -and $password -ceq $confirmation
      if (-not $valid) { Write-Host 'Passwords differ or are shorter than 6 characters. Please try again.' }
    } until ($valid)
  }
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  $salt = New-Object byte[] 16; $secret = New-Object byte[] 32
  $rng.GetBytes($salt); $rng.GetBytes($secret)
  $derive = [Security.Cryptography.Rfc2898DeriveBytes]::new($password, $salt, 260000, [Security.Cryptography.HashAlgorithmName]::SHA256)
  try {
    Write-Json $AuthFile ([ordered]@{
      password_hash = 'pbkdf2_sha256$260000$' + (To-B64 $salt) + '$' + (To-B64 $derive.GetBytes(32))
      session_secret = To-B64 $secret; session_days = 90
    })
    $script:NewPassword = $password
  } finally { $derive.Dispose(); $rng.Dispose(); $password = $null; $confirmation = $null }
}
function Ensure-Configs {
  foreach ($port in @(9090,9091,7050,7000)) {
    $configFile = Join-Path $StateRoot "p$port.json"
    if (Test-Path -LiteralPath $configFile) { $config = Read-Json $configFile }
    else {
      $config = Read-Json (Join-Path $Root "p$port\config.json")
      if ($port -eq 7000) {
        $config.target_root = Join-Path $StateRoot 'data\courses'
        $config.context_root = Join-Path $StateRoot 'data\screenshots'
        $config.state_dir = Join-Path $StateRoot 'state7000'
        if ($TestMode) { $config.screenshot_enabled = $false; $config.download_dirs = @((Join-Path $StateRoot 'empty-downloads')) }
      }
      Write-Json $configFile $config
    }
    if ($port -eq 7000) {
      foreach ($directory in @($config.target_root, $config.context_root, $config.state_dir)) {
        if (-not [IO.Path]::IsPathRooted($directory)) { throw "7000 directory must be absolute: $directory" }
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        $probe = Join-Path $directory ('.write-test-' + [guid]::NewGuid().ToString('N'))
        [IO.File]::WriteAllText($probe,'test'); [IO.File]::Delete($probe)
      }
    }
  }
  [IO.Directory]::CreateDirectory((Join-Path $StateRoot 'work')) | Out-Null
}
function Run-Probe([string]$Program, [string[]]$Arguments, [int]$TimeoutSeconds = 30) {
  $stamp = [guid]::NewGuid().ToString('N')
  $outLog = Join-Path $StateRoot "logs\probe-$stamp.out.log"
  $errLog = Join-Path $StateRoot "logs\probe-$stamp.err.log"
  $info = [Diagnostics.ProcessStartInfo]::new()
  $info.FileName = $Program
  $info.Arguments = ($Arguments | ForEach-Object { Q $_ }) -join ' '
  $info.WorkingDirectory = $Root
  $info.UseShellExecute = $false
  $info.CreateNoWindow = $true
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  $process = [Diagnostics.Process]::Start($info)
  $stdout = $process.StandardOutput.ReadToEndAsync()
  $stderr = $process.StandardError.ReadToEndAsync()
  if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    throw "Program timed out: $Program. Log: $errLog"
  }
  $exitCode = $process.ExitCode
  [IO.File]::WriteAllText($outLog, $stdout.Result, [Text.UTF8Encoding]::new($false))
  [IO.File]::WriteAllText($errLog, $stderr.Result, [Text.UTF8Encoding]::new($false))
  $process.Dispose()
  if ($exitCode -ne 0) { throw "Program failed (${exitCode}): $Program. Log: $errLog" }
  return $stdout.Result
}
function Test-WebView {
  foreach ($hive in @('HKCU:\Software\Microsoft\EdgeUpdate\Clients','HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients','HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients')) {
    foreach ($key in @(Get-ChildItem -LiteralPath $hive -ErrorAction SilentlyContinue)) {
      $value = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
      if ($value -and $value.PSObject.Properties['name'] -and $value.name -like '*WebView2*' -and $value.PSObject.Properties['pv'] -and $value.pv -ne '0.0.0.0') { return $true }
    }
  }
  return $false
}
function Ensure-Tools {
  Write-Host (Run-Probe (Join-Path $Root 'claude-code\claude.exe') @('--version'))
  Write-Host (Run-Probe (Join-Path $Root 'git\cmd\git.exe') @('--version'))
  Write-Host (Run-Probe (Join-Path $Root 'git\bin\bash.exe') @('-c','printf bash-ready'))
  if (-not (Test-WebView)) {
    if ($TestMode) { Write-Host 'TEST ONLY: WebView2 installation not performed.'; return }
    $installer = Join-Path $Root 'dependencies\WebView2-x64.exe'
    Write-Host 'Installing bundled offline WebView2 runtime. This may take several minutes...'
    $process = Start-Process -FilePath $installer -ArgumentList '/silent /install' -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(300000)) { throw 'WebView2 installation is still running. Wait, then rerun initialization.' }
    if (-not (Test-WebView)) { throw "WebView2 runtime was not detected after installation. Installer exit: $($process.ExitCode). Run dependencies\WebView2-x64.exe manually, then retry." }
  }
}
function Get-Listeners([int]$Port) {
  return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
}
function Program-Path([int]$Port) { return Join-Path $Root "p$Port\$($PortPrograms[$Port]).exe" }
function Test-Owner([int]$ProcessId, [string]$Expected) {
  $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
  return $process -and $process.ExecutablePath -and [string]::Equals($process.ExecutablePath,$Expected,[StringComparison]::OrdinalIgnoreCase)
}
function Test-Health([int]$Port) {
  $actual = $Port + $PortOffset
  $entry = $Manifest.services | Where-Object { $_.port -eq $Port }
  $response = Invoke-RestMethod -Uri "http://127.0.0.1:$actual/health" -TimeoutSec 2
  if (-not $response.ok -or $response.app -ne $entry.app -or $response.version -ne $entry.version) { throw "Port $actual is not the expected current service." }
  $ui = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$actual/" -TimeoutSec 5
  if ($ui.StatusCode -ne 200 -or [string]$ui.Content -notmatch '<html') { throw "Port $actual UI is unavailable." }
  if ($script:NewPassword) {
    $body = @{password=$script:NewPassword} | ConvertTo-Json -Compress
    $login = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$actual/login" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10
    if ($login.StatusCode -ne 200 -or -not $login.Headers['Set-Cookie']) { throw "Port $actual browser login failed." }
  }
}
function Start-One([int]$Port) {
  $actual = $Port + $PortOffset
  $program = Program-Path $Port
  $listeners = @(Get-Listeners $actual)
  if ($listeners.Count) {
    foreach ($listener in $listeners) {
      if (-not (Test-Owner $listener $program)) { throw "Port $actual is occupied by PID $listener from another program/package. It was NOT stopped. Close the old service, then retry." }
    }
    Test-Health $Port
    return
  }
  if (-not (Test-Path -LiteralPath $AuthFile)) { throw 'Run initialize.cmd first to configure login.' }
  $arguments = @('--host','0.0.0.0','--port',[string]$actual)
  if ($TestMode) { $arguments[1] = '127.0.0.1' }
  if ($Port -ne 9091) { $arguments += @('--config',(Join-Path $StateRoot "p$Port.json")) }
  $arguments += @('--auth-file',$AuthFile)
  if ($Port -eq 9090) { $arguments += @('--state-dir',(Join-Path $StateRoot 'state9090'),'--workdir',(Join-Path $StateRoot 'work'),'--claude-bin',(Join-Path $Root 'claude-code\claude.exe')) }
  $stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
  $outLog = Join-Path $StateRoot "logs\$actual-$stamp.out.log"
  $errLog = Join-Path $StateRoot "logs\$actual-$stamp.err.log"
  $oldPath = $env:PATH
  $oldBash = $env:CLAUDE_CODE_GIT_BASH_PATH
  try {
    $env:PATH = (Join-Path $Root 'git\cmd') + ';' + $oldPath
    $env:CLAUDE_CODE_GIT_BASH_PATH = Join-Path $Root 'git\bin\bash.exe'
    $process = Start-Process -FilePath $program -ArgumentList (($arguments | ForEach-Object { Q $_ }) -join ' ') -WorkingDirectory (Split-Path -Parent $program) -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru
  } finally { $env:PATH = $oldPath; $env:CLAUDE_CODE_GIT_BASH_PATH = $oldBash }
  $until = [DateTime]::UtcNow.AddSeconds(45)
  $lastError = ''
  do {
    $process.Refresh()
    if ($process.HasExited) { throw "Port $actual exited ($($process.ExitCode)). See $errLog" }
    try { Test-Health $Port; return } catch { $lastError = $_.Exception.Message }
    Start-Sleep -Milliseconds 500
  } until ([DateTime]::UtcNow -ge $until)
  throw "Port $actual did not become ready: $lastError. See $errLog"
}
function Stop-OwnedProcess([int]$ProcessId, [string]$Label) {
  try { $process = Get-Process -Id $ProcessId -ErrorAction Stop } catch { return }
  try {
    if (-not $process.HasExited) { $process.Kill() }
    if (-not $process.WaitForExit(7000)) { throw ('Timed out stopping ' + $Label + ' (PID ' + $ProcessId + ').') }
  } finally { $process.Dispose() }
}
function Stop-All {
  $processes = @(Get-CimInstance Win32_Process)
  $scanWorker = Join-Path $Root 'p7050\uia_scan_worker.exe'
  # Stop the optional scanner child first, so a forced gateway stop never leaves it polling in the background.
  foreach ($gateway in @($processes | Where-Object { $_.ExecutablePath -and [string]::Equals($_.ExecutablePath,(Program-Path 7050),[StringComparison]::OrdinalIgnoreCase) })) {
    foreach ($worker in @($processes | Where-Object { $_.ParentProcessId -eq $gateway.ProcessId -and $_.ExecutablePath -and [string]::Equals($_.ExecutablePath,$scanWorker,[StringComparison]::OrdinalIgnoreCase) })) {
      Stop-OwnedProcess $worker.ProcessId 'UI Automation scanner'
    }
  }
  foreach ($port in @(7050,7000,9091,9090)) {
    $program = Program-Path $port
    foreach ($target in @($processes | Where-Object { $_.ExecutablePath -and [string]::Equals($_.ExecutablePath,$program,[StringComparison]::OrdinalIgnoreCase) })) {
      Stop-OwnedProcess $target.ProcessId ('service ' + $port)
    }
  }
  Write-Host 'Stopped only service executables belonging to this extracted package. Data was kept.'
}
function Set-Firewall {
  if (-not (Test-Admin)) { throw 'Firewall helper requires administrator permission.' }
  foreach ($port in @(7000,7050,9090,9091)) {
    $name = "TeachingGateway-TCP-$port"
    $rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
    if ($rule) {
      Set-NetFirewallRule -Name $name -Enabled True -Action Allow -Direction Inbound -Profile Domain,Private | Out-Null
      $rule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $port | Out-Null
    } else { New-NetFirewallRule -Name $name -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port -Profile Domain,Private | Out-Null }
    $check = Get-NetFirewallRule -Name $name
    if ($check.Enabled -ne 'True' -or $check.Action -ne 'Allow') { throw "Firewall rule verification failed for $port." }
  }
}
function Ensure-Firewall {
  if ($TestMode) { Write-Host 'TEST ONLY: firewall unchanged; elevation branch requires classroom verification.'; return }
  if (Test-Admin) { Set-Firewall; return }
  # Elevate ONLY the firewall helper. Passwords, services and Startup remain under the classroom user.
  $arguments = '-NoProfile -ExecutionPolicy Bypass -File ' + (Q (Join-Path $Root 'gateway_runtime.ps1')) + ' -Command firewall -NoPause -StateRoot ' + (Q $StateRoot)
  try { $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru -Wait }
  catch { throw 'Administrator approval was denied/unavailable. Local services can run, but tablet access is not verified. Approve UAC and rerun initialize.cmd.' }
  if ($process.ExitCode -ne 0) { throw "Firewall helper failed (exit $($process.ExitCode)). See firewall logs in $StateRoot\logs." }
}
function Ensure-Autostart {
  $testStartup = [bool]$TestMode
  if ($testStartup) {
    $startup = Join-Path $StateRoot 'startup-test'
    [IO.Directory]::CreateDirectory($startup) | Out-Null
  } else { $startup = [Environment]::GetFolderPath('Startup') }
  $shell = New-Object -ComObject WScript.Shell
  $path = Join-Path $startup $(if ($testStartup) { 'TeachingGateway-test.lnk' } else { 'TeachingGateway.lnk' })
  $shortcut = $shell.CreateShortcut($path)
  $shortcut.TargetPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
  $shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ' + (Q (Join-Path $Root 'gateway_runtime.ps1')) + ' -Command start -NoPause -StateRoot ' + (Q $StateRoot)
  $shortcut.WorkingDirectory = $Root
  $shortcut.WindowStyle = 7
  $shortcut.Description = 'TeachingGateway 7000 / 7050 / 9090 / 9091'
  $shortcut.Save()
  if (-not (Test-Path -LiteralPath $path) -or $shell.CreateShortcut($path).Arguments -ne $shortcut.Arguments) { throw 'Current-user Startup verification failed.' }
  if ($testStartup) {
    Remove-Item -LiteralPath $path -Force -ErrorAction Stop
    Write-Host 'TEST ONLY: logon Startup command created, verified and removed outside the user Startup folder.'
  }
}
function Show-Urls {
  Write-Host "Data/config/logs: $StateRoot"
  Write-Host "Package: $Root"
  foreach ($address in @('127.0.0.1') + @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } | Select-Object -ExpandProperty IPAddress -Unique)) {
    Write-Host (($PortPrograms.Keys | Sort-Object | ForEach-Object { 'http://' + $address + ':' + ($_ + $PortOffset) + '/' }) -join '  ')
  }
}

try {
  [IO.Directory]::CreateDirectory((Join-Path $StateRoot 'logs')) | Out-Null
  $LogFile = Join-Path $StateRoot ('logs\' + $Command + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '.log')
  Start-Transcript -LiteralPath $LogFile -Force | Out-Null
  $TranscriptStarted = $true
  Write-Host 'TeachingGateway - deployment and recovery'
  if ($Command -eq 'firewall') { Set-Firewall }
  elseif ($Command -eq 'stop') { Stop-All }
  elseif ($Command -eq 'disable-autostart') {
    $link = Join-Path ([Environment]::GetFolderPath('Startup')) 'TeachingGateway.lnk'
    if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link; Write-Host 'Removed TeachingGateway Startup shortcut only. Rerun initialization to restore it.' }
  } else {
    Assert-Layout
    if ($Command -in @('initialize','repair')) {
      Step 'login configuration' { Ensure-Auth }
      Step 'storage configuration' { Ensure-Configs }
      Step 'bundled tools' { Ensure-Tools }
      Step 'firewall' { Ensure-Firewall }
      Step 'current-user logon startup' { Ensure-Autostart }
    }
    foreach ($port in @(7000,7050,9090,9091)) {
      $reviewPort = $port
      if ($Command -eq 'status') { Step "service $port" { Test-Health $reviewPort } }
      else { Step "service $port" { Start-One $reviewPort } }
    }
    Show-Urls
    Write-Host 'Claude API account/key is NOT included. Configure it with cc_switch.cmd before using 9090 AI tasks.'
    if ($TestMode) { Write-Host 'TEST RUN: firewall, logon startup, real LAN and paid AI calls were not exercised.' }
  }
} catch { $Failures.Add($_.Exception.Message); Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red }
finally {
  $NewPassword = $null
  $ok = $Failures.Count -eq 0
  try { Write-Json (Join-Path $StateRoot "last-$Command.json") ([ordered]@{ok=$ok; test_mode=[bool]$TestMode; command=$Command; package=$Root; time=(Get-Date).ToString('o'); checks=$Checks; failures=@($Failures.ToArray()); log=$LogFile}) } catch { $ok=$false; Write-Host "Could not save result: $($_.Exception.Message)" }
  if ($ok) { Write-Host 'Completed. For classroom use, also test access from the tablet and reboot once.' -ForegroundColor Green }
  else { Write-Host 'NOT COMPLETE. Correct the FAILED items and rerun initialize.cmd; existing data/password are preserved.' -ForegroundColor Red }
  Write-Host "Log: $LogFile"
  if ($TranscriptStarted) { Stop-Transcript | Out-Null }
  if (-not $NoPause) { [void](Read-Host 'Press Enter to close') }
}
if ($ok) { exit 0 } else { exit 1 }
