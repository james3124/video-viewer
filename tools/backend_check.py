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
import base64
import os
import shutil
import socket
import socketserver
import struct
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


class SulaMirrors(BaseHTTPRequestHandler):
    """A sulasokvid page with two mirrors: the default one is dead StreamRuby,
    the doodstream one still serves the file through pass_md5."""
    protocol_version = "HTTP/1.1"

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
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        port = self.server.server_address[1]
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/video.php":
            title = "<title>Mirror Cat</title>"
            if "s=doodstream" in (parsed.query or ""):
                body = (f"<html><head>{title}</head><body>"
                        f'<iframe src="http://127.0.0.1:{port}/e/doodok?c_poster=x">'
                        f"</iframe></body></html>").encode()
            else:
                body = (f"<html><head>{title}</head><body>"
                        f"<script>iframe.src = 'http://127.0.0.1:{port}/e/deadfail';"
                        f"</script>"
                        f'<a href="?id=mir&s=doodstream">Player 2</a>'
                        f"</body></html>").encode()
            return self._bytes(body, "text/html")
        if parsed.path == "/e/deadfail":
            return self._bytes(b"<html><title>StreamRuby \xc2\xb7 Video "
                               b"unavailable</title></html>", "text/html")
        if parsed.path == "/e/doodok":
            body = (b"<html><body><script>"
                    b"$('#x').click(function(){"
                    b"$.get('/pass_md5/abc/def', function(data){"
                    b" dpload(data); dsplayer.src({type:'video/mp4',"
                    b" src: data + makePlay() + '?token=zzz'}); }); });"
                    b"</script></body></html>")
            return self._bytes(body, "text/html")
        if parsed.path == "/pass_md5/abc/def":
            return self._bytes(f"http://127.0.0.1:{port}/media/".encode(),
                               "text/plain")
        if parsed.path.startswith("/media/"):
            return self._bytes(b"\0" * 1000, "video/mp4",
                               extra={"Accept-Ranges": "bytes"})
        self._bytes(b"no", "text/plain", 404)

    do_HEAD = do_GET                       # noqa: N815

    def do_POST(self):                      # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path == "/api/stream" and b"deadfail" in body:
            return self._bytes(b'["error", 404]', "application/json")
        self._bytes(b"no", "text/plain", 404)


class Hentaimama(BaseHTTPRequestHandler):
    """A tame hentaimama-shaped site: the episode page names a post id, each
    mirror is loaded from admin-ajax and names a base64 embed page, and that
    embed's JW Player setup carries the sources."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        port = self.server.server_address[1]
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/playlist/abc/ep1/":
            body = (f"<html><head><title>Episode Page</title></head><body>"
                    f"<h1 class='epih1'>Tame Show - Episode 1</h1>"
                    f"<div id='dt-dl-modal' data-post='4242'></div>"
                    f"<script>$(function(){{$.post('/wp-admin/admin-ajax.php', "
                    f"{{action: 'get_player_contents',a: '4242',i: n}});}});"
                    f"</script>"
                    f'<a href="#option-5">mi-1</a><a href="#option-3">mi-2</a>'
                    f'<a href="#option-1">mi-3</a></body></html>').encode()
            return self._bytes(body, "text/html")
        if parsed.path == "/playlist/abc/":
            return self._bytes(b"<html><h1>Playlist</h1></html>", "text/html")
        if parsed.path == "/embed":
            kind = urllib.parse.parse_qs(parsed.query).get("dt_embed", [""])[0]
            if kind == "hls":
                sources = [{"file": f"http://127.0.0.1:{port}"
                                    "/media/master.m3u8", "type": "hls"}]
            else:
                sources = [
                    {"file": f"http://127.0.0.1:{port}/media/1080.mp4",
                     "label": "1080p", "type": "mp4"},
                    {"file": f"http://127.0.0.1:{port}/media/720.mp4",
                     "label": "720p", "type": "mp4"},
                ]
            body = ("<html><script>var jwp = jwplayer('x').setup({sources: "
                    + json.dumps(sources) +
                    ",tracks: [{file: 'http://127.0.0.1/x.vtt'}]});"
                    "</script></html>").encode()
            return self._bytes(body, "text/html")
        self._bytes(b"no", "text/plain", 404)

    def do_POST(self):                      # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path != "/wp-admin/admin-ajax.php":
            return self._bytes(b"no", "text/plain", 404)
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        port = self.server.server_address[1]
        mirror = int((form.get("i") or ["0"])[0])
        if form.get("a") != ["4242"]:
            return self._bytes(b"[]", "application/json")
        iframe = (f'<iframe src="http://127.0.0.1:{port}/embed?'
                  f'dt_embed={"hls" if mirror == 3 else "r2-mp4"}'
                  f'&#038;p=abc&#038;ep=4242"></iframe>')
        # The real endpoint answers one slot per mirror, indexed by i-1.
        items = [""] * mirror
        items[mirror - 1] = iframe
        self._bytes(json.dumps(items).encode(), "application/json")


class HentaimamaTv(BaseHTTPRequestHandler):
    """A tame hentaimama.tv-shaped site: the episode page frames the site's
    own player, whose stream URL only a live browser challenge can mint."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/succubus-yondara-gibo-ga-kita-episode-2/":
            body = ("<html><head><title>Episode - hentaimama</title></head>"
                    "<body><h1 class='text-whitegray'>Tame Show! Episode 2</h1>"
                    '<div class="aspect-w-16 aspect-h-9">'
                    '<iframe width="1280" height="672" '
                    'src="https://nhplayer.com/v/gY8v9AwDhorebIz/" '
                    'frameborder="0" allowfullscreen></iframe></div>'
                    "</body></html>").encode()
            return self._bytes(body, "text/html")
        if parsed.path == "/serie/abc/":
            return self._bytes(b"<html><h1>Series</h1></html>", "text/html")
        self._bytes(b"no", "text/plain", 404)


class Beeg(BaseHTTPRequestHandler):
    """A tame beeg-shaped API: facts JSON, a play token, and the master the
    token points at. Only the stripped id is served, so normalization is
    proven by the extraction succeeding at all."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/facts/file/452246185949825":
            body = json.dumps({
                "file": {"data": [{"cd_value": "Tame Beeg Video"}],
                         "fl_duration": 300, "fl_width": 1920, "fl_height": 1080},
            }).encode()
            return self._bytes(body, "application/json")
        if parsed.path == "/video/play_url/452246185949825":
            return self._bytes(
                b"key=abc,end=999,limit=10/data=xyz/media=hls4A/_TPL_/"
                b"452246185949825.mp4.m3u8", "text/plain")
        if parsed.path.startswith("/key="):
            body = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=688541,"
                    "RESOLUTION=426x240\n"
                    "//ip1.example/key=abc,end=999/data=xyz/media=hls4A/"
                    "240p/452246185949825.mp4.m3u8\n").encode()
            return self._bytes(body, "application/vnd.apple.mpegurl")
        self._bytes(b"no", "text/plain", 404)


class Rule34Video(BaseHTTPRequestHandler):
    """A tame rule34video-shaped page: the player block names three signed
    get_file renditions. The CDN hop itself is tested separately at the
    redirect-guard level."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        port = self.server.server_address[1]
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/video/4710544/ghislaine-hot-sex2/":
            body = (f"<html><head><title>Fixture</title></head><body>"
                    f"<h1 class='title_video'>Tame Clip</h1>"
                    f"<script>var playerData={{"
                    f"video_url: 'http://127.0.0.1:{port}/get_file/4710544_360.mp4/?v-acctoken=a',"
                    f"postfix: '_360.mp4',video_url_text: '360p',"
                    f"video_alt_url: 'http://127.0.0.1:{port}/get_file/4710544_480p.mp4/?v-acctoken=b',"
                    f"video_alt_url_text: '480p',"
                    f"video_alt_url2: 'http://127.0.0.1:{port}/get_file/4710544_720p.mp4/?v-acctoken=c',"
                    f"video_alt_url2_text: '720p',"
                    "duration: \"PT0H2M43S\"}};</script>"
                    "</body></html>").encode()
            return self._bytes(body, "text/html")
        self._bytes(b"no", "text/plain", 404)


class UniversalPage(BaseHTTPRequestHandler):
    """A tame universal-adapter host: one page whose meta tag names a media
    URL served on the same host. The universal layer must find it without the
    meta-URL validator rejecting loopback test URLs."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        port = self.server.server_address[1]
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/media.mp4":
            return self._bytes(b"\0" * 1200, "video/mp4")
        if parsed.path == "/page":
            body = (f'<html><head>'
                    f'<title>Universal Fixture</title>'
                    f'<meta property="og:video" '
                    f'content="http://127.0.0.1:{port}/media.mp4">'
                    f'</head><body>public video page</body></html>').encode()
            return self._bytes(body, "text/html")
        self._bytes(b"no", "text/plain", 404)

    do_HEAD = do_GET                        # noqa: N815


class TumblrPost(BaseHTTPRequestHandler):
    """A tumblish canonical embed: og:video names a native mp4, with its
    480p sibling available only by guessing the rendition suffix."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _bytes(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):                       # noqa: N802
        port = self.server.server_address[1]
        path = urllib.parse.urlparse(self.path).path
        if path == "/maskofthedragon/626907179849564160":
            body = (f'<html><head><meta property="og:title" content="mona talking">'
                    f'<meta property="og:video" '
                    f'content="http://127.0.0.1:{port}/media/native_720.mp4">'
                    f"</head></html>").encode()
            return self._bytes(body, "text/html")
        if path == "/prozdvoices/673201091169681408":
            body = (b'<html><head><meta property="og:title" content="voice acting">'
                    b'<meta property="og:video" '
                    b'content="https://youtube.test/watch?v=abc">'
                    b"</head></html>")
            return self._bytes(body, "text/html")
        if path == "/quietblog/626907179849564160":
            return self._bytes(b"<html><head><title>a text post</title>"
                               b"</head></html>", "text/html")
        if path == "/media/native_720.mp4":
            return self._bytes(b"\0" * 1400, "video/mp4")
        if path == "/media/native_480.mp4":
            return self._bytes(b"\0" * 800, "video/mp4")
        self._bytes(b"no", "text/plain", 404)

    do_HEAD = do_GET                       # noqa: N815


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
        # 0.17 player slice (was [4:20]); short mock paths give a shorter
        # key here, but the seal/decrypt pair must use the same slice.
        keyarg = _b64.b64encode(self.path.encode()).decode()[6:22]
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
                      "real-hls": "case-hls", "best-file": "case-file"}.get(
                slug, "case-file")
            if slug == "best-file":
                # bestjavporn markup: video-id/video_ver on the area, and
                # the mpu is sealed with the response salt, not the
                # request salt for this ver.
                mpu = self._seal("719679", "_0x58fe15", marker.encode())
                html = (f'<!doctype html><html><head><title>Test {slug}</title>'
                        f'</head><body><div id="video-player-area" '
                        f'video-id="719679" video_ver="1"><div id="video-player" '
                        f'data-mpu="{mpu}"></div></div></body></html>')
            else:
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

        # 5b — downloads are files, not something to play inline ---------------
        # An Android WebView that renders video/mp4 ignores Content-Disposition
        # and never starts the download; octet-stream + attachment is what makes
        # the browser's download manager take it.
        st, hd, body = fetch(bport, "/api/play" + q(page, fmt="720", dl="1"))
        check("download: a direct file is an attachment, not inline video",
              st == 200
              and hd.get("Content-Type") == "application/octet-stream"
              and "attachment" in hd.get("Content-Disposition", "")
              and len(body) == 7168,
              f"{st} {hd.get('Content-Type', '')} {len(body)}B")

        # A stitched stream has no single file to fetch: its download must be
        # muxed by /api/merge (video *and* audio), never one of the halves.
        for stitch in (server.MERGE_ID, server.HLSMUX + "137:140"):
            st, hd, _ = fetch(bport, "/api/play" + q(page, fmt=stitch, dl="1"))
            loc = hd.get("Location", "")
            check(f"download: {stitch} is handed to the muxer, not a half-stream",
                  st == 302 and "/api/merge?" in loc
                  and f"fmt={urllib.parse.quote(stitch, safe='')}" in loc,
                  f"{st} {loc[:90]}")

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
        # A master playlist points at media playlists through the same raw=1
        # entry point. Those responses are playlists by design and must be
        # rewritten, not mistaken for an expired segment (the old 502).
        st, hd, body = fetch(bport, "/api/hls?" + urllib.parse.urlencode(
            {"raw": "1", "u": f"{cdn_base}/master.m3u8",
             "url": page, "fmt": "hls"}))
        raw_text = body.decode("utf-8", "replace")
        check("hls: a raw playlist request is rewritten, not refused as expired",
              st == 200 and raw_text.startswith("#EXTM3U")
              and any(l.startswith("/api/hls") for l in raw_text.splitlines()),
              f"{st} {raw_text[:70]!r}")

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

        # 7b — universal fallback: yt-dlp failure falls through to pattern
        # probes, explicit adapters still come first, and the guard is unchanged.
        if server._universal is not None:
            uni_host = ThreadingHTTPServer(("127.0.0.1", 0), UniversalPage)
            uni_port = uni_host.server_address[1]
            threading.Thread(target=uni_host.serve_forever, daemon=True).start()
            real_fetch = server._fetch_html
            server._fetch_html = lambda url: real_fetch(url)
            server.BLOCK_PRIVATE = False
            old_usable = server._universal.usable_media_url

            def allows_loopback(raw, page):
                url = old_usable(raw, page)
                if url:
                    return url
                parsed = urllib.parse.urlparse(
                    urllib.parse.urljoin(page, raw)) if page and raw else None
                if parsed and (parsed.hostname or "") == "127.0.0.1":
                    return urllib.parse.urljoin(page, raw)
                return None

            server._universal.usable_media_url = allows_loopback
            try:
                uni_page = f"http://127.0.0.1:{uni_port}/page"
                server._extract = boom
                info = server._extract_sniffed(uni_page)
                entries = server.browsable_formats(info)
                check("universal: og:video resolves after yt-dlp fails",
                      info.get("extractor_key", "").startswith("universal:")
                      and len(entries) == 1
                      and entries[0]["url"].endswith("/media.mp4")
                      and entries[0]["needsProxy"] is True,
                      f"key={info.get('extractor_key')} "
                      f"entries={len(entries)}")
                check("universal: successful probe is counted",
                      server.STATS.get("universal", 0) >= 1,
                      str(server.STATS.get("universal")))
            finally:
                server._fetch_html = real_fetch
                server._universal.usable_media_url = old_usable
                uni_host.shutdown()
                uni_host.server_close()

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

        # problem reports: a prefilled GitHub issue with the log tail, redacted
        # by default. Never publishes anything; the browser does that.
        real_log = server.REPORT_LOG_FILE
        fd, report_log = tempfile.mkstemp(prefix="viewer-report-", suffix=".log")
        os.close(fd)
        with open(report_log, "w", encoding="utf-8") as fh:
            fh.write("https://cdn.test/v.mp4?token=secret&sig=abc\n"
                     "client 192.168.1.6:8080 asked\n"
                     "Authorization: Bearer ghp_" + "a" * 30 + "\n"
                     "[http] \"GET /api/info?url=https%3A%2F%2Fcdn.test"
                     "%2Fv.mp4%3Ftoken%3Dencoded&fresh=1\" 200\n"
                     "[ui] could not play\n")
        try:
            server.REPORT_LOG_FILE = report_log
            page_q = urllib.parse.quote(
                "https://sulasokvids.icu/watch.php?id=x&token=leak", safe="")
            st, _, raw = fetch(
                bport, f"/api/report?url={page_q}&msg=Unsupported+URL+"
                       "https%3A%2F%2Ferr.test%2Fa%3Ftoken%3Dmsgleak")
            draft = json.loads(raw or b"{}")
            body = draft.get("body") or ""
            check("report: the draft targets the repo's new-issue page",
                  st == 200 and draft.get("url", "").startswith(
                      "https://github.com/james3124/video-viewer/issues/new?")
                  and draft.get("redacted") is True,
                  f"{st} {draft.get('url', '')[:60]}")
            check("report: query strings and tokens are stripped",
                  "https://cdn.test/v.mp4" in body
                  and "token=secret" not in body
                  and "sig=abc" not in body
                  and "token=leak" not in body, "")
            check("report: encoded query strings in log lines are stripped",
                  "token%3Dencoded" not in body
                  and "fresh=1" not in body, "")
            check("report: a URL inside the error message is redacted too",
                  "msgleak" not in body and "https://err.test/a" in body, "")
            check("report: LAN addresses and GitHub tokens are masked",
                  "192.168.1.6" not in body and "<local>" in body
                  and "ghp_" not in body, "")
            check("report: the log tail is attached",
                  "Recent server log" in body and "could not play" in body, "")
            st, _, raw = fetch(
                bport, f"/api/report?url={page_q}&msg=Unsupported+URL&raw=1")
            raw_body = (json.loads(raw or b"{}").get("body") or "")
            check("report: raw=1 keeps the URL when the user opts in",
                  "token=leak" in raw_body and "token=secret" in raw_body, "")
        finally:
            server.REPORT_LOG_FILE = real_log
            os.unlink(report_log)
        real_rl = server.REPORT_LIMIT
        try:
            server.REPORT_LIMIT = server.SlidingWindow(1)
            st, _, _ = fetch(bport, "/api/report?msg=one")
            check("report: first draft is allowed", st == 200, str(st))
            st, _, _ = fetch(bport, "/api/report?msg=two")
            check("report: report flood is capped", st == 429, str(st))
        finally:
            server.REPORT_LIMIT = real_rl

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

        # 10c2 — split-rendition HLS (video playlist + audio playlist) --------
        split_info = {"id": "hh", "title": "t", "url": "http://hh.test/watch/1",
                      "duration": 394, "formats": [
                          {"format_id": "hls-2077", "ext": "mp4",
                           "protocol": "m3u8_native", "vcodec": "avc1.4d4028",
                           "acodec": "none", "height": 720, "width": 1280,
                           "url": "https://cdn.test/7sop/v.m3u8"},
                          {"format_id": "hls-800", "ext": "mp4",
                           "protocol": "m3u8_native", "vcodec": "avc1.4d4028",
                           "acodec": "none", "height": 360, "width": 640,
                           "url": "https://cdn.test/3cop/v.m3u8"},
                          # yt-dlp reports no acodec at all for a media-only
                          # audio playlist: vcodec "none", acodec null.
                          {"format_id": "hls-audio-English", "ext": "mp4",
                           "protocol": "m3u8_native", "vcodec": "none",
                           "acodec": None, "resolution": "audio only",
                           "url": "https://cdn.test/snd/a.m3u8"},
                      ]}
        split = server.browsable_formats(split_info)
        audio_fmt = next((f for f in split if f["id"] == "hls-audio-English"), None)
        check("hls split: a vcodec=none rendition is audio, not nothing",
              audio_fmt is not None and audio_fmt["hasAudio"] is True
              and audio_fmt["hasVideo"] is False, str(audio_fmt))
        muxed = [f for f in split if str(f["id"]).startswith(server.HLSMUX)]
        check("hls split: each video rendition gets a video+audio entry",
              sorted(f["height"] for f in muxed) == [360, 720]
              and all(f["hasAudio"] and f["hasVideo"] for f in muxed),
              str([(f["id"], f["height"]) for f in muxed]))
        check("hls split: the mux entry always goes through the proxy",
              bool(muxed) and all(f["needsProxy"] for f in muxed),
              str([(f["id"], f["needsProxy"]) for f in muxed]))
        pick = server.best_format(split)
        check("hls split: the default is the one with sound",
              pick is not None and pick.get("hasAudio") is True
              and str(pick["id"]).startswith(server.HLSMUX),
              str(pick and pick["id"]))
        master = server.hls_master_text(
            "https://cdn.test/7sop/v.m3u8", "https://cdn.test/snd/a.m3u8",
            width=1280, height=720, vcodec="avc1.4d4028")
        check("hls split: the synthesized master names the audio group",
              master.startswith("#EXTM3U")
              and 'TYPE=AUDIO' in master
              and 'URI="https://cdn.test/snd/a.m3u8"' in master
              and 'AUDIO="aud"' in master
              and master.rstrip().endswith("https://cdn.test/7sop/v.m3u8")
              and 'CODECS="avc1.4d4028,mp4a.40.2"' in master,
              master.replace("\n", " | ")[:160])

        # 10c3 — generic embed: null/null MP4s with the quality in the name -----
        generic = server.browsable_formats(
            {"id": "tna", "title": "t", "url": "https://tna.test/v/1", "formats": [
                {"format_id": "0", "ext": "mp4", "protocol": "https",
                 "vcodec": None, "acodec": None,
                 "url": "https://cdn.test/clip-1080p_60fps.mp4?rs=1"},
                {"format_id": "1", "ext": "mp4", "protocol": "https",
                 "vcodec": None, "acodec": None,
                 "url": "https://cdn.test/clip-720p_60fps.mp4?rs=1"},
            ]})
        check("formats: an unsized null/null mp4 is video+audio, not audio only",
              len(generic) == 2
              and all(e["hasVideo"] and e["hasAudio"] for e in generic)
              and [e["height"] for e in generic] == [1080, 720]
              and all(e["fps"] == 60 for e in generic)
              and generic[0]["label"].startswith("1080p"),
              str([(e["height"], e["fps"], e["hasVideo"], e["hasAudio"],
                    e["label"]) for e in generic]))
        check("formats: generic mp4 quality is the default (not an audio track)",
              server.best_format(generic) is not None
              and server.best_format(generic)["id"] == "0", "")

        # 10d — sulasokvid adapter against a tame embed host ---------------------
        sula = ThreadingHTTPServer(("127.0.0.1", 0), Sula)
        sport = sula.server_address[1]
        threading.Thread(target=sula.serve_forever, daemon=True).start()
        try:
            check("sulasok: registered as a player site",
                  "sulasokvid.co" in server._PLAYER_SITES
                  and "sulasokvids.icu" in server._PLAYER_SITES, "")
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

        # 10d1 — sulasokvid mirror fallback: the default player is dead, a
        # later mirror (doodstream pass_md5) still has the file. The adapter
        # must walk the mirrors instead of trusting the first embed.
        mir = ThreadingHTTPServer(("127.0.0.1", 0), SulaMirrors)
        mport = mir.server_address[1]
        threading.Thread(target=mir.serve_forever, daemon=True).start()
        try:
            got = server.sulasokvid_extract(
                f"http://127.0.0.1:{mport}/watch.php?id=mir")
            fmt = (got.get("formats") or [{}])[0]
            check("sulasok: a dead mirror falls through to a live one",
                  got.get("title") == "Mirror Cat"
                  and fmt.get("format_id") == "doodstream"
                  and fmt.get("url", "").startswith(
                      f"http://127.0.0.1:{mport}/media/")
                  and "token=zzz&expiry=" in fmt.get("url", ""),
                  f"title={got.get('title')!r} fmt={fmt.get('format_id')!r} "
                  f"url={fmt.get('url', '')[:70]!r}")
            check("sulasok: a doodstream mp4 keeps its Referer",
                  fmt.get("http_headers", {}).get("Referer", "")
                  .endswith("/e/doodok"), "")
            entries = server.browsable_formats(
                {"id": "m", "title": "t", "url": got["webpage_url"],
                 "formats": [fmt]})
            check("sulasok: the doodstream mp4 is proxied",
                  len(entries) == 1 and entries[0]["kind"] == "direct"
                  and entries[0]["needsProxy"] is True,
                  f"kind={entries[0]['kind'] if entries else None}")
        finally:
            mir.shutdown()
            mir.server_close()

        # 10e — hentaimama: post id → admin-ajax mirror → base64 embed → JW
        # Player sources. Mirror 5 is progressive mp4, mirror 3 is the HLS
        # master; the listing page must say so instead of burning yt-dlp's time.
        hm = ThreadingHTTPServer(("127.0.0.1", 0), Hentaimama)
        hmport = hm.server_address[1]
        threading.Thread(target=hm.serve_forever, daemon=True).start()
        try:
            check("hentaimama: registered as a player site",
                  "hentaimama.io" in server._PLAYER_SITES, "")
            try:
                server.hentaimama_extract(
                    f"http://127.0.0.1:{hmport}/playlist/abc/")
                check("hentaimama: a playlist page says what it is", False,
                      "no raise")
            except server.NotAVideoPage:
                check("hentaimama: a playlist page says what it is", True, "")
            except Exception as exc:  # noqa: BLE001
                check("hentaimama: a playlist page says what it is", False,
                      f"wrong error: {type(exc).__name__}")
            try:
                got = server.hentaimama_extract(
                    f"http://127.0.0.1:{hmport}/playlist/abc/ep1/")
            except Exception as exc:  # noqa: BLE001
                check("hentaimama: the episode resolves through its mirrors",
                      False, f"{type(exc).__name__}: {str(exc)[:80]}")
            else:
                fmts = got.get("formats") or []
                check("hentaimama: the episode resolves through its mirrors",
                      got.get("title") == "Tame Show - Episode 1"
                      and [f.get("format_id") for f in fmts] ==
                      ["hm-1080", "hm-720", "hm-hls"],
                      f"title={got.get('title')!r} "
                      f"ids={[f.get('format_id') for f in fmts]}")
                check("hentaimama: mp4 renditions keep their Referer",
                      fmts and all(
                          f["http_headers"]["Referer"].startswith(
                              f"http://127.0.0.1:{hmport}/embed?") for f in fmts),
                      fmts[0].get("http_headers", {}).get("Referer", "")
                      if fmts else "")
                entries = server.browsable_formats(
                    {"id": "h", "title": "t", "url": got.get("webpage_url"),
                     "formats": fmts})
                check("hentaimama: the sources become playable, proxied formats",
                      [e["kind"] for e in entries] == ["direct", "direct", "hls"]
                      and all(e["needsProxy"] for e in entries)
                      and entries[0]["label"].startswith("1080p")
                      and entries[2]["ext"] == "m3u8",
                      str([(e["kind"], e["label"], e["needsProxy"])
                           for e in entries]))
        finally:
            hm.shutdown()
            hm.server_close()

        # 10e1 — hentaimama.tv: the episode frames the site's own player, whose
        # signed URL is minted by a live browser challenge. The adapter must
        # not forge that: it hands the frame over as an embed instead.
        hmtv = ThreadingHTTPServer(("127.0.0.1", 0), HentaimamaTv)
        hmtvport = hmtv.server_address[1]
        threading.Thread(target=hmtv.serve_forever, daemon=True).start()
        try:
            check("hentaimama.tv: registered as a player site",
                  "hentaimama.tv" in server._PLAYER_SITES, "")
            try:
                server.hentaimama_tv_extract(
                    f"http://127.0.0.1:{hmtvport}/serie/abc/")
                check("hentaimama.tv: a series page says what it is", False,
                      "no raise")
            except server.NotAVideoPage:
                check("hentaimama.tv: a series page says what it is", True, "")
            except Exception as exc:  # noqa: BLE001
                check("hentaimama.tv: a series page says what it is", False,
                      f"wrong error: {type(exc).__name__}")
            try:
                real_fetch = server._fetch_html

                def fake_fetch(u):
                    if "nhplayer.com" in u:
                        return (u, '<ul><li data-id="/player.php?vid=TEST">'
                                   'Main Player</li></ul>')
                    return real_fetch(u)

                server._fetch_html = fake_fetch
                try:
                    got = server.hentaimama_tv_extract(
                        f"http://127.0.0.1:{hmtvport}"
                        "/succubus-yondara-gibo-ga-kita-episode-2/")
                finally:
                    server._fetch_html = real_fetch
            except Exception as exc:  # noqa: BLE001
                check("hentaimama.tv: the episode offers the site player",
                      False, f"{type(exc).__name__}: {str(exc)[:80]}")
            else:
                check("hentaimama.tv: the episode offers the site player",
                      got.get("title") == "Tame Show! Episode 2"
                      and got.get("embed") ==
                      "https://nhplayer.com/player.php?vid=TEST"
                      and got.get("formats") == [],
                      f"title={got.get('title')!r} embed={got.get('embed')!r}")
        finally:
            hmtv.shutdown()
            hmtv.server_close()

        # 10e3 — beeg: yt-dlp asks `facts/file` for hls_resources the API no
        # longer returns. The adapter follows the site's own two-call flow:
        # facts for metadata, play_url for the token that names the master.
        beeg = ThreadingHTTPServer(("127.0.0.1", 0), Beeg)
        beegport = beeg.server_address[1]
        threading.Thread(target=beeg.serve_forever, daemon=True).start()
        try:
            check("beeg: registered as a player site",
                  "beeg.com" in server._PLAYER_SITES, "")
            old_store, old_cdn = server._BEEG_STORE, server._BEEG_CDN
            server._BEEG_STORE = f"http://127.0.0.1:{beegport}"
            server._BEEG_CDN = f"http://127.0.0.1:{beegport}"
            try:
                got = server.beeg_extract("https://beeg.com/-0452246185949825")
            finally:
                server._BEEG_STORE, server._BEEG_CDN = old_store, old_cdn
            fmts = got.get("formats") or []
            check("beeg: the leading-zero id becomes a signed master",
                  got.get("title") == "Tame Beeg Video"
                  and got.get("duration") == 300
                  and len(fmts) == 1
                  and fmts[0]["url"].endswith(
                      "/key=abc,end=999,limit=10/data=xyz/media=hls4A/"
                      "_TPL_/452246185949825.mp4.m3u8")
                  and fmts[0]["protocol"] == "m3u8_native",
                  f"title={got.get('title')!r} "
                  f"url={(fmts[0]['url'] if fmts else '')[-70:]}")
            entries = server.browsable_formats(
                {"id": "b", "title": "t", "url": "https://beeg.com/-0452",
                 "formats": fmts})
            check("beeg: the master becomes a proxied HLS format",
                  [e["kind"] for e in entries] == ["hls"]
                  and entries[0]["needsProxy"] is True,
                  str([(e["kind"], e["needsProxy"]) for e in entries]))
        finally:
            beeg.shutdown()
            beeg.server_close()

        # 10e4 — blogger/gogoanime: the episode page frames a Blogger player
        # whose bootstrap data holds signed muxed googlevideo URLs; yt-dlp's
        # Blogger extractor no longer parses it. Other embed hosts fall back
        # to the frame itself, like hentaimama.tv.
        blogger_html = (
            '<html><script>window["cfg"] = "{\\"streamingData\\":{\\"formats\\":'
            '[{\\"itag\\":18,\\"url\\":\\"https://rr1.googlevideo.com/videoplayback'
            '?expire\\u003d1\\u0026itag\\u003d18\\u0026mime\\u003dvideo/mp4'
            '\\u0026x\\u003d1\\"},'
            '{\\"itag\\":22,\\"url\\":\\"https://rr1.googlevideo.com/videoplayback'
            '?expire\\u003d1\\u0026itag\\u003d22\\u0026mime\\u003dvideo/mp4'
            '\\u0026x\\u003d2\\"}]}}";</script></html>')

        check("gogoanime: registered as a player site",
              "gogoanime.fi" in server._PLAYER_SITES
              and "gogoanime3.co" in server._PLAYER_SITES
              and "blogger.com" in server._PLAYER_SITES, "")

        got = server.blogger_extract(
            "https://www.blogger.com/video.g?token=TEST", html=blogger_html)
        fmts = got.get("formats") or []
        check("blogger: signed muxed renditions decode from the bootstrap data",
              [f.get("format_id") for f in fmts] == ["blogger-22", "blogger-18"]
              and all(f["http_headers"] == {"User-Agent": server.DEFAULT_UA}
                      for f in fmts),
              f"ids={[f.get('format_id') for f in fmts]}")
        entries = server.browsable_formats(
            {"id": "b", "title": "t", "url": "https://blogger.test/x",
             "formats": fmts})
        check("blogger: renditions are direct, so the phone fetches the bytes",
              [e["kind"] for e in entries] == ["direct", "direct"]
              and not any(e["needsProxy"] for e in entries),
              str([(e["id"], e["needsProxy"]) for e in entries]))

        episode_html = (
            '<html><body><h1>Suikoden: The Anime Episode 5</h1>'
            '<iframe src="https://www.blogger.com/video.g?token=TEST"></iframe>'
            "</body></html>")
        real_fetch_text = server._fetch_text
        server._fetch_text = lambda u, h, **kw: blogger_html if "blogger.com" in u else real_fetch_text(u, h, **kw)
        try:
            got = server.gogoanime_extract(
                "https://gogoanime.fi/suikoden-episode-5/", html=episode_html)
        finally:
            server._fetch_text = real_fetch_text
        check("gogoanime: the blogger embed resolves through the episode page",
              got.get("extractor_key") == "gogoanime"
              and got.get("title") == "Suikoden: The Anime Episode 5"
              and [f.get("format_id") for f in (got.get("formats") or [])]
              == ["blogger-22", "blogger-18"],
              f"title={got.get('title')!r}")

        got = server.gogoanime_extract(
            "https://gogoanime.fi/whatever-episode-1/",
            html='<html><h1>Other Embed</h1><iframe src="https://upbolt.to/e/abc"></iframe></html>')
        check("gogoanime: an unreducible embed is offered as an embed",
              got.get("embed") == "https://upbolt.to/e/abc"
              and got.get("formats") == [],
              f"embed={got.get('embed')!r}")

        # 10e5 — hianimes: the zokoanime player ships window.__P, base64 of an
        # XOR'd JSON blob; the adapter runs the same decode its own player does.
        def zoko_blob(obj):
            payload = json.dumps(obj, separators=(",", ":")).encode()
            key = b"otaku-embed-v1"
            xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(payload))
            return base64.b64encode(xored).decode()

        check("hianimes: registered as a player site",
              "hianimes.se" in server._PLAYER_SITES
              and "zokoanime.video" in server._PLAYER_SITES, "")

        player_html = ('<html><script>window.__P="' + zoko_blob({
            "src": "https://hls.test/master.m3u8",
            "subtitles": [{"lang": "en", "label": "English (SDH)",
                           "default": True,
                           "src": "https://hls.test/subs/en.vtt"}],
        }) + '"</script></html>')
        watch_html = ('<html><h1>Primal Episode 1</h1>'
                      '<iframe src="https://zokoanime.video/stream/anime/1/dub'
                      '?autostart=true&amp;asi=0"></iframe></html>')
        real_fetch_text = server._fetch_text
        server._fetch_text = (lambda u, h, **kw: player_html
                              if "zokoanime" in u else real_fetch_text(u, h, **kw))
        try:
            got = server.hianimes_extract(
                "https://hianimes.se/watch/primal-episode-1-ep-1-59jrf5-udqmm",
                html=watch_html)
        finally:
            server._fetch_text = real_fetch_text
        fmts = got.get("formats") or []
        check("hianimes: the blob names the HLS master",
              got.get("extractor_key") == "hianimes"
              and [f.get("format_id") for f in fmts] == ["zoko-hls"]
              and fmts[0]["url"] == "https://hls.test/master.m3u8"
              and fmts[0]["http_headers"]["Referer"].startswith(
                  "https://zokoanime.video/stream/anime/1/dub"),
              f"urls={[f.get('url') for f in fmts]}")
        check("hianimes: subtitles survive the decode",
              ((got.get("subtitles") or {}).get("en") or [{}])[0].get("url")
              == "https://hls.test/subs/en.vtt",
              (got.get("subtitles") and json.dumps(got["subtitles"])[:90]) or "missing")

        # 10e7 — kisskh.co: anonymous JSON APIs + a deterministic AES kkey,
        # ported straight from the site's own derivation.
        check("kisskh: registered as a player site",
              "kisskh.co" in server._PLAYER_SITES
              and "t.kisskh.id" in server._PLAYER_SITES, "")

        check("kisskh: kkey matches the site's own (video)",
              server._kisskh_kkey(228668, server._KISSKH_VI_GUID) == (
                  "CBB770E89E45CAF856407F2394F9D3BE4B36D037A024ABAA00B7A126845306C"
                  "F01B7AA4FC021A9D02CD7E196646AE8DA03113C362039A7F93603315048123B8"
                  "73FA611925BF9C9570C69CF43BBE28E6705F4260060EE1AB1CAC36DAA3CB36B8"
                  "FA3921F6B6D394B334F3FBEC5DFE110C818BC1ABFACC3CDFB6121862B6ACC9FB8"),
              "")
        check("kisskh: kkey matches the site's own (subtitles)",
              server._kisskh_kkey(228668, server._KISSKH_SUB_GUID) == (
                  "A5938838F3D2DD75CB4508CDE7A7F24FC7FC326085A867E1631740A4C1AE9A"
                  "6741FADE11A4B3854F7273BEDF0568EC0864ADB2DC7343ECBDF87CC0DB9DBC0"
                  "E6CCADDE1027F067E3ED66746CC2FFB4F5051D80B9C4C3955533E47EA95420A0"
                  "A3C2659CD95AB64CC1AC856C35CC1AC7E532E333B7FAAB2C00F340E6B4299C525B5"),
              "")

        drama_json = {"id": 14105, "title": "Resident Evil (2026)",
                      "episodes": [{"id": 228668, "number": 1.0, "sub": 5}],
                      "episodesCount": 1}
        episode_json = {"Video": "https://media.test/resident-evil/index.m3u8?v=abc123",
                        "Video_tmp": "", "ThirdParty": None, "Type": 1}
        subtitle_json = [
            {"src": "https://subs.test/14105/1/aa.ar.srt", "label": "Arabic",
             "land": "ar", "default": False},
            {"src": "https://subs.test/14105/1/aa.en.srt", "label": "English",
             "land": "en", "default": True},
        ]

        def fake_json_get(url, headers, timeout=25):
            if "/api/DramaList/Drama/14105" in url:
                return drama_json
            if "/api/DramaList/Episode/228668.png" in url:
                # the kkey has to ride along, or the real API refuses
                return episode_json if "kkey=" in url else ("error", 403)
            if "/api/Sub/228668" in url:
                return subtitle_json if "kkey=" in url else ("error", 403)
            return ("error", 404)

        real_json_get = server._json_get
        server._json_get = fake_json_get
        try:
            got = server.kisskh_extract(
                "https://kisskh.co/Drama/Resident-Evil--2026-/Episode-1"
                "?id=14105&ep=228668")
        finally:
            server._json_get = real_json_get
        fmts = got.get("formats") or []
        check("kisskh: HLS master resolved through the episode API",
              got.get("extractor_key") == "kisskh"
              and [f.get("format_id") for f in fmts] == ["kisskh-hls"]
              and fmts[0]["url"] == (
                  "https://media.test/resident-evil/index.m3u8?v=abc123")
              and fmts[0]["http_headers"]["Referer"].startswith(
                  "https://kisskh.co"),
              f"urls={[f.get('url') for f in fmts]}")
        check("kisskh: title carries drama and episode number",
              got.get("title") == "Resident Evil (2026) — Episode 1",
              repr(got.get("title")))
        subs = got.get("subtitles") or {}
        check("kisskh: subtitle tracks survive (ar/en)",
              set(subs) == {"ar", "en"}
              and subs["en"][0]["ext"] == "srt"
              and subs["en"][0]["url"].endswith(".en.srt"),
              json.dumps({k: len(v) for k, v in subs.items()}))
        try:
            server.kisskh_extract("https://kisskh.co/Drama/resident-evil-2026")
            check("kisskh: listing page raises NotAVideoPage", False)
        except server.NotAVideoPage:
            check("kisskh: listing page raises NotAVideoPage", True)

        # The shortlink host is an alias: it must follow the redirect, read
        # id/ep from the canonical URL, and still answer through the adapter.
        real_resolve = server._kisskh_resolve
        server._kisskh_resolve = (lambda u:
                                  "https://kisskh.co/Drama/Resident-Evil--2026-/"
                                  "Episode-1?id=14105&ep=228668")
        server._json_get = fake_json_get
        try:
            got2 = server.kisskh_extract("https://t.kisskh.id/UY7R")
        finally:
            server._kisskh_resolve = real_resolve
            server._json_get = real_json_get
        check("kisskh: shortlink resolves to the canonical episode",
              (got2.get("formats") or [{}])[0].get("url", "").endswith(
                  "index.m3u8?v=abc123")
              and got2.get("title") == "Resident Evil (2026) — Episode 1",
              repr(got2.get("title")))

        if server._universal is not None:
            ld = ('<script type="application/ld+json">'
                  '{"@type":"VideoObject","url":"https://site.test/author/me",'
                  '"author":{"url":"https://site.test/author/me"},'
                  '"contentUrl":"https://cdn.test/v.mp4"}</script>')
            urls = [u for u, _k in server._universal._ld_json_urls(ld)]
            check("universal: JSON-LD no longer offers page URLs as media",
                  urls == ["https://cdn.test/v.mp4"], str(urls))

        # 10e2 — rule34video: the signed rendition tiers are already in the
        # player data; yt-dlp stops at them. The adapter publishes them and
        # names the CDN hop it expects, so the media guard stays deny-by-default.
        r34 = ThreadingHTTPServer(("127.0.0.1", 0), Rule34Video)
        r34port = r34.server_address[1]
        threading.Thread(target=r34.serve_forever, daemon=True).start()
        try:
            got = server.rule34video_extract(
                f"http://127.0.0.1:{r34port}/video/4710544/ghislaine-hot-sex2/")
            fmts = got.get("formats") or []
            check("rule34video: signed renditions resolve in quality order",
                  got.get("title") == "Tame Clip"
                  and got.get("duration") == 163
                  and [f.get("format_id") for f in fmts] ==
                  ["r34-720", "r34-480", "r34-360"]
                  and all(f.get("_redirect_allow") ==
                          ("boomio-cdn.com",) for f in fmts),
                  f"ids={[f.get('format_id') for f in fmts]} "
                  f"dur={got.get('duration')}")
            entries = server.browsable_formats(
                {"id": "r", "title": "t", "url": got.get("webpage_url"),
                 "formats": fmts})
            check("rule34video: browseable formats stay proxied",
                  [e["id"] for e in entries] == ["r34-720", "r34-480", "r34-360"]
                  and all(e["needsProxy"] for e in entries),
                  str([(e["id"], e.get("redirectAllow")) for e in entries]))
        finally:
            r34.shutdown()
            r34.server_close()

        # 10d1a — vimeo: the watch page wants a login, the public player
        # embed does not. The adapter rewrites, it does not re-implement.
        real_extract = server._extract
        seen: dict = {}

        def fake_extract(u, *a, **kw):
            seen["url"] = u
            return {"title": "Vimeo Video", "extractor_key": "Vimeo",
                    "formats": [{"format_id": "hls-1",
                                 "url": "https://cdn.test/v.m3u8",
                                 "ext": "mp4", "protocol": "m3u8_native",
                                 "http_headers": {}}],
                    "webpage_url": u}

        check("vimeo: registered as a player site",
              "vimeo.com" in server._PLAYER_SITES, "")
        try:
            server._extract = fake_extract
            got = server.vimeo_extract("https://vimeo.com/76979871")
            check("vimeo: the watch page is resolved via the player embed",
                  seen.get("url") == "https://player.vimeo.com/video/76979871"
                  and got.get("webpage_url") == "https://vimeo.com/76979871",
                  f"called={seen.get('url')!r}")
            check("vimeo: an already-canonical player URL is left to yt-dlp",
                  server.vimeo_extract(
                      "https://player.vimeo.com/video/76979871") is None, "")
            try:
                server.vimeo_extract("https://vimeo.com/staff")
                check("vimeo: a channel page says so", False, "no raise")
            except server.NotAVideoPage:
                check("vimeo: a channel page says so", True, "")
        finally:
            server._extract = real_extract

        # 10d1b — tumblr: the blog subdomain 403s, the canonical www embed
        # carries og:video; native mp4s get their 480p sibling probed.
        tum = ThreadingHTTPServer(("127.0.0.1", 0), TumblrPost)
        tport = tum.server_address[1]
        threading.Thread(target=tum.serve_forever, daemon=True).start()
        real_embed = server._TUMBLR_EMBED_URL
        server._TUMBLR_EMBED_URL = (
            f"http://127.0.0.1:{tport}/{{blog}}/{{post_id}}?is_embed=true")
        try:
            check("tumblr: registered as a player site",
                  "tumblr.com" in server._PLAYER_SITES, "")
            check("tumblr: www post URLs split into blog + id",
                  server._tumblr_post_ids(urllib.parse.urlparse(
                      "https://www.tumblr.com/maskofthedragon/626907179849564160"))
                  == ("maskofthedragon", "626907179849564160"), "")
            got = server.tumblr_extract(
                "https://maskofthedragon.tumblr.com/post/626907179849564160/"
                "mona-talking-in-english")
            heights = [f.get("height") for f in got.get("formats") or []]
            check("tumblr: og:video resolves a native mp4 and its 480p sibling",
                  heights == [720, 480]
                  and got["formats"][0].get("filesize") == 1400
                  and got["formats"][0].get("_sniffed") is True,
                  f"heights={heights}")
            seen.clear()
            try:
                server._extract = fake_extract
                got = server.tumblr_extract(
                    "https://prozdvoices.tumblr.com/post/673201091169681408/"
                    "what-recording-voice-acting-sounds-like")
                check("tumblr: an embedded provider is handed to yt-dlp",
                      seen.get("url") == "https://youtube.test/watch?v=abc"
                      and got.get("webpage_url", "").startswith(
                          "https://prozdvoices.tumblr.com/post/"),
                      f"called={seen.get('url')!r}")
            finally:
                server._extract = real_extract
            try:
                server.tumblr_extract(
                    "https://quietblog.tumblr.com/post/626907179849564160")
                check("tumblr: a post without video says so", False, "no raise")
            except server.NotAVideoPage:
                check("tumblr: a post without video says so", True, "")
            check("tumblr: a direct media URL is not treated as a post",
                  server.tumblr_extract(
                      "https://va.media.tumblr.com/tumblr_x_720.mp4") is None, "")
        finally:
            server._TUMBLR_EMBED_URL = real_embed
            tum.shutdown()
            tum.server_close()

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
            check("bestjavporn: registered as a player-site adapter",
                  "bestjavporn.com" in server._PLAYER_SITES, "")
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

            try:
                got = server.javhdporn_extract(f"{jbase}/video/best-file")
                fmts = got.get("formats") or []
                check("bestjavporn: video-id/video_ver markup resolves",
                      len(fmts) == 1 and fmts[0]["ext"] == "mp4"
                      and fmts[0]["url"].endswith("/real.mp4"),
                      str([(f["format_id"], f["ext"]) for f in fmts]))
            except Exception as exc:  # noqa: BLE001
                check("bestjavporn: video-id/video_ver markup resolves", False,
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
                          st == 200
                          and hd.get("Content-Type") == "application/octet-stream"
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
                          st == 200
                          and hd.get("Content-Type") == "application/octet-stream"
                          and not body
                          and "attachment" in hd.get("Content-Disposition", ""),
                          f"{st} {bool(body)}")
                    # Playback of a merge from a split-HLS source must use the
                    # HLS route: ffmpeg reading the progressive URLs open-ended
                    # is throttled by googlevideo to ~0.5x realtime.
                    st, hd, body = fetch(
                        bport, f"/api/merge?{urllib.parse.urlencode({'url': page, 'fmt': 'h1'})}")
                    check("merge: playback of an HLS source transmuxes, not "
                          "progressive-reads",
                          st == 200 and hd.get("Content-Type") == "video/mp4"
                          and b"ftyp" in body[:32],
                          f"{st} {len(body)}B {hd.get('Content-Type', '')}")
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

        # 11a — a plain .vtt URL that only answers with the player's Referer
        # (dramahot). yt-dlp downloads nothing for a direct .vtt, so the
        # server must fetch it itself and send the referer the adapter knew.
        class SubServer(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):                       # noqa: N802
                ref = self.headers.get("Referer") or ""
                if "player.test" not in ref:
                    body = b"denied"
                    self.send_response(403)
                else:
                    body = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
                    self.send_response(200)
                self.send_header("Content-Type", "text/vtt")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        subSrv = ThreadingHTTPServer(("127.0.0.1", 0), SubServer)
        subport = subSrv.server_address[1]
        threading.Thread(target=subSrv.serve_forever, daemon=True).start()
        try:
            gate_url = f"http://127.0.0.1:{subport}/subs/only.vtt"
            check("subs: a referer-gated vtt is refused without the referer",
                  server.cached_vtt(gate_url) is None, "")
            vtt_path = server.cached_vtt(gate_url, referer="https://player.test/embed")
            check("subs: with the player referer it is fetched and cached",
                  vtt_path is not None
                  and open(vtt_path, encoding="utf-8").read().startswith("WEBVTT"),
                  vtt_path or "(none)")
            check("subs: the cached copy is served even without asking upstream",
                  server.cached_vtt(gate_url) is not None, "")
        finally:
            subSrv.shutdown()
            subSrv.server_close()
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

        # 16d — declarative extra-site recipes --------------------------------
        ext = server._extra
        check("extra-site: sites_extra is importable", ext is not None)
        if ext is not None:
            saved = dict(ext.SITES)
            real_fetch = server._fetch_text
            real_extract = server._extract
            try:
                ext.SITES.clear()
                ext._add(ext.SiteRule(host="extra.test", strategy="meta"))
                ext._add(ext.SiteRule(host="video.extra.test", strategy="player"))
                ext._add(ext.SiteRule(
                    host="api.test", strategy="api",
                    api="/api/info?id={id}&alt=0",
                    api_id=r'episodeToken\s*=\s*"([^"]+)"',
                    api_token=r'csrfToken\s*=\s*"([^"]+)"'))
                server._EXTRA_SITES = ext.SITES
                server._EXTRA_HOSTS = set(ext.SITES)

                check("extra-site: exact host resolves",
                      ext.rule_for("https://extra.test/watch/1").host == "extra.test")
                check("extra-site: a subdomain uses the parent recipe",
                      ext.rule_for("https://cdn.extra.test/watch/1").host == "extra.test")
                check("extra-site: the longest suffix wins",
                      ext.rule_for("https://video.extra.test/watch/1").strategy == "player")
                check("extra-site: unknown hosts are left alone",
                      ext.rule_for("https://example.com/x") is None)

                page = ('<html><head><title>Example Clip</title>'
                        '<meta property="og:video" content="/media/clip.mp4">'
                        '</head></html>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="meta"),
                                  "https://extra.test/watch/1", page)
                check("extra-site: og:video becomes a format",
                      bool(got["formats"]) and got["formats"][0]["url"]
                      == "https://extra.test/media/clip.mp4", str(got["formats"]))
                check("extra-site: the title is read without tags",
                      got["title"] == "Example Clip", str(got["title"]))

                ld = ('<script type="application/ld+json">{"@type":"VideoObject",'
                      '"contentUrl":"https://cdn.extra.test/1080.mp4",'
                      '"name":"Clip"}</script>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="jsonld"),
                                  "https://extra.test/watch/1", ld)
                check("extra-site: JSON-LD VideoObject contentUrl is read",
                      bool(got["formats"])
                      and got["formats"][0]["url"].endswith("1080.mp4"),
                      str(got["formats"]))

                html5 = ('<video><source src="https://cdn.extra.test/720.mp4" '
                         'type="video/mp4"><source src="https://cdn.extra.test/480.mp4">'
                         '</video>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="html5"),
                                  "https://extra.test/watch/1", html5)
                check("extra-site: <source> tags become formats",
                      len(got["formats"]) == 2, str(got["formats"]))

                player = ('<script>var player = {sources: [{file: '
                          '"https://cdn.extra.test/master.m3u8", label: "1080p"}, '
                          '{file: "https://cdn.extra.test/720.mp4", label: "720p"}]};'
                          '</script>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="player"),
                                  "https://extra.test/watch/1", player)
                check("extra-site: a JW Player source list is read",
                      len(got["formats"]) == 2
                      and got["formats"][0]["ext"] == "m3u8",
                      str([(f["url"], f["ext"]) for f in got["formats"]]))

                ifr = '<iframe src="https://player.extra.test/embed/1"></iframe>'
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="iframe"),
                                  "https://extra.test/watch/1", ifr)
                check("extra-site: a player frame becomes an embed",
                      got["embed"] == "https://player.extra.test/embed/1",
                      str(got["embed"]))

                social = ('<iframe src="https://www.youtube.com/embed/abc"></iframe>'
                          '<iframe src="https://www.facebook.com/plugins/like.php?'
                          'href=x"></iframe>'
                          '<iframe src="https://ad.a-ads.com/1/?size=300x250">'
                          '</iframe>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="iframe"),
                                  "https://extra.test/watch/1", social)
                check("extra-site: social/ad frames are not the video",
                      got["embed"] is None, str(got["embed"]))

                mixed = ('<iframe src="https://a.adtng.com/get/1"></iframe>'
                         '<iframe src="https://go.gsrv.dev/banner.go?spaceid=1">'
                         '</iframe>'
                         '<iframe src="https://video.extra.test/embed/1"></iframe>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="iframe"),
                                  "https://extra.test/watch/1", mixed)
                check("extra-site: the same-site player beats an ad frame",
                      got["embed"] == "https://video.extra.test/embed/1",
                      str(got["embed"]))

                scanned = ('<script>var a = "https://extra.test/get_file/1/abc/1/1.mp4";'
                           'var b = "https://extra.test/get_file/1/abc/1/1_720p.mp4";'
                           'var c = "https://extra.test/preview.mp4";'
                           'var d = "https://extra.test/1_vthumb.mp4";</script>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="auto"),
                                  "https://extra.test/watch/1", scanned)
                urls = [f["url"] for f in got["formats"]]
                check("extra-site: auto scans full media, not thumbnails",
                      len(urls) == 2 and not any("preview" in u or "vthumb" in u
                                                 for u in urls), str(urls))

                only_preview = ('<script>var a = '
                                '"https://extra.test/preview.mp4";</script>')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="auto"),
                                  "https://extra.test/watch/1", only_preview)
                check("extra-site: a preview alone is not a video",
                      not got["formats"] and got["embed"] is None,
                      str(got))

                got = ext.extract(ext.SiteRule(host="extra.test", strategy="auto"),
                                  "https://extra.test/watch/1", mixed)
                check("extra-site: auto falls back to the same-site player",
                      got["embed"] == "https://video.extra.test/embed/1",
                      str(got["embed"]))

                hosters = ('<li data-link-target="/redirect/42"></li>'
                           '<li data-link-target="/redirect/43"></li>')
                got = ext.extract(ext.SiteRule(
                    host="extra.test", strategy="embed",
                    patterns=(r'data-link-target="([^"]+)"',)),
                    "https://extra.test/watch/1", hosters)
                check("extra-site: an embed pattern picks the first hoster",
                      got["embed"] == "https://extra.test/redirect/42",
                      str(got["embed"]))

                api_rule = ext.SiteRule(
                    host="api.test", strategy="api",
                    api="/api/info?id={id}&alt=0",
                    api_id=r'episodeToken\s*=\s*"([^"]+)"',
                    api_token=r'csrfToken\s*=\s*"([^"]+)"')
                api_page = ('<script>var episodeToken="abc";'
                            ' var csrfToken="tok";</script>')
                request = ext.api_request(api_rule, "https://api.test/watch/1",
                                          api_page)
                check("extra-site: the API request carries the id and token",
                      request[0] == "https://api.test/api/info?id=abc&alt=0"
                      and request[1].get("CSRF-Token") == "tok", str(request))
                api_got = ext.api_extract(
                    '{"grabber":"https://cdn.api.test/1.mp4"}',
                    "https://api.test/watch/1")
                check("extra-site: an API payload yields media",
                      bool(api_got["formats"])
                      and api_got["formats"][0]["url"].endswith("1.mp4"),
                      str(api_got))

                captured = []

                def api_fetch(u, h, timeout=25, data=None):
                    if "/api/" in u:
                        captured.append((u, dict(h)))
                        return '{"grabber":"https://cdn.api.test/1.mp4"}'
                    return api_page

                server._fetch_text = api_fetch
                info = server.extra_site_extract("https://api.test/watch/1")
                check("extra-site: the server wrapper falls back to the API",
                      bool(server.browsable_formats(info))
                      and server.browsable_formats(info)[0]["url"]
                      .endswith("1.mp4"), str(info))
                check("extra-site: the API call used the page token",
                      bool(captured)
                      and captured[0][1].get("CSRF-Token") == "tok",
                      str(captured))

                # Packed fixture: ext._unpack_js decodes it; no JS engine runs.
                packed = (
                    "eval(function(p,a,c,k,e,d){return p}('"
                    "<iframe src=\"1://2.3/e/4\"></iframe>',62,5,"
                    "'|https|streamtape|com|abc'.split('|'),0,{}))")
                got = ext.extract(ext.SiteRule(host="extra.test",
                                               strategy="auto"),
                                  "https://extra.test/watch/1", packed)
                check("extra-site: a packed player config is decoded, not run",
                      got["embed"] == "https://streamtape.com/e/abc",
                      str(got["embed"]))

                ad = ('<meta property="og:video" '
                      'content="https://doubleclick.net/ad.mp4">')
                got = ext.extract(ext.SiteRule(host="extra.test", strategy="meta"),
                                  "https://extra.test/watch/1", ad)
                check("extra-site: tracker media is never served",
                      not got["formats"], str(got["formats"]))

                server._fetch_text = lambda u, h, timeout=25, data=None: page
                info = server.extra_site_extract("https://extra.test/watch/1")
                check("extra-site: the server wrapper dispatches to the recipe",
                      bool(server.browsable_formats(info))
                      and server.browsable_formats(info)[0]["url"]
                      .endswith("clip.mp4"), str(info))
                check("extra-site: unknown hosts return None",
                      server.extra_site_extract("https://example.com/x") is None)

                reached = []
                server._extract = lambda u, playlist=False: reached.append(u)
                info = server._extract_sniffed("https://extra.test/watch/1")
                check("extra-site: a known host never reaches the yt-dlp crawl",
                      not reached, str(reached))
                check("extra-site: the recipe's answer is used",
                      bool(info.get("formats")), str(info))
            finally:
                server._fetch_text = real_fetch
                server._extract = real_extract
                ext.SITES.clear()
                ext.SITES.update(saved)
                server._EXTRA_SITES = ext.SITES
                server._EXTRA_HOSTS = set(ext.SITES)

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

            # A split-rendition site (YouTube) has no complete direct file, only
            # a video-only and an audio-only track. Neither is a download worth
            # starting, so the pick must fail and let the caller mux instead of
            # silently saving a silent film.
            split_only = {"formats": [
                {"format_id": "137", "ext": "mp4", "protocol": "https",
                 "height": 1080, "url": "https://cdn.test/v-1080.mp4",
                 "vcodec": "avc1", "acodec": "none"},
                {"format_id": "140", "ext": "m4a", "protocol": "https",
                 "url": "https://cdn.test/a-128.m4a",
                 "vcodec": "none", "acodec": "mp4a"},
            ], "title": "Split"}
            server.get_info = lambda url, **kw: split_only
            try:
                server.resolve_download("https://site.test/v")
                check("download: a video-only/audio-only pair is not a file", False)
            except Exception as exc:  # noqa: BLE001
                check("download: a video-only/audio-only pair is not a file",
                      "no single file" in str(exc), str(exc)[:60])
            # ...but naming one explicitly still honours the user's choice.
            check("download: an explicit video-only id is still honoured",
                  server.resolve_download("https://site.test/v", "137")["url"]
                  .endswith("v-1080.mp4"))
        finally:
            server.get_info = real_get

        # 16b — HTTP keep-alive -----------------------------------------------
        # A kept-alive socket must not pin a worker forever, and a browser that
        # walks away from one is normal. Both used to leave a handler thread
        # blocked in readline and print a ConnectionResetError traceback.

        def _read_response(sock):
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    raise RuntimeError("closed before headers")
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                key, _, val = line.partition(b":")
                if key.strip().lower() == b"content-length":
                    length = int(val.strip())
            while len(body) < length:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
            return head, body

        real_ka = server.Handler.timeout
        try:
            server.Handler.timeout = 1          # reap an idle connection fast
            ka = socket.create_connection(("127.0.0.1", bport), timeout=5)
            try:
                ka.sendall(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n"
                           b"Connection: keep-alive\r\n\r\n")
                _read_response(ka)
                # Say nothing more, exactly like a browser that walked away.
                ka.settimeout(5)
                try:
                    reaped = ka.recv(1) == b""
                except (ConnectionResetError, socket.timeout):
                    reaped = True
                check("keep-alive: an idle connection is reaped, not pinned",
                      reaped, "" if reaped else "socket stayed open")
            finally:
                ka.close()
        finally:
            server.Handler.timeout = real_ka

        # A client that resets mid-connection must not take the server (or the
        # log) down: handle() swallows the RST.
        check("keep-alive: the handler owns the connection loop",
              server.Handler.handle is not socketserver.BaseRequestHandler.handle)
        rst = socket.create_connection(("127.0.0.1", bport), timeout=5)
        rst.sendall(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        rst.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                       struct.pack("ii", 1, 0))
        rst.close()                              # RST, not a clean FIN
        time.sleep(0.1)
        st, _hd, _b = fetch(bport, "/api/health")
        check("keep-alive: a reset connection does not break the server",
              st == 200, str(st))

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

            # A failed fetch marks the entry stale for a cool-down. It must not
            # mark the chunk inflight, because `inflight` is only cleared by a
            # fetch that actually runs — the chunk would never be retried, even
            # after the network is back.
            stale = server._media_get("https://stale.test/clip.mp4", "")
            stale.url = "http://127.0.0.1:1/clip.mp4"   # nothing listens there
            stale.total = len(body)
            old_interval = server.RECONNECT_PROBE_INTERVAL
            server.RECONNECT_PROBE_INTERVAL = 60
            try:
                server._media_fetch_chunk(stale, 0)     # fails → stale
                server._media_fetch_chunk(stale, 0)     # cool-down → skip
                check("media: a stale chunk stays retryable (no inflight leak)",
                      stale.stale_since is not None and not stale.inflight,
                      f"stale={stale.stale_since} inflight={sorted(stale.inflight)}")
            finally:
                server.RECONNECT_PROBE_INTERVAL = old_interval
                server._media_destroy(stale)

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