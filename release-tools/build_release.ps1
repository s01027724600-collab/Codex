param([string]$OutputRoot = '', [string]$Python = '', [switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe' }
if (-not $OutputRoot) { $OutputRoot = Join-Path $PSScriptRoot ('out\' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
$Package = Join-Path $OutputRoot 'TeachingGateway-x64'
[IO.Directory]::CreateDirectory($Package) | Out-Null
$utf8 = [Text.UTF8Encoding]::new($false)
$builds = @(
  @{port=7000; name='course_monitor'; version='0.3.3'; app='course-collector'; extra=@('--add-data', ((Join-Path $Project 'p7000\ui.html') + ';.'),'--hidden-import','tkinter')},
  @{port=7050; name='touchpad_gateway'; version='0.2.7'; app='claude-code-gateway-touchpad'; extra=@('--add-data', ((Join-Path $Project 'p7050\ui.html') + ';.'))},
  @{port=9090; name='claude_gateway_agent'; version='0.1.5'; app='claude-code-gateway'; extra=@()},
  @{port=9091; name='kill_gateway'; version='0.1.1'; app='claude-code-gateway-kill'; extra=@()}
)
foreach ($build in $builds) {
  $port = $build.port
  $destination = Join-Path $Package "p$port"
  if (-not $SkipBuild) {
    $work = Join-Path $OutputRoot "build\$port"
    [IO.Directory]::CreateDirectory($work) | Out-Null
    $arguments = @('-m','PyInstaller','--noconfirm','--onedir','--console','--noupx',
      '--name',$build.name,'--distpath',(Join-Path $OutputRoot 'dist'),
      '--workpath',$work,'--specpath',$work) + $build.extra + @((Join-Path $Project "p$port\$($build.name).py"))
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw "Build failed for $port" }
  }
  if (Test-Path -LiteralPath $destination) { throw "Destination already exists: $destination. Use a fresh OutputRoot." }
  Copy-Item -LiteralPath (Join-Path $OutputRoot "dist\$($build.name)") -Destination $destination -Recurse
  Copy-Item -LiteralPath (Join-Path $Project "p$port\config.json") -Destination (Join-Path $destination 'config.json')
  if ($port -eq 7000 -or $port -eq 7050) { Copy-Item -LiteralPath (Join-Path $Project "p$port\ui.html") -Destination $destination }
}
# UI Automation companion: build from current C# source, not an old exe.
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$wpf = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\WPF'
& $csc /nologo /target:exe /platform:x64 /optimize+ (('/out:' + (Join-Path $Package 'p7050\uia_scan_worker.exe'))) "/r:$wpf\UIAutomationClient.dll" "/r:$wpf\UIAutomationTypes.dll" "/r:$wpf\WindowsBase.dll" (Join-Path $Project 'p7050\uia_scan_worker.cs')
if ($LASTEXITCODE -ne 0) { throw 'UI Automation companion build failed.' }
Copy-Item -LiteralPath (Join-Path $Project 'p7050\scan_uia.ps1') -Destination (Join-Path $Package 'p7050')
foreach ($directory in @('claude-code','cc-switch','dependencies','git')) { [IO.Directory]::CreateDirectory((Join-Path $Package $directory)) | Out-Null }
$claude = Join-Path $PSScriptRoot 'downloads\claude-x64.exe'
if ((Get-FileHash -LiteralPath $claude).Hash -ne '51080c42aca4532d866e8f9d4efbe81bac6dfca2807f201125961e37a64155a7') { throw 'Claude Code official manifest checksum mismatch.' }
Copy-Item -LiteralPath $claude -Destination (Join-Path $Package 'claude-code\claude.exe')
Copy-Item -LiteralPath (Join-Path $Project 'tools\cc-switch\cc-switch.exe'),(Join-Path $Project 'tools\cc-switch\portable.ini') -Destination (Join-Path $Package 'cc-switch')
$webview = Join-Path $PSScriptRoot 'downloads\WebView2-x64.exe'
$signature = Get-AuthenticodeSignature -LiteralPath $webview
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'WebView2 Microsoft signature verification failed.' }
Copy-Item -LiteralPath $webview -Destination (Join-Path $Package 'dependencies\WebView2-x64.exe')
$gitArchive = Join-Path $PSScriptRoot 'downloads\PortableGit-x64.7z.exe'
$gitDestination = Join-Path $Package 'git'
$extract = Start-Process -FilePath $gitArchive -ArgumentList @('-y',('-o"' + $gitDestination + '"')) -WindowStyle Hidden -PassThru -Wait
if ($extract.ExitCode -ne 0 -or -not (Test-Path -LiteralPath (Join-Path $gitDestination 'bin\bash.exe'))) { throw 'Portable Git extraction failed.' }
Copy-Item -Path (Join-Path $PSScriptRoot 'template\*') -Destination $Package -Recurse
[IO.Directory]::CreateDirectory((Join-Path $Package 'docs')) | Out-Null
Copy-Item -Path (Join-Path $Project 'docs\*.md') -Destination (Join-Path $Package 'docs')
# Normalize Windows launch scripts for cmd.exe and Windows PowerShell 5.1.
Get-ChildItem -LiteralPath $Package -File | Where-Object { $_.Extension -in @('.cmd','.ps1') } | ForEach-Object {
  $text = [IO.File]::ReadAllText($_.FullName).Replace("`r`n","`n").Replace("`n","`r`n")
  $encoding = if ($_.Extension -eq '.ps1') { [Text.UTF8Encoding]::new($true) } else { [Text.ASCIIEncoding]::new() }
  [IO.File]::WriteAllText($_.FullName,$text,$encoding)
}
# Defaults are templates only. Initializer writes machine-specific settings outside the package.
$configFile = Join-Path $Package 'p7000\config.json'
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
$config.target_root=''; $config.context_root=''; $config.state_dir=''
[IO.File]::WriteAllText($configFile,($config | ConvertTo-Json -Depth 8),$utf8)
$files = @(Get-ChildItem -LiteralPath $Package -Recurse -File | ForEach-Object {
  [ordered]@{path=$_.FullName.Substring($Package.Length+1); size=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName).Hash.ToLowerInvariant()}
})
$sourceHashes = @($builds | ForEach-Object {
  $source = Join-Path $Project "p$($_.port)\$($_.name).py"
  [ordered]@{port=$_.port; file=[IO.Path]::GetFileName($source); sha256=(Get-FileHash -LiteralPath $source).Hash.ToLowerInvariant()}
})
$manifest = [ordered]@{format=1; built_at=(Get-Date).ToString('o'); architecture='x64'; services=@($builds | ForEach-Object { [ordered]@{port=$_.port; app=$_.app; version=$_.version} }); tools=@{claude_code='2.1.160'; cc_switch='3.16.1'; git='2.55.0.windows.5'}; sources=$sourceHashes; files=$files}
[IO.File]::WriteAllText((Join-Path $Package 'manifest.json'),($manifest | ConvertTo-Json -Depth 12),$utf8)
Write-Host "PACKAGE_READY=$Package"
