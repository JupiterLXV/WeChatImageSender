@echo off
chcp 65001 >nul
echo 正在创建虚拟环境并安装依赖...
py -3 -m venv .venv
if errorlevel 1 (
    python -m venv .venv
)
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
echo 依赖安装完成！请双击“启动程序.cmd”运行。
pause
