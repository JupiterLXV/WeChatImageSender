@echo off
chcp 65001 >nul
echo 正在安装 PyInstaller 并打包 WeChatImageSender.exe ...
if exist ".\.venv\Scripts\python.exe" (
    .\.venv\Scripts\python.exe -m pip install pyinstaller
) else (
    python -m pip install pyinstaller
)
powershell -ExecutionPolicy Bypass -File .\build.ps1
pause
