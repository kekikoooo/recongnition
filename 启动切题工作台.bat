@echo off
chcp 65001 >nul
title StudyHelp 通用智能交互题库工作台
color 0B

echo ============================================================
echo   StudyHelp 通用交互题库工作台 (Universal StudyHelp)
echo   自适应试卷与教材 · 无固定章节预设 · 动态多学科解题
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/3] 正在释放 8089 端口(杀掉旧进程)...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8089 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
ping 127.0.0.1 -n 2 >nul

set "PY_CMD=python"
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
) else if exist "K:\AI\github\deeptutor2\.python312\python.exe" (
    set "PY_CMD=K:\AI\github\deeptutor2\.python312\python.exe"
)

rem ===== 本地代理网络守护 =====
set "HTTP_PROXY=http://127.0.0.1:7890"
set "HTTPS_PROXY=http://127.0.0.1:7890"
set "NO_PROXY=localhost,127.0.0.1"

echo [2/3] 正在启动纯净 RESTful 工作台服务 (端口 8089)...
start "" "%PY_CMD%" main.py

echo [3/3] 正在等待工作台就绪并拉起浏览器...
powershell -NoProfile -Command "$ok=$false; for($i=0; $i -lt 30; $i++){ try{ $tcp = New-Object System.Net.Sockets.TcpClient; $tcp.Connect('127.0.0.1', 8089); $tcp.Close(); $ok=$true; break }catch{ Start-Sleep -Milliseconds 300 } }; if(-not $ok){ exit 1 }"

if %errorlevel% neq 0 (
    echo.
    echo [!] 警告: 服务端口响应超时，请排查问题。
    pause
    exit /b 1
)


exit
