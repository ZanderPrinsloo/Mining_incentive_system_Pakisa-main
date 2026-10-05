@echo off
rem ============================================================
rem  Pakisa dashboard - one-time server setup
rem  Run this ONCE from an ELEVATED Command Prompt on the server
rem  (Right-click cmd -> Run as administrator)
rem ============================================================
setlocal
cd /d "%~dp0.."

echo.
echo [1/4] Checking Python...
py -3 --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3 not found. Install Python 3.12+ first and re-run.
    exit /b 1
)
py -3 --version

echo.
echo [2/4] Creating virtual environment...
if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 ( echo ERROR: could not create venv & exit /b 1 )
)

echo.
echo [3/4] Installing dependencies (needs internet)...
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pywin32

echo.
echo [4/4] Opening firewall port 5002...
netsh advfirewall firewall add rule name="Pakisa Dashboard" dir=in action=allow protocol=TCP localport=5002 >nul 2>&1
if errorlevel 1 ( echo WARNING: could not add firewall rule - run as administrator ) else ( echo Firewall rule added. )

echo.
echo Checking ODBC driver...
reg query "HKLM\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server" >nul 2>&1
if errorlevel 1 (
    echo WARNING: 'ODBC Driver 17 for SQL Server' not found on this machine.
    echo The app needs it to reach the reporting database. Check config\config.yaml.
) else (
    echo ODBC Driver 17 for SQL Server found.
)

echo.
echo Setup done. Next: run deploy\install_service.bat to install the service,
echo or run .venv\Scripts\python.exe run_server.py to test in the foreground.
pause
