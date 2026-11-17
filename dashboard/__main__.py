"""Start the dashboard: python -m dashboard

It serves a web page on this machine only, by default at
http://127.0.0.1:8765. The page reads the journal and the last evaluation
report. It cannot place an order, change a setting or start a run.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

import config
from dashboard.app import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only dashboard over the journal. It decides and changes nothing.")
    parser.add_argument("--host", default=config.DASHBOARD_HOST,
                        help="Address to listen on (DASHBOARD_HOST). The default serves this machine only.")
    parser.add_argument("--port", type=int, default=config.DASHBOARD_PORT, help="Port to listen on (DASHBOARD_PORT)")
    parser.add_argument("--journal", default=None, help="The journal to show (JOURNAL_PATH)")
    parser.add_argument("--evaluation-dir", default=None, help="Where evaluation reports are (EVALUATION_DIR)")
    args = parser.parse_args(argv)

    app = create_app(journal_path=args.journal, evaluation_dir=args.evaluation_dir, host=args.host)
    print(f"Dashboard at http://{args.host}:{args.port}  (read-only; stop it with Ctrl-C)", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
