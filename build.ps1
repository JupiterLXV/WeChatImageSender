$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm --onedir --windowed --name WeChatImageSender --collect-all uiautomation --collect-submodules comtypes.gen main.py
if ($LASTEXITCODE -ne 0) { throw '打包失败' }
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $PSScriptRoot 'dist\WeChatImageSender\使用说明.md')
