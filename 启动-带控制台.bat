@echo off
rem 调试用：带控制台窗口启动，能看到报错信息。
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo 还没装依赖，先双击「一键安装依赖.bat」。
    pause
    exit /b 1
)

if not exist "config.yaml" copy /Y "config.example.yaml" "config.yaml" >nul
".venv\Scripts\python.exe" app_window.py
pause
