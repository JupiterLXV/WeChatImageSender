@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "WeChatImageSender.exe" (
    start "" "WeChatImageSender.exe" --demo
) else if exist "releases\3.6.0\WeChatImageSender\WeChatImageSender.exe" (
    start "" "releases\3.6.0\WeChatImageSender\WeChatImageSender.exe" --demo
) else if exist ".\.venv\Scripts\python.exe" (
    .\.venv\Scripts\python.exe main.py --demo
) else (
    python main.py --demo
)
