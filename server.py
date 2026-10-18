#!/usr/bin/env python3
"""
Viewer backend — resolves "any link" into something a browser can play.

What it does
------------
* **Resolves** every link yt-dlp knows (~1000 sites) with `-J`, playlists,
  channels and profile pages included.
* **Proxies** media whose CDN demands headers a browser cannot attach
  (`Referer`, `Cookie`, a pinned UA). That is what makes age-gated /
  bot-checked hosts (the 18+ bucket, TikTok, Twitter, Dailymotion, …) work.
* **Proxies HLS too**: `/api/hls` fetches the playlist with the right headers
  and rewrites every segment, key and init-map URI back through here, so
  adaptive streams survive header protection.
* **Re-extracts on demand**: when a signed CDN URL has expired (403) it pulls a
  fresh one and retries instead of failing.
* **Caches** everything: in-process LRU + on-disk JSON (survives restarts,
  warms up cold serverless containers) + `ETag`/`304` + `Cache-Control`, so a
  repeat visit never spawns yt-dlp again.
* Runs anywhere: `python3 server.py`, as a WSGI app (`app`) or a
  `BaseHTTPRequestHandler` (`handler`) on Vercel & friends.

Run:  python3 server.py [--host 127.0.0.1] [--port 8080] [--cookies FILE]

Security: this runs a video extractor over whatever URL you hand it, so keep the
default localhost binding. When exposing it publicly set `VIEWER_TOKEN` so only
callers holding the secret can make the server fetch URLs for them.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import email.message
import hashlib
import hmac
import importlib.util
import io
import json
import os
from html import unescape as html_unescape
import re
import shutil
import socket
import stat as stat_mod
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.1"


# --------------------------------------------------------------------------- #
#  Configuration — every knob is an environment variable
# --------------------------------------------------------------------------- #

def env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def env_int(*names: str, default: int = 0) -> int:
    try:
        return int(env(*names))
    except (TypeError, ValueError):
        return default


def env_float(*names: str, default: float = 0.0) -> float:
    try:
        return float(env(*names))
    except (TypeError, ValueError):
        return default


def env_bool(*names: str, default: bool = False) -> bool:
    raw = env(*names).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "enabled")


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# Shared secret. Empty = open (fine for localhost, unwise for the internet).
TOKEN = env("VIEWER_TOKEN", "VIEWER_API_KEY")
COOKIES = env("YTDLP_COOKIES", "VIEWER_COOKIES", "COOKIES")
# Cookies pasted in as base64 instead of a file in the repo — for hosts where
# committing cookies.txt would leak a logged-in session to everyone.
COOKIES_B64 = env("VIEWER_COOKIES_B64", "YTDLP_COOKIES_B64")
_COOKIES_READY = False
CACHE_TTL = env_int("VIEWER_TTL", "YTDLP_TTL", default=900)       # info TTL, seconds
NEG_TTL = env_int("VIEWER_NEG_TTL", default=45)                   # remember failures briefly
SUB_TTL = env_int("VIEWER_SUB_TTL", default=6 * 3600)
DISK_CACHE = env_bool("VIEWER_DISK_CACHE", default=True)
MAX_MEM_ENTRIES = env_int("VIEWER_MAX_CACHE", default=256)
MAX_DISK_BYTES = env_int("VIEWER_MAX_DISK_BYTES", default=8_000_000)
PROXY_TIMEOUT = env_float("VIEWER_PROXY_TIMEOUT", default=60)
YTDLP_TIMEOUT = env_int("VIEWER_YTDLP_TIMEOUT", default=120)
CONCURRENCY = env_int("VIEWER_YTDLP_CONCURRENCY", default=4)       # parallel extractions
RATE_PER_MIN = env_int("VIEWER_RATE", default=300)                # 0 disables the limiter
MANIFEST_TTL = env_int("VIEWER_MANIFEST_TTL", default=15)
HTTP_CACHE = env_bool("VIEWER_HTTP_CACHE", default=True)          # ETag / Cache-Control
DEBUG = env_bool("VIEWER_DEBUG")                                 # full tracebacks on stderr
PLAYLIST_PROBE = env_bool("VIEWER_PLAYLIST_PROBE", default=True)
BLOCK_PRIVATE = env_bool("VIEWER_BLOCK_PRIVATE", default=False)
SOCKET_TIMEOUT = env_int("YTDLP_SOCKET_TIMEOUT", default=20)
AGE_LIMIT = env_int("VIEWER_AGE_LIMIT", default=18)               # YouTube age gate; no-op elsewhere
EXTRA_ARGS = [a for a in env("YTDLP_ARGS").split() if a]

CACHE_DIR = env("VIEWER_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "viewer-cache")
INFO_DIR = os.path.join(CACHE_DIR, "info")
SUB_DIR = os.path.join(CACHE_DIR, "subs")
YTDLP_DIR = os.path.join(CACHE_DIR, "yt-dlp")

HAS_CURL_CFFI = _has_module("curl_cffi")
IMPERSONATE = env("YTDLP_IMPERSONATE", default=("chrome" if HAS_CURL_CFFI else ""))
if IMPERSONATE.lower() in ("off", "0", "no", "false", "none", "disable"):
    IMPERSONATE = ""      # an explicit "off" must beat the auto-default
if IMPERSONATE and not HAS_CURL_CFFI:
    IMPERSONATE = ""      # without curl_cffi yt-dlp would just fail on --impersonate

# Browser impersonation: what gets past Cloudflare/DataDome on many hosts. If it
# turns out to be unusable we stop paying for a failed retry on every extraction.
IMPERSONATE_FAILURES = {"n": 0}
MAX_IMPERSONATE_FAILURES = env_int("VIEWER_IMPERSONATE_FAILURES", default=3)


def impersonate_ready() -> bool:
    """True while browser impersonation is worth attempting."""
    return bool(IMPERSONATE) and IMPERSONATE_FAILURES["n"] < MAX_IMPERSONATE_FAILURES


def impersonate_target(profile: str):
    """`--impersonate` for the API, which wants an ImpersonateTarget, not a string."""
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        client, _, version = str(profile).partition(":")
        return ImpersonateTarget(client or None, version or None)
    except Exception:  # noqa: BLE001
        return profile

notice: list[str] = []


def detect_platform() -> str:
    """Where is this running? Datacenter IP ranges get blocked by hosts that
    happily serve a home connection, so it is worth naming."""
    if env("VERCEL", "VERCEL_ENV"):
        return "Vercel"
    if env("RENDER"):
        return "Render"
    if env("RAILWAY_ENVIRONMENT"):
        return "Railway"
    if env("FLY_APP_NAME", "FLY_REGION"):
        return "Fly.io"
    if env("AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV", "VERCEL_AWS_LAMBDA_FUNCTION_NAME"):
        return "AWS Lambda"
    if env("NETLIFY"):
        return "Netlify"
    if env("DYNO"):
        return "Heroku"
    if env("KOYEB_APP_ID", "HYPERDX_SERVICE_TOKEN"):
        return "Koyeb"
    return ""


PLATFORM = detect_platform()


def note(msg: str) -> None:
    if msg not in notice:
        notice.append(msg)
        if len(notice) > 20:
            del notice[:-20]
    print(f"[viewer] {msg}", file=sys.stderr)


def ensure_dirs() -> None:
    for d in (CACHE_DIR, INFO_DIR, SUB_DIR, YTDLP_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as exc:                 # read-only bundle filesystems
            note(f"cache dir {d} unavailable ({exc}) — running memory-only")


def _looks_like_netscape_cookies(raw: bytes) -> bool:
    """A cookies.txt export is tab-separated with ≥6 fields per entry."""
    text = raw.decode("utf-8", "replace")
    if text.lstrip().startswith(("{", "<")):          # JSON export, not cookies.txt
        return False
    for line in text.splitlines():
        line = line.removeprefix("#HttpOnly_").strip()
        if line and not line.startswith("#") and len(line.split("\t")) >= 6:
            return True
    return False


def cookies_path() -> str:
    """The active cookies.txt, materialising it from base64 env once if needed.

    Order: an explicit path (--cookies / YTDLP_COOKIES) wins, then
    VIEWER_COOKIES_B64, which is written into the cache dir (mode 0600) on first
    use. Nothing is ever written into the repository.
    """
    global COOKIES, _COOKIES_READY
    if _COOKIES_READY:
        return COOKIES
    _COOKIES_READY = True
    if COOKIES and os.path.isfile(COOKIES):
        return COOKIES
    if COOKIES:
        note(f"YTDLP_COOKIES={COOKIES} does not exist — ignoring it")
    if not COOKIES_B64:
        return COOKIES

    try:
        import base64
        import binascii
        raw = base64.b64decode(re.sub(r"\s+", "", COOKIES_B64) + "===")
    except (ValueError, binascii.Error, ImportError):
        note("VIEWER_COOKIES_B64 is not valid base64 — ignoring it")
        return COOKIES
    if not _looks_like_netscape_cookies(raw):
        note("VIEWER_COOKIES_B64 does not contain a cookies.txt export — ignoring it "
             "(expected tab-separated Netscape format, not JSON)")
        return COOKIES

    ensure_dirs()
    target = os.path.join(CACHE_DIR, "cookies.txt")
    try:
        tmp = f"{target}.{os.getpid()}.part"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)          # atomic: concurrent cold starts are fine
        COOKIES = target
        note(f"cookies loaded from VIEWER_COOKIES_B64 "
             f"({len(raw)} bytes → {target})")
    except OSError as exc:
        note(f"could not write cookies to {target} ({exc}) — ignoring them")
    return COOKIES


# --------------------------------------------------------------------------- #
#  yt-dlp plumbing
# --------------------------------------------------------------------------- #

LOCAL_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "yt-dlp")
YT_DLP = (shutil.which("yt-dlp") or shutil.which("yt_dlp")
          or (LOCAL_BIN if os.access(LOCAL_BIN, os.X_OK) else None))


def _ffmpeg_path() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:                       # vendored ffmpeg (imageio-ffmpeg) when installed
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


FFMPEG = _ffmpeg_path()
RELEASE_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Headers a <video> element or hls.js can never set for us on a cross-origin
# request. Everything here is what forces a stream through the proxy.
HEADER_WHITELIST = ("user-agent", "referer", "origin", "cookie", "authorization",
                    "x-forwarded-for", "x-real-ip")
HEADER_BLOCKLIST = ("host", "connection", "content-length", "range",
                    "accept-encoding", "accept", "transfer-encoding", "upgrade")
STREAM_CHUNK = 1 << 18
EXPIRY_CODES = (401, 403, 410, 474)      # 474 = CDN "origin refused", i.e. retry
MEDIA_EXT_RE = re.compile(r"\.(mp4|m4v|webm|ogv|ogg|mov|mkv|avi|mpe?g|ts|mp3|m4a|aac|wav|flac|m3u8)(\?|#|$)", re.I)
_URI_ATTR = re.compile(r'(URI=")([^"]+)(")')


def install_yt_dlp(dest: str = LOCAL_BIN, url: str = RELEASE_URL) -> str:
    """Download the self-contained yt-dlp build from the GitHub release."""
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
    os.chmod(dest, os.stat(dest).st_mode | stat_mod.S_IXUSR | stat_mod.S_IXGRP | stat_mod.S_IXOTH)
    version = subprocess.run([dest, "--version"], capture_output=True, text=True)
    if version.returncode != 0:
        raise RuntimeError(f"downloaded yt-dlp does not run: {version.stderr.strip()[:200]}")
    note(f"yt-dlp {version.stdout.strip()} installed at {dest}")
    return dest


_STATUS_CACHE: dict = {"at": 0.0, "version": None, "ok": False}

# How to reach yt-dlp: "auto" prefers the Python module (fast, no subprocess,
# works in sandboxes), "binary" forces the console script, "module" forces the
# import. See https://github.com/yt-dlp/yt-dlp — both ship from requirements.txt.
YTDLP_MODE = env("VIEWER_YTDLP_MODE", default="auto").lower()
HAS_YTDLP_PY = _has_module("yt_dlp")
_YTDLP_PY = None


def ytdlp_module():
    """The imported `yt_dlp` module, or None. Imported lazily (it is heavy)."""
    global _YTDLP_PY
    if _YTDLP_PY is None and HAS_YTDLP_PY:
        try:
            import yt_dlp
            _YTDLP_PY = yt_dlp
        except Exception:  # noqa: BLE001
            _YTDLP_PY = False
    return _YTDLP_PY or None


def _yt_dlp_status() -> tuple[str | None, bool]:
    """Cached "is yt-dlp usable, and which way" — must be fast for /api/health.

    Two ways to reach yt-dlp, and both are checked:
      * the `yt-dlp` console script (spawns a process — needs PATH + subprocess),
      * the `yt_dlp` Python module (just an import — also works where PATH or
        process spawning is unavailable, e.g. inside serverless sandboxes).
    """
    now = time.time()
    if now - _STATUS_CACHE["at"] < 60:
        return _STATUS_CACHE["version"], _STATUS_CACHE["ok"]
    version, ok = None, False
    mod = ytdlp_module()
    if mod is not None:
        version = getattr(getattr(mod, "version", None), "__version__", None)
        ok = True
    elif YT_DLP:
        try:
            proc = subprocess.run([YT_DLP, "--version"], capture_output=True,
                                  text=True, timeout=15)
            version = (proc.stdout or "").strip() or None
            ok = proc.returncode == 0
        except Exception:  # noqa: BLE001
            ok = False
    _STATUS_CACHE.update(at=now, version=version, ok=ok)
    return version, ok


def yt_dlp_ok() -> bool:
    return _yt_dlp_status()[1]


def yt_dlp_version() -> str | None:
    return _yt_dlp_status()[0]


def ytdlp_mode() -> str:
    """How extraction will run here: 'module', 'binary' or 'none'.

    'auto' (default) prefers the Python module: no subprocess, no PATH. The
    preference is always downgraded gracefully — a missing script falls back to
    the module and vice versa, so this only reports 'none' when yt-dlp really
    is not installed anywhere.
    """
    has_module = ytdlp_module() is not None
    if YTDLP_MODE == "binary" and YT_DLP:
        return "binary"
    if has_module:
        return "module"
    return "binary" if YT_DLP else "none"


_BASE_ARGS: dict[bool, list[str]] = {}


def base_ytdlp_args(impersonate: bool = True) -> list[str]:
    """Flags that make extraction survive hostile networks. Applies to every run."""
    if impersonate in _BASE_ARGS:
        return _BASE_ARGS[impersonate]
    args = [
        # --no-warnings is deliberately *not* passed: stderr is only read when
        # something failed, and "ERROR: This video is unavailable" is a far
        # better answer to the user than a generic failure.
        "--no-progress", "--no-color",
        "--socket-timeout", str(SOCKET_TIMEOUT),
        "--retries", "5",
        "--fragment-retries", "10",
        "--extractor-retries", "3",
        "--file-access-retries", "3",
        "--cache-dir", YTDLP_DIR,
    ]
    if AGE_LIMIT:
        args += ["--age-limit", str(AGE_LIMIT)]
    if IMPERSONATE and impersonate and impersonate_ready():
        args += ["--impersonate", IMPERSONATE]
    if env_bool("YTDLP_IPV4"):
        args += ["--force-ipv4"]
    if cookies_path() and os.path.isfile(cookies_path()):
        args += ["--cookies", cookies_path()]
        note(f"using cookies from {cookies_path()}")
    _BASE_ARGS[impersonate] = args
    return args


def ytdlp_cmd(args: list[str], impersonate: bool = True) -> list[str]:
    """Command line for the subprocess paths (merge, subtitle conversion).

    Prefers the console script, falls back to `python -m yt_dlp` when PATH does
    not have it (some sandboxes) — both work.
    """
    base = base_ytdlp_args(impersonate)
    if YT_DLP:
        return [YT_DLP, *base, *EXTRA_ARGS, *args]
    if ytdlp_module() is not None:
        return [sys.executable, "-m", "yt_dlp", *base, *EXTRA_ARGS, *args]
    raise RuntimeError("yt-dlp is not installed on this server")


def run_yt_dlp(args: list[str], timeout: int | None = None,
               impersonate: bool = True) -> tuple[int, str, str]:
    """Run yt-dlp from an arg list (no shell → no command injection)."""
    proc = subprocess.run(ytdlp_cmd(args, impersonate), capture_output=True, text=True,
                          timeout=timeout or YTDLP_TIMEOUT)
    return proc.returncode, proc.stdout, proc.stderr


# --------------------------------------------------------------------------- #
#  Cache: LRU in memory + JSON on disk + single-flight + negative entries
# --------------------------------------------------------------------------- #

_MEM: collections.OrderedDict[str, tuple[float, dict]] = collections.OrderedDict()
_NEG: dict[str, tuple[float, str]] = {}
_KEY_LOCKS: dict[str, threading.Lock] = {}
_LOCK_GUARD = threading.Lock()
_MEM_LOCK = threading.Lock()
STATS = {"hit": 0, "disk": 0, "miss": 0, "neg": 0, "refresh": 0, "busy": 0}
_SLOTS = threading.Semaphore(CONCURRENCY) if CONCURRENCY > 0 else None


def _key(url: str, playlist: bool = False) -> str:
    return ("P|" if playlist else "|") + url


def _lock_for(key: str) -> threading.Lock:
    with _LOCK_GUARD:
        lk = _KEY_LOCKS.get(key)
        if lk is None:
            lk = _KEY_LOCKS[key] = threading.Lock()
            if len(_KEY_LOCKS) > 512:
                for stale in list(_KEY_LOCKS)[:256]:
                    _KEY_LOCKS.pop(stale, None)
        return lk


def _mem_get(key: str, now: float) -> dict | None:
    with _MEM_LOCK:
        hit = _MEM.get(key)
        if not hit:
            return None
        exp, info = hit
        if exp <= now:
            _MEM.pop(key, None)
            return None
        _MEM.move_to_end(key)
        return info


def _mem_put(key: str, exp: float, info: dict) -> None:
    with _MEM_LOCK:
        _MEM[key] = (exp, info)
        _MEM.move_to_end(key)
        while MAX_MEM_ENTRIES and len(_MEM) > MAX_MEM_ENTRIES:
            _MEM.popitem(last=False)


def _disk_path(key: str) -> str:
    return os.path.join(INFO_DIR, hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _disk_get(key: str, now: float) -> dict | None:
    if not DISK_CACHE:
        return None
    try:
        with open(_disk_path(key), "rb") as fh:
            rec = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    info = rec.get("i")
    if not isinstance(info, dict) or (rec.get("e") or 0) <= now:
        return None
    return info


def _disk_put(key: str, exp: float, info: dict) -> None:
    if not DISK_CACHE:
        return
    try:
        blob = json.dumps({"e": exp, "u": key, "i": info}, separators=(",", ":"))
        if len(blob) > MAX_DISK_BYTES:
            return
        path = _disk_path(key)
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "wb") as fh:
            fh.write(blob.encode("utf-8"))
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


def prune_disk(max_age: int | None = None) -> int:
    """Drop cache files that can no longer be served."""
    cutoff = time.time() - (max_age if max_age is not None else CACHE_TTL * 3)
    removed = 0
    try:
        names = os.listdir(INFO_DIR)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(INFO_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    return removed


def friendly(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "yt-dlp timed out — the site is slow or blocking this server"
    return re.sub(r"\s+", " ", str(exc)).strip()[-400:] or exc.__class__.__name__


def ytdlp_reason(stderr: str) -> str:
    """The most useful line of yt-dlp's stderr (ERROR lines win)."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    errors = [l for l in lines if l.upper().startswith("ERROR")]
    best = (errors[-1] if errors else (lines[-1] if lines else ""))
    return best.removeprefix("ERROR:").strip()[:400]


class _QuietLog:
    """yt-dlp logger that keeps the last complaint instead of printing it."""

    def __init__(self) -> None:
        self.last = ""

    def _keep(self, msg) -> None:
        text = str(msg).strip()
        if text:
            self.last = text[-400:]

    def debug(self, msg):
        pass

    def info(self, msg):
        self._keep(msg)

    def warning(self, msg):
        self._keep(msg)

    def error(self, msg):
        self._keep(msg)


def _extra_module_opts() -> dict:
    """Map the allow-listed YTDLP_ARGS to YoutubeDL options (rest is ignored)."""
    out: dict = {}
    tokens, i = list(EXTRA_ARGS), 0
    simple = {"--proxy": "proxy", "--username": "username", "--password": "password",
              "--extractor-args": "extractor_args", "--geo-bypass-country": "geo_bypass_country"}
    while i < len(tokens):
        flag = tokens[i]
        if flag == "--cookies" and i + 1 < len(tokens):
            out["cookiefile"] = tokens[i + 1]
            i += 2
            continue
        if flag == "--referer" and i + 1 < len(tokens):
            out.setdefault("http_headers", {})["Referer"] = tokens[i + 1]
            i += 2
            continue
        if flag in simple and i + 1 < len(tokens):
            out[simple[flag]] = tokens[i + 1]
            i += 2
            continue
        i += 1
    return out


def _module_opts(playlist: bool = False, impersonate: bool = True) -> dict:
    """YoutubeDL options mirroring base_ytdlp_args()."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "socket_timeout": SOCKET_TIMEOUT,
        "retries": 5,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "cachedir": YTDLP_DIR,
        "age_limit": AGE_LIMIT or None,
        "impersonate": (impersonate_target(IMPERSONATE)
                        if (impersonate and impersonate_ready()) else None),
        "noplaylist": not playlist,
        "extract_flat": "in_playlist" if playlist else False,
    }
    cookie_file = cookies_path()
    if cookie_file and os.path.isfile(cookie_file):
        opts["cookiefile"] = cookie_file
    opts.update(_extra_module_opts())
    return {k: v for k, v in opts.items() if v is not None}


def _module_extract(url: str, playlist: bool = False, impersonate: bool = True) -> dict:
    """Extract in-process — no subprocess, no PATH, works in serverless sandboxes."""
    mod = ytdlp_module()
    plans: list[dict] = []
    if playlist:
        plans.append({"noplaylist": False, "extract_flat": "in_playlist"})
    plans.append({"noplaylist": True, "extract_flat": False})
    if not playlist:
        plans.append({"noplaylist": False, "extract_flat": "in_playlist"})

    detail = ""
    for extra in plans:
        log = _QuietLog()
        opts = _module_opts(impersonate=impersonate)
        opts.update(extra)
        opts["logger"] = log
        try:
            with mod.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            text = friendly(exc)
            if text.startswith("ERROR:"):
                text = text[6:].strip()
            detail = text or detail
            continue
        if isinstance(info, dict) and (info.get("formats") or info.get("entries")
                                       or info.get("url")):
            return info
        detail = (log.last or detail
                  or f"yt-dlp found no downloadable formats for {url} — it may be "
                     f"private, region-locked, deleted or live-only")
    raise RuntimeError(detail or f"yt-dlp could not extract {url}")


_SNIFF_CACHE: dict[str, tuple[float, list[tuple[str, str]]]] = {}
SNIFF_TTL = env_int("VIEWER_SNIFF_TTL", default=900)

# --------------------------------------------------------------------------- #
#  API-driven sites
#
#  Some pages carry nothing but an id: the stream URL only exists in the JSON
#  endpoint the site's own player calls. yt-dlp has no extractor for them, so we
#  call the same public endpoint a browser would. Kept as a small, explicit table
#  rather than a heuristic — guessing at API shapes is how extractors rot.
# --------------------------------------------------------------------------- #

_API_SITES = {
    # host suffix: (id patterns, endpoint templates)
    "hanime.tv": (
        # the Astro island props carry ["video_id", [0, 3210]]
        (r'"video_id"\s*:\s*\[\s*\d+\s*,\s*(\d+)\s*\]',
         r'"video_id"\s*:\s*(\d+)'),
        ("https://guest.freeanimehentai.net/api/v11/video?id={id}",
         "https://hanime.tv/api/v11/video?id={id}"),
    ),
}


def _json_get(url: str, headers: dict, timeout: int = 25):
    """GET some JSON, preferring curl_cffi's browser TLS profile when present."""
    try:
        from curl_cffi import requests as _cr          # noqa: PLC0415
        resp = _cr.get(url, headers=headers, timeout=timeout, impersonate="chrome")
        if resp.status_code == 200:
            return resp.json()
        return ("error", resp.status_code)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        return ("error", 0)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return ("error", exc.code)
    except Exception:  # noqa: BLE001
        return ("error", 0)


def _media_urls_from_json(obj, depth: int = 0) -> list[str]:
    """Collect media URLs from a JSON payload without knowing its exact shape."""
    if depth > 8:
        return []
    out: list[str] = []
    if isinstance(obj, str):
        for m in re.finditer(r'https?://[^\s"\'\\]+?\.(?:m3u8|mp4|webm|m4v)(?:\?[^\s"\'\\]*)?',
                             obj, re.I):
            out.append(m.group(0))
    elif isinstance(obj, dict):
        for value in obj.values():
            out += _media_urls_from_json(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            out += _media_urls_from_json(value, depth + 1)
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _title_from_json(obj, depth: int = 0):
    """Grab a human title from a JSON payload if one is obviously there."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key in ("title", "name", "video_name", "episode_name"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:160]
        for key in ("video", "data", "result", "item", "videos"):
            if key in obj:
                found = _title_from_json(obj[key], depth + 1)
                if found:
                    return found
    elif isinstance(obj, list) and obj:
        return _title_from_json(obj[0], depth + 1)
    return None


def api_site_extract(url: str, html: str | None = None) -> dict | None:
    """For sites that only publish a stream URL through a JSON API."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    entry = next((v for k, v in _API_SITES.items()
                  if host == k or host.endswith("." + k)), None)
    if not entry:
        return None
    patterns, endpoints = entry
    if html is None:
        page = _fetch_html(url)
        if not page:
            raise RuntimeError(
                f"{host} did not serve the page to this server — try from the same "
                f"network you watch from, or add cookies (VIEWER_COOKIES_B64)")
        _, html = page

    # The payload usually lives inside an HTML-escaped JSON attribute, so the
    # quotes arrive as &quot; — normalise before looking for the id.
    haystack = (html.replace("&quot;", '"').replace("&#34;", '"')
                .replace("&#39;", "'").replace("&amp;", "&"))
    video_id = None
    for pattern in patterns:
        m = re.search(pattern, haystack)
        if m:
            video_id = m.group(1)
            break
    if not video_id:
        return None

    headers = {"Referer": url, "Origin": "/".join(url.split("/")[:3]),
               "Accept": "application/json, text/plain, */*"}
    blocked = 0
    for template in endpoints:
        api = template.format(id=video_id)
        payload = _json_get(api, headers)
        if isinstance(payload, tuple):
            blocked = blocked or (payload[1] or 0)     # report the first refusal
            continue
        found = _media_urls_from_json(payload)
        if found:
            found.sort(key=lambda u: (1 if ".m3u8" in u.lower() else 0, len(u)))
            info = synthetic_info(url, found[0])
            info["title"] = _title_from_json(payload) or info.get("title")
            note(f"resolved {host} via its JSON api")
            return info
    raise RuntimeError(
        f"{host} serves its streams from an API that refused the request "
        f"(HTTP {blocked or 403}) — it wants a real browser session: add cookies "
        f"(VIEWER_COOKIES_B64) and run the backend from the same network you watch "
        f"from")


# --------------------------------------------------------------------------- #
#  Player-frame sites
#
#  Some hosts serve the video from their own player frame and hand the browser an
#  obfuscated media URL that its own page decodes client-side. The generic
#  extractor has nothing to read, so we run the very same decoder the page already
#  runs for every visitor. There is no login, no paywall and no signed token — the
#  value is in the HTML the site serves anyway. Kept as an explicit table, like
#  _API_SITES: one named entry per site beats a decoder-shaped guess that rots.
# --------------------------------------------------------------------------- #

def wh_decode_media_url(blob: str) -> str | None:
    """The player's own `whDecodeMediaUrl()`, ported.

    base64url -> XOR each byte with (13 + i % 17) -> reverse -> base64 again.
    Returns None unless the result is an http(s) URL.
    """
    if not blob:
        return None
    data = blob.replace("-", "+").replace("_", "/")
    data += "=" * (-len(data) % 4)
    try:
        first = base64.b64decode(data)
        second = bytes(b ^ ((13 + i % 17) & 0xFF) for i, b in enumerate(first))
        second = second[::-1]
        second += b"=" * (-len(second) % 4)
        out = base64.b64decode(second).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return out if out.startswith(("http://", "https://")) else None


_WH_PLAYER_RE = re.compile(
    r"""data-(?:primary|alternate)-player-url=["']([^"']+)["']""", re.I)
_WH_SOURCES_RE = re.compile(r"""whJwSources\s*=\s*(\[.*?\])\s*;""", re.S)
_WH_LABEL_RE = re.compile(r"""<h1[^>]*>(.*?)</h1>""", re.S | re.I)
_WH_DURATION_RE = re.compile(
    r"""["']duration["']\s*:\s*["']PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?["']""", re.I)


def _wh_height(label: str) -> int | None:
    m = re.search(r"(\d{3,4})\s*[pP]", label or "")
    return int(m.group(1)) if m else None


def watchhentai_extract(url: str, html: str | None = None) -> dict | None:
    """watchhentai.net: follow the player frame it names and decode its sources."""
    if html is None:
        page = _fetch_html(url)
        if not page:
            return None
        final, html = page

    m = _WH_PLAYER_RE.search(html)
    if not m:
        # Series pages never carry a player frame — that is the site's own
        # naming, not a guess. Saying so now keeps the resolver off yt-dlp's
        # 16+ s crawl of a page it can never understand.
        if "/series/" in urllib.parse.urlparse(url).path:
            raise NotAVideoPage(
                "a series page lists episodes — open an episode link; it has no "
                "single video to play")
        return None
    player_url = urllib.parse.urljoin(url, m.group(1))

    # The player frame is only served to a browser coming from a video page.
    frame = _fetch_html(player_url)
    if not frame:
        raise RuntimeError(
            "the player frame refused the request — it wants the video page as the "
            "Referer; retry, or add cookies (VIEWER_COOKIES_B64)")
    _, player_html = frame

    src_match = _WH_SOURCES_RE.search(player_html)
    if not src_match:
        raise RuntimeError(
            "the player frame carried no source list — this site's player markup "
            "changed, so the decoder needs updating")

    try:
        sources = json.loads(src_match.group(1))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"the player's source list did not parse: {exc}") from exc

    formats = []
    for src in sources if isinstance(sources, list) else []:
        if not isinstance(src, dict):
            continue
        media = wh_decode_media_url(src.get("file") or "")
        if not media:
            continue
        ext = (urllib.parse.urlparse(media).path.rsplit(".", 1)[-1] or "mp4").lower()
        if ext not in ("mp4", "webm", "m4v", "mov"):
            continue
        label = str(src.get("label") or "")
        height = _wh_height(label)
        # The CDN refuses a request without the player page as its Referer, which
        # is exactly what a browser sends — needsProxy then routes it via us.
        formats.append({
            "format_id": f"wh-{height or ext}",
            "url": media,
            "ext": ext,
            "protocol": "https",
            "height": height,
            "vcodec": None,
            "acodec": None,
            "_sniffed": True,
            "http_headers": {"User-Agent": DEFAULT_UA, "Referer": player_url},
        })
    if not formats:
        raise RuntimeError("every source in the player frame decoded to nothing usable")

    formats.sort(key=lambda f: f.get("height") or 0, reverse=True)

    title = None
    h1 = _WH_LABEL_RE.search(html)
    if h1:
        title = html_unescape(re.sub(r"<[^>]+>", "", h1.group(1))).strip()[:160] or None
    # The VideoObject block with the runtime lives in the player frame, not on the
    # page, so look in both and take whichever answers.
    duration = None
    for blob in (player_html, html):
        dur = _WH_DURATION_RE.search(blob)
        if dur:
            duration = (int(dur.group(1) or 0) * 3600 + int(dur.group(2) or 0) * 60
                        + float(dur.group(3) or 0)) or None
            break

    note(f"decoded {len(formats)} source(s) from the watchhentai player frame")
    return {
        "title": title,
        "extractor_key": "watchhentai",
        "duration": duration,
        "formats": formats,
        "webpage_url": url,
    }


_PLAYER_SITES = {"watchhentai.net": watchhentai_extract}


class NotAVideoPage(RuntimeError):
    """An adapter read the page and can state what it is: not a single video.

    Unlike a provisional failure (frame unreachable, markup changed), a
    page-type fact read off the URL/HTML is decisive — no other path in the
    ladder will succeed either, so the resolver can stop and say so instead of
    spending 20+ s proving it again.
    """


def player_site_extract(url: str, html: str | None = None) -> dict | None:
    """Hand the page to an adapter for a host we know how to decode."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    fn = next((v for k, v in _PLAYER_SITES.items()
               if host == k or host.endswith("." + k)), None)
    return fn(url, html) if fn else None


SNIFF_MAX = env_int("VIEWER_SNIFF_MAX", default=4)
SNIFF_TIMEOUT = env_int("VIEWER_SNIFF_TIMEOUT", default=25)
MEDIA_URL_RE = re.compile(
    r"""["'(]([^"'()\s<>]{6,500}?\.(?:m3u8|mp4|webm|m4v|mov)(?:\?[^"'()\s<>]*)?)["')]""",
    re.I)
EMBED_ATTR_RE = re.compile(
    r"""<(?:iframe|embed|object|source|video)\b[^>]*?\b(?:src|data-src|data-media-src|"""
    r"""data-video-src|data-hls|data-file)=["']([^"']+)["']""", re.I)
FEED_LINK_RE = re.compile(
    r"""<link\b[^>]*?type=["']application/(?:rss|atom)\+xml["'][^>]*?href=["']([^"']+)["']""",
    re.I)
FEED_HREF_RE = re.compile(r"""<link\b[^>]*?href=["']([^"']+)["'][^>]*?type=["']application/(?:rss|atom)\+xml""", re.I)
# Endpoint shapes that are almost always a player we can resolve further.
PLAYER_HINTS = ("player", "embed", "/e/", "watch", "video", "stream", "play",
                "jwplayer", "kt_player", "clean-tube", "media", "html5")
SKIP_HOST_SUFFIX = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
                    ".woff", ".woff2", ".ico", ".mp3", ".jpg.webp")


def _fetch_html(url: str) -> tuple[str, str] | None:
    """Fetch a page the way a browser would. Returns (final_url, html)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "/".join(url.split("/")[:3]) + "/",
    })
    try:
        with urllib.request.urlopen(req, timeout=SNIFF_TIMEOUT) as resp:
            raw = resp.read(2 << 20)
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.geturl(), raw.decode(charset, "replace")
    except Exception:  # noqa: BLE001
        return None


def sniff_targets(url: str) -> list[tuple[str, str]]:
    """Things worth trying when yt-dlp gives up: media files, player frames, feeds.

    Ordered best-first: a direct .m3u8/.mp4 plays immediately, a player frame is
    worth another extraction pass, an RSS feed turns the page into a playlist.
    """
    now = time.time()
    hit = _SNIFF_CACHE.get(url)
    if hit and hit[0] > now:
        return hit[1]

    page = _fetch_html(url)
    if not page:
        return []
    final, html = page
    found: list[tuple[str, str]] = []

    def add(kind: str, candidate: str) -> None:
        try:
            absolute = urllib.parse.urljoin(final, candidate.strip())
        except Exception:  # noqa: BLE001
            return
        host = urllib.parse.urlparse(absolute).hostname or ""
        low = absolute.lower().split("?")[0]
        if not absolute.startswith(("http://", "https://")) or not host:
            return
        if any(low.endswith(sfx) for sfx in SKIP_HOST_SUFFIX):
            return
        if not is_private_url(absolute):
            found.append((kind, absolute))

    for m in MEDIA_URL_RE.finditer(html):
        add("media", m.group(1))
    for m in EMBED_ATTR_RE.finditer(html):
        src = m.group(1)
        add("media" if re.search(r"\.(m3u8|mp4|webm|m4v|mov)(\?|$)", src, re.I) else "page", src)
    for m in (FEED_LINK_RE.finditer(html) or FEED_HREF_RE.finditer(html)):
        add("feed", m.group(1))

    # Frames first only when they look like a player; otherwise every nav link
    # in the page becomes a candidate and the ladder turns into a crawl.
    def rank(item: tuple[str, str]) -> tuple[int, str]:
        kind, link = item
        same_host = link.split("/")[2] == url.split("/")[2]
        is_player = any(h in link.lower() for h in PLAYER_HINTS)
        # A progressive file beats a playlist: it plays natively, without hls.js.
        adaptive = 1 if ".m3u8" in link.lower() else 0
        return ({"media": 0, "page": 1 if is_player else 3, "feed": 2}[kind]
                + (1 if same_host else 0) + adaptive, link)

    ordered = sorted(dict.fromkeys(found), key=rank)[:SNIFF_MAX]
    _SNIFF_CACHE[url] = (now + SNIFF_TTL, ordered)
    for stale in [k for k, (exp, _) in _SNIFF_CACHE.items() if exp <= now][:64]:
        _SNIFF_CACHE.pop(stale, None)
    return ordered


def synthetic_info(url: str, media_url: str) -> dict:
    """A one-format info dict for a media file we found in the page ourselves."""
    ext = (urllib.parse.urlparse(media_url).path.rsplit(".", 1)[-1] or "mp4").lower()
    return {
        "title": None,
        "extractor_key": "sniffed",
        "formats": [{
            "format_id": f"sniffed-{ext}",
            "url": media_url,
            "ext": ext,
            "protocol": "https",
            "vcodec": None, "acodec": None,
            "_sniffed": True,             # "we found this file in the page"
            "http_headers": {"User-Agent": DEFAULT_UA, "Referer": url},
        }],
        "webpage_url": url,
    }


def _extract(url: str, playlist: bool = False, depth: int = 0) -> dict:
    """`yt-dlp -J` equivalent, with a short ladder of fallbacks.

    Browser impersonation is tried first; if it fails the whole ladder runs once
    more without it (some hosts refuse the impersonated request but serve the
    plain one). After a few failures impersonation is switched off for good, so a
    broken profile costs one retry, not every request.
    """
    worker = _module_extract if ytdlp_mode() == "module" else _subprocess_extract
    passes = (True, False) if impersonate_ready() else (False,)
    last: Exception | None = None
    for impersonate in passes:
        try:
            info = worker(url, playlist, impersonate)
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
        # Only the *un*impersonated success proves impersonation was the
        # problem. A host that refuses both attempts is blocking us, not our
        # TLS fingerprint — counting that would disable impersonation for good
        # and break every other host that actually needs it.
        if not impersonate and len(passes) > 1:
            IMPERSONATE_FAILURES["n"] += 1
            note(f"impersonation was rejected by {_host_of(url)} — retrying without it")
            if IMPERSONATE_FAILURES["n"] >= MAX_IMPERSONATE_FAILURES:
                note(f"impersonation disabled after {IMPERSONATE_FAILURES['n']} hosts "
                     f"rejected it; bot-protected sites may not work until it is fixed")
        else:
            IMPERSONATE_FAILURES["n"] = 0
        return info
    if last is None:
        raise RuntimeError("yt-dlp failed")
    raise RuntimeError(with_hint(friendly(last)))


def _is_comment_playlist(info: dict) -> bool:
    """True when yt-dlp's generic extractor latched onto a page's comment links.

    On a busy WordPress page the first <a href> it likes can be a comment
    permalink, which it happily reports as a 200-entry "Comments for …"
    playlist. It has no playable formats, so accepting it as success means the
    video is never found and the user gets an empty player — and following it
    walks from page to page chasing other people's comments.
    """
    entries = info.get("entries")
    if not entries:
        return False
    if info.get("formats") or info.get("url"):
        return False                       # a real playlist that also plays
    title = str((info.get("playlist") or {}).get("title") or info.get("title") or "")
    if re.search(r"\bcomments?\b", title, re.I):
        return True
    links = [str(e.get("url") or "") for e in entries if isinstance(e, dict)]
    return bool(links) and all(
        re.search(r"/comment-page-\d+|#comment-\d+|/comments?/|#respond", u, re.I)
        for u in links)


def _known_adapter_host(url: str) -> bool:
    host = (_host_of(url) or "").lower()
    return any(host == k or host.endswith("." + k)
               for k in (*_PLAYER_SITES, *_API_SITES))


def _extract_sniffed(url: str, playlist: bool = False) -> dict:
    """Resolve a link: a site that needs an adapter first, then yt-dlp, then a
    bounded look for the media in the page ourselves.

    WordPress tube sites and similar wrappers hide the stream behind a player
    frame (`player.php?q=…`) or simply link to it, which the generic extractor
    does not follow. One bounded extra pass is usually enough to turn
    "Unsupported URL" into a playable format.
    """
    # Hosts with an adapter *start* there, and only fall back to yt-dlp if the
    # adapter cannot answer. The generic extractor costs 15+ s to crawl such a
    # page — and frequently answers "Unsupported URL" anyway — while the
    # adapter is two quick requests with the answer already in them. Measured
    # on a watchhentai page: 2.4 s through the adapter, 16.6 s through yt-dlp.
    api_error: Exception | None = None
    player_error: Exception | None = None
    tried_adapters: list[str] = []

    if _known_adapter_host(url):
        for step, fn in (("api", api_site_extract), ("player", player_site_extract)):
            tried_adapters.append(step)
            try:
                info = fn(url)
                if info is not None:
                    return info
            except NotAVideoPage:
                # The adapter read the page and found nothing any other path
                # could use either; report its verdict now instead of spending
                # the yt-dlp crawl + sniffer pass re-proving it.
                raise
            except Exception as exc:  # noqa: BLE001
                # Keep each adapter's diagnosis: the player one says exactly
                # what changed on that site ("the player frame carried no
                # source list"), which is the error worth reporting in the end.
                if step == "player":
                    player_error = exc
                else:
                    api_error = exc

    try:
        info = _extract(url, playlist=playlist)
        # "Succeeded" is not the same as "playable": a comment playlist has to
        # fall through to the ladder below instead of ending the search.
        if not _is_comment_playlist(info):
            return info
        note(f"{_host_of(url)}: yt-dlp only found comment links — looking for the "
             f"video itself")
        original = RuntimeError(
            f"yt-dlp found only comment links on {_host_of(url)}, not the video")
    except Exception as first:  # noqa: BLE001
        original = first

    # Sites that only expose their stream through a JSON API need asking first:
    # there is nothing in the HTML for the sniffer to find.
    if "api" not in tried_adapters:
        try:
            api_info = api_site_extract(url)
            if api_info is not None:
                return api_info
        except Exception as exc:  # noqa: BLE001
            api_error = exc

    # Player-frame hosts hand the browser an obfuscated media URL; run the same
    # decoder their own page runs.
    if "player" not in tried_adapters:
        try:
            player_info = player_site_extract(url)
            if player_info is not None:
                return player_info
        except Exception as exc:  # noqa: BLE001
            player_error = exc

    for kind, candidate in sniff_targets(url):
        try:
            if kind == "media":
                return synthetic_info(url, candidate)
            found = _extract(candidate, playlist=(kind == "feed"))
            # An RSS feed on a comment-heavy page yields a playlist of comments:
            # no formats, and every entry is another page's comment permalink.
            if not _is_comment_playlist(found):
                return found
            note(f"{_host_of(candidate)} is only a comment feed — skipping it")
        except Exception:  # noqa: BLE001
            continue

    # The adapter's own complaint is the most specific one, so it wins.
    for err in (player_error, api_error, original):
        if err is not None:
            raise err
    raise RuntimeError(f"nothing playable found on {_host_of(url)}")


def clean_reason(text: str) -> str:
    """Drop yt-dlp's wiki boilerplate so the answer is one sentence, not a dump."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    kept = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
            if s and "http" not in s]
    return " ".join(kept).strip(" .—-") or text[:400]


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).hostname or url[:40]
    except Exception:  # noqa: BLE001
        return url[:40]


# Bot checks, logins and age gates are the three things that make a host
# "work on my laptop but not here". Say which one it is instead of leaving the
# user with a bare yt-dlp error.
_HINTS = (
    (("sign in to confirm", "cloudflare", "cf-mitigated", "datadome", "captcha",
      "are you a robot", "bot check", "access denied", "forbidden",
      "redirection detected", "how do i pass cookies"),
     "this host is fingerprinting or blocking the server — install curl_cffi "
     "(YTDLP_IMPERSONATE=chrome) and/or run the backend from a non-datacenter IP"),
    (("login", "log in", "sign in", "premium", "member", "subscribe", "paid", "ppv"),
     "this host wants an account or a cookie — export cookies.txt from a browser "
     "that is already logged in and set VIEWER_COOKIES_B64 (see the README)"),
    (("age", "verify", "consent", "parental", "are you over"),
     "this host shows an age/consent gate — cookies from a browser that passed it "
     "fix it (VIEWER_COOKIES_B64)"),
    (("geo", "in your country", "not available in", "region", "blocked in"),
     "this host is region-locked — use a proxy via YTDLP_ARGS='--proxy …' or cookies "
     "from the right region"),
)

# Nothing a server can do about these, so never blame the platform either.
_TERMINAL_HINTS = (
    (("private", "removed", "deleted", "terminated", "unavailable"),
     "this video is private, removed or gone — nothing a server can do"),
    (("unsupported url", "no video formats", "no formats found"),
     "this page does not embed a video — it is a landing page that links elsewhere, "
     "so there is nothing here to extract"),
)


def _matches(text_low: str, needle: str) -> bool:
    """Word-boundary match, so 'age' does not fire on 'webpage'."""
    return re.search(r"\b" + re.escape(needle).replace(r"\ ", r"\s+"), text_low) is not None


def with_hint(reason: str) -> str:
    """Say what failed *and* the one action that fixes it."""
    reason = clean_reason(reason)
    low = reason.lower()
    where = (f" (your backend runs on {PLATFORM}, whose IP ranges these hosts block "
             f"outright — a VPS, Fly.io or a home connection usually works)"
             if PLATFORM else "")
    for needles, hint in _HINTS:
        if any(_matches(low, n) for n in needles):
            return f"{reason} — {hint}{where}"
    for needles, hint in _TERMINAL_HINTS:
        if any(_matches(low, n) for n in needles):
            return f"{reason} — {hint}"
    return reason


def _subprocess_extract(url: str, playlist: bool = False,
                        impersonate: bool = True) -> dict:
    plans: list[list[str]] = []
    flat = ["-J", "--yes-playlist", "--flat-playlist", url]
    if playlist:
        plans.append(flat)              # playlists / channels / profiles
    plans.append(["-J", "--no-playlist", url])
    if not playlist:
        plans.append(flat)              # the "looks like a playlist" fallback
    plans.append(["-J", "--no-playlist", "--ignore-no-formats-error", url])

    best = ""                      # best real ERROR seen across attempts
    detail = ""                    # last useful line, warning or error
    for plan in plans:
        try:
            rc, out, err = run_yt_dlp(plan, impersonate=impersonate)
        except subprocess.TimeoutExpired as exc:
            best = best or friendly(exc)
            continue
        if rc == 0 and (out or "").strip():
            try:
                info = json.loads(out)
            except ValueError:
                detail = "yt-dlp returned malformed JSON"
                continue
            if isinstance(info, dict) and (info.get("formats") or info.get("entries")
                                           or info.get("url")):
                return info
            # Reached via --ignore-no-formats-error: keep looking, but remember
            # why, so the client gets a sentence instead of an HTML page.
            detail = (ytdlp_reason(err) or
                      f"yt-dlp found no downloadable formats for {url} — it may be "
                      f"private, region-locked, deleted or live-only")
            continue
        if [l for l in (err or "").splitlines() if l.strip().upper().startswith("ERROR")]:
            best = ytdlp_reason(err)
        else:
            detail = detail or ytdlp_reason(err)
    raise RuntimeError(best or detail or f"yt-dlp could not extract {url}")


def get_info(url: str, *, fresh: bool = False, playlist: bool = False) -> dict:
    """Cached `yt-dlp -J <url>`.

    `fresh=True` skips both caches — used to recover from an expired CDN URL.
    Concurrent requests for one URL share a single extraction.
    """
    key = _key(url, playlist)
    now = time.time()

    if not fresh:
        info = _mem_get(key, now)
        if info is not None:
            STATS["hit"] += 1
            return info
        if DISK_CACHE:
            info = _disk_get(key, now)
            if info is not None:
                _mem_put(key, now + CACHE_TTL, info)
                STATS["disk"] += 1
                return info
        neg = _NEG.get(key)
        if neg and neg[0] > now:
            STATS["neg"] += 1
            raise RuntimeError(neg[1])
        STATS["miss"] += 1
    else:
        STATS["refresh"] += 1

    with _lock_for(key):
        if not fresh:                    # someone may have filled it while we waited
            info = _mem_get(key, time.time())
            if info is None and DISK_CACHE:
                info = _disk_get(key, time.time())
                if info is not None:
                    _mem_put(key, time.time() + CACHE_TTL, info)
            if info is not None:
                return info

        acquired = _SLOTS.acquire(blocking=False) if _SLOTS else True
        if not acquired:
            STATS["busy"] += 1
            raise RuntimeError("every extraction slot is busy — try again in a few seconds")
        try:
            info = _extract_sniffed(url, playlist=playlist)
        except Exception as exc:  # noqa: BLE001
            _NEG[key] = (time.time() + NEG_TTL, friendly(exc))
            raise
        finally:
            if _SLOTS is not None:
                _SLOTS.release()

        exp = time.time() + CACHE_TTL
        _mem_put(key, exp, info)
        _disk_put(key, exp, info)
        _NEG.pop(key, None)
        return info


# --------------------------------------------------------------------------- #
#  Small helpers
# --------------------------------------------------------------------------- #

def human_size(n) -> str:
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def valid_url(url: str) -> bool:
    return str(url or "").startswith(("http://", "https://"))


def is_private_url(url: str) -> bool:
    """Optional SSRF guard for public deployments (VIEWER_BLOCK_PRIVATE=1)."""
    if not BLOCK_PRIVATE:
        return False
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = info[4][0]
        if ip.startswith(("127.", "10.", "192.168.", "169.254.")) or ip in ("::1", "0.0.0.0"):
            return True
        if ip.startswith("172.") and 16 <= int(ip.split(".")[1] or 0) <= 31:
            return True
    return False


class Expired(Exception):
    """The CDN rejected our URL — it was signed and has now run out."""


def _chunks(resp, prefix: bytes = b""):
    """Yield an already-read prefix, then the rest of the response body."""
    if prefix:
        yield prefix
    while True:
        try:
            buf = resp.read(STREAM_CHUNK)
        except (socket.timeout, TimeoutError, OSError):
            return                      # truncated stream: end the response here
        if not buf:
            return
        yield buf


# --------------------------------------------------------------------------- #
#  Format selection
# --------------------------------------------------------------------------- #

MERGE_ID = "__merge__"      # synthetic format id for the ffmpeg-stitched stream
MUXED_OK_EXT = ("mp4", "m4v", "webm", "mov", "mkv")
AUDIO_EXTS = {"m4a", "mp3", "aac", "opus", "ogg", "oga", "wav", "flac", "weba"}


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


def _clean_headers(raw: dict | None) -> dict:
    """Keep only the headers we are willing to replay upstream."""
    return {k: v for k, v in (raw or {}).items()
            if k and v and str(k).lower() in HEADER_WHITELIST}


def _codec_state(value) -> str:
    """'yes' | 'no' | 'unknown' — yt-dlp uses null for "did not report"."""
    if value is None:
        return "unknown"
    text = str(value).strip().lower()
    if text in ("", "none"):
        return "no" if text else "unknown"
    return "yes"


def static_build() -> str:
    """A short id for the current frontend files.

    The phone keeps a page open while you edit the code on the server. Without
    this the tab happily runs the old JavaScript against the new backend, and a
    fixed bug looks like it is still broken. index.html gets this value
    substituted into its build meta tag, and app.js compares it with /api/health.
    """
    newest = 0.0
    for name in ("index.html", "app.js", "ytdlp.js", "style.css", "config.js"):
        try:
            newest = max(newest, os.path.getmtime(os.path.join(ROOT, name)))
        except OSError:
            continue
    return hashlib.sha1(f"{int(newest)}".encode()).hexdigest()[:10]


def browsable_formats(info: dict) -> list[dict]:
    """Slim, browser-usable view over whatever yt-dlp reported."""
    fmts, seen = [], set()

    def push(f: dict) -> None:
        url = f.get("url")
        if not url or not str(url).startswith("http"):
            return
        proto = (f.get("protocol") or "").lower()
        ext = (f.get("ext") or "").lower()
        sized = bool(f.get("height") or f.get("width"))
        # Several extractors (Pornhub and others) report vcodec/acodec as null
        # even for perfectly playable progressive files. null means "unknown";
        # only the string "none" means the track is genuinely absent. Calling
        # every codec-less format audio-only hid the best stream on the site and
        # pushed the default onto HLS, which needs hls.js in the browser.
        v_state, a_state = _codec_state(f.get("vcodec")), _codec_state(f.get("acodec"))
        has_video = v_state == "yes" or (v_state == "unknown" and sized)
        has_audio = (a_state == "yes"
                     or (a_state == "unknown" and (sized or ext in AUDIO_EXTS)))
        if f.get("_sniffed"):                 # we found the file ourselves
            has_video = has_audio = True

        if "m3u8" in proto or ext == "m3u8":
            kind, label = "hls", "HLS (adaptive stream)"
        elif "mpd" in proto or ext == "mpd" or "http_dash_segments" in proto:
            kind, label = "dash", "DASH (not playable directly)"
        elif ext == "mhtml":
            kind, label = "dash", "storyboard (not playable)"
        else:
            kind = "direct"
            if f.get("_sniffed"):
                # A file we found ourselves. When we know the resolution, lead
                # with it — "1080p · mp4" is the only thing that tells the
                # quality buttons apart when a site offers several.
                label = f"{f['height']}p · {ext or 'media'}" if f.get("height") \
                    else f"{ext or 'media'} · found in the page"
            elif has_video and has_audio:
                label = f"{f['height']}p" if f.get("height") else (f.get("format_note") or "video+audio")
                if ext:
                    label += f" · {ext}"
            elif has_video:
                label = f"{f['height']}p · {ext or 'video'} · no audio"
            else:
                label = f"audio only · {ext or 'audio'}"

        headers = _clean_headers(f.get("http_headers"))
        size = f.get("filesize") or f.get("filesize_approx")

        entry = {
            "id": str(f.get("format_id")),
            "label": label,
            "kind": kind,                      # direct | hls | dash | merge
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
            "expiresIn": f.get("expires_in"),
            "sniffed": bool(f.get("_sniffed")),
            # Anything beyond a plain UA has to be re-fetched by us: <video> and
            # hls.js cannot attach Referer/Cookie cross-origin. That single flag
            # is what makes header-protected (18+, TikTok, …) links playable.
            "needsProxy": any(k.lower() != "user-agent" for k in headers),
            "headers": headers,
        }
        key = (entry["id"], entry["url"][:64])
        if key in seen:
            return
        seen.add(key)
        fmts.append(entry)

    for group in (info.get("formats") or [], info.get("requested_formats") or []):
        for f in group:
            if isinstance(f, dict):
                push(f)

    if not fmts and info.get("url"):
        headers = _clean_headers(info.get("http_headers"))
        fmts.append({
            "id": "0", "label": "direct", "kind": "direct", "ext": info.get("ext"),
            "width": None, "height": None, "fps": None,
            "hasAudio": True, "hasVideo": True,
            "url": info["url"],
            "needsProxy": any(k.lower() != "user-agent" for k in headers),
            "headers": headers, "size": info.get("filesize"),
            "sizeHuman": human_size(info.get("filesize")),
            "protocol": (info.get("protocol") or ""), "expiresIn": info.get("expires_in"),
        })

    rank = {"direct": 0, "hls": 1, "merge": 2, "dash": 3}
    fmts.sort(key=lambda e: (
        rank.get(e["kind"], 4),
        -(e["height"] or 0),
        -(e["fps"] or 0),
        -e["size"] if e["size"] else 0,
        e["label"] or "",
    ))
    return fmts


def best_format(fmts: list[dict]) -> dict | None:
    """Pick what to play by default.

    Progressive file with sound and picture first, then an HLS playlist, then
    whatever yt-dlp considers best. Audio-only is never the default.
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


def playlist_entries(info: dict, limit: int = 200) -> list[dict]:
    """Flatten yt-dlp playlist/channel output into something the UI can list."""
    out = []
    for e in (info.get("entries") or []):
        if not isinstance(e, dict):
            continue
        ie = (e.get("ie_key") or e.get("extractor_key") or "").lower()
        url = e.get("url") or e.get("webpage_url") or e.get("original_url")
        if not url and e.get("id") and "youtu" in ie:
            url = f"https://www.youtube.com/watch?v={e['id']}"
        # yt-dlp appends an internal smuggle fragment to some URLs; it is for
        # yt-dlp, not for browsers, and it would pollute our cache keys.
        url = (url.split("#__", 1)[0] if url else url)
        if url and not valid_url(url):
            url = None
        thumb = None
        for t in reversed(e.get("thumbnails") or []):
            if isinstance(t, dict) and t.get("url"):
                thumb = t["url"]
                break
        out.append({
            "id": str(e.get("id") or len(out) + 1),
            "title": (e.get("title") or e.get("id") or "untitled")[:160],
            "url": url,
            "duration": e.get("duration"),
            "uploader": e.get("uploader") or e.get("channel"),
            "thumb": thumb,
            "live": bool(e.get("live_status") in ("is_live", "is_upcoming")),
        })
        if len(out) >= limit:
            break
    return out


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


# --------------------------------------------------------------------------- #
#  Resolution: page URL → streamable target
# --------------------------------------------------------------------------- #

def resolve_target(page: str, fmt: str = "", fresh: bool = False) -> dict:
    """Turn (page url, format id) into {url, headers, kind} we can stream."""
    if not valid_url(page):
        raise ValueError("Missing/invalid ?url=")
    if is_private_url(page):
        raise ValueError("Refusing to fetch a private/loopback address")
    looks_like_media = bool(MEDIA_EXT_RE.search(page))
    if not fmt and (ytdlp_mode() == "none" or looks_like_media):
        return {"url": page, "headers": {}, "kind": "direct", "id": None}

    info = get_info(page, fresh=fresh, playlist=PLAYLIST_PROBE)
    fmts = browsable_formats(info)
    chosen = next((f for f in fmts if fmt and f["id"] == fmt), None)
    if chosen is None:
        chosen = best_format(fmts)
    if chosen is None:
        if not looks_like_media:
            # Never hand a web page back as if it were media — that would just
            # produce a confusing "video doesn't load" further down the stack.
            raise RuntimeError(
                "yt-dlp found nothing playable for this link — it may be private, "
                "region-locked, deleted, or subscriber-only")
        return {"url": page, "headers": {}, "kind": "direct", "id": None}
    if chosen["kind"] == "merge":
        return {"url": page, "headers": {}, "kind": "merge", "id": MERGE_ID}
    return {
        "url": chosen["url"],
        "headers": chosen.get("headers") or {},
        "kind": chosen["kind"],
        "id": chosen["id"],
        "ext": chosen.get("ext"),
    }


def resolve_headers(page: str, fmt: str = "") -> dict:
    """Headers the CDN wants — best effort, never raises."""
    if not valid_url(page) or ytdlp_mode() == "none":
        return {}
    try:
        return resolve_target(page, fmt).get("headers") or {}
    except Exception:  # noqa: BLE001
        return {}


def safe_filename(title: str, ext: str = "mp4") -> str:
    """A download name that survives every OS, plus an RFC 5987 UTF-8 version."""
    base = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", " ", title or "").strip()
    base = re.sub(r"\s+", " ", base)[:90].strip(" .") or "video"
    ext = (ext or "mp4").lower().strip(".") or "mp4"
    if not base.lower().endswith("." + ext):
        base = f"{base}.{ext}"
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", base).replace('"', "'")[:120]
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{urllib.parse.quote(base, safe='')}")


def resolve_download(page: str, fmt: str = "") -> dict:
    """Pick a *single file* to hand the browser as a download.

    Adaptive HLS is not a file, so it is skipped: saving an .m3u8 as .mp4 would
    be worse than saying why. A plain media URL counts even when the extractor
    reports no codecs — the container tells the browser what it is anyway.
    """
    info = get_info(page, playlist=PLAYLIST_PROBE)
    fmts = browsable_formats(info)
    direct = [f for f in fmts if f["kind"] == "direct"]

    chosen = next((f for f in direct if fmt and f["id"] == fmt), None)
    if chosen is None:
        def rank(f: dict) -> tuple:
            # Default downloads should be playable anywhere and not enormous:
            # mp4 first, capped at 1080p, then webm/mkv, then anything bigger.
            big = (f.get("height") or 0) > 1080
            return (0 if f["ext"] in ("mp4", "m4v") and not big else
                    1 if f["ext"] in MUXED_OK_EXT and not big else
                    2 if f["ext"] in MUXED_OK_EXT else 3,
                    -(f.get("height") or 0))
        direct.sort(key=rank)
        chosen = direct[0] if direct else None

    if chosen is None:
        kinds = sorted({f["kind"] for f in fmts}) or ["nothing"]
        raise RuntimeError(
            f"this link has no single file to save — yt-dlp only found "
            f"{', '.join(kinds)} here"
            + (", and the ffmpeg merge can still produce a file"
               if FFMPEG is not None else ""))
    return {
        "url": chosen["url"],
        "headers": chosen.get("headers") or {},
        "title": info.get("title"),
        "ext": chosen.get("ext") or "mp4",
        "height": chosen.get("height"),
    }


# --------------------------------------------------------------------------- #
#  Subtitle conversion, cached on disk
# --------------------------------------------------------------------------- #

def cached_vtt(sub_url: str, timeout: int = 90) -> str | None:
    """Convert a caption track to .vtt once, then serve it from disk."""
    if not valid_url(sub_url):
        return None
    ensure_dirs()
    path = os.path.join(SUB_DIR, hashlib.sha1(sub_url.encode("utf-8")).hexdigest() + ".vtt")
    try:
        if time.time() - os.path.getmtime(path) < SUB_TTL and os.path.getsize(path):
            return path
    except OSError:
        pass

    parent = SUB_DIR if os.access(SUB_DIR, os.W_OK) else None
    try:
        work = tempfile.mkdtemp(prefix="sub-", dir=parent)
    except OSError:
        return None
    base = os.path.join(work, "sub")
    try:
        rc, _, _ = run_yt_dlp(
            ["--skip-download", "--convert-subs", "vtt", "-o", base + ".%(ext)s", sub_url],
            timeout=timeout)
        produced = None
        if rc == 0:
            for cand in (base + ".vtt", base + ".srt", base + ".ttml",
                         base + ".srv3", base + ".json3"):
                if os.path.exists(cand):
                    produced = cand
                    break
        if not produced:
            return None
        with open(produced, encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        if not produced.endswith(".vtt"):
            body = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", body)
        if not body.lstrip().startswith("WEBVTT"):
            body = "WEBVTT\n\n" + body
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
        return path
    except (OSError, RuntimeError):
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
#  Rate limiting
# --------------------------------------------------------------------------- #

class SlidingWindow:
    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self.lock = threading.Lock()
        self.hits: dict[str, collections.deque] = {}

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.time()
        with self.lock:
            dq = self.hits.setdefault(key, collections.deque())
            while dq and now - dq[0] > self.window:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            if len(self.hits) > 4096:
                for k in [k for k, v in self.hits.items()
                          if not v or now - v[-1] > self.window]:
                    self.hits.pop(k, None)
            return True


EXTRACT_LIMIT = SlidingWindow(RATE_PER_MIN)


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
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".vtt": "text/vtt; charset=utf-8",
    ".webvtt": "text/vtt; charset=utf-8",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".m3u8": "application/vnd.apple.mpegurl",
}
# content-encoding is *not* hop-by-hop: upstream bytes are forwarded untouched,
# so dropping that header would hand the browser gzipped data it cannot read.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
# Upstream hop/connection metadata we re-generate (or simply do not want).
PROXY_DROP = {"date", "server", "alt-svc", "strict-transport-security",
              "content-security-policy", "x-content-type-options",
              "cross-origin-resource-policy"}
CORS = ("Access-Control-Allow-Origin", "*")
EXPOSE = ("Content-Length, Content-Range, Accept-Ranges, Content-Type, ETag, "
          "Last-Modified, X-Viewer-Cache, X-Viewer-Reason")


class Handler(BaseHTTPRequestHandler):
    server_version = f"Viewer/{VERSION}"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------- #
    def _cors(self, preflight: bool = False) -> None:
        self.send_header(*CORS)
        self.send_header("Access-Control-Expose-Headers", EXPOSE)
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Max-Age", "86400")

    def _send_json(self, obj, status: int = 200, cache: str = "no-store") -> None:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        tag = '"' + hashlib.sha1(body).hexdigest()[:20] + '"'
        try:
            if HTTP_CACHE and self.headers.get("If-None-Match") == tag:
                self.send_response(304)
                self.send_header("ETag", tag)
                self.send_header("Cache-Control", cache)
                self._cors()
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            if HTTP_CACHE:
                self.send_header("ETag", tag)
            self._cors()
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error_json(self, msg: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": msg}, status)

    def _raw(self, data: bytes, ctype: str, status: int = 200, cache: str = "no-store",
             etag: str | None = None, head_only: bool = False) -> None:
        try:
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", cache)
                self._cors()
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache)
            if etag:
                self.send_header("ETag", etag)
            self._cors()
            self.end_headers()
            if not head_only:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, path: str, head_only: bool = False) -> None:
        rel = path.split("?", 1)[0].lstrip("/") or "index.html"
        root = os.path.realpath(ROOT)
        target = os.path.realpath(os.path.join(root, rel))
        if target != root and not target.startswith(root + os.sep):
            return self._send_error_json("Forbidden", 403)
        if os.path.isdir(target) or not os.path.isfile(target):
            return self._send_error_json("Not found", 404)
        ctype = MIME.get(os.path.splitext(target)[1].lower(), "application/octet-stream")
        st = os.stat(target)
        etag = f'"{int(st.st_mtime):x}-{st.st_size:x}"'

        # index.html carries the build id, so a page left open in a browser can
        # notice it is running yesterday's JavaScript (see static_build()).
        if rel == "index.html":
            try:
                with open(target, "rb") as fh:
                    page = fh.read().replace(b"<!--BUILD-->",
                                             static_build().encode())
                return self._raw(page, ctype, cache="no-cache", etag=etag,
                                 head_only=head_only)
            except OSError:
                pass

        try:
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self._cors()
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(st.st_size))
            self.send_header("Last-Modified", self.date_time_string(int(st.st_mtime)))
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")     # revalidate → instant 304s
            self._cors()
            self.end_headers()
            if head_only or self.command == "HEAD":
                return
            with open(target, "rb") as fh:
                shutil.copyfileobj(fh, self.wfile, STREAM_CHUNK)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_file(self, path: str, ctype: str, head_only: bool = False) -> None:
        try:
            st = os.stat(path)
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            return self._send_error_json(f"Could not read {path}: {exc}", 500)
        self._raw(data, ctype, cache=f"public, max-age={min(SUB_TTL, 3600)}",
                  etag=f'"{int(st.st_mtime):x}-{st.st_size:x}"', head_only=head_only)

    def _query(self) -> tuple[dict, str]:
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        return {k: v[0] for k, v in q.items()}, parsed.path

    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "-"

    def _authorized(self, q: dict) -> bool:
        if not TOKEN:
            return True
        supplied = (q.get("token") or self.headers.get("X-Viewer-Token")
                    or (self.headers.get("Authorization") or "").replace("Bearer ", "").strip())
        return bool(supplied) and hmac.compare_digest(str(supplied), TOKEN)

    # -- verbs ------------------------------------------------------------- #
    def do_GET(self) -> None:      # noqa: N802
        self._route(head=False)

    def do_HEAD(self) -> None:     # noqa: N802
        self._route(head=True)

    def do_OPTIONS(self) -> None:  # noqa: N802
        try:
            self.send_response(204)
            self._cors(preflight=True)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _route(self, head: bool = False) -> None:
        try:
            q, path = self._query()
            path = path.rstrip("/") or "/"
            routes = {
                "/api/health": lambda: self._api_health(),
                "/api/info": lambda: self._api_info(q, head),
                "/api/play": lambda: self._api_play(q, head),
                "/api/merge": lambda: self._api_merge(q, head),
                "/api/hls": lambda: self._api_hls(q, head),
                "/api/subsrc": lambda: self._api_subsrc(q, head),
            }
            fn = routes.get(path)
            if fn is not None:
                if path != "/api/health":
                    if not self._authorized(q):
                        return self._send_error_json("Invalid or missing token", 401)
                    # Only the expensive/extraction calls are limited — a single
                    # HLS stream legitimately fires hundreds of segment requests.
                    heavy = path in ("/api/info", "/api/merge") or (
                        path == "/api/hls" and q.get("raw") != "1")
                    if heavy and not EXTRACT_LIMIT.allow(self._client_ip()):
                        return self._send_error_json(
                            "Too many requests from this address — try again shortly", 429)
                return fn()
            if path.startswith("/api/"):
                return self._send_error_json(f"Unknown API route {path}", 404)
            self._serve_static(path, head_only=head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Expired as exc:
            self._send_error_json(friendly(exc), 502)
        except Exception as exc:  # noqa: BLE001
            _, path = self._query()
            note(f"{path} failed: {exc}")
            if DEBUG:
                import traceback
                traceback.print_exc()
            self._send_error_json(f"{exc.__class__.__name__}: {friendly(exc)}", 502)

    def log_message(self, fmt, *args) -> None:
        ua = (self.headers.get("User-Agent") or "")[:70]
        sys.stderr.write("[http] %s | %s\n" % (fmt % args, ua))

    # -- API ---------------------------------------------------------------- #
    def _api_health(self) -> None:
        mode = ytdlp_mode()
        self._send_json({
            "ok": True,
            "backend": True,
            "version": VERSION,
            "ytDlpAvailable": mode != "none",
            "ytDlpMode": mode,                    # module | binary | none
            "ytDlpVersion": yt_dlp_version(),
            "ytDlpBinary": YT_DLP,                # the console script, if any
            "ytDlpModule": HAS_YTDLP_PY,          # importable as `import yt_dlp`
            "python": sys.version.split()[0],
            "ffmpegAvailable": FFMPEG is not None,
            "features": {
                "proxy": True,
                "hls": True,
                "merge": FFMPEG is not None and mode != "none",
                "playlists": PLAYLIST_PROBE,
                "cookies": bool(cookies_path() and os.path.isfile(cookies_path())),
                "impersonate": IMPERSONATE if impersonate_ready() else None,
                "subtitles": True,
                "cache": True,
                "download": True,
            },
            "tokenRequired": bool(TOKEN),
            "staticBuild": static_build(),
            "platform": PLATFORM or "self-hosted",
            "cache": {"dir": CACHE_DIR, "entries": len(_MEM), **STATS},
            "notices": notice[-4:],
        }, cache="no-store")

    def _api_info(self, q: dict, head: bool = False) -> None:
        url = q.get("url", "")
        if not valid_url(url):
            return self._send_error_json("Only http(s) URLs can be extracted", 400)
        if is_private_url(url):
            return self._send_error_json("Refusing to fetch a private/loopback address", 400)
        if ytdlp_mode() == "none":
            return self._send_json({
                "ok": True, "url": url, "extractable": False,
                "reason": "yt-dlp is not installed on the server. "
                          "Install it with `pip install -U yt-dlp` and restart server.py.",
                "formats": [], "title": None, "duration": None,
                "thumbnail": None, "subtitles": [], "autoFormatId": None,
            })
        try:
            info = get_info(url, fresh=q.get("fresh") == "1", playlist=PLAYLIST_PROBE)
        except Exception as exc:  # noqa: BLE001
            # Not fatal: the page still tries to play the link directly.
            return self._send_json({
                "ok": True, "url": url, "extractable": False,
                "reason": friendly(exc), "formats": [], "title": None,
                "duration": None, "thumbnail": None, "subtitles": [],
                "autoFormatId": None,
            })

        is_playlist = info.get("_type") == "playlist" or bool(info.get("entries"))
        entries = playlist_entries(info) if is_playlist else []
        idx = 0
        if is_playlist and entries:
            try:
                idx = max(0, min(len(entries) - 1, int(q.get("idx") or 0)))
            except (TypeError, ValueError):
                idx = 0
            picked = next((e for e in entries[idx:idx + 1] if e.get("url")), None)
            if not picked:
                return self._send_json({
                    "ok": True, "url": url, "extractable": False,
                    "reason": "This playlist has no playable entry.",
                    "title": info.get("title"), "formats": [], "autoFormatId": None,
                    "duration": None, "thumbnail": None, "subtitles": [],
                    "playlist": {"title": info.get("title"), "count": len(entries),
                                 "entries": entries, "index": idx},
                }, cache=self._api_cache())
            if picked["url"] != url and not q.get("entries"):
                try:
                    info = get_info(picked["url"], playlist=False)
                    url = picked["url"]
                except Exception as exc:  # noqa: BLE001
                    return self._send_json({
                        "ok": True, "url": picked["url"], "extractable": False,
                        "reason": friendly(exc), "formats": [], "title": None,
                        "duration": None, "thumbnail": None, "subtitles": [],
                        "autoFormatId": None,
                        "playlist": {"title": info.get("title"), "count": len(entries),
                                     "entries": entries, "index": idx},
                    }, cache=self._api_cache())

        fmts = browsable_formats(info)
        chosen = best_format(fmts)
        merge = merge_offer(fmts, url)
        if merge:
            fmts.insert(0, merge)

        payload = {
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
        }
        if is_playlist and entries:
            payload["playlist"] = {"title": info.get("title"), "count": len(entries),
                                   "entries": entries, "index": idx}
        self._send_json(payload, cache=self._api_cache())

    @staticmethod
    def _api_cache() -> str:
        # Shared caches must never store a token-gated response.
        return "no-store" if TOKEN else f"public, max-age={min(CACHE_TTL, 300)}"

    def _api_play(self, q: dict, head: bool = False) -> None:
        """Serve one stream: 302 → the source, or pipe the bytes ourselves."""
        src = q.get("url", "") or q.get("u", "")
        fmt = q.get("fmt", "") or q.get("f", "")
        if not valid_url(src):
            return self._send_error_json("Missing/invalid ?url=", 400)
        parsed = urllib.parse.urlparse(src)
        if parsed.path.startswith("/api/") and parsed.netloc == self.server_host:
            return self._send_error_json("Refusing to proxy our own API", 400)

        if q.get("dl") == "1":
            try:
                target = resolve_download(src, fmt)
            except ValueError as exc:
                return self._send_error_json(friendly(exc), 400)
            except Exception as exc:  # noqa: BLE001
                # Only adaptive streams? Let ffmpeg stitch a real file instead.
                if FFMPEG is not None and ytdlp_mode() != "none":
                    link = f"/api/merge?url={urllib.parse.quote(src, safe='')}&dl=1"
                    if TOKEN:
                        link += f"&token={urllib.parse.quote(TOKEN, safe='')}"
                    return self._redirect(link)
                return self._send_error_json(friendly(exc), 502)
            name = safe_filename(target["title"] or f"video-{target['ext']}",
                                 target["ext"])
            note(f"download {target['ext']} ← {src[:70]}")
            try:
                self._proxy_with_retry(src, fmt,
                                       {"url": target["url"],
                                        "headers": target["headers"]},
                                       head=head, reject_html=True,
                                       disposition=name)
            except Expired as exc:
                self._send_error_json(friendly(exc), 502)
            return

        try:
            target = resolve_target(src, fmt, fresh=q.get("fresh") == "1")
        except ValueError as exc:
            return self._send_error_json(friendly(exc), 400)
        except Exception as exc:  # noqa: BLE001
            if not MEDIA_EXT_RE.search(src):
                return self._send_error_json(friendly(exc), 502)
            # A plain media URL: just fetch it, no extraction needed.
            target = {"url": src, "headers": {}, "kind": "direct", "id": None}

        if target["kind"] == "merge":
            link = f"/api/merge?url={urllib.parse.quote(src, safe='')}"
            if TOKEN:
                link += f"&token={urllib.parse.quote(TOKEN, safe='')}"
            return self._redirect(link)

        if q.get("proxy") != "1":
            return self._redirect(target["url"])

        try:
            self._proxy_with_retry(src, fmt, target, head=head, reject_html=True)
        except Expired as exc:
            self._send_error_json(
                f"{friendly(exc)} — reload, pick another quality, or try 'Force proxy'.",
                502)

    def _redirect(self, url: str) -> None:
        try:
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _proxy_with_retry(self, page: str, fmt: str, target: dict,
                          head: bool = False, allow_refresh: bool = True,
                          reject_html: bool = False, disposition: str | None = None) -> None:
        """Stream `target`, re-extracting once if the CDN says the URL expired."""
        for attempt in (0, 1):
            try:
                self._proxy(target["url"], target.get("headers") or {}, head=head,
                            reject_html=reject_html, disposition=disposition)
                return
            except Expired:
                if (attempt or not allow_refresh or ytdlp_mode() == "none"
                        or not valid_url(page)):
                    raise
                retried = resolve_target(page, fmt, fresh=True)   # may raise → 502
                if retried["url"] == target["url"] and not retried.get("headers"):
                    raise
                note(f"expired CDN url for {page[:70]} — re-extracted and retrying")
                target = retried

    def _open_upstream(self, url: str, headers: dict, head: bool = False):
        merged = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        for k, v in (headers or {}).items():
            if not k or v is None or k.lower() in HEADER_BLOCKLIST:
                continue
            merged[k] = v
        rng = self.headers.get("Range")
        if rng:
            merged["Range"] = rng
        req = urllib.request.Request(url, headers=merged, method="HEAD" if head else "GET")
        return urllib.request.urlopen(req, timeout=PROXY_TIMEOUT)

    def _proxy(self, url: str, headers: dict, head: bool = False,
               reject_html: bool = False, disposition: str | None = None) -> None:
        try:
            resp = self._open_upstream(url, headers, head)
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
            if code in EXPIRY_CODES or 500 <= code < 600:
                raise Expired(f"The CDN refused the stream ({code})") from exc
            raise RuntimeError(f"Upstream returned {code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Upstream refused the request ({exc.reason})") from exc

        self._send_upstream(resp, head=head, reject_html=reject_html,
                            disposition=disposition)

    def _send_upstream(self, resp, head: bool = False, reject_html: bool = False,
                       prefix: bytes = b"", disposition: str | None = None) -> None:
        """Mirror an upstream response to the client (self-framing when needed)."""
        with resp:
            up = {k.lower(): v for k, v in resp.getheaders()}
            ctype = (up.get("content-type") or "").split(";")[0].strip().lower()
            if reject_html and ctype in ("text/html", "application/json",
                                         "application/xhtml+xml"):
                # Someone answered with a web page: usually an expired signature
                # that got redirected to an error/consent page.
                raise RuntimeError(
                    "the host answered with a web page instead of media — the link "
                    "expired, needs cookies, or is region-locked")
            chunked = "content-length" not in up and not head
            try:
                self.send_response(getattr(resp, "status", None) or resp.getcode() or 200)
                for k, v in resp.getheaders():
                    if k.lower() in HOP_BY_HOP or k.lower() in PROXY_DROP:
                        continue
                    if disposition and k.lower() == "content-disposition":
                        continue
                    self.send_header(k, v)
                if disposition:
                    # Makes the browser save the file even when the request that
                    # started it was cross-origin (the `download` attribute cannot
                    # do that, and is ignored for cross-origin media).
                    self.send_header("Content-Disposition", disposition)
                if chunked:
                    # Re-frame the body ourselves: an upstream response without
                    # Content-Length would otherwise hang the client forever.
                    self.send_header("Transfer-Encoding", "chunked")
                if not up.get("cache-control"):
                    self.send_header("Cache-Control", "public, max-age=3600")
                self._cors()
                self.end_headers()
                if head:
                    return
                for buf in _chunks(resp, prefix):
                    if chunked:
                        self.wfile.write(b"%X\r\n" % len(buf) + buf + b"\r\n")
                    else:
                        self.wfile.write(buf)
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    # -- HLS ----------------------------------------------------------------- #
    def _api_hls(self, q: dict, head: bool = False) -> None:
        """Proxy an HLS playlist, and every segment/key it points at.

        `?url=<page>&fmt=<id>`                    → playlist, rewritten to point back here
        `?u=<absolute>&raw=1&url=<page>&fmt=<>` → one resource: playlist (rewritten
                                                   again, so master → media → segment
                                                   all work) or raw bytes.
        """
        page = q.get("url") or q.get("p") or ""
        fmt = q.get("fmt") or q.get("f") or ""
        target_url = q.get("u") or ""
        raw = q.get("raw") == "1"

        if raw:
            if not valid_url(target_url):
                return self._send_error_json("Missing/invalid ?u=", 400)
            manifest, headers = target_url, resolve_headers(page, fmt)
            allow_refresh = False
        else:
            allow_refresh = True
            if valid_url(target_url):
                manifest, headers = target_url, {}
            else:
                try:
                    target = resolve_target(page, fmt, fresh=q.get("fresh") == "1")
                except ValueError as exc:
                    return self._send_error_json(friendly(exc), 400)
                except Exception as exc:  # noqa: BLE001
                    return self._send_error_json(friendly(exc), 502)
                if target["kind"] == "merge":
                    return self._redirect(
                        f"/api/merge?url={urllib.parse.quote(page, safe='')}")
                manifest = target["url"]
                headers = target.get("headers") or {}
        if not valid_url(manifest):
            return self._send_error_json("Missing/invalid ?url=", 400)

        resp = None
        for attempt in (0, 1):
            try:
                resp = self._open_upstream(manifest, headers)
                break
            except urllib.error.HTTPError as exc:
                if (attempt or exc.code not in EXPIRY_CODES or not allow_refresh
                        or not (ytdlp_mode() != "none" and valid_url(page))):
                    return self._send_error_json(f"Upstream returned {exc.code}", 502)
                try:                        # signed playlist URL → refresh it
                    target = resolve_target(page, fmt, fresh=True)
                    manifest = target["url"]
                    headers = target.get("headers") or {}
                except Exception as exc:  # noqa: BLE001
                    return self._send_error_json(friendly(exc), 502)
            except (urllib.error.URLError, OSError) as exc:
                return self._send_error_json(friendly(exc), 502)
        if resp is None:
            return self._send_error_json("Could not reach the stream", 502)

        with resp:
            final = resp.geturl() or manifest
            peek = resp.read(512)

            if peek.lstrip()[:7] != b"#EXTM3U":      # a segment / key: plain bytes
                return self._send_upstream(resp, head=head, reject_html=True,
                                           prefix=peek)

            body = peek + resp.read(4 << 20)          # playlists are small
            text = body.decode("utf-8", "replace")

        token = f"&token={urllib.parse.quote(TOKEN, safe='')}" if TOKEN else ""

        def point_at(absolute: str) -> str:
            return (f"/api/hls?raw=1&u={urllib.parse.quote(absolute, safe='')}"
                    f"&url={urllib.parse.quote(page, safe='')}"
                    f"&fmt={urllib.parse.quote(fmt, safe='')}{token}")

        out = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                out.append(line)
            elif stripped.startswith("#"):
                # EXT-X-KEY / EXT-X-MAP / EXT-X-PART carry URIs in attributes.
                out.append(_URI_ATTR.sub(
                    lambda m: m.group(1) + point_at(
                        urllib.parse.urljoin(final, m.group(2))) + m.group(3), line))
            else:
                out.append(point_at(urllib.parse.urljoin(final, stripped)))
        self._raw(("\n".join(out) + "\n").encode("utf-8"),
                  "application/vnd.apple.mpegurl",
                  cache=f"public, max-age={MANIFEST_TTL}", head_only=head)

    # -- merge ---------------------------------------------------------------- #
    def _api_merge(self, q: dict, head: bool = False) -> None:
        """Stream video+audio as one ffmp4:

            yt-dlp -f 'bv*+ba' -o - <page>  |  ffmpeg -i pipe:0 -c copy -f mp4 pipe:1
        """
        page = q.get("url", "")
        sel = q.get("sel", "") or "bv*+ba/b"
        sort = q.get("sort", "") or "vcodec:h264:vp9:av1,+res:1080"
        download = q.get("dl") == "1"
        if not valid_url(page):
            return self._send_error_json("Missing/invalid ?url=", 400)
        if FFMPEG is None or ytdlp_mode() == "none":
            return self._send_error_json(
                "Merging needs both yt-dlp and ffmpeg available on the server", 501)

        dl = ytdlp_cmd(["--no-playlist", "--newline", "--concurrent-fragments", "4",
                        "-S", sort, "-f", sel, "-o", "-", page])
        out = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", "pipe:0", "-c", "copy",
               "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]

        if head:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-store")
                if download:
                    self.send_header("Content-Disposition", safe_filename("video", "mp4"))
                self._cors()
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            # No Content-Length: stream until the pipeline ends.
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header("Content-Disposition", safe_filename("video", "mp4"))
            self.send_header("Connection", "close")
            self.close_connection = True
            self._cors()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        note(f"{'download ' if download else 'merge '}{sel} ← {page[:70]}")
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
            for proc in (pipe_in, pipe_out):
                try:
                    proc.stderr.close()
                except OSError:
                    pass

    # -- subtitles -------------------------------------------------------------- #
    def _api_subsrc(self, q: dict, head: bool = False) -> None:
        """Feed a usable VTT URL to <track src>, converting when needed."""
        webpage, lang, sub = q.get("url", ""), q.get("lang", ""), q.get("sub", "")
        if sub:
            vtt = cached_vtt(sub)
            return self._serve_file(vtt, "text/vtt", head_only=head) if vtt \
                else self._redirect(sub)
        if not webpage or not lang:
            return self._send_error_json("Need ?url=page&lang=en", 400)
        try:
            info = get_info(webpage, playlist=PLAYLIST_PROBE)
        except Exception as exc:  # noqa: BLE001
            return self._send_error_json(friendly(exc), 502)
        tracks = subtitle_tracks(info)
        hit = next((t for t in tracks if t["raw_lang"] == lang), None)
        if not hit:
            return self._send_error_json("No captions found for that language", 404)
        vtt = cached_vtt(hit["url"])
        if vtt:
            return self._serve_file(vtt, "text/vtt", head_only=head)
        self._redirect(hit["url"])


# --------------------------------------------------------------------------- #
#  WSGI adapter — the same handler, for Vercel / gunicorn / uwsgi / render
# --------------------------------------------------------------------------- #

def _environ_headers(environ: dict) -> email.message.Message:
    msg = email.message.Message()
    for k, v in environ.items():
        if k.startswith("HTTP_"):
            msg[k[5:].replace("_", "-").title()] = v
    if environ.get("CONTENT_TYPE"):
        msg["Content-Type"] = environ["CONTENT_TYPE"]
    if environ.get("CONTENT_LENGTH"):
        msg["Content-Length"] = environ["CONTENT_LENGTH"]
    return msg


class _Writer:
    """`self.wfile` for WSGIRequest: turns header bytes into a WSGI response."""

    def __init__(self, handler: "WSGIRequest") -> None:
        self._h = handler

    def write(self, data) -> int:
        self._h._emit(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class WSGIRequest(Handler):
    """Runs a single request described by a WSGI environ.

    BaseHTTPRequestHandler writes the status line and headers straight into
    `self.wfile`, so `_emit` parses that first chunk and hands the status and
    header list to `start_response`; everything after it is the body.
    """

    def __init__(self, environ: dict, start_response) -> None:
        self.environ = environ
        self._start_response = start_response
        self._body = io.BytesIO()
        self._sent = False
        self.client_address = (environ.get("REMOTE_ADDR", "-"), 0)
        self.request_version = "HTTP/1.1"
        self.requestline = ""
        self.command = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/") or "/"
        qs = environ.get("QUERY_STRING", "")
        if qs:
            self.path += "?" + qs
        self.headers = _environ_headers(environ)
        self.close_connection = True
        host = environ.get("HTTP_HOST") or "localhost"
        self.server_host, _, port = host.partition(":")
        self.server_port = int(port) if port.isdigit() else 80
        self.server_name = socket.getfqdn()
        self.wfile = _Writer(self)

    # -- WSGI plumbing ----------------------------------------------------- #
    def _emit(self, data: bytes) -> None:
        if not self._sent:
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1", "replace").split("\r\n")
            bits = lines[0].split(" ", 2)
            code = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 500
            reason = bits[2] if len(bits) > 2 else "OK"
            headers = []
            for line in lines[1:]:
                name, _, value = line.partition(":")
                if name:
                    headers.append((name.strip(), value.strip()))
            self._sent = True
            try:
                self._start_response(f"{code} {reason}", headers)
            except Exception:  # noqa: BLE001
                pass
            data = rest
        if not data:
            return
        raw = self.environ.get("wsgi.write")
        if raw is not None:
            raw.write(data)
            flush = getattr(raw, "flush", None)
            if flush:
                flush()
        else:
            self._body.write(data)

    def result(self):
        if not self._sent:
            self._emit(b"")
        if self.environ.get("wsgi.write") is not None:
            fw = self.environ.get("wsgi.file_wrapper")
            return fw(_DirectWriter(self.environ["wsgi.write"]), 65536) if fw else [b""]
        return [self._body.getvalue()]

    def run(self) -> None:
        if self.command == "OPTIONS":
            self.do_OPTIONS()
        elif self.command in ("GET", "HEAD"):
            self.do_GET() if self.command == "GET" else self.do_HEAD()
        else:
            self._send_error_json(f"{self.command} is not allowed", 405)

    def log_message(self, fmt, *args) -> None:      # keep serverless logs short
        pass


class _DirectWriter(io.RawIOBase):
    """File-like wrapper so `wsgi.file_wrapper` can stream our body out."""

    def __init__(self, raw) -> None:
        self.raw = raw

    def write(self, data) -> int:
        self.raw.write(data)
        return len(data)

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        pass


def app(environ: dict, start_response):     # noqa: A001 - WSGI entry point
    request = WSGIRequest(environ, start_response)
    try:
        request.run()
    except (BrokenPipeError, ConnectionResetError):
        pass
    except Exception as exc:  # noqa: BLE001
        note(f"{environ.get('PATH_INFO', '?')} failed: {exc}")
        try:
            request._sent = False
            request._body = io.BytesIO()
            body = json.dumps({"ok": False, "error": friendly(exc)}).encode("utf-8")
            request.send_response(502)
            request.send_header("Content-Type", "application/json")
            request.send_header("Content-Length", str(len(body)))
            request.send_header("Cache-Control", "no-store")
            request.send_header(*CORS)
            request.end_headers()
            request.wfile.write(body)
        except Exception:  # noqa: BLE001
            pass
    return request.result()


application = app
handler = Handler          # alias: Vercel's older Python runtime looks for this


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #

def main() -> None:
    global ROOT, CACHE_TTL, TOKEN, COOKIES, YT_DLP, FFMPEG
    global CACHE_DIR, INFO_DIR, SUB_DIR, YTDLP_DIR

    ap = argparse.ArgumentParser(description="Viewer — yt-dlp backed video server")
    ap.add_argument("--host", default=env("HOST", default="127.0.0.1"))
    ap.add_argument("--port", type=int, default=env_int("PORT", default=8080))
    ap.add_argument("--root", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--cookies", help="cookies.txt for age/geo-gated sites")
    ap.add_argument("--ttl", type=int, default=CACHE_TTL, help="info cache lifetime")
    ap.add_argument("--token", default="", help="shared secret clients must send")
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--install", action="store_true",
                    help="download the yt-dlp binary from GitHub now and exit")
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
    CACHE_TTL = max(30, args.ttl)
    TOKEN = args.token or TOKEN
    if args.cookies:
        COOKIES = args.cookies
    if args.cache_dir != CACHE_DIR:
        CACHE_DIR = args.cache_dir
        INFO_DIR = os.path.join(CACHE_DIR, "info")
        SUB_DIR = os.path.join(CACHE_DIR, "subs")
        YTDLP_DIR = os.path.join(CACHE_DIR, "yt-dlp")
    _BASE_ARGS.clear()          # rebuilt with the final cookie/impersonate setup
    YT_DLP = (shutil.which("yt-dlp") or shutil.which("yt_dlp")
              or (LOCAL_BIN if os.access(LOCAL_BIN, os.X_OK) else None))
    FFMPEG = _ffmpeg_path()

    ensure_dirs()
    if FFMPEG is None:
        note("ffmpeg not found — merging separate video+audio tracks will fail")
    if ytdlp_mode() == "none":
        note("yt-dlp NOT found → only direct media URLs will play. "
             "Fix with:  python3 server.py --install   (downloads from "
             "github.com/yt-dlp/yt-dlp)  or  pip install -U yt-dlp")
    else:
        note(f"yt-dlp {yt_dlp_version() or '?'} via {ytdlp_mode()}")
    if IMPERSONATE:
        note(f"impersonating {IMPERSONATE} (curl_cffi)")
    if TOKEN:
        note("a token is required for every /api call")
    pruned = prune_disk()
    if pruned:
        note(f"pruned {pruned} stale cache files")

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"cannot bind {args.host}:{args.port} — {exc}", file=sys.stderr)
        sys.exit(1)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        try:
            ip = socket.gethostbyname(args.host)
            note(f"listening on {args.host}:{args.port} — anyone who can reach "
                 f"{ip}:{args.port} can make yt-dlp fetch URLs"
                 + ("" if TOKEN else " (set VIEWER_TOKEN to lock it down)"))
        except OSError:
            pass

    print(f"[viewer] serving {ROOT}")
    print(f"[viewer] cache  {CACHE_DIR}")
    print(f"[viewer] yt-dlp: {yt_dlp_version() or 'not found'}", file=sys.stderr)
    print(f"[viewer] open  http://localhost:{args.port}/", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[viewer] bye")
        httpd.server_close()


if __name__ == "__main__":
    ensure_dirs()
    main()