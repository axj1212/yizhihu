@echo off
setlocal
title Yizhihu One-Click Launcher
REM ============================================================
REM  医智呼 · AI 随访外呼系统 —— 一键启动脚本
REM
REM  【使用方法】
REM   1. 直接双击本文件（开始.bat），不用先打开命令行，也不用管理员权限。
REM   2. 脚本会自行 CD 到项目目录 D:\Ai\app，无需手动切换。
REM   3. 从 8080 起自动检测端口；若被占用，自动顺延到 8081、8082 ...
REM      并把实际端口传给后端，浏览器地址也跟着变，不会打不开。
REM   4. 同时启动后端与前端：本项目的页面在 app\static 里，由 FastAPI
REM      统一托管，一个服务进程即同时提供 /api/* 接口和前端页面，
REM      因此不需要、也不应该拆成两个互相跨域的进程。
REM   5. 只有「后端 /api/health」和「前端首页 /」都返回成功后，
REM      才会自动打开默认浏览器访问系统。
REM   6. 运行期间请保留这个黑色窗口；关闭窗口（或 Ctrl+C）即停止服务。
REM
REM  【常见问题】
REM   - 端口被占用：不用管，脚本自动换端口并在窗口里打印实际地址。
REM   - 缺少 Python 依赖：脚本自动 pip 安装 requirements.txt（清华镜像）。
REM   - 想换起始端口：先执行 set YZH_BASE_PORT=9000，再双击本文件。
REM   - 仅调试、不想弹浏览器：set YZH_NO_BROWSER=1
REM ============================================================

REM ---- 自行切换到目标文件夹 ----
set "TARGET=D:\Ai\app"
if not exist "%TARGET%\run.py" set "TARGET=%~dp0"
cd /d "%TARGET%"
if not exist "run.py" (
  echo [ERROR] Cannot find run.py in %CD%
  echo Please check the project folder: D:\Ai\app
  pause
  exit /b 1
)

REM ---- 选择 Python：项目 venv > 已装依赖的 venv > PATH ----
set "PY=python"
if exist "D:\Ai\medical-followup-windows-offline-2026-10-25\.venv\Scripts\python.exe" set "PY=D:\Ai\medical-followup-windows-offline-2026-10-25\.venv\Scripts\python.exe"
if exist "%TARGET%\.venv\Scripts\python.exe" set "PY=%TARGET%\.venv\Scripts\python.exe"

echo.
echo   Project : %CD%
echo   Python  : %PY%
echo   Browser : opens automatically when BOTH backend and frontend are ready
echo.

"%PY%" _start_helper.py
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
  echo [ERROR] Launcher exited with code %RC%. See messages above.
) else (
  echo Service stopped normally.
)
echo.
pause
