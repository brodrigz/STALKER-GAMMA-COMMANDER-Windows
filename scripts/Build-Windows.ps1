param(
    [switch]$Installer,
    [string]$InnoCompilerPath,
    [string]$OutputDirectory,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run scripts/Setup-Windows.ps1 first.' }
& $python -c "import sys, struct, PyInstaller; assert sys.platform == 'win32' and struct.calcsize('P') == 8, 'Build with Windows x64 Python'"
if ($LASTEXITCODE -ne 0) { throw 'Install the Windows build dependencies: .venv\Scripts\python.exe -m pip install -r requirements-build-windows.txt' }

if ($Installer) {
    if (-not $InnoCompilerPath) {
        $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($command) { $InnoCompilerPath = $command.Source }
        foreach ($candidate in @(
            (Join-Path $repoRoot 'build/tools/inno-6.7.3/ISCC.exe'),
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 7/ISCC.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs/Inno Setup 6/ISCC.exe')
        )) {
            if (-not $InnoCompilerPath -and (Test-Path -LiteralPath $candidate)) { $InnoCompilerPath = $candidate }
        }
    }
    if (-not $InnoCompilerPath -or -not (Test-Path -LiteralPath $InnoCompilerPath)) {
        throw 'Run scripts/Setup-InnoSetup.ps1, install Inno Setup 6.3+, or pass -InnoCompilerPath pointing to ISCC.exe.'
    }
}

& (Join-Path $PSScriptRoot 'Setup-Windows.ps1') -SkipPython
$generated = Join-Path $repoRoot 'build/windows/metadata'
$buildWork = Join-Path $repoRoot 'build/windows/pyinstaller'
$distDirectory = Join-Path $repoRoot 'dist/windows'
$bundleDirectory = Join-Path $distDirectory 'STALKER-GAMMA-COMMANDER'
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repoRoot 'dist/releases' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$metadataTool = Join-Path $repoRoot 'packaging/windows/build_metadata.py'

Push-Location $repoRoot
try {
    & $python $metadataTool prepare --generated $generated
    if ($LASTEXITCODE -ne 0) { throw 'Windows build resource generation failed.' }
    & $python -m PyInstaller --noconfirm --clean --distpath $distDirectory --workpath $buildWork (Join-Path $repoRoot 'packaging/windows/commander.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    & $python $metadataTool finalise --generated $generated --bundle $bundleDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Windows release metadata generation failed.' }
    if (-not $SkipSmokeTest) {
        & (Join-Path $PSScriptRoot 'Test-WindowsPackage.ps1') -ExecutablePath (Join-Path $bundleDirectory 'STALKER-GAMMA-COMMANDER.exe')
    }
    $version = (& $python -c 'from commander_gui import __version__; print(__version__)').Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid application version.' }
    $portableZip = Join-Path $OutputDirectory "STALKER-GAMMA-COMMANDER-$version-windows-x64-portable.zip"
    & $python $metadataTool archive --generated $generated --bundle $bundleDirectory --output $portableZip
    if ($LASTEXITCODE -ne 0) { throw 'Portable archive creation failed.' }
    if (-not $SkipSmokeTest) {
        & (Join-Path $PSScriptRoot 'Test-WindowsPackage.ps1') -PortableArchive $portableZip
    }
    $artifacts = @($portableZip)
    if ($Installer) {
        & $InnoCompilerPath "/DAppVersion=$version" "/DBundleDir=$bundleDirectory" "/DOutputDir=$OutputDirectory" (Join-Path $repoRoot 'packaging/windows/installer.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Inno Setup build failed.' }
        $artifacts += Join-Path $OutputDirectory "STALKER-GAMMA-COMMANDER-$version-windows-x64-setup.exe"
    }
    $checksums = foreach ($artifact in $artifacts) {
        $digest = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
        '{0}  {1}' -f $digest, [IO.Path]::GetFileName($artifact)
    }
    $checksums | Set-Content -LiteralPath (Join-Path $OutputDirectory "STALKER-GAMMA-COMMANDER-$version-windows-x64.sha256") -Encoding ascii
    $artifacts | ForEach-Object { Write-Host "Built: $_" }
} finally {
    Pop-Location
}
