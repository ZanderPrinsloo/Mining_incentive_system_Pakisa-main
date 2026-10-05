@echo off
rem Stop a foreground dashboard started with deploy\start.bat.
taskkill /f /im python.exe >nul 2>&1
taskkill /f /im pythonw.exe >nul 2>&1
echo Stopped (if it was running).
