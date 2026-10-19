@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "dist\WeChatImageSender\WeChatImageSender.exe" (
    start "" "dist\WeChatImageSender\WeChatImageSender.exe"
    exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "main.py"
    exit /b
)
echo 请先运行“安装依赖.cmd”，或者打开已打包的 WeChatImageSender.exe。
pause
