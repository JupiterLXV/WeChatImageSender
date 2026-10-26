[CmdletBinding()]
param(
    [switch]$SkipTests,
    [ValidatePattern('^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$')]
    [string]$Version = '3.6.3',
    [string]$PythonExe
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
if (-not $PythonExe) {
    $LocalPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $PythonExe = if (Test-Path -LiteralPath $LocalPython) { $LocalPython } else { 'python' }
}

Push-Location -LiteralPath $ProjectRoot
try {
    Write-Host "Building WeChatImageSender $Version" -ForegroundColor Cyan

    # Reserve unique paths before exporting, so standalone sources also preserve older builds.
    $RunStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $RunId = "$Version-$RunStamp-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $WorkPath = Join-Path $ProjectRoot "build\$RunId"
    $DistPath = Join-Path $ProjectRoot "dist\$RunId"

    $Exporter = Join-Path $ProjectRoot 'scripts\export_single_file.py'
    if (Test-Path -LiteralPath $Exporter) {
        & $PythonExe $Exporter --output (Join-Path $DistPath 'standalone')
        if ($LASTEXITCODE -ne 0) { throw 'Single-file source export failed.' }
    }

    if (-not $SkipTests) {
        Write-Host '[1/4] Running unit tests...' -ForegroundColor Cyan
        & $PythonExe -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed. No release was created.' }
    } else {
        Write-Host '[1/4] Unit tests skipped by explicit -SkipTests option.' -ForegroundColor Yellow
    }

    # Every run has its own output paths. Existing releases and build data remain available.
    $ReleaseRoot = Join-Path $ProjectRoot 'releases'
    $ReleasePath = Join-Path $ReleaseRoot $Version
    if (Test-Path -LiteralPath $ReleasePath) {
        $ReleasePath = Join-Path $ReleaseRoot $RunId
    }
    $SpecPath = Join-Path $ProjectRoot 'WeChatImageSender.spec'

    if (-not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) {
        throw "Missing pinned build specification: $SpecPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'assets\app-icon.ico') -PathType Leaf)) {
        throw 'Missing application icon: assets\app-icon.ico'
    }

    Write-Host '[2/4] Packaging application and icon...' -ForegroundColor Cyan
    & $PythonExe -m PyInstaller --distpath $DistPath --workpath $WorkPath $SpecPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE. Existing releases were preserved."
    }
    $BuiltApp = Join-Path $DistPath 'WeChatImageSender'
    if (-not (Test-Path -LiteralPath (Join-Path $BuiltApp 'WeChatImageSender.exe') -PathType Leaf)) {
        throw 'PyInstaller returned without the expected WeChatImageSender.exe.'
    }

    Write-Host '[3/4] Preparing portable release and user guide...' -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $ReleasePath | Out-Null
    $PackagePath = Join-Path $ReleasePath 'WeChatImageSender'
    Copy-Item -LiteralPath $BuiltApp -Destination $PackagePath -Recurse
    foreach ($GuideName in @('README.md', '使用说明.md')) {
        $GuidePath = Join-Path $ProjectRoot $GuideName
        if (Test-Path -LiteralPath $GuidePath -PathType Leaf) {
            Copy-Item -LiteralPath $GuidePath -Destination $PackagePath
        }
    }
    $Launcher = @'
@echo off
cd /d "%~dp0"
start "" /D "%~dp0" "%~dp0WeChatImageSender.exe" %*
'@
    [System.IO.File]::WriteAllText(
        (Join-Path $PackagePath 'Start-WeChatImageSender.cmd'),
        ($Launcher -replace "`r?`n", "`r`n") + "`r`n",
        [System.Text.Encoding]::ASCII
    )

    Write-Host '[4/4] Creating ZIP and SHA256 checksum...' -ForegroundColor Cyan
    $ZipName = "WeChatImageSender-$Version-Windows-x64.zip"
    $ZipPath = Join-Path $ReleasePath $ZipName
    Compress-Archive -LiteralPath $PackagePath -DestinationPath $ZipPath -CompressionLevel Optimal
    $Digest = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [System.IO.File]::WriteAllText(
        "$ZipPath.sha256",
        "$Digest  $ZipName`r`n",
        [System.Text.Encoding]::ASCII
    )

    Write-Host "Build complete: $ZipPath" -ForegroundColor Cyan
    Write-Host "SHA256: $Digest"
} finally {
    Pop-Location
}
