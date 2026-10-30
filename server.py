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
import atexit
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
import signal
import socket
import stat as stat_mod
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
MEDIA_DIR = os.path.join(CACHE_DIR, "media")

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
    for d in (CACHE_DIR, INFO_DIR, SUB_DIR, YTDLP_DIR, MEDIA_DIR):
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
EXPIRY_CODES = (401, 403, 404, 410, 474)
# 404 belongs here because some CDNs (97bf1.com and kin) answer an *expired
# playlist token* with 404 instead of 403 — there was no such file long before
# the token. The refresh handler only re-extracts once, so a genuinely missing
# video costs one extraction more before failing with the same 404.

# -- media redirect guard -------------------------------------------------- #
# The phone trusts this server with every byte of a stream, so the server must
# not let the media drag it somewhere else. urllib follows 302s silently; this
# guard follows them only within the domain the stream was resolved on.
PROXY_MAX_REDIRECTS = env_int("VIEWER_MAX_REDIRECTS", default=5)
_REDIRECT_ALLOW_EXTRA = tuple(
    h.strip().lower() for h in env("VIEWER_REDIRECT_HOSTS", "").split(",")
    if h.strip())

# VIEWER_ALWAYS_PROXY=1 forces every format to report needsProxy, so phones
# never talk to a CDN directly and can never be led anywhere by one. Prefer
# the checkbox in the quality panel for a per-device choice; this is for a
# locked-down deployment.
ALWAYS_PROXY = bool(env_int("VIEWER_ALWAYS_PROXY", default=0))

_SECOND_LEVEL_TLD = {"co.uk", "org.uk", "gov.uk", "com.au", "com.br", "com.mx",
                     "co.jp", "co.in", "co.kr", "co.za", "com.tr", "com.cn",
                     "com.pl"}


def base_domain(host: str) -> str:
    """The part of a host that one operator controls.

    `video.google.com` → `google.com`, `x.example.co.uk` → `example.co.uk`,
    single-label hosts (localhost, `127.0.0.1`) pass through unchanged.
    """
    labels = (host or "").lower().rstrip(".").split(".")
    last = labels[-1] if labels else ""
    if len(labels) >= 3 and ".".join(labels[-2:]) in _SECOND_LEVEL_TLD:
        return ".".join(labels[-3:])
    if len(labels) >= 2 and last and not last.isdigit():
        return ".".join(labels[-2:])
    return host or ""


class RedirectBlocked(RuntimeError):
    """A media URL tried to hop to a host outside the one we resolved."""


class _MediaRedirectGuard(urllib.request.HTTPRedirectHandler):
    """urllib's redirect handler, confined to one operator's domain."""

    def __init__(self, allowed: str):
        super().__init__()
        self.allowed = allowed
        self.hops = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops += 1
        if self.hops > PROXY_MAX_REDIRECTS:
            raise RedirectBlocked(
                f"the media redirected {PROXY_MAX_REDIRECTS}+ times — not following")
        hop = base_domain(urllib.parse.urlparse(newurl).hostname or "")
        if hop != self.allowed and hop not in _REDIRECT_ALLOW_EXTRA:
            raise RedirectBlocked(
                f"the media tried to redirect from {self.allowed} to {hop} — refused "
                f"(allow it with VIEWER_REDIRECT_HOSTS={hop} if that CDN is expected)")
        if newurl == req.get_full_url():
            # A URL redirecting to itself is an infinite loop even though the
            # domain matches; urllib's own cycle check would only fire later
            # with a vaguer message, so own the case here.
            raise RedirectBlocked(
                f"the media redirected to itself {self.hops} times — not following")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guarded_urlopen(url: str, headers: dict, head: bool = False, timeout: float = PROXY_TIMEOUT):
    """Open a media URL without letting its redirects leave the resolved domain."""
    origin = urllib.parse.urlparse(url)
    # `netloc` carries the port, which base_domain would read as a label — use
    # the bare hostname so "host:8443" and "host" allow the same hops.
    opener = urllib.request.build_opener(
        _MediaRedirectGuard(base_domain(origin.hostname or "")))
    req = urllib.request.Request(url, headers=headers, method="HEAD" if head else "GET")
    return opener.open(req, timeout=timeout)


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

    Prefer the importable module: it runs under this interpreter, so it sees
    the same optional packages as the server (notably curl_cffi, which the
    impersonation flag needs). A bare console-script zipapp may be pinned to a
    different Python where those packages are missing — the child then dies
    with `Impersonate target "chrome" is not available` before writing a byte.
    """
    base = base_ytdlp_args(impersonate)
    if ytdlp_module() is not None:
        return [sys.executable, "-m", "yt_dlp", *base, *EXTRA_ARGS, *args]
    if YT_DLP:
        return [YT_DLP, *base, *EXTRA_ARGS, *args]
    raise RuntimeError("yt-dlp is not installed on this server")


def run_yt_dlp(args: list[str], timeout: int | None = None,
               impersonate: bool = True) -> tuple[int, str, str]:
    """Run yt-dlp from an arg list (no shell → no command injection)."""
    proc = subprocess.run(ytdlp_cmd(args, impersonate), capture_output=True, text=True,
                          timeout=timeout or YTDLP_TIMEOUT)
    return proc.returncode, proc.stdout, proc.stderr


def _drain_stderr(proc: subprocess.Popen, label: str) -> None:
    """Consume a child's stderr so a chatty failure cannot block the pipe.

    Only the last few lines are kept, and only the final one is logged when the
    child failed. Without a reader, 64 KiB of yt-dlp/ffmpeg diagnostics fills
    the pipe and the process hangs forever — which looks like a silent 0-byte
    stream on the phone.
    """
    tail: list[str] = []
    try:
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 6:
                tail.pop(0)
    except (OSError, ValueError):
        pass
    if tail and proc.returncode not in (0, None):
        note(f"{label}: {tail[-1][:300]}")


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

_API_SITES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # Nothing needs a plain JSON endpoint right now: hanime.tv was the only
    # host here and it moved to an adapter of its own when it began signing its
    # player API (see hanime_extract below). The machinery stays — and stays
    # tested — because it is the cheapest path for the next host that publishes
    # streams only through JSON.
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


def _json_post(url: str, payload: dict, headers: dict, timeout: int = 25):
    """POST JSON and parse the JSON answer, same TLS preference as _json_get."""
    body = json.dumps(payload).encode("utf-8")
    try:
        from curl_cffi import requests as _cr          # noqa: PLC0415
        resp = _cr.post(url, data=body, headers=headers, timeout=timeout,
                        impersonate="chrome")
        if resp.status_code == 200:
            return resp.json()
        return ("error", resp.status_code)
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        return ("error", 0)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
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
#  runs for every visitor. There is no login and no paywall; what the page needs
#  to decode for a visitor it ships to us too (hanime's handshake is signed, but
#  by a public blob — see its adapter below). Kept as an explicit table, like
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


_SULASOK_EMBED_RE = re.compile(
    r"""iframe\.src\s*=\s*["']([^"']+?/e/[A-Za-z0-9_-]+)["']""", re.I)
_SULASOK_TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.I)


def sulasokvid_extract(url: str, html: str | None = None) -> dict | None:
    """sulasokvid.co: watch.php is a gate — the real player is an /e/ embed.

    The page's own flow: watch.php shows an "intro" webp whose Skip button
    builds video.php, whose inline JS injects an embed iframe from the player
    host, whose /api/stream answers a plain POST with the HLS manifest. Every
    step is served to every visitor; the only gate is the ad redirect, which
    running the same steps server-side skips by construction.
    """
    parsed = urllib.parse.urlparse(url)
    video_id = (urllib.parse.parse_qs(parsed.query).get("id") or [""])[0]
    if not video_id or not parsed.path.endswith(("/watch.php", "/video.php")):
        # A listing/home page carries no single video — same reasoning as the
        # series pages on watchhentai: say so instead of burning 20 s on yt-dlp.
        raise NotAVideoPage(
            "this sulasokvid page lists videos — open a watch link (…/watch.php?id=…)")
    if parsed.path.endswith("/watch.php"):
        # video.php is where the site's own Skip button takes the browser.
        url = urllib.parse.urlunparse(parsed._replace(
            path=parsed.path[:-len("watch.php")] + "video.php"))
    if html is None or not parsed.path.endswith("/video.php"):
        page = _fetch_html(url)
        if not page:
            raise RuntimeError("the sulasokvid page did not load — retry, or check the link")
        _, html = page

    m = _SULASOK_EMBED_RE.search(html)
    if not m:
        raise RuntimeError(
            "the sulasokvid page named no player embed — its markup changed, so "
            "the adapter needs updating")
    embed_url = m.group(1)
    embed = urllib.parse.urlparse(embed_url)
    stream = _json_post(
        f"{embed.scheme}://{embed.netloc}/api/stream",
        # device/codecs are what the page's own detectDevice()/detectCodecs()
        # would send for a plain desktop browser without av1/h265.
        {"filecode": embed.path.rsplit("/", 1)[-1], "device": "web", "codecs": []},
        {"User-Agent": DEFAULT_UA, "Referer": embed_url,
         "Content-Type": "application/json"},
    )
    streaming = stream.get("streaming_url") if isinstance(stream, dict) else None
    if not streaming:
        raise RuntimeError(
            "the player's stream API answered without a stream URL — its scheme "
            "changed, so the adapter needs updating")

    # The manifest token is bound to the extracting IP, so this stream can only
    # play from this server anyway — needsProxy (Referer header) is correct,
    # not just polite.
    note("resolved the sulasokvid player embed to an HLS manifest")
    t = _SULASOK_TITLE_RE.search(html)
    return {
        "title": html_unescape(t.group(1)).strip()[:160] or None if t else None,
        "extractor_key": "sulasokvid",
        "formats": [{
            "format_id": "sulasok-hls",
            "url": streaming,
            "ext": "m3u8",
            "protocol": "https",
            "vcodec": None,
            "acodec": None,
            "_sniffed": True,
            "http_headers": {"User-Agent": DEFAULT_UA, "Referer": embed_url},
        }],
        "webpage_url": url,
    }


# --------------------------------------------------------------------------- #
#  hanime.tv — a signed player handshake, run by the site's own wasm
#
#  The guest JSON endpoint this adapter used to call is gone. The player now:
#    1. signs the current unix second with a wasm blob (the signature covers the
#       timestamp and window.origin),
#    2. AES-256-GCM seals {timestamp, directive, slug} and POSTs it, together
#       with that signature, to auth.hanime.tv/api/v11/handshake,
#    3. reads the HLS sources out of the answer's `x-token` header.
#  All three steps are public and run for every visitor; we run the same ones
#  from the server. The blob executes in wasmtime with a no-op import surface
#  and a three-property JS value model, so it cannot touch the network, the
#  disk, or anything else — it reads the clock and the origin, nothing more.
#
#  Two optional packages: wasmtime (run the signer) and cryptography (seal and
#  open the handshake). Without them every other site is unaffected; this one
#  says what to install and the UI keeps its "Open on site" link.
# --------------------------------------------------------------------------- #

HANIME_BASE = "https://hanime.tv"
HANIME_CSRF_URL = "https://ct.hanime.tv/csrf-token"
HANIME_AUTH_URL = "https://auth.hanime.tv/api/v11/handshake"
HANIME_ORIGIN = "https://hanime.tv"          # what the signer attests
HANIME_SEAL_KEY = hashlib.sha256(b"htv-insecure-handshake-v1").digest()
HANIME_SEAL_AAD = b"htv-insecure-v1"

_HANIME_SLUG_RE = re.compile(r"/videos/hentai/([A-Za-z0-9_-]+)")
_HANIME_VENDOR_RE = re.compile(
    r"""["'](https?://[^"']*?/vendor\.[0-9a-f]+\.min\.js)["']""")
_HANIME_WASM_RE = re.compile(r'base64Decode\("([A-Za-z0-9+/=]{1000,})"\)')
_HANIME_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_HANIME_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_HANIME_DURATION_RE = re.compile(
    r'"duration"\s*:\s*"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?"')


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_dec(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _htv_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
    except ImportError:
        raise RuntimeError(
            "hanime seals its player handshake with AES-256-GCM — install the "
            "`cryptography` package (pip install cryptography) to play it here"
        ) from None
    return AESGCM


def _htv_seal(payload: dict) -> str:
    """Seal a handshake payload in the envelope the player sends."""
    aesgcm = _htv_aesgcm()
    iv = os.urandom(12)
    sealed = aesgcm(HANIME_SEAL_KEY).encrypt(
        iv, json.dumps(payload, separators=(",", ":")).encode(), HANIME_SEAL_AAD)
    envelope = {"v": 1, "alg": "AES-256-GCM", "iv": _b64u(iv),
                "tag": _b64u(sealed[-16:]), "data": _b64u(sealed[:-16])}
    return _b64u(json.dumps(envelope, separators=(",", ":")).encode())


def _htv_unseal(token: str) -> dict:
    """Open the same envelope. ValueError if it is not one we understand."""
    aesgcm = _htv_aesgcm()
    try:
        envelope = json.loads(_b64u_dec(token).decode("utf-8"))
        sealed = _b64u_dec(envelope["data"]) + _b64u_dec(envelope["tag"])
        plain = aesgcm(HANIME_SEAL_KEY).decrypt(
            _b64u_dec(envelope["iv"]), sealed, HANIME_SEAL_AAD)
    except Exception as exc:  # noqa: BLE001 - any malformed envelope, one answer
        raise ValueError(f"unexpected handshake envelope: {exc}") from None
    try:
        return json.loads(plain.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"handshake payload is not JSON: {exc}") from None


def _htv_http(method: str, url: str, headers: dict,
              body: bytes | None = None, timeout: int = 25) -> tuple[int, dict, bytes]:
    """One browser-shaped request. The handshake needs no cookie jar."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode() or 200, dict(resp.headers), resp.read(1 << 20)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(1 << 20)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"could not reach {urllib.parse.urlparse(url).hostname}: "
            f"{getattr(exc, 'reason', exc)}") from None


def _htv_signer_blob(html: str) -> str:
    """Path to the player's signer wasm; download and cache it on first use."""
    match = _HANIME_VENDOR_RE.search(html)
    if not match:
        raise RuntimeError(
            "hanime's player bundle is not where it used to be — the site "
            "changed its player; open the video on the site instead")
    vendor_url = match.group(1)
    directory = os.path.join(CACHE_DIR, "hanime")
    name = "signer-" + hashlib.sha256(vendor_url.encode()).hexdigest()[:20] + ".wasm"
    path = os.path.join(directory, name)
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                if fh.read(4) == b"\x00asm":
                    return path
        except OSError:
            pass
        os.unlink(path)                       # truncated by an old crash: refetch
    status, _, body = _htv_http("GET", vendor_url,
                                {"User-Agent": DEFAULT_UA,
                                 "Referer": HANIME_BASE + "/"},
                                timeout=SNIFF_TIMEOUT)
    if status != 200:
        raise RuntimeError(f"hanime's player bundle answered HTTP {status}")
    blob = _HANIME_WASM_RE.search(body.decode("utf-8", "replace"))
    if not blob:
        raise RuntimeError(
            "hanime's player bundle carries no signer blob — the site changed "
            "its player; open the video on the site instead")
    wasm = base64.b64decode(blob.group(1))
    if not wasm.startswith(b"\x00asm"):
        raise RuntimeError("hanime's signer blob is not a wasm module")
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(wasm)
    os.replace(tmp, path)                     # never hand out a half-written blob
    note(f"cached hanime's signer blob ({human_size(len(wasm))})")
    return path


_HTV_ENGINE: tuple | None = None
_HTV_ENGINE_LOCK = threading.Lock()


def _htv_engine(path: str):
    """(wasmtime, engine, module) for the cached blob, compiled once."""
    global _HTV_ENGINE
    with _HTV_ENGINE_LOCK:
        if _HTV_ENGINE and _HTV_ENGINE[0] == path:
            return _HTV_ENGINE[1:]
        try:
            import wasmtime  # noqa: PLC0415
        except ImportError:
            raise RuntimeError(
                "hanime signs its player API inside a wasm blob — install the "
                "`wasmtime` package (pip install wasmtime) to run it here"
            ) from None
        engine = wasmtime.Engine()
        module = wasmtime.Module.from_file(engine, path)
        _HTV_ENGINE = (path, wasmtime, engine, module)
        return wasmtime, engine, module


def _htv_signature(seconds: int, path: str) -> str:
    """Run the player's signer: it attests (unix second, window.origin).

    The module is an embind build, so `_main` asks JS for the clock
    (emscripten_asm_const_int) and for window.top.location.origin through the
    emval imports. Both are answered here from a tiny value model: the only
    thing the module can learn about its host is the origin we choose.
    """
    wasmtime, engine, module = _htv_engine(path)
    store = wasmtime.Store(engine)

    handles: dict[int, object] = {}
    counter = [9]

    def hold(value):
        counter[0] += 1
        handles[counter[0]] = value
        return counter[0]

    window: dict = {"location": {"origin": HANIME_ORIGIN}}
    window["top"] = window
    state = {"signature": None, "time": None}

    def cstr(caller, ptr):
        mem = caller.get("z")
        out = b""
        while True:
            chunk = bytes(mem.read(store, ptr + len(out), ptr + len(out) + 256))
            end = chunk.find(b"\x00")
            if end >= 0:
                return out + chunk[:end]
            out += chunk

    def asm_const(caller, code, sig_ptr, argbuf):
        if code == 17392:                     # Date.now()/1000, in the site's build
            return seconds
        if code == 17442:                     # (signature, time) → window
            mem = caller.get("z")
            types = cstr(caller, sig_ptr).decode("ascii", "replace")
            raw = bytes(mem.read(store, argbuf, argbuf + 64))
            args, off = [], 0
            for ch in types:
                if ch in ("i", "p"):
                    args.append(struct.unpack_from("<i", raw, off)[0])
                    off += 4
                elif ch == "j":
                    args.append(struct.unpack_from("<q", raw, off)[0])
                    off += 8
                else:
                    args.append(struct.unpack_from("<d", raw, off)[0])
                    off += 8
            if args:
                state["signature"] = cstr(caller, args[0]).decode("ascii", "replace")
                state["time"] = args[1] if len(args) > 1 else None
        return 0

    def get_global(caller, name):
        key = cstr(caller, name).decode("utf-8", "replace") if name else ""
        return hold(window if key in ("", "window") else window.get(key))

    def new_cstring(caller, ptr):
        return hold(cstr(caller, ptr).decode("utf-8", "replace"))

    def get_property(caller, handle, key_handle):
        obj, key = handles.get(handle), handles.get(key_handle)
        try:
            return hold(obj[key])             # type: ignore[index]
        except (KeyError, TypeError):
            return hold(None)

    def invoke(caller, caller_id, handle, method, destructors, args):
        """kind-3 invoker: identity, converted to a libc++ std::string.

        Short strings live inline: a size word followed by the bytes.
        """
        value = handles.get(handle)
        if not isinstance(value, str):
            return 0.0
        data = value.encode("utf-8")
        buf = caller.get("E")(caller, 4 + len(data) + 24)
        caller.get("z").write(store, struct.pack("<I", len(data)) + data, buf)
        return float(buf)

    def zero(caller, *a):
        return 0

    def noop(caller, *a):
        return None

    def abort(caller, *a):
        raise RuntimeError("hanime's signer aborted on this host")

    i32, i64, f64 = wasmtime.ValType.i32, wasmtime.ValType.i64, wasmtime.ValType.f64

    def ftype(params, results=()):
        return wasmtime.FuncType([p() for p in params], [r() for r in results])

    defs = {
        "a": (ftype([i32, i32, i32]), noop),
        "b": (ftype([i32, i32, i32, i32, i32]), noop),
        "c": (ftype([i32]), noop),
        "d": (ftype([i32, i32, i32]), noop),
        "e": (ftype([i32, i32, i32]), noop),
        "f": (ftype([i32, i32, i32, i64, i64]), noop),
        "g": (ftype([i32, i32, i32], [i32]), asm_const),
        "h": (ftype([i32]), noop),
        "i": (ftype([i32, i32, i32, i32, i32], [f64]), invoke),
        "j": (ftype([i32]), noop),
        "k": (ftype([i32, i32, i32], [i32]), zero),
        "l": (ftype([i32, i32], [i32]), get_property),
        "m": (ftype([i32], [i32]), new_cstring),
        "n": (ftype([i32]), noop),
        "o": (ftype([i32, i32]), noop),
        "p": (ftype([i32], [i32]), get_global),
        "q": (ftype([i32, i32, i32, i32]), noop),
        "r": (ftype([i32, i32]), noop),
        "s": (ftype([i32, i32, i32, i32]), noop),
        "t": (ftype([i32, i32], [i32]), zero),
        "u": (ftype([i32, i32], [i32]), zero),
        "v": (ftype([]), abort),
        "w": (ftype([i32], [i32]), zero),
        "x": (ftype([i32, i32, i32]), abort),
        "y": (ftype([i32]), noop),            # window_on(): we add no listeners
    }
    imports = [wasmtime.Func(store, ftype_, fn, access_caller=True)
               for ftype_, fn in defs.values()]
    instance = wasmtime.Instance(store, module, imports)
    instance.exports(store)["C"](store, 0, 0)     # _main
    signature = state["signature"] or ""
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise RuntimeError("hanime's signer produced no signature")
    return signature


def hanime_handshake(page_url: str, slug: str, html: str) -> list[dict]:
    """Run the player's own three-step handshake; return its HLS sources."""
    path = _htv_signer_blob(html)
    now = int(time.time())
    signature = _htv_signature(now, path)

    status, _, body = _htv_http("GET", HANIME_CSRF_URL, {
        "User-Agent": DEFAULT_UA, "Referer": page_url,
        "Accept": "application/json, text/plain, */*"}, timeout=SNIFF_TIMEOUT)
    try:
        csrf = json.loads(body.decode("utf-8", "replace"))["csrf_token"]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError(
            f"hanime's csrf endpoint answered HTTP {status} without a token — "
            f"the site changed its player") from None

    sealed = _htv_seal({"timestamp_unix": now,
                        "directive": "htv_player_handshake",
                        "slug": slug})
    status, headers, _ = _htv_http("POST", HANIME_AUTH_URL, {
        "User-Agent": DEFAULT_UA, "Referer": page_url, "Origin": HANIME_BASE,
        "Accept": "application/json", "Content-Type": "application/json",
        "x-signature-version": "web2", "x-signature": signature,
        "x-time": str(now), "x-csrf-token": csrf,
    }, body=json.dumps({"token": sealed}).encode(), timeout=SNIFF_TIMEOUT)
    token = {k.lower(): v for k, v in headers.items()}.get("x-token")
    if status != 200 or not token:
        raise RuntimeError(
            f"hanime's player handshake was refused (HTTP {status}) — the site "
            f"changed its player; open the video on the site instead")
    try:
        payload = _htv_unseal(token)
    except ValueError as exc:
        raise RuntimeError(f"hanime's handshake token did not open: {exc}") from None
    sources = [s for s in payload.get("sources") or []
               if isinstance(s, dict) and s.get("kind") == "normal" and s.get("src")]
    if not sources:
        raise RuntimeError(
            "hanime's handshake returned no playable source — the video may be "
            "region-locked or members-only")
    note(f"hanime handshake: {len(sources)} source(s), signed for {now}")
    return sources


def hanime_extract(url: str, html: str | None = None) -> dict | None:
    """hanime.tv: signed player handshake → one HLS playlist per resolution."""
    slug_match = _HANIME_SLUG_RE.search(urllib.parse.urlparse(url).path)
    if not slug_match:
        raise NotAVideoPage(
            "hanime only plays single videos at /videos/hentai/<slug> — this "
            "link is a listing or a search page")
    slug = slug_match.group(1)
    if html is None:
        page = _fetch_html(url)
        if not page:
            raise RuntimeError(
                "hanime.tv did not serve the page to this server — try again, or "
                "open the video on the site")
        _, html = page

    sources = hanime_handshake(url, slug, html)

    heading = _HANIME_H1_RE.search(html) or _HANIME_TITLE_RE.search(html)
    title = None
    if heading:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", heading.group(1))).strip()
    duration = 0
    dur = _HANIME_DURATION_RE.search(html)
    if dur:
        duration = (int(dur.group(1) or 0) * 3600 + int(dur.group(2) or 0) * 60
                    + float(dur.group(3) or 0))

    formats = []
    for src in sources:
        height = src.get("height")
        label = src.get("label") or (f"{height}p" if height else "source")
        formats.append({
            "format_id": f"hanime-{label}",
            "url": HANIME_BASE + str(src["src"]),
            "ext": "m3u8",
            "protocol": "m3u8_native",
            "width": src.get("width"),
            "height": height,
            "vcodec": None,
            "acodec": None,
            "format_note": label,     # one playlist per resolution → distinct labels
            "_sniffed": True,
            "http_headers": {"User-Agent": DEFAULT_UA, "Referer": url},
        })
    note(f"decoded {len(formats)} source(s) from hanime's signed handshake")
    return {
        "title": title or "hanime video",
        "extractor_key": "hanime",
        "duration": int(duration) or None,
        "formats": formats,
        "webpage_url": url,
    }


# --------------------------------------------------------------------------- #
#  javhdporn.net — an RC4 `dex()` layer wrapped around the site's player API
#
#  The video page ships two base64 blobs (`data-mpu` and a version tag). The
#  site's own `dex()` turns them into the token its `/api/play/` endpoint wants,
#  and the endpoint answers with a player URL plus a `reserve` list of alternate
#  players — every one of them sealed the same way. The player frame then carries
#  a `data-config` blob that a second `dex()` pass opens (`src`/`img`/`vtt`).
#  None of this is a paywall: the page runs it for every visitor, at no cost, so
#  we run the same steps server-side instead of executing (or rehosting) its JS.
#
#  The one trap is that the first alternates are ad decoys: their master
#  playlists look genuine (real bandwidth/resolution/codec metadata) but every
#  media segment is a TikTok ad image. So we walk the alternates, prefer a
#  progressive file when one is offered, and read a playlist's first segment
#  before trusting it. A playlist whose segments are all ad-network images is
#  skipped, exactly as the site's own player falls through to the next reserve.
# --------------------------------------------------------------------------- #

_JVD_BASE = "https://www.javhdporn.net"
_JVD_UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36")
# The salts are the site's, one per `data-ver`; the response always answers the
# same salt, and stage two is a constant too. They are public — they are in the
# page the browser already downloads.
_JVD_REQ_SALT = {"1": "QxLUF1bgIAdeQX", "2": "SyntaxError"}
_JVD_RESP_SALT = "_0x58fe15"
_JVD_STAGE2_SALT = "_0x59a0e4"
# A media playlist whose every segment lives on one of these is an ad roll, not
# the video — the decoy providers render the same TikTok image 975 times.
_JVD_AD_HOSTS = ("tiktokcdn.com",)

_JVD_AREA_RE = re.compile(r'id="video-player-area"[^>]*data-video-id="(\d+)"')
_JVD_VIDEO_ID_RE = re.compile(r'data-video-id="(\d+)"')
_JVD_MPU_RE = re.compile(r'id="video-player"[^>]*data-mpu="([^"]+)"')
_JVD_VER_RE = re.compile(r'id="video-player"[^>]*data-ver="([^"]+)"')
_JVD_CONFIG_RE = re.compile(r'id="jwplayer"[^>]*data-config="([^"]+)"')
_JVD_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_JVD_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_JVD_LENGTH_RE = re.compile(r"[?&]length=(\d+)")


def _jvd_b64d(text: str) -> bytes:
    """The page's `atob` on a blob that may have lost its `=` padding."""
    text = (text or "").strip()
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _jvd_rc4(key: bytes, data: bytes) -> bytes:
    """Textbook RC4 — the exact keystream the site's `dex()` builds."""
    sbox = list(range(256))
    j = 0
    for i in range(256):
        j = (j + sbox[i] + key[i % len(key)]) & 255
        sbox[i], sbox[j] = sbox[j], sbox[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 255
        j = (j + sbox[i]) & 255
        sbox[i], sbox[j] = sbox[j], sbox[i]
        out.append(byte ^ sbox[(sbox[i] + sbox[j]) & 255])
    return bytes(out)


def _jvd_dex_str(keyarg: str, salt: str, blob: str) -> str:
    """The site's `dex()`: key `base64(keyarg+salt)` reversed, RC4, base64 again."""
    key = base64.b64encode((keyarg + salt).encode()).decode()[::-1]
    return _jvd_b64d(_jvd_rc4(key.encode(), _jvd_b64d(blob)).decode("latin-1")
                     ).decode("latin-1")


def _jvd_http(method: str, url: str, headers: dict,
              data: bytes | None = None,
              timeout: int | None = None) -> tuple[int, str]:
    """One browser-shaped request, preferring curl_cffi's TLS profile."""
    timeout = SNIFF_TIMEOUT if timeout is None else timeout
    try:
        from curl_cffi import requests as _cr          # noqa: PLC0415
        resp = _cr.request(method, url, headers=headers, data=data,
                           timeout=timeout, impersonate="chrome")
        return resp.status_code, resp.text
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        raise RuntimeError(f"could not reach {_host_of(url)}") from None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.getcode() or 200, resp.read(2 << 20).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2 << 20).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"could not reach {_host_of(url)}: {getattr(exc, 'reason', exc)}") from None


def _jvd_height(label: str) -> int | None:
    m = re.search(r"(\d{3,4})\s*[pP]", label or "")
    return int(m.group(1)) if m else None


def _jvd_player(play_url: str, referer: str) -> tuple[list, dict, str]:
    """Fetch one player frame and open its `data-config` → (sources, config, url)."""
    play_url = "https:" + play_url if play_url.startswith("//") else play_url
    headers = {"User-Agent": _JVD_UA, "Referer": referer}
    status, page = _jvd_http("GET", play_url, headers)
    match = _JVD_CONFIG_RE.search(page)
    if not match:
        raise RuntimeError(f"the player frame carried no config (HTTP {status})")
    parsed = urllib.parse.urlparse(play_url)
    # Stage-two key: base64(path[?query])[4:20] — the same slice the frame uses.
    keyarg = base64.b64encode(
        (parsed.path + (("?" + parsed.query) if parsed.query else "")).encode()
    ).decode()[4:20]
    cfg = json.loads(_jvd_dex_str(keyarg, _JVD_STAGE2_SALT, match.group(1)))
    raw = cfg.get("src")
    srcs = json.loads(_jvd_b64d(raw).decode("utf-8")) if isinstance(raw, str) else (raw or [])
    return (srcs if isinstance(srcs, list) else []), cfg, play_url


def _jvd_hls_is_video(playlist: str, referer: str) -> bool:
    """True unless this HLS chain resolves to ad-image segments.

    A decoy master playlist advertises real resolutions, so the only honest test
    is to read a media playlist and look at what its segments actually are.
    """
    headers = {"User-Agent": _JVD_UA, "Referer": referer}
    status, text = _jvd_http("GET", playlist, headers)
    if status != 200 or not text.lstrip().startswith("#EXTM3U"):
        return False
    base = playlist
    media = next((l.strip() for l in text.splitlines()
                  if l.strip() and not l.strip().startswith("#")), None)
    if media:                                  # a master → follow its first variant
        base = urllib.parse.urljoin(playlist, media)
        status, text = _jvd_http("GET", base, headers)
        if status != 200 or not text.lstrip().startswith("#EXTM3U"):
            return False
    segs = [urllib.parse.urljoin(base, l.strip()) for l in text.splitlines()
            if l.strip() and not l.strip().startswith("#")][:20]
    if not segs:
        return False

    def is_ad(seg: str) -> bool:
        host = (urllib.parse.urlparse(seg).hostname or "").lower()
        return any(host == a or host.endswith("." + a) for a in _JVD_AD_HOSTS)

    return not all(is_ad(seg) for seg in segs)


def javhdporn_extract(url: str, html: str | None = None) -> dict | None:
    """javhdporn.net: run the page's own `dex()` chain to reach the real stream.

    The site offers several player "reserves" per video; some are ad decoys that
    answer with an HLS chain of TikTok ad images. We take every alternate the
    API hands us, keep a progressive file if any provides one, and otherwise use
    the first playlist whose segments are actually video.
    """
    parsed = urllib.parse.urlparse(url)
    if "/video/" not in parsed.path:
        raise NotAVideoPage(
            "javhdporn only plays single videos at /video/<slug> — this link is a "
            "listing, a tag or a search page")
    headers = {"User-Agent": _JVD_UA, "Referer": _JVD_BASE + "/"}
    if html is None:
        status, html = _jvd_http("GET", url, headers)
        if status != 200:
            raise RuntimeError(
                f"javhdporn did not serve the page (HTTP {status}) — retry, or open "
                f"the video on the site")

    vid = (_JVD_AREA_RE.search(html) or _JVD_VIDEO_ID_RE.search(html))
    mpu = _JVD_MPU_RE.search(html)
    ver = _JVD_VER_RE.search(html)
    if not (vid and mpu and ver):
        raise RuntimeError(
            "the javhdporn page carried no player data — the site changed its "
            "markup, so the adapter needs updating")
    vid, ver = vid.group(1), ver.group(1)
    sources = _jvd_dex_str(vid, _JVD_REQ_SALT.get(ver, _JVD_RESP_SALT), mpu.group(1))

    body = urllib.parse.urlencode({"sources": sources, "ver": ver}).encode()
    status, text = _jvd_http("POST", _JVD_BASE + "/api/play/", {
        **headers, "Referer": url, "Origin": _JVD_BASE,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded"}, data=body)
    try:
        answer = json.loads(text)
    except ValueError:
        raise RuntimeError(
            f"javhdporn's play api answered HTTP {status} without JSON — the site "
            f"changed its player, so the adapter needs updating") from None
    if not answer.get("status") or not answer.get("data"):
        raise RuntimeError(
            "javhdporn's play api refused the request — the site changed its "
            "player, so the adapter needs updating")

    alternates = [("primary", _jvd_dex_str(vid, _JVD_RESP_SALT, answer["data"]))]
    try:
        reserves = json.loads(_jvd_dex_str(vid, _JVD_RESP_SALT, answer.get("reserve") or ""))
    except (ValueError, TypeError):
        reserves = []
    for item in reserves if isinstance(reserves, list) else []:
        if isinstance(item, dict) and item.get("data"):
            alternates.append((f"reserve:{item.get('lo') or '?'}",
                               _jvd_dex_str(vid, _JVD_RESP_SALT, item["data"])))

    direct, playlist, failures, duration = [], [], [], None
    for label, play in alternates:
        try:
            srcs, cfg, play_url = _jvd_player(play, url)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{label}: {exc}")
            continue
        if duration is None and cfg.get("vtt"):
            length = _JVD_LENGTH_RE.search(str(cfg["vtt"]))
            if length:
                duration = int(length.group(1)) or None
        for src in srcs:
            if not isinstance(src, dict) or not src.get("file"):
                continue
            file_ = str(src["file"])
            stype = str(src.get("type") or "").lower()
            entry = (label, play_url, file_, src)
            if stype == "hls" or ".m3u8" in file_.lower():
                playlist.append(entry)
            else:
                direct.append(entry)

    formats: list[dict] = []

    def add(entry, is_hls: bool) -> None:
        label, play_url, file_, src = entry
        note_ = str(src.get("label") or "")
        ext = (src.get("type") or "").lower() if not is_hls else "m3u8"
        if ext not in ("mp4", "m4v", "webm", "mov", "m3u8"):
            ext = "m3u8" if is_hls else (urllib.parse.urlparse(file_).path.rsplit(".", 1)[-1] or "mp4").lower()
        formats.append({
            "format_id": f"jvd-{label}-{note_ or ('hls' if is_hls else ext)}",
            "url": file_,
            "ext": ext,
            "protocol": "m3u8_native" if is_hls else "https",
            "height": _jvd_height(note_),
            "vcodec": None,
            "acodec": None,
            # The CDN wants the player frame as its Referer, like a browser sends
            # it — needsProxy then routes the request (and HLS segments) via us.
            "_sniffed": True,
            "http_headers": {"User-Agent": DEFAULT_UA, "Referer": play_url},
        })

    # A progressive file plays natively and does not need hls.js, so prefer one.
    for entry in direct:
        add(entry, is_hls=False)
    if not formats:
        for entry in playlist:
            if _jvd_hls_is_video(entry[2], entry[1]):
                add(entry, is_hls=True)
                break

    if not formats:
        detail = ("the alternates offered only ad playlists"
                  if playlist else "no alternate answered")
        raise RuntimeError(
            f"javhdporn offered no playable source for this video ({detail}) — "
            f"open it on the site instead"
            + (f" [{'; '.join(failures)}]" if failures else ""))

    formats.sort(key=lambda f: (f["protocol"] == "m3u8_native",
                                -(f.get("height") or 0)))
    heading = _JVD_H1_RE.search(html) or _JVD_TITLE_RE.search(html)
    title = None
    if heading:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", heading.group(1))).strip()
    note(f"javhdporn: decoded {len(formats)} source(s) from {len(alternates)} "
         f"alternate player(s)")
    return {
        "title": title or "javhdporn video",
        "extractor_key": "javhdporn",
        "duration": duration,
        "formats": formats,
        "webpage_url": url,
    }


# Sites whose markup needs decoding by an adapter — a player frame to walk, or
# (hanime) an API that signs every request. One named function per host.
_PLAYER_SITES = {"watchhentai.net": watchhentai_extract,
                 "sulasokvid.co": sulasokvid_extract,
                 "hanime.tv": hanime_extract,
                 "javhdporn.net": javhdporn_extract}


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


def session_bound_media_note(url: str) -> str | None:
    """Some CDNs mint stream URLs only for the session that requested them.

    Measured on TikTok: every video format comes from `v16/v19-webapp-prime`
    tokens that a real browser also gets 403 on when replayed from another
    session (the CLI works only because it downloads inside the minting
    session; our stateless proxy cannot). Only the music CDN (`tiktokcdn.com`)
    serves anyone. An "adapter" cannot statelessly fix this either — the
    sanctioned path is the user's own cookies.
    """
    host = (_host_of(url) or "").lower()
    if "tiktok" in host and "webapp" in url.lower():
        return ("TikTok mints its video URLs only for the session that requested "
                "them, so the server cannot replay them — only the audio streams "
                "without cookies. Save a TikTok cookies file via "
                "VIEWER_COOKIES_B64 (or run server.py with your cookies.txt) and "
                "re-resolve to watch the video.")
    return None


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


class Unreachable(RuntimeError):
    """The source could not be contacted at all (offline, DNS, timeout)."""


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
            kind = "hls"
            # A quality label matters when a site offers one playlist per
            # resolution (hanime): without it every button would read "HLS".
            label = (f"{f['format_note']} · HLS" if f.get("format_note")
                     else "HLS (adaptive stream)")
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
            "needsProxy": True if ALWAYS_PROXY else any(
                k.lower() != "user-agent" for k in headers),
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
            "needsProxy": True if ALWAYS_PROXY else any(
                k.lower() != "user-agent" for k in headers),
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
        return {"url": page, "headers": {}, "kind": "direct", "id": None,
                "ext": None, "title": None, "filesize": 0, "duration": None}

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
        return {"url": page, "headers": {}, "kind": "direct", "id": None,
                "ext": None, "title": None, "filesize": 0, "duration": None}
    if chosen["kind"] == "merge":
        return {"url": page, "headers": {}, "kind": "merge", "id": MERGE_ID}
    return {
        "url": chosen["url"],
        "headers": chosen.get("headers") or {},
        "kind": chosen["kind"],
        "id": chosen["id"],
        "ext": chosen.get("ext"),
        "title": info.get("title"),
        # The windowed cache needs the full size and runtime to decide how
        # much of the file to keep and where the playhead sits in it.
        "filesize": chosen.get("size") or 0,
        "duration": info.get("duration"),
    }


def resolve_headers(page: str, fmt: str = "") -> dict:
    """Headers the CDN wants — best effort, never raises."""
    if not valid_url(page) or ytdlp_mode() == "none":
        return {}
    try:
        return resolve_target(page, fmt).get("headers") or {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
#  Media cache — a size-tiered, playhead-anchored window of the bytes in view
#
#  This is not an offline library. It keeps a rolling window that follows the
#  playhead: for a 2.47 GB file it holds ~25 %, for a 100 MB file ~75 %, and as
#  you play forward it drops what is behind you and fetches the next stretch.
#  The window is deliberately short-lived: an idle entry is deleted after
#  VIEWER_MEDIA_TTL (5 min by default), and the whole directory is wiped when
#  the server exits — or on the next start when the previous process died.
#
#  Why server-side: the UI is opened over plain HTTP on a LAN address, and
#  browsers only allow a Service Worker / Cache API in a secure context. There
#  is no browser storage to lean on, so the bytes live next to server.py.
# --------------------------------------------------------------------------- #

MEDIA_CACHE = env_bool("VIEWER_MEDIA_CACHE",
                       default=PLATFORM not in ("Vercel", "AWS Lambda", "Netlify"))
MEDIA_TTL = env_int("VIEWER_MEDIA_TTL", default=300)          # idle seconds
MEDIA_SWEEP = max(5, env_int("VIEWER_MEDIA_SWEEP", default=30))
MEDIA_MAX_MB = env_int("VIEWER_MEDIA_MAX_MB", default=4096)   # global ceiling
MEDIA_CHUNK = max(64 * 1024, env_int("VIEWER_MEDIA_CHUNK", default=1 << 20))
CACHE_BACK = max(0, env_int("VIEWER_CACHE_BACK", default=10))  # seconds behind
PREFETCH_BPS = max(0, env_int("VIEWER_PREFETCH_MBPS", default=8)) * 1_000_000
CLEANUP_ON_EXIT = env_bool("VIEWER_MEDIA_CLEANUP_ON_EXIT", default=True)
RECONNECT_PROBE_INTERVAL = max(2, env_int("VIEWER_RECONNECT_PROBE", default=10))

# Fraction of a file kept on disk, by total size (decimal MB, like a phone
# shows it). Small files are cheap to keep most of; a multi-gigabyte file is
# kept as a window around the playhead instead of eating the disk.
_CACHE_TIERS = ((1_000_000_000, 0.25), (300_000_000, 0.35),
                (100_000_000, 0.50), (0, 0.75))


def cache_fraction(size: int) -> float:
    """How much of a file of `size` bytes we are willing to keep."""
    size = int(size or 0)
    for floor, frac in _CACHE_TIERS:
        if size >= floor:
            return frac
    return _CACHE_TIERS[-1][1]


class MediaEntry:
    """One (page, format) pair currently being cached."""

    def __init__(self, key: str, page: str, fmt: str) -> None:
        self.key = key
        self.page = page
        self.fmt = fmt
        self.dir = os.path.join(MEDIA_DIR, key)
        self.total = 0                 # bytes in the full file
        self.duration = 0.0
        self.fraction = 0.0
        self.budget = 0                # bytes we are allowed to keep
        self.url = ""                  # current upstream URL (may be signed)
        self.headers: dict = {}
        self.title = ""
        self.ext = "mp4"
        self.ctype = "video/mp4"
        self.etag = ""
        self.last_modified = ""
        self.playhead = 0.0
        self.has_position = False
        self.position_at = 0.0
        self.anchor = 0                # byte offset when no heartbeat has arrived
        self.paused = False
        self.present: set[int] = set()  # chunk indices fully written
        self.ranges: dict[int, list] = {}   # partial chunk idx → [(start,end)]
        self.inflight: set[int] = set()
        self.on_disk = 0
        self.last_used = time.time()
        self.created = time.time()
        self.player_until = 0.0
        self.stop = threading.Event()
        self.filler: threading.Thread | None = None
        self.lock = threading.RLock()
        self.stale_since: float | None = None   # network suspected down since

    def chunk_len(self, idx: int) -> int:
        if not self.total:
            return MEDIA_CHUNK
        return max(0, min(MEDIA_CHUNK, self.total - idx * MEDIA_CHUNK))


_MEDIA: dict[str, MediaEntry] = {}
_MEDIA_LOCK = threading.RLock()
_MEDIA_SWEEP_STARTED = False
_MEDIA_CLOSING = threading.Event()
_MEDIA_READY = False
_MEDIA_FOREIGN = False     # another live process owns MEDIA_DIR


# -- lifecycle -------------------------------------------------------------- #

def _media_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def media_startup() -> None:
    """Claim media/ — wipe what a dead previous process left behind.

    A SIGKILL, a power cut or the OS reaping the backend skips `atexit`, so the
    "wipe on exit" guarantee is completed here on the next start. A live PID in
    .owner means another instance is using the directory; leave it alone.
    """
    global _MEDIA_READY, _MEDIA_FOREIGN
    if not MEDIA_CACHE or _MEDIA_READY:
        return
    _MEDIA_READY = True
    _MEDIA_FOREIGN = False
    _MEDIA_CLOSING.clear()
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
    except OSError:
        return
    owner = os.path.join(MEDIA_DIR, ".owner")
    previous = 0
    try:
        with open(owner, "r", encoding="utf-8") as fh:
            previous = int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        previous = 0
    if previous and previous != os.getpid() and _media_pid_alive(previous):
        _MEDIA_FOREIGN = True
        note(f"media cache is owned by pid {previous} — caching is disabled here")
        return
    _MEDIA.clear()
    for name in os.listdir(MEDIA_DIR):
        path = os.path.join(MEDIA_DIR, name)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except OSError:
            pass
    try:
        with open(owner, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        pass
    _media_start_sweeper()


def media_shutdown() -> None:
    """Stop workers and, by default, drop the whole cache. Never raises."""
    global _MEDIA_READY, _MEDIA_SWEEP_STARTED, _MEDIA_FOREIGN
    _MEDIA_CLOSING.set()
    for ent in list(_MEDIA.values()):
        ent.stop.set()
    if CLEANUP_ON_EXIT and not _MEDIA_FOREIGN:
        try:
            shutil.rmtree(MEDIA_DIR, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
    _MEDIA.clear()
    _MEDIA_READY = False
    _MEDIA_SWEEP_STARTED = False
    _MEDIA_FOREIGN = False


def _media_start_sweeper() -> None:
    global _MEDIA_SWEEP_STARTED
    with _MEDIA_LOCK:
        if _MEDIA_SWEEP_STARTED:
            return
        _MEDIA_SWEEP_STARTED = True
    threading.Thread(target=_media_sweeper, daemon=True, name="media-sweeper").start()


def _media_sweeper() -> None:
    while not _MEDIA_CLOSING.wait(MEDIA_SWEEP):
        _media_sweep_once()


def _media_sweep_once(now: float | None = None) -> None:
    now = time.time() if now is None else now
    for ent in list(_MEDIA.values()):
        if now - ent.last_used > MEDIA_TTL and now > ent.player_until:
            _media_destroy(ent)
    _media_enforce_quota()


def _media_destroy(ent: MediaEntry) -> None:
    ent.stop.set()
    with _MEDIA_LOCK:
        if _MEDIA.get(ent.key) is ent:
            _MEDIA.pop(ent.key, None)
    shutil.rmtree(ent.dir, ignore_errors=True)


# -- entry lookup / metadata ------------------------------------------------ #

def _media_key(page: str, fmt: str) -> str:
    return hashlib.sha1(f"{page}\n{fmt}".encode("utf-8")).hexdigest()


def _media_get(page: str, fmt: str, create: bool = True) -> MediaEntry | None:
    if _MEDIA_FOREIGN:
        return None
    key = _media_key(page, fmt)
    with _MEDIA_LOCK:
        ent = _MEDIA.get(key)
        if ent is None and create:
            ent = MediaEntry(key, page, fmt)
            _MEDIA[key] = ent
        if ent is not None:
            ent.last_used = time.time()
        return ent


def _media_set_total(ent: MediaEntry, total: int) -> None:
    total = int(total or 0)
    if total <= 0:
        return
    changed = False
    with ent.lock:
        if ent.total != total:
            ent.total = total
            ent.fraction = cache_fraction(total)
            budget = int(total * ent.fraction)
            cap = MEDIA_MAX_MB * 1_000_000 if MEDIA_MAX_MB > 0 else budget
            budget = max(MEDIA_CHUNK, min(budget, cap))
            ent.budget = max(MEDIA_CHUNK, (budget // MEDIA_CHUNK) * MEDIA_CHUNK)
            changed = True
    if changed:
        _media_start_filler(ent)


def _media_attach(ent: MediaEntry | None, target: dict) -> None:
    """Refresh the upstream link and learn the total size, when known."""
    if ent is None:
        return
    with ent.lock:
        if target.get("url"):
            ent.url = target["url"]
        if target.get("headers") is not None:
            ent.headers = dict(target.get("headers") or {})
        if target.get("title"):
            ent.title = str(target["title"])
        if target.get("ext"):
            ent.ext = str(target["ext"]).lower().lstrip(".") or ent.ext
        dur = target.get("duration")
        if dur:
            try:
                ent.duration = float(dur)
            except (TypeError, ValueError):
                pass
        size = target.get("filesize") or target.get("filesize_approx") or 0
        try:
            size = int(size or 0)
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            _media_set_total(ent, size)
    _media_start_filler(ent)


def _media_position(page: str, fmt: str, t: float | None, duration: float | None,
                    paused: bool) -> MediaEntry | None:
    ent = _media_get(page, fmt)
    if ent is None:
        return None
    with ent.lock:
        if duration and duration > 0:
            ent.duration = float(duration)
        if t is not None:
            ent.playhead = max(0.0, float(t))
            ent.has_position = True
            ent.position_at = time.time()
        ent.paused = bool(paused)
        ent.last_used = time.time()
    _media_start_filler(ent)
    return ent


# -- window math ------------------------------------------------------------ #

def _media_window(ent: MediaEntry) -> tuple[int, int]:
    """Byte window [start, end) the cache should hold right now.

    The window is `budget` bytes wide, anchored just behind the playhead. Near
    the end of a file it clamps and simply shrinks: we do not backfill content
    the playhead has already passed.
    """
    with ent.lock:
        total = ent.total
        budget = ent.budget or MEDIA_CHUNK
        if total <= 0:
            return (0, 0)
        if ent.has_position and ent.duration > 0:
            ratio = max(0.0, min(1.0, ent.playhead / ent.duration))
            anchor = int(total * ratio)
            back = int(total * (CACHE_BACK / ent.duration))
        else:
            anchor = ent.anchor
            back = 0
        start = max(0, anchor - max(0, back))
        end = min(total, start + budget)
        if total - end < MEDIA_CHUNK:
            # Include the final, partial chunk: otherwise the last few hundred
            # KB of a file can never be served from cache.
            end = total
        if end <= start:                 # the anchor sat past the end
            start, end = max(0, total - budget), total
        return (start, end)


def _media_plan_chunks(ent: MediaEntry) -> tuple[int, int]:
    start, end = _media_window(ent)
    if end <= start:
        return (0, -1)
    return (start // MEDIA_CHUNK, (end - 1) // MEDIA_CHUNK)


# -- chunk storage ---------------------------------------------------------- #

def _merge_span(spans: list, lo: int, hi: int) -> list:
    out = []
    for s, e in spans:
        if e < lo or s > hi:
            out.append((s, e))
        else:
            lo, hi = min(lo, s), max(hi, e)
    out.append((lo, hi))
    out.sort()
    return out


def _span_covers(spans: list, lo: int, hi: int) -> bool:
    cur = lo
    for s, e in sorted(spans):
        if s > cur:
            return False
        cur = max(cur, e)
        if cur >= hi:
            return True
    return cur >= hi


def _media_write(ent: MediaEntry, offset: int, data: bytes) -> None:
    if not data or _MEDIA_CLOSING.is_set():
        return
    with ent.lock:
        pos = 0
        while pos < len(data):
            abs_off = offset + pos
            idx = abs_off // MEDIA_CHUNK
            within = abs_off - idx * MEDIA_CHUNK
            take = min(MEDIA_CHUNK - within, len(data) - pos)
            _media_write_piece(ent, idx, within, data[pos:pos + take])
            pos += take
        ent.last_used = time.time()


def _media_write_piece(ent: MediaEntry, idx: int, within: int, piece: bytes) -> None:
    if idx in ent.present:
        return
    length = ent.chunk_len(idx)
    if length <= 0:
        return
    hi = min(within + len(piece), length)
    if hi <= within:
        return
    try:
        os.makedirs(ent.dir, exist_ok=True)
        path = os.path.join(ent.dir, f"chunk_{idx}.bin")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.pwrite(fd, piece[:hi - within], within)
        finally:
            os.close(fd)
    except OSError:
        return
    spans = _merge_span(ent.ranges.get(idx, []), within, hi)
    ent.ranges[idx] = spans
    if _span_covers(spans, 0, length):
        try:
            os.truncate(path, length)
        except OSError:
            pass
        ent.present.add(idx)
        ent.ranges.pop(idx, None)
        ent.on_disk += length


def _media_remove_chunk(ent: MediaEntry, idx: int) -> None:
    complete = idx in ent.present
    length = ent.chunk_len(idx) if complete else 0
    try:
        os.remove(os.path.join(ent.dir, f"chunk_{idx}.bin"))
    except OSError:
        pass
    ent.present.discard(idx)
    ent.ranges.pop(idx, None)
    ent.inflight.discard(idx)
    if complete:
        ent.on_disk = max(0, ent.on_disk - length)


def _media_enforce(ent: MediaEntry) -> None:
    """Drop everything outside the current window (and partial chunks too)."""
    if not ent.total or not ent.budget:
        return
    lo, hi = _media_plan_chunks(ent)
    if hi < lo:
        return
    with ent.lock:
        for idx in list(set(ent.present) | set(ent.ranges)):
            if idx < lo or idx > hi:
                _media_remove_chunk(ent, idx)


def _media_covers(ent: MediaEntry, start: int, end: int) -> bool:
    """True when every chunk touching [start, end) is fully cached."""
    if end <= start or not ent.total:
        return False
    first, last = start // MEDIA_CHUNK, (end - 1) // MEDIA_CHUNK
    with ent.lock:
        return all(idx in ent.present for idx in range(first, last + 1))


def _media_disk_usage() -> int:
    return sum(ent.on_disk for ent in list(_MEDIA.values()))


def _media_enforce_quota() -> None:
    if MEDIA_MAX_MB <= 0:
        return
    limit = MEDIA_MAX_MB * 1_000_000
    if _media_disk_usage() <= limit:
        return
    now = time.time()
    for ent in sorted(list(_MEDIA.values()), key=lambda e: e.last_used):
        if _media_disk_usage() <= limit:
            return
        if now < ent.player_until:
            continue
        _media_destroy(ent)


# -- background filler ------------------------------------------------------ #

def _media_start_filler(ent: MediaEntry | None) -> None:
    if not MEDIA_CACHE or ent is None or not ent.total:
        return
    with ent.lock:
        if ent.filler is not None and ent.filler.is_alive():
            return
        ent.stop.clear()
        ent.filler = threading.Thread(target=_media_fill, args=(ent,), daemon=True,
                                      name=f"media-{ent.key[:8]}")
        ent.filler.start()


def _media_fetch_chunk(ent: MediaEntry, idx: int) -> None:
    with ent.lock:
        if idx in ent.present or idx in ent.inflight or not ent.url or not ent.total:
            return
        # If we previously flagged this entry as unreachable, only re-probe the
        # signed URL once every RECONNECT_PROBE_INTERVAL — otherwise each
        # missing-chunk retry would hammer a host that is still offline.
        now = time.time()
        if ent.stale_since is not None and now - ent.stale_since < RECONNECT_PROBE_INTERVAL:
            # Still within the cool-down: mark inflight so _media_fill skips
            # this chunk until the probe interval elapses, then return without
            # touching the network.
            ent.inflight.add(idx)
            return
        ent.inflight.add(idx)
        url = ent.url
        headers = dict(ent.headers)
        start = idx * MEDIA_CHUNK
        end = min(ent.total, start + MEDIA_CHUNK) - 1
    headers["Range"] = f"bytes={start}-{end}"
    resp = None
    try:
        for attempt in (0, 1):
            try:
                resp = guarded_urlopen(url, headers, timeout=PROXY_TIMEOUT)
                break
            except urllib.error.HTTPError as exc:
                if attempt or exc.code not in EXPIRY_CODES:
                    return
                try:                     # signed URL expired → re-resolve once
                    target = resolve_target(ent.page, ent.fmt, fresh=True)
                except Exception:        # noqa: BLE001
                    return
                if not target.get("url"):
                    return
                with ent.lock:
                    ent.url = target["url"]
                    ent.headers = dict(target.get("headers") or {})
                    ent.stale_since = None
                    url = ent.url
                    headers = {**ent.headers,
                               "Range": f"bytes={start}-{end}"}
            except (urllib.error.URLError, OSError):
                with ent.lock:
                    if ent.stale_since is None:
                        ent.stale_since = now
                return
        if resp is None:
            return
        with resp:
            status = getattr(resp, "status", 200) or 200
            if start > 0 and status != 206:
                return                   # server ignored Range → wrong offset
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].lower()
            if ctype in ("text/html", "application/xhtml+xml", "application/json"):
                return
            got = 0
            want = end - start + 1
            while got < want:
                buf = resp.read(min(STREAM_CHUNK, want - got))
                if not buf:
                    break
                _media_write(ent, start + got, buf)
                got += len(buf)
            with ent.lock:
                ent.stale_since = None   # network is back
            if PREFETCH_BPS and got:
                time.sleep(min(1.0, got / PREFETCH_BPS))
    finally:
        with ent.lock:
            ent.inflight.discard(idx)


def _media_fill(ent: MediaEntry) -> None:
    while not _MEDIA_CLOSING.is_set() and not ent.stop.is_set():
        try:
            with ent.lock:
                ready = bool(ent.total and ent.budget and ent.url and not ent.paused)
                player_until = ent.player_until
            if not ready:
                ent.stop.wait(0.5)
                continue
            if time.time() < player_until:
                ent.stop.wait(0.25)      # never compete with the playing stream
                continue
            lo, hi = _media_plan_chunks(ent)
            if hi < lo:
                ent.stop.wait(0.5)
                continue
            missing = [i for i in range(lo, hi + 1)
                       if i not in ent.present and i not in ent.inflight]
            if not missing:
                _media_enforce(ent)
                ent.stop.wait(0.5)
                continue
            _media_fetch_chunk(ent, missing[0])
        except Exception:  # noqa: BLE001
            ent.stop.wait(1.0)


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
UI_LOG_LIMIT = SlidingWindow(20)


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


_SIG_PATH_KEYS = {"expire", "sig", "ei", "ip", "source", "id", "requiressl",
                  "ratebypass", "pfa", "sgovp", "sparams", "signature",
                  "signature-ttl", "playlist_type", "txp", "epbp", "mt",
                  "fvip", "keepalive", "fexp", "clen", "dur", "gir", "lmt",
                  "rqh", "xpc", "cps", "met", "mh", "mm", "mn", "ms", "mv",
                  "mvi", "pl", "rms", "pcm2", "gcr", "ph", "initcwndbps",
                  "bui", "spc", "vprv", "lsig", "lsparams"}
_SIG_QUERY_KEYS = {"expire", "sig", "ei", "ip", "signature", "signature-ttl"}


def _normalize_url(url: str) -> str:
    """Strip signature-bound parts from a URL so two URLs for the same
    segment (different signatures) normalize to the same value."""
    parsed = urllib.parse.urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p
                  and p not in _SIG_PATH_KEYS]
    query = {k: v for k, v in urllib.parse.parse_qs(parsed.query).items()
             if k not in _SIG_QUERY_KEYS}
    new_query = urllib.parse.urlencode(query, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(
        path="/".join(path_parts), query=new_query))


def _find_matching_segment(text: str, final: str,
                           requested_url: str) -> str | None:
    """Find a segment URL in playlist `text` that matches `requested_url`.

    Two URLs match when they identify the same segment — i.e. their paths are
    the same apart from signature-related parameters (expire, sig, ei, ip, …)
    that change on every re-extraction.
    """
    target = _normalize_url(requested_url)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        seg_url = urllib.parse.urljoin(final, stripped)
        if _normalize_url(seg_url) == target:
            return seg_url
    return None


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
                "/api/position": lambda: self._api_position(q, head),
                "/api/ui-log": lambda: self._api_ui_log(q, head),
                "/api/reconnect": lambda: self._api_reconnect(q, head),
            }
            fn = routes.get(path)
            if fn is not None:
                # ui-log stays token-free like health: it is the error box's
                # way of telling the operator what the phone saw, and it is
                # exactly what must still work when the UI is stranded.
                if path not in ("/api/health", "/api/ui-log"):
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
        except RedirectBlocked as exc:
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
                "alwaysProxy": ALWAYS_PROXY,
                "proxy": True,
                "hls": True,
                "merge": FFMPEG is not None and mode != "none",
                "playlists": PLAYLIST_PROBE,
                "cookies": bool(cookies_path() and os.path.isfile(cookies_path())),
                "impersonate": IMPERSONATE if impersonate_ready() else None,
                "subtitles": True,
                "cache": True,
                "cacheWindow": bool(MEDIA_CACHE),
                "download": True,
            },
            "tokenRequired": bool(TOKEN),
            "staticBuild": static_build(),
            "platform": PLATFORM or "self-hosted",
            "cache": {"dir": CACHE_DIR, "entries": len(_MEM), **STATS},
            "mediaCache": {
                "enabled": bool(MEDIA_CACHE),
                "entries": len(_MEDIA),
                "bytes": _media_disk_usage(),
                "ttl": MEDIA_TTL,
            },
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
            # A video-only HLS variant is not a better default than the merge:
            # it plays silently. When the only muxed option is the ffmpeg
            # stitched stream, prefer it so "play" actually has sound.
            if chosen and not chosen.get("hasAudio") and chosen.get("kind") in ("hls", "dash"):
                chosen = merge
        if MEDIA_CACHE:
            for f in fmts:
                if f.get("kind") != "direct":
                    continue
                ent = _media_get(url, f["id"], create=False)
                if ent is not None:
                    f["cachedBytes"] = ent.on_disk
                    if ent.budget:
                        f["cacheBudget"] = ent.budget

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
            # Direct formats are routed through /api/play?proxy=1 when this is
            # true, which is what lets the windowed cache see the bytes.
            "cacheEnabled": bool(MEDIA_CACHE),
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
                    if fmt:
                        # keep the variant the user is watching, so the file we
                        # save matches the quality on screen
                        link += f"&fmt={urllib.parse.quote(fmt, safe='')}"
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

        # Cache-first: if we already hold the requested byte range, serve it
        # without touching the network. This is what keeps playback alive when
        # the CDN drops a connection, and it also makes seeking inside the
        # window instant.
        ent = None
        if MEDIA_CACHE and q.get("proxy") == "1":
            ent = _media_get(src, fmt, create=False)
            if ent is not None and self._media_serve(ent, head=head):
                return

        try:
            target = resolve_target(src, fmt, fresh=q.get("fresh") == "1")
        except ValueError as exc:
            return self._send_error_json(friendly(exc), 400)
        except Exception as exc:  # noqa: BLE001
            if not MEDIA_EXT_RE.search(src):
                # The extractor is down, but the bytes in the window are still
                # perfectly good: play those before reporting a failure.
                if ent is not None and self._media_serve(ent, head=head):
                    return
                return self._send_error_json(friendly(exc), 502)
            # A plain media URL: just fetch it, no extraction needed.
            target = {"url": src, "headers": {}, "kind": "direct", "id": None,
                      "ext": None, "title": None, "filesize": 0, "duration": None}

        if target["kind"] == "merge":
            link = f"/api/merge?url={urllib.parse.quote(src, safe='')}"
            if TOKEN:
                link += f"&token={urllib.parse.quote(TOKEN, safe='')}"
            return self._redirect(link)

        if q.get("proxy") != "1":
            return self._redirect(target["url"])

        if MEDIA_CACHE and target["kind"] == "direct":
            key_fmt = fmt or str(target.get("id") or "")
            ent = _media_get(src, key_fmt)
            _media_attach(ent, target)
            if self._media_serve(ent, head=head):
                return
        else:
            ent = None

        try:
            self._proxy_with_retry(src, fmt, target, head=head, reject_html=True,
                                   media=ent)
        except (Expired, Unreachable) as exc:
            if ent is not None and self._media_serve(ent, head=head):
                return
            if ent is not None and ent.total:
                # The cache knows this file but not this stretch of it. Say so
                # plainly instead of blaming the CDN for a mere miss.
                return self._send_error_json(
                    "This part isn't cached yet and the source is unreachable — "
                    "rewind a little and try again.", 503)
            self._send_error_json(
                f"{friendly(exc)} — reload, pick another quality, or try 'Force proxy'.",
                502)

    # -- windowed media cache ------------------------------------------------- #

    def _media_serve(self, ent, head: bool = False) -> bool:
        """Answer this request from disk when the Range is fully cached.

        Returns False when any part of the requested range is missing, so the
        caller streams (and tees) from upstream instead. Never sends a truncated
        206: a partial answer would look like a corrupt video to the browser.
        """
        rng = self.headers.get("Range") or ""
        m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
        if not m or not ent.total:
            return False
        total = ent.total
        if m.group(1):
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else total - 1
        elif m.group(2):
            n = int(m.group(2))
            if n <= 0:
                return False
            start, end = max(0, total - n), total - 1
        else:
            return False
        if start >= total:
            return False
        end = min(end, total - 1)
        if end < start or not _media_covers(ent, start, end + 1):
            return False

        now = time.time()
        ent.player_until = now + 3
        ent.last_used = now
        length = end - start + 1
        try:
            self.send_response(206)
            self.send_header("Content-Type", ent.ctype or "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            if ent.etag:
                self.send_header("ETag", ent.etag)
            if ent.last_modified:
                self.send_header("Last-Modified", ent.last_modified)
            # It is a cache, but a deliberately disposable one: the browser
            # must ask us again so eviction and TTL stay authoritative.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Viewer-Cache", "hit")
            self._cors()
            self.end_headers()
            if head:
                return True
            # Hold the entry lock while reading so a concurrent eviction cannot
            # unlink a chunk between the coverage check and the read.
            with ent.lock:
                self._media_write_out(ent, start, end)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return True

    def _media_write_out(self, ent, start: int, end: int) -> None:
        """Write cached chunks [start, end] to the client, one chunk at a time."""
        idx = start // MEDIA_CHUNK
        pos = start
        while pos <= end:
            base = idx * MEDIA_CHUNK
            lo = pos - base
            hi = min(ent.chunk_len(idx), end - base + 1)
            if hi <= lo:
                pos = base + hi
                idx += 1
                continue
            path = os.path.join(ent.dir, f"chunk_{idx}.bin")
            try:
                with open(path, "rb") as fh:
                    fh.seek(lo)
                    left = hi - lo
                    while left > 0:
                        buf = fh.read(min(STREAM_CHUNK, left))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        left -= len(buf)
            except OSError:
                return
            pos = base + hi
            idx += 1

    def _api_position(self, q: dict, head: bool = False) -> None:
        """The player tells us where it is, so the window can follow it.

        Range requests alone cannot distinguish "watching at 3:15" from
        "prefetching 500 MB ahead", so the UI sends a throttled heartbeat.
        """
        src = q.get("url", "") or q.get("u", "")
        fmt = q.get("fmt", "") or q.get("f", "")
        if not valid_url(src):
            return self._send_error_json("Missing/invalid ?url=", 400)
        if not MEDIA_CACHE:
            return self._send_json({"ok": True, "cache": False})
        try:
            t = float(q.get("t") or 0)
        except (TypeError, ValueError):
            t = 0.0
        try:
            dur = float(q.get("d") or q.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        paused = str(q.get("paused") or "").lower() in ("1", "true", "yes")
        ent = _media_position(src, fmt, t, dur, paused)
        if ent is None:
            return self._send_json({"ok": True, "cache": False})
        start, end = _media_window(ent)
        self._send_json({
            "ok": True, "cache": True,
            "cachedBytes": ent.on_disk,
            "budgetBytes": ent.budget,
            "windowStart": start, "windowEnd": end,
            "ttl": MEDIA_TTL,
        })

    def _api_reconnect(self, q: dict, head: bool = False) -> None:
        """Frontend calls this when the browser regains internet.

        Forces a fresh re-extraction of the cached (page, fmt) pair so stale
        signed URLs are replaced with live ones, then restarts the prefetcher
        so the cache window refills. Best-effort: never raises.
        """
        src = q.get("url", "") or q.get("u", "")
        fmt = q.get("fmt", "") or q.get("f", "")
        if not valid_url(src):
            return self._send_error_json("Missing/invalid ?url=", 400)
        if not MEDIA_CACHE:
            return self._send_json({"ok": True, "cache": False})
        ent = _media_get(src, fmt, create=False)
        if ent is None:
            return self._send_json({"ok": False, "reason": "not cached"})
        try:
            target = resolve_target(src, fmt, fresh=True)
        except Exception as exc:  # noqa: BLE001
            return self._send_json({"ok": False, "reason": friendly(exc)})
        if not target.get("url"):
            return self._send_json({"ok": False, "reason": "no url"})
        with ent.lock:
            ent.url = target["url"]
            ent.headers = dict(target.get("headers") or {})
            ent.stale_since = None
        _media_start_filler(ent)
        self._send_json({"ok": True, "refreshed": True, "url": target["url"]})

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
                          reject_html: bool = False, disposition: str | None = None,
                          media=None) -> None:
        """Stream `target`, re-extracting once if the CDN says the URL expired."""
        for attempt in (0, 1):
            try:
                self._proxy(target["url"], target.get("headers") or {}, head=head,
                            reject_html=reject_html, disposition=disposition,
                            media=media)
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
                if media is not None:
                    # Keep the cache entry on the fresh link, not the dead one.
                    _media_attach(media, retried)

    def _open_upstream(self, url: str, headers: dict, head: bool = False):
        merged = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        for k, v in (headers or {}).items():
            if not k or v is None or k.lower() in HEADER_BLOCKLIST:
                continue
            merged[k] = v
        rng = self.headers.get("Range")
        if rng:
            merged["Range"] = rng
        return guarded_urlopen(url, merged, head=head, timeout=PROXY_TIMEOUT)

    def _proxy(self, url: str, headers: dict, head: bool = False,
               reject_html: bool = False, disposition: str | None = None,
               media=None) -> None:
        if media is not None:
            # A player request is in flight: park the prefetcher so the two do
            # not compete for the same upstream link or the LAN.
            now = time.time()
            media.player_until = now + 3
            media.last_used = now
            if not (media.has_position and now - media.position_at <= 10):
                m = re.match(r"\s*bytes=(\d+)-", self.headers.get("Range") or "")
                if m:
                    media.anchor = int(m.group(1))
                    if media.duration > 0 and media.total > 0:
                        media.playhead = media.duration * (media.anchor / media.total)
                        media.has_position = True
        try:
            resp = self._open_upstream(url, headers, head)
        except urllib.error.HTTPError as exc:
            code = exc.code
            try:
                exc.close()
            except Exception:  # noqa: BLE001
                pass
            if code in EXPIRY_CODES or 500 <= code < 600:
                raise Expired(session_bound_media_note(url)
                              or f"The CDN refused the stream ({code})") from exc
            raise RuntimeError(f"Upstream returned {code}") from exc
        except urllib.error.URLError as exc:
            raise Unreachable(session_bound_media_note(url)
                              or f"Upstream refused the request ({exc.reason})") from exc
        except OSError as exc:
            raise Unreachable(f"Upstream is unreachable ({exc})") from exc

        self._send_upstream(resp, head=head, reject_html=reject_html,
                            disposition=disposition, media=media)

    def _send_upstream(self, resp, head: bool = False, reject_html: bool = False,
                       prefix: bytes = b"", disposition: str | None = None,
                       media=None) -> None:
        """Mirror an upstream response to the client (self-framing when needed)."""
        with resp:
            up = {k.lower(): v for k, v in resp.getheaders()}
            ctype = (up.get("content-type") or "").split(";")[0].strip().lower()
            if reject_html:
                # Someone answered with a web page: usually an expired signature
                # that got redirected to an error/consent page. Judge by the
                # body when we have one — hanime's CDN serves real media as
                # .html with text/html, and trusting the header alone would
                # reject every segment of it.
                document = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")[:1] == b"<"
                if ctype == "application/json" or (
                        ctype in ("text/html", "application/xhtml+xml")
                        and (document or not prefix)):
                    raise RuntimeError(
                        "the host answered with a web page instead of media — the link "
                        "expired, needs cookies, or is region-locked")
            chunked = "content-length" not in up and not head

            # Where this response lands in the file, so the tee writes the
            # bytes at the right offset even for a 206 that starts mid-file.
            media_pos = None
            if media is not None:
                media.ctype = up.get("content-type") or media.ctype
                media.etag = up.get("etag") or media.etag
                media.last_modified = up.get("last-modified") or media.last_modified
                cr = up.get("content-range") or ""
                m = re.search(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", cr)
                if m:
                    media_pos = int(m.group(1))
                    if m.group(3) != "*":
                        _media_set_total(media, int(m.group(3)))
                elif up.get("content-length"):
                    media_pos = 0
                    try:
                        _media_set_total(media, int(up["content-length"]))
                    except (TypeError, ValueError):
                        pass
                if not media.total:
                    media_pos = None      # unknown size: no window to roll
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
                media_tick = 0
                for buf in _chunks(resp, prefix):
                    if media is not None and media_pos is not None:
                        _media_write(media, media_pos, buf)
                        media_pos += len(buf)
                        media_tick += len(buf)
                        if media_tick >= 8 * MEDIA_CHUNK:
                            media_tick = 0
                            _media_enforce(media)
                    if chunked:
                        self.wfile.write(b"%X\r\n" % len(buf) + buf + b"\r\n")
                    else:
                        self.wfile.write(buf)
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                if media is not None and media_pos is not None:
                    _media_enforce(media)
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
            allow_refresh = bool(page and fmt)
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

            if raw:
                # Raw segment/key request, but re-extraction returned a
                # playlist — the segment URL expired. Find the matching
                # segment in the fresh playlist and serve it instead.
                segment_url = _find_matching_segment(text, final, target_url)
                if segment_url:
                    try:
                        resp2 = self._open_upstream(segment_url, headers)
                        with resp2:
                            peek2 = resp2.read(512)
                            if peek2.lstrip()[:7] != b"#EXTM3U":
                                return self._send_upstream(
                                    resp2, head=head, reject_html=True,
                                    prefix=peek2)
                    except Exception:  # noqa: BLE001
                        pass
                return self._send_error_json(
                    "Segment URL expired — refresh the playlist", 502)

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
    def _stream_hls_transmux(self, q: dict, target: dict, head: bool,
                             download: bool) -> None:
        """ffmpeg reads HLS on its own — hand it the manifest and pipe a real
        fragmented .mp4 out. Like the yt-dlp merge, the 200 goes out before we
        know the CDN will cooperate, so hard failures shrink the file instead
        of rewriting the status; stderr is drained on a thread both to prevent
        a full-pipe deadlock and to keep the last ffmpeg word in our log."""
        headers = target.get("headers") or {}
        name = safe_filename(target.get("title") or "video", "mp4")
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if headers:
            cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
        cmd += ["-i", target["url"], "-c", "copy",
                # HLS ships its AAC audio as ADTS frames; a bare copy dies the
                # moment the audio starts ("Malformed AAC bitstream") and the
                # download collapses to a few seconds of video.
                "-bsf:a", "aac_adtstoasc",
                "-movflags", "+frag_keyframe+empty_moov", "-f", "mp4", "pipe:1"]

        if head:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-store")
                if download:
                    self.send_header("Content-Disposition", name)
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
                self.send_header("Content-Disposition", name)
            self.send_header("Connection", "close")
            self.close_connection = True
            self._cors()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        note(f"{'download' if download else 'transmux'} hls ← {target['url'][:70]}")
        proc = subprocess.Popen(cmd, stdout=self.wfile, stderr=subprocess.PIPE,
                                bufsize=1 << 16)
        last = {"line": b""}

        def drain() -> None:
            try:
                for line in iter(proc.stderr.readline, b""):
                    if line.strip():
                        last["line"] = line.strip()[:300]
            except (OSError, ValueError):
                pass

        threading.Thread(target=drain, daemon=True).start()
        try:
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.stderr.close()
            except OSError:
                pass
        if not last["line"] or proc.returncode == 0:
            return
        note("  ffmpeg: "
             + last["line"].decode("utf-8", "replace").strip()[:200])

    def _api_merge(self, q: dict, head: bool = False) -> None:
        """Stream video+audio as one fMP4.

        yt-dlp resolves the signed URLs (`--get-url`); ffmpeg fetches them and
        muxes video+audio straight to the client, so playback starts at once
        and runs for the whole file instead of downloading to disk first.
        """
        page = q.get("url", "")
        fmt = q.get("fmt", "") or q.get("f", "")
        sel = q.get("sel", "") or "bv*[ext=mp4]+ba[ext=m4a]/bv*[ext=mp4]+ba/b[ext=mp4]/bv*+ba/b"
        sort = q.get("sort", "") or "vcodec:h264:vp9:av1,acodec:mp4a:opus,+res:1080"
        download = q.get("dl") == "1"
        if not valid_url(page):
            return self._send_error_json("Missing/invalid ?url=", 400)
        if FFMPEG is None or ytdlp_mode() == "none":
            return self._send_error_json(
                "Merging needs both yt-dlp and ffmpeg available on the server", 501)

        # Download links for HLS streams land here from /api/play's dl=1
        # redirect: they name a page *our panel* resolved (sulasokvid-style
        # adapters, header-protected manifests), not one yt-dlp's format
        # selection understands. ffmpeg reads HLS natively, so transmux the
        # resolved manifest straight to fragmented mp4 — at the variant the
        # user is watching when fmt names one. The info cache may carry an
        # already-expired token, so probe before committing: an expiry-code
        # answer re-extracts once, other errors leave ffmpeg to try anyway.
        if download:
            target = None
            try:
                target = resolve_target(page, fmt, fresh=q.get("fresh") == "1")
            except Exception:  # noqa: BLE001
                target = None
            if target and target.get("kind") == "hls":
                try:
                    with guarded_urlopen(target["url"], target.get("headers") or {},
                                         head=False, timeout=15):
                        pass                        # token alive — nothing to read
                except urllib.error.HTTPError as exc:
                    if exc.code in EXPIRY_CODES:
                        note(f"manifest probe said {exc.code} — re-extracting fresh")
                        try:
                            target = resolve_target(page, fmt, fresh=True)
                        except Exception:  # noqa: BLE001
                            target = None
                except Exception:  # noqa: BLE001
                    pass                            # ffmpeg will surface what's real
            if target and target.get("kind") == "hls":
                return self._stream_hls_transmux(q, target, head, download)

        # Let yt-dlp do only the extraction and hand us the signed URLs
        # (--get-url); ffmpeg does the downloading. When yt-dlp writes to
        # stdout it cannot run its own merge post-processor, so `-o -` gives
        # video *only*, and the old `yt-dlp -o - | ffmpeg -i pipe:0` path
        # produced silent video that died a few seconds in. Two -i inputs let
        # ffmpeg fetch the video and audio URLs in parallel and interleave the
        # fragments itself, so the whole file streams as one fMP4 with sound.
        url_args = ["--no-playlist", "--get-url", "-S", sort, "-f", sel, page]
        try:
            _, url_out, url_err = run_yt_dlp(url_args, timeout=YTDLP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return self._send_error_json(
                "yt-dlp timed out resolving the video — try again", 504)
        urls = [line.strip() for line in url_out.splitlines() if line.strip()]
        if not urls:
            err = " ".join(url_err.split())[-300:] or "yt-dlp returned no URL"
            note(f"merge/yt-dlp: {err}")
            # Nothing has been sent yet, so this is a real error — not the
            # silent 0-byte 200 that made the bug so hard to see.
            return self._send_error_json(f"yt-dlp could not resolve the video: {err}", 502)

        cookie = cookies_path()
        inputs: list[str] = []
        for u in urls:
            inputs += ["-user_agent", DEFAULT_UA,
                       # Signed CDN URLs are bound to the player page: without
                       # its Referer they answer 403, which is what killed the
                       # earlier ffmpeg hand-off.
                       "-headers", f"User-Agent: {DEFAULT_UA}\r\nReferer: {page}\r\n"]
            if cookie and os.path.isfile(cookie):
                inputs += ["-cookiefile", cookie]
            inputs += ["-i", u]
        maps = ["-map", "0:v:0", "-map", "1:a:0"] if len(urls) == 2 else ["-map", "0"]
        out = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               *inputs, *maps, "-c", "copy",
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
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header("Content-Disposition", safe_filename("video", "mp4"))
            self.send_header("Connection", "close")
            self.close_connection = True
            self._cors()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        note(f"{'download ' if download else 'merge '}{sel} "
             f"({len(urls)} input{'s' if len(urls) > 1 else ''}) ← {page[:70]}")
        proc = subprocess.Popen(out, stdout=self.wfile, stderr=subprocess.PIPE,
                                bufsize=1 << 16)
        last = {"line": ""}

        def drain() -> None:
            try:
                for raw in iter(proc.stderr.readline, b""):
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        last["line"] = line[:300]
            except (OSError, ValueError):
                pass

        threading.Thread(target=drain, daemon=True).start()
        try:
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode not in (0, None):
            note(f"merge/ffmpeg: {last['line'] or f'exited {proc.returncode}'}")

    # -- UI telemetry ----------------------------------------------------------- #
    def _api_ui_log(self, q: dict, head: bool = False) -> None:
        """One line per error the browser shows, so 'still can't play' is
        diagnosable from the log without asking the user to transcribe UI text.
        Token-free on purpose (the error path must never be locked out), so it
        is capped hard per address and the message truncated."""
        if not UI_LOG_LIMIT.allow(self._client_ip()):
            return self._send_error_json("Too many ui-log messages", 429)
        msg = (q.get("msg") or "").strip()[:200]
        if msg and not head:
            sys.stderr.write(f"[ui] {msg}\n")
            sys.stderr.flush()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

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
    global CACHE_DIR, INFO_DIR, SUB_DIR, YTDLP_DIR, MEDIA_DIR

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
        MEDIA_DIR = os.path.join(CACHE_DIR, "media")
    _BASE_ARGS.clear()          # rebuilt with the final cookie/impersonate setup
    YT_DLP = (shutil.which("yt-dlp") or shutil.which("yt_dlp")
              or (LOCAL_BIN if os.access(LOCAL_BIN, os.X_OK) else None))
    FFMPEG = _ffmpeg_path()

    ensure_dirs()
    media_startup()
    atexit.register(media_shutdown)

    def _stop(signum, frame):
        media_shutdown()
        raise KeyboardInterrupt

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _stop)
        except (ValueError, OSError):
            pass
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