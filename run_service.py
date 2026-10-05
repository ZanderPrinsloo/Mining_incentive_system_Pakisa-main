"""Run the Pakisa dashboard as a Windows service (pywin32).

Usage (from an elevated Command Prompt, project root):
    .venv\\Scripts\\python.exe run_service.py install
    .venv\\Scripts\\python.exe run_service.py start
    .venv\\Scripts\\python.exe run_service.py stop
    .venv\\Scripts\\python.exe run_service.py remove

Optionally run as a specific account (instead of LocalSystem):
    .venv\\Scripts\\python.exe run_service.py --username DOMAIN\\user --password ***** install
"""
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import servicemanager
import win32event
import win32service
import win32serviceutil
from waitress import create_server

from web.app import create_app

SERVICE_NAME = "PakisaDashboard"
SERVICE_DISPLAY = "Pakisa Stoping Analysis Dashboard"


class DashboardService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = (
        "Serves the Pakisa mining incentive dashboard (Waitress, port 5002)."
    )

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self.main()

    def main(self):
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "5002"))
        threads = int(os.getenv("WEB_THREADS", "8"))
        app = create_app()
        server = create_server(app, host=host, port=port, threads=threads)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        server.close()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(DashboardService)
