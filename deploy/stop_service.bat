@echo off
rem Stop the iCalc Phakisa dashboard Windows service.
setlocal
cd /d "%~dp0.."
net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: administrator rights are needed. Right-click Command Prompt, choose "Run as administrator", and run this again.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run_service.py stop
echo Service 'iCalcPhakisaDashboard' stopped.
pause
