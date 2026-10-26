@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "main.py" (
    if exist ".\.venv\Scripts\pythonw.exe" (
        start "" ".\.venv\Scripts\pythonw.exe" main.py
    ) else (
        python main.py
    )
) else if exist "WeChatImageSender.exe" (
    start "" "WeChatImageSender.exe"
) else if exist "releases\3.6.3\WeChatImageSender\WeChatImageSender.exe" (
    start "" "releases\3.6.3\WeChatImageSender\WeChatImageSender.exe"
) else (
    echo 未找到程序入口，请确认已完整解压项目或程序包。
    pause
)
