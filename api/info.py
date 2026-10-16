"""Vercel serverless function for `GET /api/info`.

Both entry points are exported on purpose:
  * `app`     – WSGI callable, what modern Vercel Python runtimes look for;
  * `handler` – a `BaseHTTPRequestHandler` subclass, for the older runtime and
                for anything that imports this module directly.

Both run exactly the same code path as `python3 server.py`.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Serverless bundles are read-only: keep the cache in the writable temp dir.
os.environ.setdefault("VIEWER_CACHE_DIR", os.environ.get("TMPDIR", "/tmp") + "/viewer-cache")

from server import app, ensure_dirs, handler  # noqa: E402,F401

# Warm the on-disk cache dir: a cold serverless container still finds the
# cache the previous invocation left behind in /tmp.
ensure_dirs()
