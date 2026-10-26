@echo off
rem 无窗口模式：只起后端，再用浏览器访问 http://127.0.0.1:8010
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
".venv\Scripts\python.exe" run.py >> "logs\console.log" 2>&1
