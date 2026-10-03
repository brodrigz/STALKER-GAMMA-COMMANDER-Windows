# Install the pinned build tool under build/tools (no administrator rights).
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDirectory = Join-Path $repoRoot 'build/tools'
$compilerDirectory = Join-Path $toolsDirectory 'inno-6.7.3'
$compiler = Join-Path $compilerDirectory 'ISCC.exe'
if (Test-Path -LiteralPath $compiler) {
    Write-Host "Inno Setup compiler: $compiler"
    return
}
New-Item -ItemType Directory -Force -Path $toolsDirectory | Out-Null
$installer = Join-Path $toolsDirectory 'innosetup-6.7.3.exe'
$checksum = '9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732'
if (-not (Test-Path -LiteralPath $installer)) {
    Invoke-WebRequest -Uri 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe' -OutFile $installer
}
if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $checksum) {
    throw 'Inno Setup download failed SHA-256 verification.'
}
# /CURRENTUSER keeps setup registration per-user; /DIR keeps compiler files in
# the workspace. Do not create desktop or Start Menu shortcuts for the tool.
$process = Start-Process -FilePath $installer -ArgumentList @(
    '/CURRENTUSER', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/NOICONS', '/SP-',
    "/DIR=`"$compilerDirectory`""
) -WindowStyle Hidden -PassThru -Wait
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $compiler)) {
    throw "Inno Setup compiler installation failed (exit $($process.ExitCode))."
}
Write-Host "Inno Setup compiler: $compiler"
