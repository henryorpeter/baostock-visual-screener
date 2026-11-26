@echo off
chcp 65001 >nul
title KLineRadar A股选股与看盘终端 (Port 8506)
cd /d "%~dp0"
set "PORT=8506"
set "PY_EXE=.venv\Scripts\python.exe"

rem 1. 自动准备本地 .venv（首次克隆免手动建环境）
if not exist "%PY_EXE%" (
    echo [首次运行] 未检测到 .venv，正在自动创建 Python 虚拟环境...
    py -3 -V >nul 2>&1
    if errorlevel 1 (
        python -V >nul 2>&1
        if errorlevel 1 (
            echo [错误] 未检测到 Python 3，请先安装 Python 3.10+ 并勾选 Add to PATH：https://www.python.org/downloads/
            pause
            exit /b 1
        )
        python -m venv .venv
    ) else (
        py -3 -m venv .venv
    )
    if not exist "%PY_EXE%" (
        echo [错误] 创建 .venv 失败，请检查当前目录读写权限后重试。
        pause
        exit /b 1
    )
)

rem 2. 检查核心依赖（兼容全新安装与旧版升级），缺失则自动安装并支持国内镜像回退
"%PY_EXE%" -c "import starlette, uvicorn, numpy, pandas, requests" >nul 2>&1
if errorlevel 1 (
    echo [依赖安装] 正在安装/更新 Web 终端核心依赖...
    "%PY_EXE%" -m pip install -q -r requirements.txt
    if errorlevel 1 (
        echo [依赖安装] 默认源较慢或不可达，正在切换国内镜像重试...
        "%PY_EXE%" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        if errorlevel 1 (
            echo [错误] 依赖安装失败，请检查网络连接后重试。
            pause
            exit /b 1
        )
    )
)

rem 3. 自动清理 8506 端口上的旧进程
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" taskkill /F /PID %%a >nul 2>&1
)

echo ====================================================
echo  正在启动 KLineRadar A股选股与看盘终端...
echo  本机访问: http://localhost:%PORT%
echo ====================================================
"%PY_EXE%" web_server.py --port %PORT% --open
pause