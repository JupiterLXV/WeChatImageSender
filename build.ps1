$ErrorActionPreference = "Stop"

Write-Host "=== Building WeChatImageSender v3.0.0 ===" -ForegroundColor Cyan

$PythonExe = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }

Write-Host "[1/4] Running unit tests..." -ForegroundColor Yellow
& $PythonExe -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw "Unit tests failed."
}

Write-Host "[2/4] Cleaning old build directories..." -ForegroundColor Yellow
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

Write-Host "[3/4] Running PyInstaller..." -ForegroundColor Yellow
& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name "WeChatImageSender" `
    --add-data "README.md;." `
    main.py

Get-ChildItem -Path "." -Filter "*.md" | ForEach-Object {
    Copy-Item $_.FullName -Destination "dist\WeChatImageSender\" -Force
}
Get-ChildItem -Path "." -Filter "*.cmd" | ForEach-Object {
    Copy-Item $_.FullName -Destination "dist\WeChatImageSender\" -Force
}

Write-Host "[4/4] Creating dist\WeChatImageSender-Windows-x64.zip ..." -ForegroundColor Yellow
Compress-Archive -Path "dist\WeChatImageSender\*" -DestinationPath "dist\WeChatImageSender-Windows-x64.zip" -Force

Write-Host "=== Build Complete: dist\WeChatImageSender-Windows-x64.zip ===" -ForegroundColor Green
