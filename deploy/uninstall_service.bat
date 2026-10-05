@echo off
rem Stop and remove the Pakisa dashboard Windows service.
setlocal
cd /d "%~dp0.."

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: run this from an ELEVATED Command Prompt.
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python run_service.py stop >nul 2>&1
python run_service.py remove
echo Service removed.
pause
