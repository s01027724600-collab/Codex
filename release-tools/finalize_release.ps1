param([Parameter(Mandatory=$true)][string]$PackageRoot, [string]$ZipPath = '')
$ErrorActionPreference='Stop'
$PackageRoot = [IO.Path]::GetFullPath($PackageRoot)
if (-not (Test-Path -LiteralPath (Join-Path $PackageRoot 'manifest.json'))) { throw 'Expected a built package containing manifest.json.' }
Copy-Item -Path (Join-Path $PSScriptRoot 'template\*') -Destination $PackageRoot -Recurse -Force
Get-ChildItem -LiteralPath $PackageRoot -File | Where-Object { $_.Extension -in @('.cmd','.ps1') } | ForEach-Object {
  $text=[IO.File]::ReadAllText($_.FullName).Replace("`r`n","`n").Replace("`n","`r`n")
  $encoding=if($_.Extension -eq '.ps1'){[Text.UTF8Encoding]::new($true)}else{[Text.ASCIIEncoding]::new()}
  [IO.File]::WriteAllText($_.FullName,$text,$encoding)
}
$manifest=Get-Content -LiteralPath (Join-Path $PackageRoot 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$manifest.files=@(Get-ChildItem -LiteralPath $PackageRoot -File -Recurse | Where-Object { $_.FullName -ne (Join-Path $PackageRoot 'manifest.json') } | ForEach-Object {
  [ordered]@{path=$_.FullName.Substring($PackageRoot.Length+1);size=$_.Length;sha256=(Get-FileHash -LiteralPath $_.FullName).Hash.ToLowerInvariant()}
})
[IO.File]::WriteAllText((Join-Path $PackageRoot 'manifest.json'),($manifest|ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
if ($ZipPath) {
  if(Test-Path -LiteralPath $ZipPath){throw 'ZIP already exists; choose a new filename.'}
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  [IO.Compression.ZipFile]::CreateFromDirectory($PackageRoot,[IO.Path]::GetFullPath($ZipPath),[IO.Compression.CompressionLevel]::Optimal,$true)
  $hash=Get-FileHash -LiteralPath $ZipPath
  [IO.File]::WriteAllText(($ZipPath+'.sha256'),($hash.Hash.ToLowerInvariant()+'  '+[IO.Path]::GetFileName($ZipPath)+"`r`n"),[Text.Encoding]::ASCII)
  Get-Item -LiteralPath $ZipPath | Select-Object FullName,Length
  $hash | Select-Object Hash
}
