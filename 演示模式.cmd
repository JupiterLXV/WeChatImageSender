@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "dist\WeChatImageSender\WeChatImageSender.exe" (
    start "" "dist\WeChatImageSender\WeChatImageSender.exe" --demo
    exit /b
)
start "" ".venv\Scripts\pythonw.exe" "main.py" --demo
