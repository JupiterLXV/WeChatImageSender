@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "WeChatImageSender.exe" (
    start "" "WeChatImageSender.exe"
) else if exist "releases\3.6.0\WeChatImageSender\WeChatImageSender.exe" (
    start "" "releases\3.6.0\WeChatImageSender\WeChatImageSender.exe"
) else if exist ".\.venv\Scripts\pythonw.exe" (
    start "" ".\.venv\Scripts\pythonw.exe" main.py
) else (
    python main.py
)
