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
        st, _, _ = fetch(bport, "/../server.py")
        check("static: path traversal refused", st in (403, 404), str(st))

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

        smuggled = server.playlist_entries({"entries": [
            {"title": "t", "url": "https://site.test/p/1/#__youtubedl_smuggle=%7B%22http_headers%22%3A%7B%7D%7D"},
            {"title": "no url"},
        ]})
        check("playlists: smuggle fragment stripped from entry urls",
              smuggled[0]["url"] == "https://site.test/p/1/", smuggled[0]["url"])
        check("playlists: entries without a usable url are dropped",
              smuggled[1]["url"] is None)

        # 16 — API-driven sites ---------------------------------------------
        page = ('<astro-island props="{&quot;video_id&quot;:[0,3210],'
                '&quot;slug&quot;:[0,&quot;some-video&quot;]}"></astro-island>')
        real_json = server._json_get
        try:
            server._json_get = lambda url, headers, timeout=25: {
                "video": {"title": "Example",
                          "videos": [{"url": "https://cdn.x/hls/i.m3u8"},
                                     {"url": "https://cdn.x/mp4/720.mp4"}]}}
            info = server.api_site_extract("https://hanime.tv/videos/hentai/x", html=page)
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
                server.api_site_extract("https://hanime.tv/videos/hentai/x", html=page)
                check("api-site: a blocked api explains itself", False)
            except Exception as exc:  # noqa: BLE001
                check("api-site: a blocked api explains itself",
                      "cookies" in str(exc) and "403" in str(exc), str(exc)[:70])
        finally:
            server._json_get = real_json
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