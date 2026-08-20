"""Start the Pakisa Stoping Analysis web dashboard."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.app import create_app

if __name__ == "__main__":
    app = create_app()
    print("Dashboard running at http://localhost:5002")
    app.run(debug=True, port=5002)
