@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo 安装完成，请双击“启动程序.cmd”。
pause
exit /b 0
:fail
echo 安装失败。请确认已安装 Python 3.10 或更新版本，且网络可以连接 PyPI。
pause
exit /b 1
