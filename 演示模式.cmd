@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "main.py" (
    if exist ".\.venv\Scripts\python.exe" (
        .\.venv\Scripts\python.exe main.py --demo
    ) else (
        python main.py --demo
    )
) else if exist "WeChatImageSender.exe" (
    start "" "WeChatImageSender.exe" --demo
) else if exist "releases\3.6.2\WeChatImageSender\WeChatImageSender.exe" (
    start "" "releases\3.6.2\WeChatImageSender\WeChatImageSender.exe" --demo
) else (
    echo 未找到程序入口，请确认已完整解压项目或程序包。
    pause
)
