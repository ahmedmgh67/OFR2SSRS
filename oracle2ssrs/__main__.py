"""Command-line entry points.

    oracle2ssrs [--host HOST] [--port PORT] [--threads N]
        Serve the web app with waitress (default http://127.0.0.1:5057).

    oracle2ssrs-convert <input> [--out OUTDIR] [--strict] [--quiet]
        Batch-convert Oracle Reports XML files without the web UI.
"""
from __future__ import annotations

import argparse
import os
from typing import List, Optional

from . import _use_backend


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oracle2ssrs", description="Serve the Oracle -> SSRS converter web app.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (0.0.0.0 for other machines)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5057")))
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args(argv)

    _use_backend()
    from waitress import serve
    from app import app  # backend/app.py; loads .env from the current folder

    print(f"Oracle -> SSRS Converter   http://{args.host}:{args.port}")
    serve(app, host=args.host, port=args.port, threads=args.threads,
          channel_timeout=300)
    return 0


def convert_main(argv: Optional[List[str]] = None) -> int:
    _use_backend()
    import cli  # backend/cli.py
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
