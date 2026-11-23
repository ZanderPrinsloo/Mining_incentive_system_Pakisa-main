"""Run the Phakisa dashboard as a Windows service (pywin32 + Waitress).

The service is called "iCalcPhakisaDashboard" (display name "iCalc Phakisa Stoping Analysis
Dashboard"), so it is easy to find in services.msc by searching for "iCalc".

Usage (from an ELEVATED Command Prompt, project root) — or use the deploy\\*.bat files:
    .venv\\Scripts\\python.exe run_service.py install      (or: --username DOMAIN\\user --password *** install)
    .venv\\Scripts\\python.exe run_service.py start | stop | restart | remove
    .venv\\Scripts\\python.exe run_service.py debug        (run in this window, for testing)

Why it is built this way (avoids error 1053 "the service did not respond in a timely fashion"):
  * Windows starts the REAL python.exe (not the .venv launcher, which hands over to a second
    process that Windows does not see) with this script; the script adds the .venv packages itself.
  * The service reports "running" straight away; the dashboard (database connection, cache
    warm-up) is loaded afterwards, so a slow database can never make the start time out.
  * Everything is logged to logs\\service.log.
"""
import ctypes
import logging
import os
import site
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_SITE = ROOT / ".venv" / "Lib" / "site-packages"
if VENV_SITE.is_dir() and str(VENV_SITE) not in sys.path:
    site.addsitedir(str(VENV_SITE))          # also runs pywin32's .pth (adds its DLL folder)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import servicemanager  # noqa: E402
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402

SERVICE_NAME = "iCalcPhakisaDashboard"
SERVICE_DISPLAY = "iCalc Phakisa Stoping Analysis Dashboard"

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(filename=ROOT / "logs" / "service.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("service")


def _real_python():
    """The installed python.exe behind the .venv (the .venv one is only a launcher)."""
    return getattr(sys, "_base_executable", None) or sys.executable


class DashboardService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = "Serves the Phakisa mining incentive dashboard (Waitress, port 5002, database STPTM2000)."
    _exe_name_ = _real_python()
    _exe_args_ = f'"{Path(__file__).resolve()}"'

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        log.info("stop requested")
        if self.server is not None:
            try:
                self.server.close()
            except Exception:  # noqa: BLE001
                log.exception("closing the web server")
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        # pywin32 has already reported SERVICE_RUNNING when it gets here; load the dashboard in the background.
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))
        threading.Thread(target=self._serve, daemon=True).start()
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        log.info("service stopped")

    def _serve(self):
        try:
            from waitress import create_server
            from web.app import create_app
            host = os.getenv("HOST", "0.0.0.0")
            port = int(os.getenv("PORT", "5002"))
            threads = int(os.getenv("WEB_THREADS", "8"))
            app = create_app()
            self.server = create_server(app, host=host, port=port, threads=threads)
            log.info("serving on http://%s:%s", host, port)
            self.server.run()
        except Exception:  # noqa: BLE001
            log.exception("the dashboard could not start")
            servicemanager.LogErrorMsg(f"{SERVICE_NAME}: the dashboard could not start - see logs\\service.log")
            win32event.SetEvent(self.stop_event)


def _is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Started by Windows (Service Control Manager).
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(DashboardService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        needs_admin = {"install", "update", "remove", "start", "stop", "restart"}
        if needs_admin & {a.lower() for a in sys.argv[1:]} and not _is_admin():
            sys.exit("ERROR: administrator rights are needed. Open Command Prompt with 'Run as administrator' and try again.")
        win32serviceutil.HandleCommandLine(DashboardService)
