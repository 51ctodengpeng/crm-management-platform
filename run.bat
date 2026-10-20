@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   客户管理系统 - 启动（端口 8081）
echo ============================================
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 未找到虚拟环境，请先执行：
  echo   python -m venv .venv
  echo   .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
echo 正在启动 http://127.0.0.1:8081 （首次运行会自动建表和创建管理员）...
.venv\Scripts\python.exe desktop.py --no-browser
pause
