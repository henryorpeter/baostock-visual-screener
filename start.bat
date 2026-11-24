@echo off
chcp 65001 >nul
title KLineRadar A股技术形态选股终端
cd /d "%~dp0"
echo ====================================================
echo  正在启动 KLineRadar A股技术形态选股终端...
echo  访问地址: http://localhost:8501
echo ====================================================
".venv\Scripts\streamlit.exe" run baostock_screener.py
pause
