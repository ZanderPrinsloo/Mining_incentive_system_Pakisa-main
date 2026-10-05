"""Production entry point for the Pakisa Stoping Analysis dashboard.

Serves the Flask app with Waitress on all network interfaces so colleagues
on the intranet can open http://<server-hostname>:5002 in a browser.

For local development use `python run_web.py` (Flask debug server) instead.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from waitress import serve

from web.app import create_app

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "5002"))
THREADS = int(os.getenv("WEB_THREADS", "8"))

if __name__ == "__main__":
    app = create_app()
    print(f"Dashboard serving on http://{HOST}:{PORT}")
    serve(app, host=HOST, port=PORT, threads=THREADS)
