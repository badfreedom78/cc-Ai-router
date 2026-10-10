@echo off
chcp 936 >nul 2>&1
REM ============================================================
REM  ENCODING RULE - DO NOT CHANGE:
REM  cmd.exe reads .bat files using the OEM codepage (CP936 here),
REM  so this file MUST stay GBK/ANSI encoded WITH CRLF line endings.
REM  Saving it as UTF-8 or LF-only silently corrupts the script
REM  (byte misalignment eats real characters, e.g. 'echo.' -> 'o.').
REM ============================================================
REM ============================================================
REM  AI Router - 第一次运行（一键安装并启动）
REM
REM  双击本文件即可自动完成：
REM    [1] 找到可用的 Python 3.10+
REM    [2] 在 gui\.venv 创建独立虚拟环境
REM    [3] 安装 litellm / fastapi 等依赖（需联网，首次较慢）
REM    [4] 由 config.example.yaml 生成 config.yaml
REM    [5] 打开可视化控制台
REM
REM  之后的日常启动请双击「启动 AI Router.vbs」（无命令行窗口）。
REM ============================================================

setlocal EnableExtensions
cd /d "%~dp0"

set "VENV=%~dp0gui\.venv"
set "PYEXE=%VENV%\Scripts\python.exe"
set "PYWEXE=%VENV%\Scripts\pythonw.exe"
set "MARKER=%VENV%\.deps-ok"
set "REQ=%~dp0gui\requirements.txt"
set "CFG=%~dp0config.yaml"
set "CFGEX=%~dp0config.example.yaml"

title AI Router 控制台

echo.
echo ============================================================
echo  AI Router - 首次运行准备
echo ============================================================
echo.

REM ---------- 1) 找一个可用的 Python ----------

set "BASEPY="

where py >nul 2>nul
if %errorlevel%==0 (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "BASEPY=%%i"
)

if not defined BASEPY (
    where python >nul 2>nul
    if %errorlevel%==0 (
        for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "BASEPY=%%i"
    )
)

if not defined BASEPY if exist "C:\Python312\python.exe" set "BASEPY=C:\Python312\python.exe"
if not defined BASEPY if exist "C:\Python311\python.exe" set "BASEPY=C:\Python311\python.exe"
if not defined BASEPY if exist "C:\Python310\python.exe" set "BASEPY=C:\Python310\python.exe"

if not defined BASEPY (
    echo [X] 找不到 Python 3.10 或更高版本。
    echo     请先安装 Python：https://www.python.org/downloads/
    echo     安装时务必勾选 "Add Python to PATH"。
    echo.
    pause
    exit /b 1
)

echo [1/4] 使用的解释器：%BASEPY%

REM ---------- 2) 创建虚拟环境 ----------

if not exist "%PYEXE%" (
    echo [2/4] 创建虚拟环境 gui\.venv ...
    "%BASEPY%" -m venv "%VENV%"
    if errorlevel 1 (
        echo [X] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [2/4] 复用已存在的虚拟环境
)

REM ---------- 3) 安装依赖 ----------

if not exist "%MARKER%" (
    echo [3/4] 安装依赖（首次运行需联网下载，请耐心等待）...
    "%PYEXE%" -m pip install --upgrade pip --quiet
    "%PYEXE%" -m pip install -r "%REQ%"
    if errorlevel 1 (
        echo.
        echo [X] 依赖安装失败，请检查网络后重试。
        echo     可改用国内镜像：
        echo     "%PYEXE%" -m pip install -r "%REQ%" -i https://pypi.tuna.tsinghua.edu.cn/simple
        echo.
        pause
        exit /b 1
    )
    echo ok> "%MARKER%"
) else (
    echo [3/4] 依赖已就绪
)

REM ---------- 4) 生成 config.yaml ----------

if not exist "%CFG%" (
    if exist "%CFGEX%" (
        copy /y "%CFGEX%" "%CFG%" >nul
        echo [4/4] 已由 config.example.yaml 生成 config.yaml
    ) else (
        echo [!] 未找到 config.example.yaml，跳过配置生成。
    )
) else (
    echo [4/4] 已存在 config.yaml，保持不变
)

REM 若仍是占位符，提示用户去填写
if exist "%CFG%" (
    findstr /c:"sk-your-key-here" "%CFG%" >nul 2>nul
    if not errorlevel 1 (
        echo.
        echo ============================================================
        echo  [!] 重要：config.yaml 里仍是占位符，请先填写你的接口与密钥
        echo      需要修改的地方：api_base 与 api_key
        echo      文件位置：%CFG%
        echo ============================================================
        echo.
    )
)

echo.
echo ============================================================
echo  准备完成，即将打开可视化控制台 ...
echo ============================================================
echo.

REM ---------- 启动（不留命令行窗口）----------
REM start 派生独立进程后立刻返回，本批处理随即退出、控制台自动关闭；
REM pythonw.exe 本身不带控制台，所以最终只会剩下可视化窗口。

if exist "%PYWEXE%" (
    start "" "%PYWEXE%" "%~dp0gui\main.py"
) else (
    start "" "%PYEXE%" "%~dp0gui\main.py"
)

endlocal
exit /b 0
