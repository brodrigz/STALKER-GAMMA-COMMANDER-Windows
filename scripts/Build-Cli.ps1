param(
    [string]$CliSource,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$manifest = Get-Content -LiteralPath (Join-Path $repoRoot 'cli/windows-backend.json') -Raw | ConvertFrom-Json
$backendDir = Join-Path $repoRoot 'cli/windows'
$stampPath = Join-Path $backendDir 'commander-cli-build.json'
$sourceArchive = Join-Path $repoRoot 'cli/windows-source.zip'
$required = @($manifest.executable, $manifest.archiver, 'resources/7z.dll', 'resources/cloudscraper.exe', 'libcurl-impersonate.dll', 'git2-3f4182d.dll', 'cacert.pem', 'CLI-LICENSE.txt')
# A release build must never silently replace our executable with the upstream one.
if (-not $Force -and (Test-Path -LiteralPath $stampPath) -and (Test-Path -LiteralPath $sourceArchive)) {
    $stamp = Get-Content -LiteralPath $stampPath -Raw | ConvertFrom-Json
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $backendDir $_)) })
    if ($missing.Count -eq 0 -and $stamp.source_revision -eq $manifest.source_revision -and
        $stamp.version -eq $manifest.version -and
        $stamp.executable_sha256 -eq (Get-FileHash -LiteralPath (Join-Path $backendDir $manifest.executable) -Algorithm SHA256).Hash.ToLowerInvariant()) {
        Write-Host "Pinned Commander CLI $($manifest.version) is already built."
        return
    }
}

if (-not $CliSource) {
    $sibling = Join-Path (Split-Path -Parent $repoRoot) 'stalker-gamma-cli'
    if (Test-Path -LiteralPath (Join-Path $sibling '.git')) {
        $siblingRevision = & git -c "safe.directory=$($sibling.Replace('\','/'))" -C $sibling rev-parse HEAD
        if ($LASTEXITCODE -eq 0 -and $siblingRevision -eq $manifest.source_revision) { $CliSource = $sibling }
    }
}
if (-not $CliSource) {
    $CliSource = Join-Path $repoRoot "build/cli-source/$($manifest.source_revision)"
    if (-not (Test-Path -LiteralPath (Join-Path $CliSource '.git'))) {
        & git clone --no-checkout $manifest.source_repository $CliSource
        if ($LASTEXITCODE -ne 0) { throw 'Could not clone the Commander CLI source.' }
        & git -C $CliSource checkout --detach $manifest.source_revision
        if ($LASTEXITCODE -ne 0) { throw 'Could not check out the pinned Commander CLI revision.' }
    }
}
$CliSource = (Resolve-Path -LiteralPath $CliSource).Path
$gitSafety = "safe.directory=$($CliSource.Replace('\','/'))"
$revision = & git -c $gitSafety -C $CliSource rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $revision -ne $manifest.source_revision) {
    throw "CLI source must be at pinned revision $($manifest.source_revision). Update cli/windows-backend.json when advancing the fork."
}
$changes = & git -c $gitSafety -C $CliSource status --porcelain --untracked-files=normal
if ($LASTEXITCODE -ne 0 -or $changes) { throw 'Commit CLI source changes before building a pinned backend.' }

$dotnet = Join-Path $repoRoot 'build/tools/dotnet/dotnet.exe'
if (-not (Test-Path -LiteralPath $dotnet)) { $dotnet = (Get-Command dotnet -ErrorAction Stop).Source }
$downloadDir = Join-Path $repoRoot 'build/downloads'
New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
$dependency = Get-Content -LiteralPath (Join-Path $CliSource 'build/win/dependencies.json') -Raw | ConvertFrom-Json
$archive = Join-Path $downloadDir 'stalker-gamma-1.35.0-win-x64.zip'
if (-not (Test-Path -LiteralPath $archive) -or
    (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $dependency.sha256) {
    Invoke-WebRequest -Uri $dependency.url -OutFile $archive
}
& (Join-Path $CliSource 'build/win/Build-CommanderCli.ps1') -OutputDirectory $backendDir -DependenciesArchive $archive -DotnetPath $dotnet -Version $manifest.version
& git -c $gitSafety -C $CliSource archive --format=zip --output=$sourceArchive HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not archive the CLI sources.' }
foreach ($file in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $backendDir $file))) { throw "The built CLI is incomplete: $file" }
}
