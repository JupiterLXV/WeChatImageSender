@echo off
chcp 65001 >nul
if exist "WeChatImageSender.exe" (
    start "" "WeChatImageSender.exe" --demo
) else if exist ".\.venv\Scripts\python.exe" (
    .\.venv\Scripts\python.exe main.py --demo
) else (
    python main.py --demo
)
