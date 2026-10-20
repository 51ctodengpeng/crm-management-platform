@echo off
rem ============================================================
rem  One-click build: package the CRM system as a Windows EXE
rem  Double-click this file. Output: dist\CRMSystem.exe
rem ============================================================
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Virtual environment not found, creating one...
    python -m venv .venv || goto :error
)

echo [1/2] Installing build dependencies (first run may take a few minutes)...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip || goto :error
".venv\Scripts\python.exe" -m pip install -q -r requirements-build.txt || goto :error

echo [2/2] Building EXE...
".venv\Scripts\python.exe" build_exe.py || goto :error

echo.
echo All done. Output: dist\CRMSystem.exe
pause
exit /b 0

:error
echo.
echo Build FAILED. Please read the messages above.
pause
exit /b 1
