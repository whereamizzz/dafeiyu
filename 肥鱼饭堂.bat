@echo off
rem ============================================================
rem  Da Fei Yu - launcher (double click this one)
rem  Paths are all relative to this file, so the folder can be moved.
rem ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "config.yaml" if exist "config.example.yaml" (
    copy /Y "config.example.yaml" "config.yaml" >nul
    echo 已生成 config.yaml（配置模板），记得在控制台里填 DeepSeek API Key。
)

if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "app_window.py"
    exit /b 0
)
if exist ".venv\Scripts\python.exe" (
    start "" ".venv\Scripts\python.exe" "app_window.py"
    exit /b 0
)

echo.
echo   还没装依赖。先双击「一键安装依赖.bat」，装完再回来点这个。
echo.
pause
exit /b 1
