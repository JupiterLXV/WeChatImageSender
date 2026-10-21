@echo off
chcp 65001 >nul
if exist ".\.venv\Scripts\pythonw.exe" (
    start "" ".\.venv\Scripts\pythonw.exe" main.py
) else (
    python main.py
)
