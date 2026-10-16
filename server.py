#!/usr/bin/env python3
"""
Viewer backend — resolves "any link" into a playable stream URL using yt-dlp,
and optionally proxies the bytes so CDNs that need special headers still work.

Requires:  python3 + yt-dlp   (pip install -U yt-dlp)
Run:       python3 server.py [--host 127.0.0.1] [--port 8080] [--cookies FILE]

The page itself (index.html / style.css / app.js) is served from this folder,
so http://localhost:8080/ works exactly like opening the file directly, plus the
yt-dlp power for YouTube, Vimeo, TikTok, Twitter, Bilibili, Twitch, thousands
more extractors.

Security note: this runs a video extractor on whatever URL you send it, so it is
a personal tool, not a public service. Keep the default localhost binding.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --------------------------------------------------------------------------- #
#  yt-dlp plumbing
# --------------------------------------------------------------------------- #

LOCAL_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "yt-dlp")
YT_DLP = shutil.which("yt-dlp") or shutil.which("yt_dlp") or (LOCAL_BIN if os.access(LOCAL_BIN, os.X_OK) else None)

def _ffmpeg_path() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:                      # allow a vendored ffmpeg (e.g. on Vercel)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None

FFMPEG = _ffmpeg_path()
RELEASE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
CACHE_TTL = 600            # seconds an extracted URL stays usable in our cache
INFO_CACHE: dict[str, tuple[float, dict]] = {}
EXTRA_ARGS = [a for a in os.environ.get("YTDLP_ARGS", "").split() if a]
DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_notice: list[str] = []


def note(msg: str) -> None:
    if msg not in _notice:
        _notice.append(msg)
        print(f"[viewer] {msg}", file=sys.stderr)


def install_yt_dlp(dest: str = LOCAL_BIN, url: str = RELEASE_URL) -> str:
    """Download the self-contained yt-dlp build from the GitHub release.

    This is a standalone zipapp, so it works on any machine with python3 —
    no pip, no virtualenv.
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    note(f"downloading yt-dlp from {url}")
    try:
        with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    except Exception as exc:  # noqa: BLE001
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(f"could not download yt-dlp: {exc}") from exc
    os.replace(tmp, dest)
    os.chmod(dest, os.stat(dest).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    version = subprocess.run([dest, "--version"], capture_output=True, text=True)
    if version.returncode != 0:
        raise RuntimeError(f"downloaded yt-dlp does not run: {version.stderr.strip()[:200]}")
    note(f"yt-dlp {version.stdout.strip()} installed at {dest}")
    return dest


def yt_dlp_ok() -> bool:
    if not YT_DLP:
        return False
    proc = subprocess.run([YT_DLP, "--version"], capture_output=True,
                          text=True, timeout=15)
    return proc.returncode == 0



def yt_dlp_version() -> str | None:
    if not YT_DLP:
        return None
    proc = subprocess.run([YT_DLP, "--version"], capture_output=True,
                          text=True, timeout=30)
    return (proc.stdout or "").strip() or None


def run_yt_dlp(args: list[str], timeout: int = 90) -> tuple[int, str, str]:
    """Run yt-dlp from an arg list (no shell → no command injection)."""
    cmd = [YT_DLP, "--no-warnings", "--no-playlist", "--newline", *EXTRA_ARGS, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def get_info(url: str) -> dict:
    """Cached `yt-dlp -J <url>`."""
    now = time.time()
    hit = INFO_CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]

    rc, out, err = run_yt_dlp(["-J", url])
    if rc != 0 or not (out or "").strip():
        rc2, out2, err2 = run_yt_dlp(["--flat-playlist", "-J", url])
        if rc2 != 0 or not (out2 or "").strip():
            raise RuntimeError((err or out or err2 or "yt-dlp failed").strip()[-400:])

    info = json.loads(out)
    INFO_CACHE[url] = (now + CACHE_TTL, info)
    return info


def human_size(n) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def friendly(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc)).strip() or exc.__class__.__name__


# --------------------------------------------------------------------------- #
#  Format selection
# --------------------------------------------------------------------------- #

HEADER_WHITELIST = ("user-agent", "referer", "origin", "cookie")
MERGE_ID = "__merge__"      # synthetic format id for the ffmpeg-stitched stream


def merge_offer(fmts: list[dict], page_url: str) -> dict | None:
    """A synthetic 'video + audio' format, only when they came in split."""
    if FFMPEG is None:
        return None
    has_video = any(f["kind"] == "direct" and f.get("hasVideo") for f in fmts)
    has_audio = any(f["kind"] == "direct" and f.get("hasAudio") and not f.get("hasVideo")
                    for f in fmts)
    has_both = any(f["kind"] == "direct" and f.get("hasVideo") and f.get("hasAudio")
                   for f in fmts)
    if not (has_video and has_audio) or has_both:
        return None
    return {
        "id": MERGE_ID,
        "label": "video + audio · merged with ffmpeg",
        "kind": "merge",
        "ext": "mp4",
        "width": None,
        "height": None,
        "hasAudio": True,
        "hasVideo": True,
        "size": None,
        "sizeHuman": "stream",
        "url": page_url,
        "protocol": "ffmpeg",
        "needsProxy": False,
        "headers": {},
    }


def browsable_formats(info: dict) -> list[dict]:
    """Slim, browser-usable view over whatever yt-dlp reported."""
    fmts, seen = [], set()

    def push(f: dict) -> None:
        url = f.get("url")
        if not url or not str(url).startswith("http"):
            return
        proto = (f.get("protocol") or "").lower()
        ext = (f.get("ext") or "").lower()
        has_video = (f.get("vcodec") or "none") not in ("none", "")
        has_audio = (f.get("acodec") or "none") not in ("none", "")

        if "m3u8" in proto or ext == "m3u8":
            kind, label = "hls", "HLS (adaptive stream)"
        elif "mpd" in proto or ext == "mpd":
            kind, label = "dash", "DASH (not playable directly)"
        elif "http_dash_segments" in proto:
            kind, label = "dash", "DASH fragments"
        else:
            kind = "direct"
            if has_video and has_audio:
                label = f"{f['height']}p" if f.get("height") else (f.get("format_note") or "video+audio")
                if ext:
                    label += f" · {ext}"
            elif has_video:
                label = f"{f['height']}p · {ext or 'video'} · no audio"
            else:
                label = f"audio only · {ext or 'audio'}"

        headers = {k: v for k, v in (f.get("http_headers") or {}).items()
                   if k.lower() in HEADER_WHITELIST}
        size = f.get("filesize") or f.get("filesize_approx")

        entry = {
            "id": str(f.get("format_id")),
            "label": label,
            "kind": kind,                      # direct | hls | dash
            "ext": ext,
            "width": f.get("width"),
            "height": f.get("height"),
            "fps": round(f["fps"]) if f.get("fps") else None,
            "hasAudio": has_audio,
            "hasVideo": has_video,
            "size": size,
            "sizeHuman": human_size(size),
            "url": url,
            "protocol": proto,
            # CDNs that need more than a plain UA must be proxied through us.
            "needsProxy": any(k.lower() != "user-agent" for k in headers),
            "headers": headers,
        }
        key = (entry["id"], entry["url"][:64])
        if key in seen:
            return
        seen.add(key)
        fmts.append(entry)

    for f in (info.get("formats") or []):
        push(f)
    for f in (info.get("requested_formats") or []):
        push(f)

    if not fmts and info.get("url"):
        fmts.append({
            "id": "0", "label": "direct", "kind": "direct", "ext": info.get("ext"),
            "url": info["url"], "needsProxy": False,
            "headers": info.get("http_headers") or {}, "size": info.get("filesize"),
            "sizeHuman": human_size(info.get("filesize")),
        })

    rank = {"direct": 0, "hls": 1, "dash": 2}
    fmts.sort(key=lambda e: (
        rank.get(e["kind"], 3),
        -(e["height"] or 0),
        -(e["fps"] or 0),
        -e["size"] if e["size"] else 0,
        e["label"] or "",
    ))
    return fmts


MUXED_OK_EXT = ("mp4", "m4v", "webm", "mov", "mkv")


def best_format(fmts: list[dict]) -> dict | None:
    """Pick what to play by default.

    Priority: a progressive file with sound and picture first, then an HLS
    playlist (adaptive, also sound + picture), then whatever yt-dlp considers
    best. Audio-only and video-only-only streams are never the default.
    """
    both = [f for f in fmts if f["kind"] == "direct" and f.get("hasVideo") and f.get("hasAudio")]
    for f in both:
        if (f.get("height") or 0) <= 1080 and f["ext"] in MUXED_OK_EXT:
            return f
    hls = [f for f in fmts if f["kind"] == "hls" and f.get("hasVideo")]
    for group in ([f for f in both if f["ext"] in MUXED_OK_EXT], hls, both, fmts):
        if group:
            return group[0]
    return None


def subtitle_tracks(info: dict) -> list[dict]:
    out = []
    for gname, group in (("subs", info.get("subtitles") or {}),
                         ("auto", info.get("automatic_captions") or {})):
        for lang, entries in group.items():
            for entry in entries or []:
                ext = (entry.get("ext") or "").lower()
                url = entry.get("url")
                if ext not in ("vtt", "srt", "ttml", "srv3", "json3", "srv1", "srv2"):
                    continue
                if not url or not str(url).startswith("http"):
                    continue
                out.append({
                    "lang": lang if gname == "subs" else f"{lang} (auto)",
                    "raw_lang": lang, "auto": gname == "auto", "ext": ext, "url": url,
                })
    seen, dedup = set(), []
    for s in out:
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        dedup.append(s)
    return dedup[:40]


def write_vtt(src_url: str, timeout: int = 60) -> str | None:
    """Convert a caption URL to .vtt with yt-dlp and return the local path."""
    cache_dir = os.path.join(tempfile.gettempdir(), "viewer-subs")
    os.makedirs(cache_dir, exist_ok=True)
    base = os.path.join(cache_dir, "sub")
    for f in os.listdir(cache_dir):          # one conversion at a time is enough
        try:
            os.remove(os.path.join(cache_dir, f))
        except OSError:
            pass
    rc, _, _ = run_yt_dlp(
        ["--skip-download", "--convert-subs", "vtt",
         "-o", base + ".%(ext)s", src_url], timeout=timeout)
    if rc == 0:
        if os.path.exists(base + ".vtt"):
            return base + ".vtt"
        if os.path.exists(base + ".srt"):     # crude but working srt → vtt
            with open(base + ".srt", encoding="utf-8", errors="replace") as fh:
                body = fh.read()
            vp = base + ".vtt"
            with open(vp, "w", encoding="utf-8") as fh:
                fh.write("WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", body))
            return vp
    return None


# --------------------------------------------------------------------------- #
#  HTTP layer
# --------------------------------------------------------------------------- #

ROOT = os.path.dirname(os.path.abspath(__file__))
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".vtt": "text/vtt",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
}
CORS = ("Access-Control-Allow-Origin", "*")


class Handler(BaseHTTPRequestHandler):
    server_version = "Viewer/1.0"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------- #
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(*CORS)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_json(self, msg: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": msg}, status)

    def _serve_static(self, path: str, head_only: bool = False) -> None:
        rel = path.lstrip("/") or "index.html"
        target = os.path.realpath(os.path.join(ROOT, rel))
        root = os.path.realpath(ROOT)
        if target != root and not target.startswith(root + os.sep):
            return self._send_error_json("Forbidden", 403)
        if os.path.isdir(target) or not os.path.isfile(target):
            return self._send_error_json("Not found", 404)
        ctype = MIME.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        size = os.path.getsize(target)
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-cache")
            self.send_header(*CORS)
            self.end_headers()
            if not head_only:
                with open(target, "rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_file(self, path: str, ctype: str) -> None:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=600")
            self.send_header(*CORS)
            self.end_headers()
            self.wfile.write(data)
        except OSError as exc:
            self._send_error_json(f"Could not read {path}: {exc}", 500)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _query(self) -> tuple[dict, str]:
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        return {k: v[0] for k, v in q.items()}, parsed.path

    # -- verbs ------------------------------------------------------------ #
    def do_GET(self) -> None:    # noqa: N802
        self._route(head=False)

    def do_HEAD(self) -> None:   # noqa: N802
        self._route(head=True)

    def do_OPTIONS(self) -> None:  # noqa: N802
        try:
            self.send_response(204)
            self.send_header(*CORS)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route(self, head: bool = False) -> None:
        try:
            q, path = self._query()
            if path == "/api/health":
                return self._api_health()
            if path == "/api/info":
                return self._api_info(q)
            if path == "/api/play":
                return self._api_play(q, head=head)
            if path == "/api/merge":
                return self._api_merge(q, head=head)
            if path == "/api/subsrc":
                return self._api_subsrc(q)
            if path.startswith("/api/"):
                return self._send_error_json(f"Unknown API route {path}", 404)
            return self._serve_static(path, head_only=head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            _, path = self._query()
            note(f"{path} failed: {exc}")
            self._send_error_json(f"{exc.__class__.__name__}: {friendly(exc)}", 502)

    def log_message(self, fmt, *args) -> None:
        sys.stderr.write("[http] %s\n" % (fmt % args))

    # -- API -------------------------------------------------------------- #
    def _api_health(self) -> None:
        self._send_json({
            "ok": True,
            "backend": True,
            "ytDlp": YT_DLP,
            "ytDlpVersion": yt_dlp_version(),
            "ytDlpAvailable": bool(YT_DLP) and yt_dlp_ok(),
            "ffmpeg": FFMPEG,
            "ffmpegAvailable": FFMPEG is not None,
            "notices": _notice[-4:],
        })

    def _api_info(self, q: dict) -> None:
        url = q.get("url", "")
        parsed = urllib.parse.urlparse(url)
        if not parsed.netloc or parsed.scheme not in ("http", "https"):
            return self._send_error_json("Only http(s) URLs can be extracted", 400)
        if not YT_DLP:
            return self._send_json({
                "ok": True, "url": url, "extractable": False,
                "reason": "yt-dlp is not installed on the server. "
                          "Install it with `pip install -U yt-dlp` and restart server.py.",
                "formats": [], "title": None, "duration": None,
                "thumbnail": None, "subtitles": [], "autoFormatId": None,
            })
        try:
            info = get_info(url)
        except Exception as exc:
            return self._send_json({          # not fatal: the page can still try direct
                "ok": True, "url": url, "extractable": False,
                "reason": friendly(exc), "formats": [], "title": None,
                "duration": None, "thumbnail": None, "subtitles": [],
                "autoFormatId": None,
            })

        fmts = browsable_formats(info)
        chosen = best_format(fmts)
        # "both audio and video": when yt-dlp only reports separate tracks
        # (YouTube above 720p, Twitch, …), ffmpeg can stitch them into one file
        # and we stream that instead. Offered in the UI, not forced as default.
        merge = merge_offer(fmts, url)
        if merge:
            fmts.insert(0, merge)
        self._send_json({
            "ok": True, "url": url, "extractable": bool(fmts),
            "title": info.get("title"),
            "description": (info.get("description") or "")[:600],
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader") or info.get("channel"),
            "extractor": info.get("extractor_key") or info.get("extractor"),
            "isLive": bool(info.get("is_live")),
            "formats": fmts,
            "autoFormatId": chosen["id"] if chosen else None,
            "subtitles": subtitle_tracks(info),
            "needsProxyHint": bool(chosen and chosen["needsProxy"]),
        })

    def _api_play(self, q: dict, head: bool = False) -> None:
        """Serve one stream: 302 → the source, or pipe the bytes ourselves."""
        fmt_id = q.get("fmt", "")
        src = q.get("url", "")
        if not src.startswith(("http://", "https://")):
            return self._send_error_json("Missing/invalid ?url=", 400)
        if src.startswith("/api/"):
            return self._send_error_json("Refusing to proxy our own API", 400)

        headers = {}
        if fmt_id and YT_DLP:
            try:
                info = get_info(src)
                fmts = browsable_formats(info)
                chosen = next((f for f in fmts if f["id"] == fmt_id), None)
                if chosen:
                    src = chosen["url"]
                    headers = chosen.get("headers") or {}
            except Exception:
                pass  # fall back to the URL the page sent

        if q.get("proxy") != "1":
            return self._redirect(src, head)
        try:
            self._proxy(src, headers, head=head)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            note(f"proxy failed for {src[:80]}: {exc}")
            self._send_error_json(
                f"Upstream refused the request ({friendly(exc)}). "
                f"Try switching to 'Direct' instead of 'Proxy'.", 502)

    def _redirect(self, url: str, head: bool = False) -> None:
        try:
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.send_header(*CORS)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass
        _ = head

    def _api_merge(self, q: dict, head: bool = False) -> None:
        """Stream video+audio as one ffmp4:

            yt-dlp -f 'bv*+ba' -o - <page>  |  ffmpeg -i pipe:0 -c copy -f mp4 pipe:1

        `sel` is any yt-dlp format selector, e.g. "bv*+ba/b", "299+251",
        "bestvideo[height<=1080]+bestaudio". Output is a fragmented mp4 so the
        browser can start playing before the download finishes.
        """
        page = q.get("url", "")
        sel = q.get("sel", "") or "bv*+ba/b"
        sort = q.get("sort", "") or "vcodec:h264:vp9:av1,+res:1080"
        if not page.startswith(("http://", "https://")):
            return self._send_error_json("Missing/invalid ?url=", 400)
        if FFMPEG is None or not YT_DLP:
            return self._send_error_json(
                "Merging needs both yt-dlp and ffmpeg on the PATH", 501)

        dl = [YT_DLP, "--no-warnings", "--no-playlist", "--no-cache-dir",
              "--newline", *EXTRA_ARGS,
              "-S", sort, "-f", sel, "-o", "-", page]
        out = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", "pipe:0", "-c", "copy",
               "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]

        if head:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header(*CORS)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            # No Content-Length: stream until the pipeline ends.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.send_header(*CORS)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        note(f"merge {sel} ← {page[:80]}")
        pipe_in = subprocess.Popen(dl, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=1 << 16)
        pipe_out = subprocess.Popen(out, stdin=pipe_in.stdout,
                                    stdout=self.wfile, stderr=subprocess.PIPE,
                                    bufsize=1 << 16)
        pipe_in.stdout.close()          # only the child needs that end
        try:
            pipe_out.wait()
        finally:
            try:
                if pipe_in.poll() is None:
                    pipe_in.kill()
                if pipe_out.poll() is None:
                    pipe_out.kill()
            except OSError:
                pass
            pipe_in.stderr.close()
            pipe_out.stderr.close()

    def _proxy(self, url: str, headers: dict, head: bool = False) -> None:
        merged = {"User-Agent": DEFAULT_UA}
        merged.update({k: v for k, v in (headers or {}).items() if k})
        rng = self.headers.get("Range")
        req = urllib.request.Request(url, headers=merged,
                                     method="HEAD" if head else "GET")
        if rng:
            req.add_header("Range", rng)

        with urllib.request.urlopen(req, timeout=45) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            try:
                self.send_response(status)
                for k, v in resp.getheaders():
                    if k.lower() in HOP_BY_HOP:
                        continue
                    self.send_header(k, v)
                self.send_header(*CORS)
                self.end_headers()
                if not head:
                    shutil.copyfileobj(resp, self.wfile)
                else:
                    resp.read()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _api_subsrc(self, q: dict) -> None:
        """Feed a usable VTT URL to <track src>, converting when needed."""
        webpage, lang, sub = q.get("url", ""), q.get("lang", ""), q.get("sub", "")
        if sub:
            vtt = write_vtt(sub)
            return self._serve_file(vtt, "text/vtt") if vtt else self._redirect(sub)
        if not webpage or not lang:
            return self._send_error_json("Need ?url=page&lang=en", 400)
        try:
            info = get_info(webpage)
        except Exception as exc:
            return self._send_error_json(friendly(exc), 502)
        tracks = subtitle_tracks(info)
        hit = next((t for t in tracks if t["raw_lang"] == lang), None)
        if not hit:
            return self._send_error_json("No captions found for that language", 404)
        if hit["ext"] in ("vtt", "srt"):
            vtt = write_vtt(hit["url"])
            if vtt:
                return self._serve_file(vtt, "text/vtt")
        self._redirect(hit["url"])


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def main() -> None:
    global ROOT, CACHE_TTL, EXTRA_ARGS, YT_DLP
    ap = argparse.ArgumentParser(description="Viewer — yt-dlp backed video server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--cookies", help="cookies.txt for age/geo-gated sites")
    ap.add_argument("--ttl", type=int, default=CACHE_TTL)
    ap.add_argument("--install", action="store_true",
                    help=f"download the yt-dlp binary from GitHub now and exit")
    ap.add_argument("--update", action="store_true",
                    help="re-download yt-dlp from GitHub and exit")
    args = ap.parse_args()

    if args.install or args.update:
        try:
            install_yt_dlp()
        except Exception as exc:  # noqa: BLE001
            print(f"install failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    ROOT = os.path.realpath(args.root)
    CACHE_TTL = args.ttl
    if args.cookies:
        EXTRA_ARGS += ["--cookies", args.cookies]

    if FFMPEG is None:
        note("ffmpeg not found — merging separate video+audio tracks will fail")

    if not YT_DLP:
        note("yt-dlp NOT found → only direct media URLs will play. "
             "Fix with:  python3 server.py --install   (downloads from "
             "github.com/yt-dlp/yt-dlp)  or  pip install -U yt-dlp")

    ThreadingHTTPServer.allow_reuse_address = True
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"cannot bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        sys.exit(1)

    if args.host not in ("127.0.0.1", "localhost"):
        try:
            ip = socket.gethostbyname(args.host)
            note(f"listening on {args.host}:{args.port} — anyone who can reach "
                 f"{ip}:{args.port} can make yt-dlp fetch URLs")
        except OSError:
            pass

    print(f"[viewer] serving {ROOT}")
    print(f"[viewer] yt-dlp: {yt_dlp_version() or 'not found'}", file=sys.stderr)
    print(f"[viewer] open  http://localhost:{args.port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[viewer] bye")
        httpd.server_close()


if __name__ == "__main__":
    main()
