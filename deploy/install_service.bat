@echo off
rem ============================================================
rem  Install the Pakisa dashboard as a Windows service
rem  Run from an ELEVATED Command Prompt.
rem
rem  Default: runs as LocalSystem (works if the app uses the
rem  SQL login in .env). To run as a specific account instead:
rem      install_service.bat DOMAIN\user password
rem ============================================================
setlocal
cd /d "%~dp0.."

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: run this from an ELEVATED Command Prompt.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: no virtual environment found. Run deploy\setup.bat first.
    exit /b 1
)

call ".venv\Scripts\activate.bat"

if "%~1"=="" (
    python run_service.py install
) else (
    python run_service.py --username %~1 --password %~2 install
)
if errorlevel 1 ( echo ERROR: service install failed & exit /b 1 )

python run_service.py start
if errorlevel 1 ( echo ERROR: service did not start - check Event Viewer & exit /b 1 )

echo.
echo Service 'PakisaDashboard' installed and STARTED.
echo Test on the server:  http://localhost:5002
echo From other PCs:      http://%COMPUTERNAME%:5002
pause
