@echo off
chcp 65001 >nul
title KLineRadar A股选股与看盘终端 (Port 8506)
cd /d "%~dp0"
set PORT=8506
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%PORT% " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" taskkill /F /PID %%a >nul 2>&1
)
echo ====================================================
echo  正在启动 KLineRadar A股选股与看盘终端...
echo  本机访问: http://localhost:%PORT%
echo ====================================================
".venv\Scripts\python.exe" web_server.py --port %PORT% --open
pause
