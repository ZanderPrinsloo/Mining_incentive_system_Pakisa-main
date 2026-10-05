@echo off
rem Start the dashboard in the foreground (for testing, not for the service).
cd /d "%~dp0.."
call ".venv\Scripts\activate.bat"
python run_server.py
pause
