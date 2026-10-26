@echo off
rem 第一次使用：双击这个装依赖（需要电脑上已有 Python 3.12）
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo 没找到 Python 启动器。请先去 python.org 装 Python 3.12，
    echo 安装时勾选 "Add python.exe to PATH"，装完重开这个窗口。
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo 正在创建虚拟环境...
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo 创建失败：多半是没装 Python 3.12。装好再试。
        pause
        exit /b 1
    )
)

echo 正在安装依赖（第一次比较慢，几分钟，走清华镜像）...
".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo 安装失败。检查网络，或把 -i 后面的镜像地址换成 https://mirrors.aliyun.com/pypi/simple
    pause
    exit /b 1
)

if not exist "config.yaml" copy /Y "config.example.yaml" "config.yaml" >nul
if not exist "data" mkdir "data"
if not exist "logs" mkdir "logs"

echo.
echo   装好了。下一步：双击「肥鱼饭堂.bat」。
echo.
pause
