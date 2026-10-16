"""Minimal WSGI harness: exercises the same app() Vercel runs.

    python3 tools/wsgi_check.py            # buffered-body mode
    python3 tools/wsgi_check.py --stream   # wsgi.write + file_wrapper mode
"""

import io
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, make_server

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STREAM = "--stream" in sys.argv


class Buffer:
    """Fake wsgi.write / file_wrapper, so we can watch the raw framing."""

    def __init__(self):
        self.data = bytearray()

    def write(self, chunk):
        self.data += chunk
        return len(chunk)

    def flush(self):
        pass


def call(app, path, method="GET"):
    """Run one request through the WSGI app."""
    sink = Buffer() if STREAM else None
    captured = {}

    def start_response(status, headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = headers

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path.split("?")[0],
        "QUERY_STRING": path.split("?")[1] if "?" in path else "",
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "HTTP_HOST": "test",
        "REMOTE_ADDR": "127.0.0.1",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.url_scheme": "http",
    }
    if STREAM:
        environ["wsgi.write"] = sink
        environ["wsgi.file_wrapper"] = lambda f, bs=8192: _File(f, bs)
    body = b"".join(app(environ, start_response))
    if STREAM and not body:
        body = bytes(sink.data)
    return captured["status"], dict(captured["headers"]), body


class _File:
    """Stand-in for wsgi.file_wrapper: the body already went to wsgi.write."""

    def __init__(self, f, block_size):
        self.f = f
        self.block_size = block_size

    def __iter__(self):
        return iter((b"",))

    def write(self, data):
        return self.f.write(data)


def main():
    from server import app

    checks = [
        ("GET", "/api/health", 200),
        ("GET", "/", 200),
        ("HEAD", "/", 200),
        ("GET", "/nope", 404),
        ("GET", "/api/bogus", 404),
        ("POST", "/api/health", 405),
        ("OPTIONS", "/api/info", 204),
        ("GET", "/api/info", 400),          # missing ?url=
        ("GET", "/api/play?url=ftp://x", 400),
    ]
    ok = True
    for method, path, want in checks:
        status, headers, body = call(app, path, method)
        got = int(status.split()[0])
        flag = "ok " if got == want else "FAIL"
        ok &= got == want
        ctype = headers.get("Content-Type", "-")
        print(f"{flag} {method:7} {path:28} -> {status:24} {ctype:24} {len(body)}B")
    print("\nWSGI harness:", "ALL PASS" if ok else "FAILURES")
    print("mode:", "streaming (wsgi.write + file_wrapper)" if STREAM else "buffered")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())