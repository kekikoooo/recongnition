@echo off
chcp 65001 >nul
title StudyHelp 题库系统 - 环境一键自举与依赖安装
color 0A

echo ============================================================
echo      StudyHelp 书籍题库自动化工作台 - 环境初始化
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/4] 正在探测本地 Python 环境...
set "PY_CMD="

rem 优先检测本地虚拟环境
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
    echo [*] 发现已存在本地虚拟环境 .venv
    goto check_python
)

rem 探测系统全局 python
python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    echo [*] 发现系统全局 Python
    goto create_venv
)

rem 探测常见特定目录 Python
if exist "K:\AI\github\deeptutor2\.python312\python.exe" (
    set "PY_CMD=K:\AI\github\deeptutor2\.python312\python.exe"
    echo [*] 发现特定 Python 环境
    goto create_venv
)

echo [!] 错误: 未在系统中找到 Python 3.10+，请先安装 Python 并添加至 PATH 环境变量。
pause
exit /b 1

:create_venv
echo.
echo [2/4] 正在创建专属独立虚拟环境 (.venv)...
if not exist ".venv" (
    "%PY_CMD%" -m venv .venv
    if %errorlevel% neq 0 (
        echo [!] 虚拟环境创建失败，将尝试直接使用系统环境。
    ) else (
        echo [OK] 专属虚拟环境 .venv 创建成功！
    )
)
if exist ".venv\Scripts\python.exe" (
    set "PY_CMD=.venv\Scripts\python.exe"
)

:check_python
echo.
echo [3/4] 正在升级 pip 并安装核心依赖包...
"%PY_CMD%" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
"%PY_CMD%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

if %errorlevel% neq 0 (
    echo.
    echo [WARN] 清华镜像源安装异常，尝试使用官方源重试...
    "%PY_CMD%" -m pip install -r requirements.txt
)

echo.
echo [4/4] 依赖自检测试...
"%PY_CMD%" -c "import fastapi, uvicorn, pymupdf, rapidocr_onnxruntime, PIL; print('[OK] 核心依赖测试全部通过！')"

if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo  🎉 环境初始化全部完成！您可以随时执行 pipeline.py 或运行工作台。
    echo ============================================================
) else (
    echo.
    echo [!] 部分依赖可能未能成功加载，请检查上方报错信息。
)

echo.
pause
