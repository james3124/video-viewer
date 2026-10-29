"""Backend self-test — no external network needed.

    python3 tools/backend_check.py

Runs the real server on a throwaway port and checks the parts that decide
whether a header-protected host (18+ sites, TikTok, Twitter…) actually plays:

  1. a CDN that demands Referer/Cookie is only reachable through the proxy,
     and the proxy really sends those headers;
  2. a signed URL that expires is re-extracted and the retry succeeds;
  3. HLS playlists + segments are rewritten and streamed;
  4. caching: one extraction serves many requests, ETag gives 304s;
  5. token gate, rate limit, HEAD/OPTIONS, unknown routes;
  6. SSRF guard.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'ok  ' if cond else 'FAIL'} {name}{('  — ' + extra) if extra else ''}")


# --------------------------------------------------------------------------- #
#  A fake "hostile" CDN: 403 unless the right Referer/Cookie/UA arrive
# --------------------------------------------------------------------------- #

class CDN(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits = 0

    def log_message(self, *a):
        pass

    def _guard(self):
        CDN.hits += 1
        got = {
            "referer": self.headers.get("Referer", ""),
            "cookie": self.headers.get("Cookie", ""),
            "ua": self.headers.get("User-Agent", ""),
        }
        need = {"referer": "https://www.example-site.test/watch",
                "cookie": "session=adult-ok", "ua": "ViewerUA/1.0"}
        # gen=1 is the "signature that has already expired" case
        if got != need or "gen=1" in self.path:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return got

    def do_GET(self):                        # noqa: N802
        got = self._guard()
        if got is None:
            return
        if self.path.startswith("/master.m3u8"):
            body = ("#EXTM3U\n#EXT-X-VERSION:3\n"
                    '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
                    "#EXTINF:4.0,\nseg0.ts\n#EXTINF:4.0,\nsub/seg1.ts\n"
                    "#EXT-X-ENDLIST\n").encode()
            ctype = "application/vnd.apple.mpegurl"
        elif self.path.endswith("key.bin"):
            body, ctype = b"0" * 16, "application/octet-stream"
        elif self.path.endswith(".ts"):
            body, ctype = b"\x47" + b"TS" * 100, "video/mp2t"
        else:
            body, ctype = b"FAKE-MP4-BYTES" * 512, "video/mp4"

        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            start = int(first or 0)
            stop = int(last) if last else len(body) - 1
            chunk = body[start:stop + 1]
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{stop}/{len(body)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)
            return

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):                       # noqa: N802
        self._guard()
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


HX = {"ts": b""}   # filled by the merge test with a real tiny encode


class Vault(BaseHTTPRequestHandler):
    """A deterministic Range-capable media origin for the windowed cache."""

    protocol_version = "HTTP/1.1"
    body = b""
    up = True
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):                        # noqa: N802
        self._send(True)

    def do_HEAD(self):                       # noqa: N802
        self._send(False)

    def _send(self, body: bool) -> None:
        Vault.hits += 1
        if not Vault.up:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = Vault.body
        rng = self.headers.get("Range")
        partial = False
        start, end = 0, len(data) - 1
        if rng and rng.startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            try:
                start = int(first or 0)
                end = int(last) if last else len(data) - 1
            except ValueError:
                start, end = 0, len(data) - 1
            end = min(end, len(data) - 1)
            partial = True
        chunk = data[start:end + 1]
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"vault-v1"')
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        if body:
            self.wfile.write(chunk)


class Zone(BaseHTTPRequestHandler):
    """A tame origin on 127.0.0.1, for the media redirect guard exercises."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _302(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _bytes(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        if self.path == "/file":
            self._bytes(b"ZONE01", "text/plain")
        elif self.path == "/same":
            self._302("/file")                       # same host — may follow
        elif self.path == "/off":
            self._302("http://example.com/x.mp4")    # leaves the domain
        elif self.path == "/loop":
            self._302("/loop")                       # redirect to itself, always
        elif self.path == "/chain":
            self._302("/chain?i=1")                  # fresh URL every hop
        elif self.path.startswith("/chain?i="):
            self._302(f"/chain?i={int(self.path.partition('i=')[2]) + 1}")
        elif self.path == "/hls/master.m3u8":
            self._bytes(b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                        b"#EXTINF:0.5,\nseg0.ts\n#EXTINF:0.5,\nseg1.ts\n"
                        b"#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl")
        elif self.path in ("/seg0.ts", "/seg1.ts",
                           "/hls/seg0.ts", "/hls/seg1.ts"):
            self._bytes(HX["ts"], "video/mp2t")
        elif self.path == "/fake-page":
            self._bytes(b"<html><body>not a video</body></html>", "text/html")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


class Sula(BaseHTTPRequestHandler):
    """A tame sulasokvid-shaped site: video.php's inline JS names an /e/ embed,
    and that embed's /api/stream answers a filecode with a manifest URL."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):                       # noqa: N802
        if self.path.startswith("/video.php"):
            port = self.server.server_address[1]
            body = (f"<!doctype html><html><head><title>Katkat</title></head><body>"
                    f"<script>const iframe = document.createElement('iframe');"
                    f"iframe.src = 'http://127.0.0.1:{port}/e/deadbeef01';"
                    f"</script></body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self):                      # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/api/stream" and b"deadbeef01" in body:
            port = self.server.server_address[1]
            out = json.dumps({"streaming_url":
                              f"http://127.0.0.1:{port}/hls/master.m3u8?token=t",
                              "title": ""}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        else:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()


class Hanime(BaseHTTPRequestHandler):
    """A tame hanime-shaped host: page → player bundle → csrf → handshake → HLS.

    The bundle carries a fake signer blob (wasm header plus padding), so the
    test exercises blob extraction and caching without running the real signer;
    the signature itself is stubbed by the caller.
    """
    protocol_version = "HTTP/1.1"
    slug = "tennen-koi-iro-alcohol-2"
    seen: dict = {}

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200,
               extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, val in (extra or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        import base64 as _b64                      # noqa: PLC0415
        port = self.server.server_address[1]
        if self.path == f"/videos/hentai/{self.slug}":
            body = (f'<!doctype html><html><head>'
                    f"<title>Watch Tennen Koi-iro Alcohol 2 Hentai Video - hanime.tv</title>"
                    f'<script src="http://127.0.0.1:{port}/vendor.abc123.min.js"></script>'
                    f'<script type="application/ld+json">'
                    f'{{"name": "Tennen Koi-iro Alcohol 2", "duration": "PT22M4S"}}'
                    f"</script></head><body><h1>Tennen Koi-iro Alcohol 2</h1>"
                    f"<p>not the player</p></body></html>").encode()
            return self._bytes(body, "text/html")
        if self.path.startswith("/vendor."):
            wasm = b"\x00asm\x01\x00\x00\x00" + b"\x00" * 900
            body = ('var w; function f(){ return base64Decode("'
                    + _b64.b64encode(wasm).decode() + '") }').encode()
            return self._bytes(body, "application/javascript")
        if self.path == "/csrf-token":
            return self._bytes(json.dumps({"csrf_token": "test-csrf"}).encode(),
                               "application/json")
        if self.path.startswith("/hls/"):
            return self._bytes(
                b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
                b'#EXT-X-KEY:METHOD=AES-128,URI="https://ct.test/sign.bin"\n'
                b"#EXTINF:1.0,\nseg0.ts\n#EXT-X-ENDLIST\n",
                "application/vnd.apple.mpegurl")
        self._bytes(b"no", "text/plain", 404)

    def do_POST(self):                      # noqa: N802
        import server as _server                   # noqa: PLC0415
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/handshake":
            return self._bytes(b"", "text/plain", 404)
        Hanime.seen = {k.lower(): self.headers.get(k) for k in
                       ("x-signature", "x-signature-version", "x-time",
                        "x-csrf-token")}
        port = self.server.server_address[1]
        try:
            payload = _server._htv_unseal(json.loads(raw)["token"])
            Hanime.seen["payload"] = payload
            token = _server._htv_seal({"sources": [
                {"kind": "promotion", "label": "1080p", "height": 1080,
                 "width": 1920, "src": ""},
                {"kind": "normal", "label": "720p", "height": 720, "width": 1280,
                 "src": "/hls/1/aaa720"},
                {"kind": "normal", "label": "480p", "height": 480, "width": 854,
                 "src": "/hls/1/bbb480"},
                {"kind": "normal", "label": "360p", "height": 360, "width": 640,
                 "src": "/hls/1/ccc360"},
            ]})
        except Exception as exc:  # noqa: BLE001
            Hanime.seen["error"] = f"{type(exc).__name__}: {exc}"
            return self._bytes(json.dumps({"error_id": "TEST"}).encode(),
                               "application/json", 401)
        return self._bytes(b'{"status":"OK"}', "application/json",
                           extra={"x-token": token})


class Jvd(BaseHTTPRequestHandler):
    """A tame javhdporn-shaped host: an RC4 `dex()` page, a play API with
    alternates, and player frames whose `data-config` hides the sources.

    Everything is sealed with the server's own `_jvd_rc4`, so the test drives
    the real decrypt path instead of a stub. The decoy playlist points at
    tiktokcdn.com (never fetched — the adapter only reads segment hostnames);
    the genuine sources live on this host.
    """
    protocol_version = "HTTP/1.1"
    seen: dict = {}

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _seal(keyarg: str, salt: str, data: bytes) -> str:
        import base64 as _b64                             # noqa: PLC0415
        import server as _s                               # noqa: PLC0415
        key = _b64.b64encode((keyarg + salt).encode()).decode()[::-1]
        plain = _b64.b64encode(data)
        stream = _s._jvd_rc4(key.encode(), b"\x00" * len(plain))
        cipher = bytes(a ^ b for a, b in zip(plain, stream))
        return _b64.b64encode(cipher).decode()

    def _player(self, sources: list, vtt: str = "") -> bytes:
        import base64 as _b64                             # noqa: PLC0415
        keyarg = _b64.b64encode(self.path.encode()).decode()[4:20]
        cfg = {"src": _b64.b64encode(json.dumps(sources).encode()).decode(),
               "img": "", "vtt": vtt}
        blob = self._seal(keyarg, "_0x59a0e4", json.dumps(cfg).encode())
        return (f'<html><body><div id="jwplayer" data-config="{blob}">'
                f"</div></body></html>").encode()

    def do_GET(self):                       # noqa: N802
        port = self.server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        if self.path.startswith("/video/"):
            slug = self.path.rstrip("/").rsplit("/", 1)[-1]
            marker = {"with-file": "case-file", "ads-only": "case-ads",
                      "real-hls": "case-hls"}.get(slug, "case-file")
            mpu = self._seal("719679", "QxLUF1bgIAdeQX", marker.encode())
            html = (f'<!doctype html><html><head><title>Test {slug}</title>'
                    f'</head><body><div id="video-player-area" '
                    f'data-video-id="719679"><div id="video-player" '
                    f'data-mpu="{mpu}" data-ver="1"></div></div></body></html>')
            return self._bytes(html.encode(), "text/html")
        if self.path.startswith("/p/primary") or self.path.startswith("/p/nl"):
            return self._bytes(self._player(
                [{"file": f"{base}/ad/master.m3u8", "type": "hls",
                  "label": "1080p"}]), "text/html")
        if self.path.startswith("/p/fr"):
            return self._bytes(self._player(
                [{"file": f"{base}/real.mp4", "type": "mp4", "label": "1080p"}],
                vtt=f"{base}/slides.jpg?length=9741"), "text/html")
        if self.path.startswith("/p/real"):
            return self._bytes(self._player(
                [{"file": f"{base}/vod/master.m3u8", "type": "hls",
                  "label": "720p"}]), "text/html")
        if self.path == "/ad/master.m3u8":
            return self._bytes(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,"
                               b"RESOLUTION=1920x1080\nad-media.m3u8\n",
                               "application/vnd.apple.mpegurl")
        if self.path == "/ad/ad-media.m3u8":
            return self._bytes(
                b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\n"
                b"https://p16-ad-site-sign-sg.tiktokcdn.com/roll.jpg\n"
                b"#EXTINF:10.0,\n"
                b"https://p19-ad-site-sign-sg.tiktokcdn.com/roll.jpg\n",
                "application/vnd.apple.mpegurl")
        if self.path == "/vod/master.m3u8":
            return self._bytes(b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,"
                               b"RESOLUTION=1280x720\nvod-media.m3u8\n",
                               "application/vnd.apple.mpegurl")
        if self.path == "/vod/vod-media.m3u8":
            return self._bytes(
                b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10.0,\nseg0.ts\n",
                "application/vnd.apple.mpegurl")
        if self.path.startswith("/real.mp4"):
            return self._bytes(b"\x00\x00\x00 ftypisom" + b"\x00" * 2048,
                               "video/mp4")
        return self._bytes(b"no", "text/plain", 404)

    def do_POST(self):                      # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/api/play/":
            return self._bytes(b"", "text/plain", 404)
        params = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        marker = (params.get("sources") or [""])[0]
        port = self.server.server_address[1]
        base = f"http://127.0.0.1:{port}"
        Jvd.seen = {"sources": marker, "ver": (params.get("ver") or [""])[0],
                    "origin": self.headers.get("Origin"),
                    "xrw": self.headers.get("X-Requested-With")}
        if marker == "case-ads":
            alts = [("de", f"{base}/p/primary?t=1"), ("nl", f"{base}/p/nl?t=1")]
        elif marker == "case-hls":
            alts = [("primary", f"{base}/p/real?t=1")]
        else:
            alts = [("de", f"{base}/p/primary?t=1"), ("fr", f"{base}/p/fr?t=1")]
        sealed = [{"data": self._seal("719679", "_0x58fe15", u.encode()),
                   "lo": lo} for lo, u in alts]
        out = {"status": True,
               "data": self._seal("719679", "_0x58fe15", alts[0][1].encode()),
               "reserve": self._seal("719679", "_0x58fe15",
                                     json.dumps(sealed).encode())}
        return self._bytes(json.dumps(out).encode(), "application/json")


class Backend(BaseHTTPRequestHandler):
    """Routes straight into server.Handler without touching the socket layer."""

    protocol_version = "HTTP/1.1"
    server_version = "test"

    def log_message(self, *a):
        pass


def make_backend(port: int) -> ThreadingHTTPServer:
    import server
    ThreadingHTTPServer.allow_reuse_address = True
    return ThreadingHTTPServer(("127.0.0.1", port), server.Handler)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are part of what we test, so never follow them here."""

    def redirect_request(self, *a, **kw):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def fetch(port: int, path: str, headers: dict | None = None, method="GET"):
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with OPENER.open(req, timeout=60) as r:
            return r.status, dict(r.getheaders()), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


def q(url: str, **kw) -> str:
    parts = {"url": url}
    parts.update(kw)
    return "?" + urllib.parse.urlencode(parts)


def main() -> int:
    import server

    tmp = tempfile.mkdtemp(prefix="viewer-check-")
    server.CACHE_DIR = tmp
    server.INFO_DIR = os.path.join(tmp, "info")
    server.SUB_DIR = os.path.join(tmp, "subs")
    server.YTDLP_DIR = os.path.join(tmp, "yt-dlp")
    server.MEDIA_DIR = os.path.join(tmp, "media")
    server.ensure_dirs()

    cdn = ThreadingHTTPServer(("127.0.0.1", 0), CDN)
    cdn_port = cdn.server_address[1]
    threading.Thread(target=cdn.serve_forever, daemon=True).start()

    backend = make_backend(0)
    bport = backend.server_address[1]
    threading.Thread(target=backend.serve_forever, daemon=True).start()

    cdn_base = f"http://127.0.0.1:{cdn_port}"
    page = "https://www.example-site.test/watch"     # what yt-dlp would report

    calls = {"n": 0}

    def fake_extract(url, playlist=False):
        """Stand in for the real yt-dlp call: the cache above it is the real one."""
        calls["n"] += 1
        generation = calls["n"]
        return {
            "title": "Fake clip",
            "duration": 30,
            "extractor_key": "Fake",
            "formats": [
                {"format_id": "720", "ext": "mp4", "protocol": "https",
                 "vcodec": "h264", "acodec": "aac", "height": 720, "fps": 30,
                 # generation 1 is a URL the CDN rejects → forces the refresh path
                 "url": f"{cdn_base}/clip.mp4?gen={generation}",
                 "filesize": 6144,
                 "http_headers": {"User-Agent": "ViewerUA/1.0",
                                  "Referer": page,
                                  "Cookie": "session=adult-ok"}},
                {"format_id": "hls", "ext": "mp4", "protocol": "m3u8_native",
                 "vcodec": "h264", "acodec": "aac", "height": 720,
                 "url": f"{cdn_base}/master.m3u8",
                 "http_headers": {"User-Agent": "ViewerUA/1.0", "Referer": page,
                                  "Cookie": "session=adult-ok"}},
                {"format_id": "uonly", "ext": "mp4", "protocol": "https",
                 "vcodec": "h264", "acodec": "none", "height": 1080,
                 "url": f"{cdn_base}/video-only.mp4", "http_headers": {}},
            ],
        }

    server._extract = fake_extract      # yt-dlp replaced, everything else real

    try:
        # 1 — header-protected format is flagged for the proxy ----------------
        st, hd, body = fetch(bport, "/api/info" + q(page))
        data = json.loads(body)
        by_id = {f["id"]: f for f in data["formats"]}
        check("info: header-protected format flagged needsProxy",
              st == 200 and by_id["720"]["needsProxy"] is True)
        check("info: plain format not flagged",
              by_id["uonly"]["needsProxy"] is False)
        check("info: 18+-style headers reach the client payload",
              by_id["720"]["headers"].get("Referer") == page)
        check("info: cacheable for browsers/CDNs", "max-age" in hd.get("Cache-Control", ""),
              hd.get("Cache-Control", ""))

        # 2 — direct playback of that URL is refused by the CDN -------------
        try:
            OPENER.open(f"{cdn_base}/clip.mp4", timeout=10)
            check("cdn: refuses a plain request", False)
        except urllib.error.HTTPError as exc:
            check("cdn: refuses a plain request", exc.code == 403)

        # 3 — proxy sends the headers, and refreshes an expired URL ----------
        st, hd, body = fetch(bport, "/api/play" + q(page, fmt="720", proxy="1"))
        check("proxy: streams a header-protected format", st == 200 and len(body) > 0,
              f"{st}, {len(body)}B")
        check("proxy: forwards upstream Accept-Ranges",
              hd.get("Accept-Ranges") == "bytes")
        check("proxy: re-extracted after the 403",
              calls["n"] >= 2, f"extractions={calls['n']}")

        # 4 — ranged proxy request (seeking) ---------------------------------
        st, hd, body = fetch(bport, "/api/play" + q(page, fmt="720", proxy="1"),
                             {"Range": "bytes=0-99"})
        check("proxy: honours Range", st == 206 and hd.get("Content-Range", "").startswith("bytes 0-99"),
              hd.get("Content-Range", ""))

        # 5 — redirect instead of proxying ------------------------------------
        st, hd, _ = fetch(bport, "/api/play" + q(page, fmt="720"))
        check("play: 302 to the CDN when proxy is not requested",
              st == 302 and "Location" in hd, str(st))

        # 6 — HLS: manifest rewrite + key + segments ---------------------------
        st, hd, manifest = fetch(bport, "/api/hls" + q(page, fmt="hls"))
        text = manifest.decode()
        lines = [l for l in text.splitlines() if l.strip() and not l.startswith("#")]
        check("hls: manifest served", st == 200 and text.startswith("#EXTM3U"))
        check("hls: segment URIs rewritten", all(l.startswith("/api/hls") for l in lines))
        check("hls: EXT-X-KEY URI rewritten",
              any('URI="/api/hls' in l for l in text.splitlines() if l.startswith("#EXT-X-KEY")))
        check("hls: relative segment paths resolved against the manifest",
              all("u=http%3A%2F%2F127.0.0.1" in l for l in lines))
        seg = [l for l in lines if l.startswith("/api/hls")][0]
        st, hd, body = fetch(bport, seg)
        check("hls: rewritten segment streams through the proxy",
              st == 200 and len(body) > 0, f"{st}, {len(body)}B")

        # 7 — caching ----------------------------------------------------------
        before = calls["n"]
        for _ in range(5):
            fetch(bport, "/api/info" + q(page))
        check("cache: repeated /api/info does not re-extract", calls["n"] == before,
              f"extractions={calls['n']}")
        st, hd, _ = fetch(bport, "/api/info" + q(page))
        tag = hd.get("ETag")
        st2, _, _ = fetch(bport, "/api/info" + q(page), {"If-None-Match": tag})
        check("cache: ETag revalidation returns 304", bool(tag) and st2 == 304, str(st2))

        # a failing URL is remembered briefly instead of hammering the host
        def boom(url, playlist=False):
            raise RuntimeError("HTTP Error 404: Not Found")

        server._extract = boom
        fetch(bport, "/api/info" + q("https://example.test/gone"))
        check("cache: failure cached so we do not retry instantly",
              server._NEG.get(server._key("https://example.test/gone", True)) is not None)
        server._extract = fake_extract

        # 8 — auth / methods / routes -------------------------------------------
        server.TOKEN = "hunter2"
        st, _, body = fetch(bport, "/api/info" + q(page))
        check("token: /api/info is closed without a token", st == 401)
        st, _, _ = fetch(bport, "/api/info" + q(page), {"X-Viewer-Token": "hunter2"})
        check("token: X-Viewer-Token accepted", st == 200)
        st, _, _ = fetch(bport, "/api/info" + q(page, token="hunter2"))
        check("token: ?token= accepted (media elements need it)", st == 200)
        st, _, _ = fetch(bport, "/api/health")
        check("token: /api/health stays public", st == 200)
        server.TOKEN = ""

        st, hd, _ = fetch(bport, "/api/health", method="OPTIONS")
        check("cors: preflight advertises methods + headers",
              st == 204 and hd.get("Access-Control-Allow-Origin") == "*")
        st, _, _ = fetch(bport, "/api/bogus")
        check("routes: unknown /api path is 404 json", st == 404)

        # the browser error beacon: reaches the operator without a token, capped
        real_ul = server.UI_LOG_LIMIT
        try:
            server.UI_LOG_LIMIT = server.SlidingWindow(1)
            st, _, _ = fetch(bport, "/api/ui-log?msg=smoke-test+from+the+suite")
            check("ui-log: the browser error beacon lands", st == 204, str(st))
            st, _, _ = fetch(bport, "/api/ui-log?msg=second")
            check("ui-log: beacon flood is capped", st == 429, str(st))
        finally:
            server.UI_LOG_LIMIT = real_ul
        st, _, _ = fetch(bport, "/../server.py")
        check("static: path traversal refused", st in (403, 404), str(st))
        st, hd, raw = fetch(bport, "/vendor/hls.min.js")
        check("static: the vendored hls.js is served from this server",
              st == 200 and len(raw) > 100_000
              and "javascript" in hd.get("Content-Type", "").lower(),
              f"{st} {len(raw)}B {hd.get('Content-Type', '')}")
        st, hd, raw = fetch(bport, "/index.html")
        check("index: hls.js comes from us, not a third-party CDN",
              st == 200 and b"vendor/hls.min.js" in raw and b"jsdelivr" not in raw,
              "index.html script src")

        # 9 — rate limit ---------------------------------------------------------
        server.EXTRACT_LIMIT = server.SlidingWindow(2)
        codes = [fetch(bport, "/api/info" + q(page))[0] for _ in range(4)]
        check("ratelimit: extraction calls are capped per address",
              429 in codes, str(codes))
        server.EXTRACT_LIMIT = server.SlidingWindow(0)

        # 10 — SSRF guard ----------------------------------------------------------
        server.BLOCK_PRIVATE = True
        st, _, _ = fetch(bport, "/api/info" + q("http://127.0.0.1:9/x.mp4"))
        check("ssrf: loopback refused when VIEWER_BLOCK_PRIVATE=1", st == 400, str(st))
        server.BLOCK_PRIVATE = False

        # 10b — media redirect guard --------------------------------------------
        zone = ThreadingHTTPServer(("127.0.0.1", 0), Zone)
        zport = zone.server_address[1]
        threading.Thread(target=zone.serve_forever, daemon=True).start()
        try:
            host = "127.0.0.1"
            table = [
                ("video.google.com", "google.com"),
                ("x.example.co.uk", "example.co.uk"),
                ("v16m.tiktokcdn.com", "tiktokcdn.com"),
                ("rr2---sn-qpuvuxaxjvh-jxce.googlevideo.com", "googlevideo.com"),
                ("localhost", "localhost"),          # single labels pass through
                ("127.0.0.1", "127.0.0.1"),
            ]
            wrong = [f"{h}->{server.base_domain(h)} (want {w})"
                     for h, w in table if server.base_domain(h) != w]
            check("redirect guard: base_domain trims CDN hosts to their operator",
                  not wrong, "; ".join(wrong) or f"{len(table)} hosts ok")

            # a redirect that stays inside the domain is followed to the file
            try:
                resp = server.guarded_urlopen(f"http://{host}:{zport}/same",
                                              {"Range": "bytes=0-5"}, timeout=10)
                first = resp.read(6)
                resp.close()
            except Exception as exc:  # noqa: BLE001
                check("redirect guard: follows a redirect on the same domain",
                      False, f"{type(exc).__name__}: {exc}")
            else:
                check("redirect guard: follows a redirect on the same domain",
                      resp.status == 200 and b"ZONE01" in first
                      and resp.headers.get("Content-Type", "").startswith("text/plain"),
                      f"{resp.status}, {first!r}")

            # leaving the domain is refused before example.com is ever contacted
            try:
                server.guarded_urlopen(f"http://{host}:{zport}/off", {}, timeout=10)
                check("redirect guard: refuses an off-domain redirect", False,
                      "no exception raised")
            except server.RedirectBlocked as exc:
                check("redirect guard: refuses an off-domain redirect",
                      "example.com" in str(exc) and "VIEWER_REDIRECT_HOSTS" in str(exc),
                      str(exc))
            except Exception as exc:  # noqa: BLE001
                check("redirect guard: refuses an off-domain redirect", False,
                      f"wrong error: {type(exc).__name__}: {str(exc)[:70]}")

            # a url that redirects to itself can never finish, and must abort
            try:
                server.guarded_urlopen(f"http://{host}:{zport}/loop", {}, timeout=10)
                check("redirect guard: a self-redirect cannot loop the stream", False,
                      "no exception raised")
            except Exception as exc:  # noqa: BLE001
                check("redirect guard: a self-redirect cannot loop the stream",
                      isinstance(exc, (server.RedirectBlocked, urllib.error.HTTPError)),
                      f"{type(exc).__name__}: {str(exc)[:80]}")

            # distinct urls every hop: only the hop cap itself ends the chain
            try:
                server.guarded_urlopen(f"http://{host}:{zport}/chain", {}, timeout=10)
                check("redirect guard: refuses to chase an endless hop chain", False,
                      "no exception raised")
            except server.RedirectBlocked as exc:
                check("redirect guard: refuses to chase an endless hop chain",
                      "redirected" in str(exc)
                      and str(server.PROXY_MAX_REDIRECTS) in str(exc),
                      str(exc)[:90])
            except Exception as exc:  # noqa: BLE001
                check("redirect guard: refuses to chase an endless hop chain", False,
                      f"wrong error: {type(exc).__name__}: {str(exc)[:70]}")
        finally:
            zone.shutdown()
            zone.server_close()

        # 10c — the ALWAYS_PROXY override ---------------------------------------
        bare_info = {"id": "x", "title": "t", "url": "http://a.test/",
                     "formats": [{"format_id": "f1", "url": "http://a.test/f.mp4",
                                  "ext": "mp4"}]}         # no http_headers at all
        real_always = server.ALWAYS_PROXY
        try:
            server.ALWAYS_PROXY = False
            entries = server.browsable_formats(bare_info)
            check("proxy: a header-free format does not need the proxy",
                  len(entries) == 1 and entries[0]["needsProxy"] is False,
                  f"{len(entries)} entry(ies)")
            server.ALWAYS_PROXY = True
            entries = server.browsable_formats(bare_info)
            check("proxy: ALWAYS_PROXY flags every format for the proxy",
                  len(entries) == 1 and entries[0]["needsProxy"] is True,
                  f"{len(entries)} entry(ies)")
        finally:
            server.ALWAYS_PROXY = real_always

        # 10d — sulasokvid adapter against a tame embed host ---------------------
        sula = ThreadingHTTPServer(("127.0.0.1", 0), Sula)
        sport = sula.server_address[1]
        threading.Thread(target=sula.serve_forever, daemon=True).start()
        try:
            check("sulasok: registered as a player site",
                  "sulasokvid.co" in server._PLAYER_SITES, "")
            try:
                server.sulasokvid_extract("http://127.0.0.1:1/some-listing")
                check("sulasok: a non-watch page says so", False, "no raise")
            except server.NotAVideoPage:
                check("sulasok: a non-watch page says so", True, "")
            except Exception as exc:  # noqa: BLE001
                check("sulasok: a non-watch page says so", False,
                      f"wrong error: {type(exc).__name__}")
            try:
                got = server.sulasokvid_extract(
                    f"http://127.0.0.1:{sport}/watch.php?id=katkat")
            except Exception as exc:  # noqa: BLE001
                check("sulasok: watch.php resolves through the player API",
                      False, f"{type(exc).__name__}: {str(exc)[:80]}")
            else:
                fmt = (got.get("formats") or [{}])[0]
                check("sulasok: watch.php resolves through the player API",
                      got.get("title") == "Katkat"
                      and got.get("webpage_url", "").endswith("/video.php?id=katkat")
                      and fmt.get("ext") == "m3u8"
                      and fmt.get("url", "").endswith("/hls/master.m3u8?token=t")
                      and fmt.get("http_headers", {}).get("Referer", "")
                      .endswith("/e/deadbeef01"),
                      f"title={got.get('title')!r} url={fmt.get('url', '')[:60]!r}")
                if fmt.get("url"):
                    entries = server.browsable_formats(
                        {"id": "k", "title": "t", "url": got["webpage_url"],
                         "formats": [fmt]})
                    check("sulasok: the manifest plays as proxied HLS",
                          len(entries) == 1 and entries[0]["kind"] == "hls"
                          and entries[0]["needsProxy"] is True,
                          f"kind={entries[0]['kind'] if entries else None}")
        finally:
            sula.shutdown()
            sula.server_close()

        # 10d2 — hanime.tv: signed player handshake ---------------------------
        han = ThreadingHTTPServer(("127.0.0.1", 0), Hanime)
        hport = han.server_address[1]
        threading.Thread(target=han.serve_forever, daemon=True).start()
        keep = {k: getattr(server, k) for k in
                ("HANIME_BASE", "HANIME_CSRF_URL", "HANIME_AUTH_URL",
                 "_htv_signature")}
        try:
            hbase = f"http://127.0.0.1:{hport}"
            server.HANIME_BASE = hbase
            server.HANIME_CSRF_URL = hbase + "/csrf-token"
            server.HANIME_AUTH_URL = hbase + "/handshake"
            # The real signer runs the site's blob; its own test lives in the
            # probe that reverse-engineered it. Here we check the flow around it.
            server._htv_signature = lambda seconds, path: "a" * 64

            check("hanime: registered as a player-site adapter",
                  "hanime.tv" in server._PLAYER_SITES, "")
            han_page = f"{hbase}/videos/hentai/{Hanime.slug}"
            try:
                got = server.hanime_extract(han_page)
            except Exception as exc:  # noqa: BLE001
                check("hanime: page → handshake → HLS sources", False,
                      f"{type(exc).__name__}: {str(exc)[:80]}")
                got = None
            if got:
                fmts = got.get("formats") or []
                check("hanime: page → handshake → HLS sources",
                      got.get("title") == "Tennen Koi-iro Alcohol 2"
                      and got.get("duration") == 1324 and len(fmts) == 3,
                      f"title={got.get('title')!r} n={len(fmts)} d={got.get('duration')}")
                check("hanime: a pre-roll entry without a stream is dropped",
                      sorted(f.get("height") for f in fmts) == [360, 480, 720],
                      ",".join(str(f.get("height")) for f in fmts))
                check("hanime: signature + sealed token reached the api",
                      Hanime.seen.get("x-signature") == "a" * 64
                      and Hanime.seen.get("x-signature-version") == "web2"
                      and Hanime.seen.get("x-csrf-token") == "test-csrf"
                      and (Hanime.seen.get("payload") or {}).get("slug") == Hanime.slug,
                      str(Hanime.seen)[:140])
                try:
                    cached = os.listdir(os.path.join(tmp, "hanime"))
                except OSError:
                    cached = []
                check("hanime: signer blob extracted and cached",
                      any(n.startswith("signer-") and n.endswith(".wasm")
                          for n in cached), str(cached)[:80])
                entries = server.browsable_formats(
                    {"id": "h", "title": got["title"], "url": han_page,
                     "duration": got["duration"], "formats": fmts})
                check("hanime: every resolution gets its own label",
                      sorted(e["label"] for e in entries)
                      == ["360p · HLS", "480p · HLS", "720p · HLS"],
                      str([e["label"] for e in entries]))
                check("hanime: playlists are proxied (AES key needs rewriting)",
                      all(e["kind"] == "hls" and e["needsProxy"] is True
                          for e in entries), "")

            try:
                server.hanime_extract(f"{hbase}/videos/hentai/")
                check("hanime: a listing page is rejected outright", False, "no raise")
            except server.NotAVideoPage:
                check("hanime: a listing page is rejected outright", True, "")
            except Exception as exc:  # noqa: BLE001
                check("hanime: a listing page is rejected outright", False,
                      f"wrong error: {type(exc).__name__}")

            try:
                server._htv_unseal("not-an-envelope")
                check("hanime: a malformed token is refused", False, "no raise")
            except ValueError:
                check("hanime: a malformed token is refused", True, "")
        finally:
            for key, val in keep.items():
                setattr(server, key, val)
            han.shutdown()
            han.server_close()

        # 10d3 — javhdporn.net: RC4 dex() chain, ad decoys, direct preference ---
        jvd = ThreadingHTTPServer(("127.0.0.1", 0), Jvd)
        jport = jvd.server_address[1]
        threading.Thread(target=jvd.serve_forever, daemon=True).start()
        keep_base = server._JVD_BASE
        try:
            jbase = f"http://127.0.0.1:{jport}"
            server._JVD_BASE = jbase
            check("javhdporn: registered as a player-site adapter",
                  "javhdporn.net" in server._PLAYER_SITES, "")
            try:
                server.javhdporn_extract(f"{jbase}/tag/uncensored")
                check("javhdporn: a non-video page says so", False, "no raise")
            except server.NotAVideoPage:
                check("javhdporn: a non-video page says so", True, "")
            except Exception as exc:  # noqa: BLE001
                check("javhdporn: a non-video page says so", False,
                      f"wrong error: {type(exc).__name__}")

            try:
                got = server.javhdporn_extract(f"{jbase}/video/with-file")
            except Exception as exc:  # noqa: BLE001
                check("javhdporn: prefers the real file over ad playlists", False,
                      f"{type(exc).__name__}: {str(exc)[:80]}")
                got = None
            if got:
                fmts = got.get("formats") or []
                check("javhdporn: prefers the real file over ad playlists",
                      len(fmts) == 1 and fmts[0]["ext"] == "mp4"
                      and fmts[0]["url"].endswith("/real.mp4"),
                      str([(f["format_id"], f["ext"]) for f in fmts]))
                check("javhdporn: duration comes from the vtt length",
                      got.get("duration") == 9741, str(got.get("duration")))
                check("javhdporn: the dex chain reached the play api",
                      Jvd.seen.get("sources") == "case-file"
                      and Jvd.seen.get("ver") == "1"
                      and Jvd.seen.get("xrw") == "XMLHttpRequest"
                      and Jvd.seen.get("origin") == jbase,
                      str(Jvd.seen)[:140])
                entries = server.browsable_formats(got)
                check("javhdporn: the file plays through the proxy",
                      len(entries) == 1 and entries[0]["kind"] == "direct"
                      and entries[0]["needsProxy"] is True
                      and entries[0]["headers"].get("Referer", "")
                      .endswith("/p/fr?t=1"),
                      str(entries[0] if entries else None)[:120])

            try:
                server.javhdporn_extract(f"{jbase}/video/ads-only")
                check("javhdporn: an all-ad video is refused", False, "no raise")
            except server.NotAVideoPage:
                check("javhdporn: an all-ad video is refused", False,
                      "cried NotAVideoPage")
            except RuntimeError as exc:  # noqa: BLE001
                check("javhdporn: an all-ad video is refused",
                      "ad" in str(exc), str(exc)[:80])
            except Exception as exc:  # noqa: BLE001
                check("javhdporn: an all-ad video is refused", False,
                      f"wrong error: {type(exc).__name__}")

            try:
                got = server.javhdporn_extract(f"{jbase}/video/real-hls")
                fmts = got.get("formats") or []
                check("javhdporn: a genuine HLS source is accepted",
                      len(fmts) == 1 and fmts[0]["ext"] == "m3u8"
                      and fmts[0]["url"].endswith("/vod/master.m3u8"),
                      str([(f["format_id"], f["ext"]) for f in fmts]))
            except Exception as exc:  # noqa: BLE001
                check("javhdporn: a genuine HLS source is accepted", False,
                      f"{type(exc).__name__}: {str(exc)[:80]}")
        finally:
            server._JVD_BASE = keep_base
            jvd.shutdown()
            jvd.server_close()

        # 10e — /api/merge dl=1 for adapter-resolved HLS → ffmpeg transmux ------
        # The tame Zone doubles as a tiny HLS origin: master.m3u8 with two real
        # .ts segments, encoded here by the same ffmpeg the server would use.
        if server.FFMPEG:
            import subprocess as _sp                       # noqa: PLC0415
            tsdir = tempfile.mkdtemp(prefix="viewer-ts-")
            tsfile = os.path.join(tsdir, "tiny.ts")
            enc = _sp.run(
                [server.FFMPEG, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc=duration=0.5:size=128x96:rate=10",
                 "-f", "lavfi", "-i", "sine=duration=0.5",
                 "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                 "-y", tsfile], capture_output=True)
            HX["ts"] = open(tsfile, "rb").read() if enc.returncode == 0 else b""
            detail = (enc.stderr.decode("utf-8", "replace")[-160:].strip()
                      if enc.returncode else "fixture ok")
            hz = ThreadingHTTPServer(("127.0.0.1", 0), Zone)
            hport = hz.server_address[1]
            threading.Thread(target=hz.serve_forever, daemon=True).start()
            real_rt = server.resolve_target
            try:
                server.resolve_target = (
                    lambda page, fmt="", fresh=False: {
                        "url": f"http://127.0.0.1:{hport}/hls/master.m3u8",
                        "headers": {},
                        "kind": "hls", "id": fmt or "h1", "ext": "m3u8",
                        "title": "Tiny Zone Stream"})
                page = f"http://127.0.0.1:{hport}/fake-page"
                if not HX["ts"]:
                    # A broken fixture would fail for the wrong reason — say so
                    # instead of blaming the merge code.
                    check("merge: hls dl transmuxes to a real mp4", False, detail)
                else:
                    st, hd, body = fetch(
                        bport, f"/api/merge?{urllib.parse.urlencode({'url': page, 'fmt': 'h1', 'dl': '1'})}")
                    check("merge: hls dl transmuxes to a real mp4",
                          st == 200 and hd.get("Content-Type") == "video/mp4"
                          and b"ftyp" in body[:32] and len(body) > 1000,
                          f"{st} {len(body)}B {hd.get('Content-Type', '')}")
                    check("merge: the saved file is named for the video",
                          "attachment" in hd.get("Content-Disposition", "")
                          and "filename*=UTF-8''Tiny%20Zone" in hd.get("Content-Disposition", ""),
                          hd.get("Content-Disposition", "")[:80])
                    st, hd, body = fetch(
                        bport, f"/api/merge?{urllib.parse.urlencode({'url': page, 'fmt': 'h1', 'dl': '1'})}",
                        method="HEAD")
                    check("merge: a HEAD download probe answers disposition, no body",
                          st == 200 and hd.get("Content-Type") == "video/mp4"
                          and not body
                          and "attachment" in hd.get("Content-Disposition", ""),
                          f"{st} {bool(body)}")
            finally:
                server.resolve_target = real_rt
                hz.shutdown()
                hz.server_close()

        # 11 — subtitle conversion cache -------------------------------------------
        sub_url = "https://cdn.example.test/captions.vtt"
        os.makedirs(server.SUB_DIR, exist_ok=True)
        with open(os.path.join(server.SUB_DIR,
                               hashlib.sha1(sub_url.encode()).hexdigest() + ".vtt"),
                  "w", encoding="utf-8") as fh:
            fh.write("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
        check("subs: converted vtt served from the disk cache",
              server.cached_vtt(sub_url) is not None)
        st, hd, body = fetch(bport, "/api/subsrc?sub=" + urllib.parse.quote(sub_url, safe=""))
        check("subs: vtt endpoint sends a cacheable response",
              st == 200 and body.startswith(b"WEBVTT")
              and "max-age" in hd.get("Cache-Control", ""), hd.get("Cache-Control", ""))
    # 12 — codec inference (yt-dlp null vs "none") ---------------------------
        null_codecs = server.browsable_formats({"formats": [
            {"format_id": "1080p", "ext": "mp4", "protocol": "https",
             "url": "https://cdn.test/a.mp4", "vcodec": None, "acodec": None,
             "height": 1080},
            {"format_id": "audio", "ext": "m4a", "protocol": "https",
             "url": "https://cdn.test/a.m4a", "vcodec": "none", "acodec": "mp4a.40.2"},
            {"format_id": "vonly", "ext": "mp4", "protocol": "https",
             "url": "https://cdn.test/v.mp4", "vcodec": "h264", "acodec": "none",
             "height": 720},
            {"format_id": "hls", "ext": "mp4", "protocol": "m3u8_native",
             "url": "https://cdn.test/m.m3u8", "vcodec": "avc1.640015",
             "acodec": "mp4a.40.2", "height": 1080},
        ]})
        by_id = {f["id"]: f for f in null_codecs}
        check("codecs: null + height is a muxed progressive file",
              by_id["1080p"]["hasVideo"] and by_id["1080p"]["hasAudio"])
        check("codecs: such a format is labelled as video",
              "1080p" in by_id["1080p"]["label"] and "audio" not in by_id["1080p"]["label"])
        check("codecs: vcodec 'none' is still audio-only",
              not by_id["audio"]["hasVideo"] and by_id["audio"]["hasAudio"])
        check("codecs: acodec 'none' is still video-only",
              by_id["vonly"]["hasVideo"] and not by_id["vonly"]["hasAudio"])
        check("codecs: default prefers the muxed file over HLS",
              server.best_format(null_codecs)["id"] == "1080p")

        # 13 — error messages are actionable -------------------------------
        check("errors: wiki boilerplate stripped",
              "http" not in server.with_hint(
                  "Sign in to confirm you're not a bot. See https://example.test/faq "
                  "for how to pass cookies."))
        check("errors: bot check gets the IP hint",
              "curl_cffi" in server.with_hint("Sign in to confirm you're not a bot"))
        not_found = "[generic] x: Unable to download webpage: HTTP Error 404"
        check("errors: 'webpage' does not trigger the age-gate hint",
              server.with_hint(not_found) == not_found)   # no hint appended at all
        check("errors: removed videos are not blamed on the platform",
              "IP ranges" not in server.with_hint("This video has been removed"))
        landing = server.with_hint("Unsupported URL: https://example.test/")
        check("errors: a video-less landing page says so",
              "landing page" in landing)
        check("errors: landing pages are not blamed on the IP",
              "IP ranges" not in landing)

        # 14 — the sniffer: what to try when yt-dlp gives up ----------------
        html = (
            '<link rel="alternate" type="application/rss+xml" '
            'href="https://site.test/tag/x/feed/">'
            '<iframe src="/wp-content/plugins/p/player-x.php?q=abc"></iframe>'
            '<source data-src="https://cdn.test/media/clip.mp4">'
            '<script>var s = "https://cdn.test/live/index.m3u8";</script>'
            '<img src="/logo.png"><script src="/app.js"></script>')
        real_fetch = server._fetch_html
        server._fetch_html = lambda url: ("https://site.test/post/1/", html)
        try:
            server._SNIFF_CACHE.clear()
            found = server.sniff_targets("https://site.test/post/1/")
        finally:
            server._fetch_html = real_fetch
        media = [u for k, u in found if k == "media"]
        check("sniff: finds the mp4 behind a data-src",
              any(u.endswith("clip.mp4") for u in media), str(media))
        check("sniff: finds the m3u8 in a script",
              any(u.endswith("index.m3u8") for u in media), str(media))
        check("sniff: finds the player frame",
              any(u.endswith("player-x.php?q=abc") for _, u in found))
        check("sniff: finds the RSS feed",
              any(u.endswith("/feed/") for _, u in found))
        check("sniff: ignores images and scripts",
              not any(u.endswith((".png", ".js")) for _, u in found))
        check("sniff: a progressive file outranks an HLS playlist",
              found and found[0][0] == "media" and found[0][1].endswith("clip.mp4"),
              str(found[:1]))

        sniffed = server.browsable_formats(
            server.synthetic_info("https://site.test/post/1/",
                                  "https://cdn.test/media/clip.mp4"))
        check("sniff: synthetic format counts as video+audio",
              sniffed and sniffed[0]["hasVideo"] and sniffed[0]["hasAudio"])
        check("sniff: synthetic format sends a Referer",
              sniffed and sniffed[0]["headers"].get("Referer") == "https://site.test/post/1/")
        check("sniff: synthetic format is marked and says where it came from",
              sniffed and sniffed[0]["sniffed"] and "found in the page" in sniffed[0]["label"])
        # A sniffed file with a known resolution must lead with it, or every
        # quality button on a multi-quality site reads the same.
        sized = server.browsable_formats({"formats": [dict(
            server.synthetic_info("https://site.test/post/1/",
                                  "https://cdn.test/media/clip.mp4")["formats"][0],
            height=1080)]})
        check("sniff: a sniffed format with a height leads with the resolution",
              sized and sized[0]["label"].startswith("1080p"),
              sized[0]["label"] if sized else "none")

        smuggled = server.playlist_entries({"entries": [
            {"title": "t", "url": "https://site.test/p/1/#__youtubedl_smuggle=%7B%22http_headers%22%3A%7B%7D%7D"},
            {"title": "no url"},
        ]})
        check("playlists: smuggle fragment stripped from entry urls",
              smuggled[0]["url"] == "https://site.test/p/1/", smuggled[0]["url"])
        check("playlists: entries without a usable url are dropped",
              smuggled[1]["url"] is None)

        # 16 — API-driven sites (the table is empty today; keep it working) ---
        page = ('<astro-island props="{&quot;video_id&quot;:[0,3210],'
                '&quot;slug&quot;:[0,&quot;some-video&quot;]}"></astro-island>')
        real_json, real_api = server._json_get, server._API_SITES
        try:
            server._API_SITES = {"api.test": (
                (r'"video_id"\s*:\s*\[\s*\d+\s*,\s*(\d+)\s*\]',),
                ("https://api.test/v?id={id}",))}
            server._json_get = lambda url, headers, timeout=25: {
                "video": {"title": "Example",
                          "videos": [{"url": "https://cdn.x/hls/i.m3u8"},
                                     {"url": "https://cdn.x/mp4/720.mp4"}]}}
            info = server.api_site_extract("https://api.test/videos/hentai/x", html=page)
            fmts = server.browsable_formats(info)
            check("api-site: a stream url from the api becomes a format",
                  bool(fmts) and fmts[0]["url"].endswith("720.mp4"),
                  fmts[0]["url"] if fmts else "none")
            check("api-site: progressive file preferred over the playlist",
                  bool(fmts) and ".m3u8" not in fmts[0]["url"])
            check("api-site: title is taken from the payload",
                  info.get("title") == "Example", str(info.get("title")))
            server._json_get = lambda url, headers, timeout=25: ("error", 403)
            try:
                server.api_site_extract("https://api.test/videos/hentai/x", html=page)
                check("api-site: a blocked api explains itself", False)
            except Exception as exc:  # noqa: BLE001
                check("api-site: a blocked api explains itself",
                      "cookies" in str(exc) and "403" in str(exc), str(exc)[:70])
        finally:
            server._json_get, server._API_SITES = real_json, real_api
        check("api-site: unknown hosts are left to the normal path",
              server.api_site_extract("https://example.com/x") is None)

        # 16b — comment playlists must not end the search ---------------------
        comments = {"playlist": {"title": "\n\tComments for Watch Hentai\t", "count": 2},
                    "entries": [{"url": "https://site.test/comment-page-1/#comment-9"},
                                {"url": "https://site.test/comment-page-2/#comment-11"}]}
        check("comment-playlist: a page's comment links are rejected",
              server._is_comment_playlist(comments))
        check("comment-playlist: a real playlist is kept",
              not server._is_comment_playlist(
                  {"playlist": {"title": "Best of"},
                   "entries": [{"url": "https://youtube.com/watch?v=abc"}]}))
        check("comment-playlist: a playlist that also plays is kept",
              not server._is_comment_playlist({"entries": [{"url": "https://x/1"}],
                                               "formats": [{"url": "https://x/1.mp4"}]}))
        check("comment-playlist: a plain video is kept",
              not server._is_comment_playlist({"formats": [{"url": "https://x/1.mp4"}]}))

        # 16c — player-frame sites with an obfuscated media url ---------------
        blob = ("MF9Lc2UnUHdicVNVYSJ3UWlISW54eUVfZFN7dXRTQkIvJEpqeWQjXnt8Qk94KUp7cylK"
                "V3dJInVkQSd3ZC5hSmxWREd-YmJKZ31LcSEjREJ3XHZ_a189bH8oa18iWF50KEtSeg")
        check("player-site: the page's own decoder round-trips its blob",
              server.wh_decode_media_url(blob)
              == "https://hstorage.xyz/files/K/kotowarenai-haha/"
                 "kotowarenai-haha-2_1080p.mp4",
              str(server.wh_decode_media_url(blob)))
        check("player-site: junk decodes to nothing rather than to bytes",
              server.wh_decode_media_url("not-base64-at-all!!") is None
              and server.wh_decode_media_url("") is None)

        wh_page = ('<h1 class="entry-title">Kotowarenai Haha &#8211; Episode 2</h1>'
                   '<iframe class="metaframe" data-primary-player-url='
                   '"https://watchhentai.net/player/19823/1/mp4/" '
                   'data-alternate-player-url="https://watchhentai.net/player-alt/x/">'
                   '<script>{"@type":"VideoObject","duration":"PT24M50S"}</script>')
        frame = ('<script>var whJwSources = [{"file":"' + blob + '",'
                 '"type":"video/mp4","label":"1080p","default":true}];</script>')
        real_fetch = server._fetch_html
        try:
            server._fetch_html = lambda u: (u, frame if "/player/" in u else wh_page)
            info = server.player_site_extract(
                "https://watchhentai.net/videos/kotowarenai-haha-episode-2-id-01/")
            fmts = server.browsable_formats(info)
            check("player-site: a decoded source becomes a format",
                  bool(fmts) and fmts[0]["url"].endswith("_1080p.mp4"),
                  fmts[0]["url"] if fmts else "none")
            check("player-site: the quality label becomes the height",
                  bool(fmts) and fmts[0]["height"] == 1080,
                  str(fmts[0]["height"]) if fmts else "none")
            check("player-site: the player frame is the Referer, so it needs the proxy",
                  bool(fmts) and fmts[0]["needsProxy"]
                  and fmts[0]["headers"].get("Referer") == "https://watchhentai.net/player/19823/1/mp4/",
                  str(fmts[0].get("headers")) if fmts else "none")
            check("player-site: title is unescaped",
                  info.get("title") == "Kotowarenai Haha – Episode 2",
                  str(info.get("title")))
            check("player-site: iso-8601 duration is parsed",
                  info.get("duration") == 1490, str(info.get("duration")))

            server._fetch_html = lambda u: None
            try:
                server.player_site_extract("https://watchhentai.net/videos/x/",
                                           html=wh_page)
                check("player-site: an unreachable player frame explains itself", False)
            except Exception as exc:  # noqa: BLE001
                check("player-site: an unreachable player frame explains itself",
                      "Referer" in str(exc), str(exc)[:70])
            server._fetch_html = lambda u: (u, "<html>markup changed</html>")
            try:
                server.player_site_extract("https://watchhentai.net/videos/x/",
                                           html=wh_page)
                check("player-site: changed markup is reported, not guessed at", False)
            except Exception as exc:  # noqa: BLE001
                check("player-site: changed markup is reported, not guessed at",
                      "decoder needs updating" in str(exc), str(exc)[:70])
        finally:
            server._fetch_html = real_fetch
        check("player-site: unknown hosts are left to the normal path",
              server.player_site_extract("https://example.com/x") is None)

        # a series page answers instantly with its own reason, not after the
        # generic crawl
        bare = "<html><body><h1>Kotowarenai Haha</h1></body></html>"
        try:
            server.player_site_extract(
                "https://watchhentai.net/series/kotowarenai-haha-id-01/",
                html=bare)
            check("player-site: a series page says so, fast", False)
        except Exception as exc:  # noqa: BLE001
            check("player-site: a series page says so, fast",
                  isinstance(exc, server.NotAVideoPage) and "series" in str(exc),
                  str(exc)[:70])
        check("player-site: a non-series page without a frame stays None",
              server.player_site_extract("https://watchhentai.net/videos/x/",
                                         html=bare) is None)

        # known hosts start with their adapter; yt-dlp is only the fallback
        real_extract = server._extract
        real_player = server.player_site_extract
        reached_ytdlp = []
        try:
            server._extract = lambda u, playlist=False: reached_ytdlp.append(u)
            server.player_site_extract = lambda u: {
                "id": "x", "title": "t",
                "formats": [{"url": "http://cdn/a.mp4", "ext": "mp4"}]}
            info = server._extract_sniffed(
                "https://watchhentai.net/videos/kotowarenai-haha-episode-2-id-01/")
            check("adapter-first: a known host never reaches the yt-dlp crawl",
                  not reached_ytdlp, "yt-dlp called for %r" % reached_ytdlp)
            check("adapter-first: the adapter's answer is used",
                  len(info.get("formats") or []) > 0,
                  "%d formats" % len(info.get("formats") or []))
            # a decisive verdict ends the search before the fallback work
            server.player_site_extract = lambda u: (_ for _ in ()).throw(
                server.NotAVideoPage("a series page lists episodes"))
            try:
                server._extract_sniffed("https://watchhentai.net/series/k-01/")
                check("decisive: a not-a-video page does not fall through", False)
            except server.NotAVideoPage:
                check("decisive: a not-a-video page does not fall through", True)
            check("decisive: yt-dlp is never reached for it",
                  not reached_ytdlp, "yt-dlp called for %r" % reached_ytdlp)
        finally:
            server._extract = real_extract
            server.player_site_extract = real_player

        # decisive vs provisional; session-bound CDNs tell the truth ----------
        check("cdn notes: a tiktok web token explains itself",
              "cookies" in (server.session_bound_media_note(
                  "https://v16-webapp-prime.tiktok.com/video/tos/abc/?a=1988") or ""),
              str((server.session_bound_media_note(
                  "https://v16-webapp-prime.tiktok.com/video/tos/abc/?a=1988") or "")[:70]))
        check("cdn notes: the app-CDN (music) family needs no note",
              server.session_bound_media_note(
                  "https://v16m.tiktokcdn.com/video/tos/x.mp4") is None)
        check("cdn notes: ordinary hosts get none",
              server.session_bound_media_note(
                  "https://v16m.tiktokcdn.com/../../x.mp4") is None
              and server.session_bound_media_note(
                  "https://cdn.example.com/a.mp4") is None)

        # a stale page must be able to notice it is stale ----------------------
        build = server.static_build()
        check("build: a frontend build id exists",
              isinstance(build, str) and len(build) >= 8, str(build))
        check("build: it is stable between calls", server.static_build() == build)
        page_html = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "index.html"), encoding="utf-8").read()
        check("build: index.html carries the placeholder for the server to fill",
              'name="viewer-build" content="<!--BUILD-->"' in page_html)
        served = server.static_build().encode()
        check("build: serving substitutes a real id, not the placeholder",
              served.decode() != "<!--BUILD-->" and len(served) == len(build))

        # a CDN 474/5xx should re-extract instead of surfacing as 502
        check("proxy: 474 counts as 'the url may have expired'",
              474 in server.EXPIRY_CODES, str(server.EXPIRY_CODES))

        # 15 — downloads -----------------------------------------------------
        disp = server.safe_filename('A "weird" / name: ünïcode', "mp4")
        check("download: header has an attachment disposition",
              disp.startswith("attachment;"))
        check("download: quotes and slashes are neutralised",
              '"' not in disp.split("filename*=UTF-8''")[0].split('filename="')[1].split('"')[0],
              disp[:90])
        check("download: utf-8 filename is included",
              "filename*=UTF-8''" in disp and "%C3%BC" in disp)
        check("download: extension is appended once",
              disp.count(".mp4") >= 1 and not disp.split("filename=")[1].startswith(".mp4"))
        check("download: empty title still names the file",
              "video.mp4" in server.safe_filename("", "mp4"))

        # main() resets the cached yt-dlp flags; it used to set the cache to None,
        # which broke every subprocess path once that cache became a dict.
        check("base args cache survives the main() reset",
              (server._BASE_ARGS.clear() or server.base_ytdlp_args()) is not None)

        dl_info = {"formats": [
            {"format_id": "hls", "ext": "mp4", "protocol": "m3u8_native",
             "url": "https://cdn.test/m.m3u8", "vcodec": "avc1", "acodec": "mp4a"},
            {"format_id": "big", "ext": "webm", "protocol": "https", "height": 2160,
             "url": "https://cdn.test/4k.webm", "vcodec": "vp9", "acodec": "opus"},
            {"format_id": "sd", "ext": "mp4", "protocol": "https", "height": 480,
             "url": "https://cdn.test/sd.mp4", "vcodec": None, "acodec": None},
        ], "title": "Example"}
        real_get = server.get_info
        server.get_info = lambda url, **kw: dl_info
        try:
            picked = server.resolve_download("https://site.test/v")
            check("download: skips HLS and picks a real file",
                  picked["url"].endswith(".mp4"), picked["url"])
            check("download: defaults to a sane size, not the 4K file",
                  picked["height"] == 480, str(picked["height"]))
            check("download: no codec metadata needed for a plain file",
                  picked["ext"] == "mp4")
            check("download: an explicit format id wins",
                  server.resolve_download("https://site.test/v", "big")["url"].endswith("4k.webm"))
            server.get_info = lambda url, **kw: {"formats": [dl_info["formats"][0]],
                                                 "title": "HLS only"}
            try:
                server.resolve_download("https://site.test/v")
                check("download: refuses when only HLS exists", False)
            except Exception as exc:  # noqa: BLE001
                check("download: refuses when only HLS exists",
                      "no single file" in str(exc), str(exc)[:60])
        finally:
            server.get_info = real_get

        # 17 — windowed media cache ------------------------------------------
        # Stop any prefetchers left over from the proxy tests above, then start
        # with a clean, small-chunk cache so the arithmetic is easy to see.
        server.media_shutdown()
        server.MEDIA_CACHE = True
        server.MEDIA_CHUNK = 65536
        server.MEDIA_MAX_MB = 0          # quota off for the window tests
        server.MEDIA_TTL = 300
        server.MEDIA_SWEEP = 30
        server._MEDIA.clear()
        server.media_startup()

        check("media: size tiers follow the 75/50/35/25 % table",
              [server.cache_fraction(n) for n in
               (99_999_999, 100_000_000, 299_999_999, 300_000_000,
                999_999_999, 1_000_000_000)]
              == [0.75, 0.50, 0.50, 0.35, 0.35, 0.25],
              str([server.cache_fraction(n) for n in
                   (99_999_999, 100_000_000, 299_999_999, 300_000_000,
                    999_999_999, 1_000_000_000)]))

        # 100 MB / 10 min, playhead at 3:15 → ~75 MB from just behind 3:15
        big = server.MediaEntry("synthetic-big", "p", "f")
        big.total, big.duration = 100_000_000, 600
        big.fraction = server.cache_fraction(big.total)
        big.budget = 75_000_000
        big.playhead, big.has_position = 195, True
        lo, hi = server._media_window(big)
        check("media: a 100 MB file keeps ~75 % from the playhead",
              0 < lo < 32_500_000 and hi == 100_000_000 and hi - lo > 60_000_000,
              f"{lo}..{hi} ({(hi - lo) // 1_000_000} MB)")

        huge = server.MediaEntry("synthetic-huge", "p", "f")
        huge.total, huge.duration = 2_469_331_593, 9741.3
        huge.fraction = server.cache_fraction(huge.total)
        huge.budget = (int(huge.total * huge.fraction) // server.MEDIA_CHUNK
                       * server.MEDIA_CHUNK)
        huge.playhead, huge.has_position = 195, True
        lo, hi = server._media_window(huge)
        check("media: a 2.47 GB file keeps ~25 % around the playhead",
              0.20 < (hi - lo) / huge.total <= 0.26,
              f"{(hi - lo) / huge.total:.3f} of the file")

        body = bytes((i * 7 + 11) & 0xFF for i in range(8 * 65536))
        Vault.body = body
        Vault.up = True
        vault = ThreadingHTTPServer(("127.0.0.1", 0), Vault)
        vport = vault.server_address[1]
        threading.Thread(target=vault.serve_forever, daemon=True).start()
        vault_url = f"http://127.0.0.1:{vport}/clip.mp4"
        ent = server._media_get(vault_url, "")
        ent.paused = True                # keep the prefetcher out of the checks
        server._media_set_total(ent, len(body))
        try:
            st, hd, got = fetch(bport, "/api/play" + q(vault_url, proxy="1"),
                                {"Range": "bytes=0-131071"})
            check("media: a proxied range is teed into the cache",
                  st == 206 and got == body[:131072]
                  and {0, 1} <= ent.present and ent.total == len(body),
                  f"{st} present={sorted(ent.present)} total={ent.total}")

            hits = Vault.hits
            Vault.up = False
            st, hd, got = fetch(bport, "/api/play" + q(vault_url, proxy="1"),
                                {"Range": "bytes=0-131071"})
            check("media: a cached range survives the source going away",
                  st == 206 and hd.get("X-Viewer-Cache") == "hit"
                  and got == body[:131072] and Vault.hits == hits,
                  f"{st} {hd.get('Content-Range')} upstream={Vault.hits - hits}")

            st, hd, got = fetch(bport, "/api/play" + q(vault_url, proxy="1"),
                                {"Range": "bytes=393216-524287"})
            check("media: an uncached range offline is a clear 503, not a truncated 206",
                  st == 503, f"{st} {got[:80]!r}")

            st, hd, raw = fetch(bport, "/api/position" +
                                q(vault_url, fmt="", t="15", d="30", paused="1"))
            pos = json.loads(raw) if raw else {}
            check("media: the heartbeat moves the window to the playhead",
                  st == 200 and pos.get("cache") is True
                  and pos.get("budgetBytes") == 393216
                  and 80_000 < pos.get("windowStart", 0) < 95_000
                  and 470_000 < pos.get("windowEnd", 0) <= 524288,
                  str(pos))

            Vault.up = True
            ent.has_position = False     # no recent heartbeat → range anchoring
            ent.duration = 0             # no byte↔time back-buffer in this check
            ent.position_at = 0
            st, hd, got = fetch(bport, "/api/play" + q(vault_url, proxy="1"),
                                {"Range": "bytes=393216-524287"})
            # The tee writes the bytes before the client sees them, but the
            # eviction pass runs just after the last write; give it a moment.
            deadline = time.time() + 2
            while time.time() < deadline and any(i < 6 for i in list(ent.present)):
                time.sleep(0.02)
            check("media: playing forward drops the chunks behind the window",
                  st == 206 and got == body[393216:524288]
                  and 0 not in ent.present and 6 in ent.present,
                  f"present={sorted(ent.present)} anchor={ent.anchor} "
                  f"win={server._media_window(ent)} hp={ent.has_position} "
                  f"d={ent.duration} paused={ent.paused} budget={ent.budget}")

            first = ent.filler
            server._media_start_filler(ent)
            check("media: only one prefetcher runs per entry",
                  ent.filler is first and (first is None or first.is_alive()))

            ent.last_used = time.time() - server.MEDIA_TTL - 5
            ent.player_until = 0
            server._media_sweep_once()
            check("media: an idle entry is deleted after the TTL",
                  server._media_get(vault_url, "", create=False) is None
                  and not os.path.isdir(os.path.join(server.MEDIA_DIR, ent.key)))

            older = server._media_get("https://q.test/a", "1")
            newer = server._media_get("https://q.test/b", "1")
            older.on_disk, newer.on_disk = 700_000, 700_000
            older.last_used = time.time() - 10
            newer.last_used = time.time()
            older.player_until = newer.player_until = 0
            server.MEDIA_MAX_MB = 1
            server._media_enforce_quota()
            check("media: the global quota evicts the least recently used",
                  server._media_get("https://q.test/a", "1", create=False) is None
                  and server._media_get("https://q.test/b", "1",
                                        create=False) is not None)
            server.MEDIA_MAX_MB = 0

            os.makedirs(os.path.join(server.MEDIA_DIR, "junk"), exist_ok=True)
            with open(os.path.join(server.MEDIA_DIR, "junk", "x.bin"), "wb") as fh:
                fh.write(b"x")
            server.media_shutdown()
            check("media: the cache is gone when the server exits",
                  not os.path.exists(server.MEDIA_DIR))

            os.makedirs(os.path.join(server.MEDIA_DIR, "leftover"), exist_ok=True)
            with open(os.path.join(server.MEDIA_DIR, "leftover", "chunk_0.bin"),
                      "wb") as fh:
                fh.write(b"x")
            with open(os.path.join(server.MEDIA_DIR, ".owner"), "w",
                      encoding="utf-8") as fh:
                fh.write("99999999")
            server.media_startup()
            check("media: a crashed process's cache is wiped on startup",
                  os.path.exists(os.path.join(server.MEDIA_DIR, ".owner"))
                  and not os.path.exists(os.path.join(server.MEDIA_DIR, "leftover")),
                  str(os.listdir(server.MEDIA_DIR)))

            os.makedirs(os.path.join(server.MEDIA_DIR, "live"), exist_ok=True)
            live_marker = os.path.join(server.MEDIA_DIR, "live", "chunk_0.bin")
            with open(live_marker, "wb") as fh:
                fh.write(b"x")
            with open(os.path.join(server.MEDIA_DIR, ".owner"), "w",
                      encoding="utf-8") as fh:
                fh.write(str(os.getppid()))
            server._MEDIA_READY = False
            server.media_startup()
            check("media: a live owner's cache is left alone",
                  os.path.exists(live_marker))
        finally:
            Vault.up = True
            server.media_shutdown()
            vault.shutdown()
            vault.server_close()
    finally:
        backend.shutdown()
        cdn.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())