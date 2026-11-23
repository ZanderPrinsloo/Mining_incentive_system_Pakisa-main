@echo off
rem Start the iCalc Phakisa dashboard Windows service.
setlocal
cd /d "%~dp0.."
net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: administrator rights are needed. Right-click Command Prompt, choose "Run as administrator", and run this again.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" run_service.py start
if errorlevel 1 ( echo ERROR: the service did not start - see logs\service.log and Event Viewer & pause & exit /b 1 )
echo Service 'iCalcPhakisaDashboard' started:  http://localhost:5002   /   http://%COMPUTERNAME%:5002
pause
