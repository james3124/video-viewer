"""Universal fallback extractor — pattern families, never host names.

yt-dlp knows ~1000 sites; the failures that remain are structural, not exotic:
media hiding in meta tags, JSON-LD, an HTML5 `<video>` tag, a player config, a
nested player frame or a manifest's variant list. This module tries each
pattern once, in an explicit order, and returns the same info-dict shape the
explicit adapters in `server.py` produce — so `browsable_formats`,
`resolve_target` and the proxy need no changes.

Scope: public streams only. Login, paywall, DRM and geo blocks are refused
with a clear reason; the user's own cookies remain the only session. The media
redirect guard in server.py is never relaxed here: only an explicit adapter
may name a CDN hop.

Trigger: after yt-dlp and the explicit adapters have failed (see
`server.py:_extract_sniffed`). Adding a site decoder stays a deliberate
`_PLAYER_SITES` entry — this file learns patterns, not hosts.

Env:
  VIEWER_UNIVERSAL        0 disables the whole layer (default on)
  VIEWER_UNIVERSAL_VERIFY 1 HEAD-checks the top candidates (default on)
  VIEWER_UNIVERSAL_TIMEOUT overall deadline in seconds (default 6)
  VIEWER_UNIVERSAL_MAX_FETCH max page fetches, including iframes (default 3)
"""

from __future__ import annotations

import html as _html
import json
import re
import time
import urllib.parse

VERSION_SNAPSHOT = 1

# --------------------------------------------------------------------------
# tunables (env, read once)
# --------------------------------------------------------------------------

import os  # noqa: E402


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


_UNIVERSAL_ENABLED = _env_int("VIEWER_UNIVERSAL", 1) != 0
_VERIFY = _env_int("VIEWER_UNIVERSAL_VERIFY", 1) != 0
_DEADLINE_S = max(1, _env_int("VIEWER_UNIVERSAL_TIMEOUT", 6))
_MAX_FETCHES = max(1, _env_int("VIEWER_UNIVERSAL_MAX_FETCH", 3))
_VERIFY_N = 3

# --------------------------------------------------------------------------
# evidence words that end the search with a reason instead of a stream
# --------------------------------------------------------------------------

_LOGIN_MARKERS = (
    "login to watch", "log in to watch", "sign in to watch",
    "must be logged in", "premium members", "subscribe to watch",
    "members only", "paywall", "buy this video",
)
_DRM_MARKERS = ("widevine", "fairplay", "playready", "drm license", "drm/")
# Tracker/ad slots: ranking demotes them, never serves them.
_AD_HOSTS = ("doubleclick", "googlesyndication", "juicyads", "ads.", ".ad.")

_PAGE_FETCH_MAX = 2 << 20     # matches server._fetch_html
_MANIFEST_FETCH_MAX = 256 << 10

_URL = re.compile(r"https?://[^\s\"'<>)\]]", re.I)
# Local/LAN/userinfo fragments are never usable media URLs.
_BAD_URL_TOKEN = re.compile(r"(localhost|127\.|192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.|[^\@/]*@)", re.I)

_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_CONTENT_RE = re.compile(r"content\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_META_NAME_RE = re.compile(
    r"(?:property|name)\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_LD_JSON_RE = re.compile(
    r"<script[^>]*type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.S | re.I)
_VIDEO_TAG_RE = re.compile(r"<video\b[^>]*>", re.I)
_SOURCE_TAG_RE = re.compile(r"<source\b[^>]*>", re.I)
_SRC_RE = re.compile(r"\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_SRC_DAT_RE = re.compile(
    r"\bdata-(?:src|media-src|video-src|hls|file|mp4|m3u8)\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_TYPE_RE = re.compile(r"\btype\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*\bsrc\s*=\s*['\"]([^'\"]+)['\"]", re.I)
_OG_VIDEO_NAMES = (
    "og:video", "og:video:secure_url", "og:video:url",
    "twitter:player:stream", "twitter:player:stream:content_type",
)
# Player-config arrays — kept narrow on purpose: pattern families, not
# per-site selectors. `sources:` is the JW Player/Vimeo shape, `file:` the
# Video.js/Plyr one, `src:` a strays-only fallback in video.js `<source>`
# strings embedded in JS.
_CONFIG_RE = re.compile(
    r"(?:sources?\s*:|file\s*:)\s*(\[.*?\])", re.S)
_SRC_ARRAY_RE = re.compile(
    r"src\s*:\s*['\"](https?://[^'\"]+\.(?:m3u8|mp4|webm|m4v)(?:\?[^'\"]*)?)['\"]", re.I)
_MEDIA_ANCHOR_RE = re.compile(
    r"""["'`](https?://[^"'`\s<>{}]{6,500}?\.(?:m3u8|mp4|webm|m4v|mov)(?:\?[^"'`\s<>{}]{0,300})?)["'`]""",
    re.I)
_HEIGHT_RE = re.compile(r"_(\d{3,4})p(?:[_.\-]|$|/)|[-_](\d{3,4})p(?:[_.\-]|$|/)|"
                        r"(\d{3,4})\s*[pP]\b|/(\d{3,4})p/", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _html.unescape(_TAG_STRIP_RE.sub("", text or ""))).strip()


def _height_of(url: str, label: str = "") -> int | None:
    for candidate in (label, url):
        m = _HEIGHT_RE.search(candidate or "")
        if m:
            digits = next((g for g in m.groups() if g), None)
            if digits:
                value = int(digits)
                if 120 <= value <= 4320:
                    return value
    return None


def _ext_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rsplit(".", 1)[-1].lower() if "." in urllib.parse.urlparse(url).path else ""
    return path or "mp4"


def _is_ad_host(host: str) -> bool:
    host = (host or "").lower()
    return any(token in host for token in _AD_HOSTS)


def usable_media_url(raw: str, page: str) -> str | None:
    """One candidate in, a vetted absolute URL or None."""
    if not raw or not page:
        return None
    raw = _html.unescape(str(raw).replace("\\/", "/")).strip()
    if not raw:
        return None
    try:
        joined = urllib.parse.urljoin(page, raw)
    except Exception:  # noqa: BLE001
        return None
    parsed = urllib.parse.urlparse(joined)
    host = (parsed.hostname or "").lower()
    if not host or parsed.scheme not in ("http", "https"):
        return None
    if parsed.scheme == "http" and parsed.netloc != host and "@" in parsed.netloc:
        return None
    if host in ("localhost",) or host.startswith(("127.", "0.", "10.", "192.168.",
                                                  "172.16.", "172.17.", "172.18.",
                                                  "172.19.", "172.20.", "172.21.",
                                                  "172.22.", "172.23.", "172.24.",
                                                  "172.25.", "172.26.", "172.27.",
                                                  "172.28.", "172.29.", "172.30.",
                                                  "172.31.", "[::1]", "::1")):
        return None
    if "@" in (parsed.netloc or "") and parsed.username:
        return None
    if _BAD_URL_TOKEN.search(host) or _BAD_URL_TOKEN.search(urllib.parse.unquote(parsed.path)):
        return None
    if _is_ad_host(host):
        return None
    return joined


class Candidate:
    __slots__ = ("url", "kind", "ext", "height", "label", "probe",
                 "page", "confidence", "ad")

    def __init__(self, url, kind, ext, height, label, probe, page, confidence,
                 ad=False):
        self.url = url
        self.kind = kind
        self.ext = ext
        self.height = height
        self.label = label
        self.probe = probe
        self.page = page
        self.confidence = confidence
        self.ad = ad

    def __repr__(self):  # pragma: no cover - debug aid
        return f"<Candidate {self.probe} {self.kind} {self.height}p {self.url[:70]}>"


def _same_host(a: str, b: str) -> bool:
    try:
        return (urllib.parse.urlparse(a).hostname or "").lower() == \
               (urllib.parse.urlparse(b).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

def _meta_urls(html: str):
    replay = html or ""
    for tag in _META_RE.finditer(replay):
        attrs = tag.group(0)
        name = _META_NAME_RE.search(attrs)
        if not name:
            continue
        key = name.group(1).strip().lower()
        if key not in _OG_VIDEO_NAMES:
            continue
        content = _META_CONTENT_RE.search(attrs)
        if content:
            yield content.group(1), key


def _ld_json_urls(html: str):
    for block in _LD_JSON_RE.findall(html or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        yield from _walk_json_media(data)


_LD_MEDIA_EXT_RE = re.compile(r"\.(?:mp4|m4v|webm|mov|m3u8|mpd|ts)(?:\?|#|$)", re.I)


def _walk_json_media(obj, depth: int = 0):
    if depth > 6:
        return
    if isinstance(obj, dict):
        # contentURL/file/src name the media itself; a bare `url` in JSON-LD is
        # usually the page (or its author), so only trust it with an extension.
        for key in ("contentURL", "contentUrl", "src", "file"):
            value = obj.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                yield value, key
        page_like = obj.get("url")
        if (isinstance(page_like, str)
                and page_like.startswith(("http://", "https://"))
                and _LD_MEDIA_EXT_RE.search(page_like)):
            yield page_like, "url"
        for value in obj.values():
            yield from _walk_json_media(value, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_media(item, depth + 1)


def _media_tag_urls(html: str):
    for tag in _VIDEO_TAG_RE.finditer(html or ""):
        text = tag.group(0)
        for regex in (_SRC_RE, _SRC_DAT_RE):
            found = regex.search(text)
            if found:
                yield found.group(1), "video-src"
        break
    for tag in _SOURCE_TAG_RE.finditer(html or ""):
        text = tag.group(0)
        found = _SRC_RE.search(text) or _SRC_DAT_RE.search(text)
        if found:
            yield found.group(1), "source-src"


def _config_urls(html: str):
    for match in _CONFIG_RE.finditer(html or ""):
        snippet = match.group(1)
        # Normalise JPEG-style escaped slashes and HTML entities, then read
        # the array of {file, label, type} objects.
        snippet = snippet.replace("\\/", "/")
        try:
            data = json.loads(snippet)
        except ValueError:
            for token in _SRC_ARRAY_RE.finditer(match.group(0)):
                yield token.group(1), "src"
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                for key in ("file", "src", "url"):
                    if isinstance(item.get(key), str):
                        yield item[key], str(item.get("label") or "")
                        break
            elif isinstance(item, str):
                yield item, ""


# --------------------------------------------------------------------------
# ctx / probe runner
# --------------------------------------------------------------------------

class Context:
    def __init__(self, url, html, deadline, fetch, referer_hint=""):
        self.url = url
        self.html = html or ""
        self.origin = "/".join(url.split("/")[:3]) if url else ""
        self.deadline = deadline
        self.fetch = fetch
        self.referer = referer_hint or self.origin + "/"
        self.candidates: list[Candidate] = []
        self.trace: list[str] = []
        self.frames: list[str] = []

    def offer(self, raw, kind, ext, height, label, probe, confidence,
              page=None):
        url = usable_media_url(raw, page or self.url)
        if not url:
            return False
        if ext not in ("m3u8", "mp4", "webm", "m4v", "mov", "mpd"):
            ext = "m3u8" if url.split("?")[0].lower().endswith(".m3u8") else _ext_of(url)
        if ext not in ("m3u8", "mp4", "webm", "m4v", "mov", "mpd"):
            return False
        self.candidates.append(Candidate(
            url, kind, ext, height, label, probe, page or self.url,
            confidence))
        return True

    def time_left(self) -> float:
        return max(0.0, self.deadline - time.time())


def run_probe(name, fn, ctx: Context) -> None:
    if ctx.time_left() <= 0:
        return
    before = len(ctx.candidates)
    try:
        fn(ctx)
        added = len(ctx.candidates) - before
        if added:
            ctx.trace.append(f"{name}:{added}")
    except Exception as exc:  # noqa: BLE001 - one probe must not kill the rest
        ctx.trace.append(f"{name}:error {type(exc).__name__}")


def probe_meta(ctx):
    for raw, key in _meta_urls(ctx.html):
        ctx.offer(raw, "hls" if ".m3u8" in raw.lower() else "direct",
                  "m3u8" if ".m3u8" in raw.lower() else _ext_of(raw),
                  _height_of(raw), "", "og", 90)


def probe_jsonld(ctx):
    for raw, key in _ld_json_urls(ctx.html):
        ctx.offer(raw, "hls" if ".m3u8" in raw.lower() else "direct",
                  "m3u8" if ".m3u8" in raw.lower() else _ext_of(raw),
                  _height_of(raw), "", "jsonld", 82)


def probe_media_tags(ctx):
    for raw, label in _media_tag_urls(ctx.html):
        ctx.offer(raw, "hls" if ".m3u8" in raw.lower() else "direct",
                  "m3u8" if ".m3u8" in raw.lower() else _ext_of(raw),
                  _height_of(raw), "", "html5", 88)


def probe_player_config(ctx):
    for raw, label in _config_urls(ctx.html):
        lowered = raw.lower()
        is_hls = ".m3u8" in lowered
        ctx.offer(raw, "hls" if is_hls else "direct",
                  "m3u8" if is_hls else _ext_of(raw),
                  _height_of(raw, label), label, "player-config", 86)


def probe_iframes(ctx):
    for match in _IFRAME_RE.finditer(ctx.html):
        src = _html.unescape(match.group(1)).replace("\\/", "/")
        if _is_ad_host(urllib.parse.urlparse(
                urllib.parse.urljoin(ctx.url, src)).hostname or ""):
            continue
        if any(hint in src.lower() for hint in ("player", "embed", "/e/",
                                                "watch", "stream", "jwplayer",
                                                "kt_player", "clean-tube",
                                                "media", "html5")):
            joined = urllib.parse.urljoin(ctx.url, src)
            if usable_media_url(joined, ctx.url) and \
                    joined not in ctx.frames:
                ctx.frames.append(joined)


def probe_media_anchors(ctx):
    for match in _MEDIA_ANCHOR_RE.finditer(ctx.html):
        ctx.offer(match.group(1), "direct", _ext_of(match.group(1)),
                  _height_of(match.group(1)), "", "anchor", 62)


PROBE_REGISTRY = (
    ("og", probe_meta),
    ("jsonld", probe_jsonld),
    ("html5", probe_media_tags),
    ("player-config", probe_player_config),
    ("iframe", probe_iframes),
    ("anchor", probe_media_anchors),
)


def _ranked(candidates: list[Candidate], page: str):
    best: dict[str, Candidate] = {}
    for c in candidates:
        key = (c.url, c.kind)
        if key not in best or c.confidence > best[key].confidence:
            best[key] = c
    ordered = sorted(best.values(), key=lambda c: (
        c.ad,
        not _same_host(c.page, page),
        c.kind == "hls",
        -(c.height or 0),
        -c.confidence,
    ))
    return ordered


# --------------------------------------------------------------------------
# verification / manifests
# --------------------------------------------------------------------------

def _verify(candidate, http_head, deadline: float):
    """HEAD then a 0-byte range. Returns (size, content_type) or None."""
    headers = {"Range": "bytes=0-0"}
    try:
        status, hdrs = http_head(candidate.url, headers, candidate.page,
                                 timeout=max(1, deadline - time.time()))
    except Exception:  # noqa: BLE001
        return None
    if status not in (200, 206):
        return None
    ctype = (hdrs.get("Content-Type") or "").split(";")[0].lower()
    if ctype in ("text/html", "application/xhtml+xml", "application/json"):
        return None
    header_value = hdrs.get("Content-Range") or ""
    size = None
    m = re.search(r"/(\d+)\s*$", header_value)
    if m:
        size = int(m.group(1))
    elif hdrs.get("Content-Length", "").isdigit():
        size = int(hdrs["Content-Length"])
    return size, ctype


# --------------------------------------------------------------------------
# materialise
# --------------------------------------------------------------------------

def _info_from_candidates(page, title, candidates, probe_name):
    formats = []
    seen = set()
    for c in candidates:
        key = (c.url, c.kind)
        if key in seen:
            continue
        seen.add(key)
        height = c.height
        label = str(c.label or "")
        fid = f"uni-{height or c.ext}-{len(formats)}"
        formats.append({
            "format_id": fid,
            "url": c.url,
            "ext": c.ext,
            "protocol": "m3u8_native" if c.kind == "hls" else "https",
            "height": height,
            "width": None,
            "fps": None,
            "vcodec": None,
            "acodec": None,
            "_sniffed": True,
            "_universal": {"probe": probe_name},
            "http_headers": {"Referer": c.page},
        })
    return {
        "title": title,
        "extractor_key": f"universal:{probe_name}",
        "formats": formats,
        "webpage_url": page,
    }


def _title(html: str, fallback: str | None = None):
    h1 = _H1_RE.search(html or "")
    title = _TITLE_RE.search(html or "")
    return (_clean(h1.group(1))[:160]
            if h1 else (_clean(title.group(1))[:160] if title else fallback)) or None


def refusal_reason(html: str):
    low = (html or "").lower()
    if any(marker in low for marker in _LOGIN_MARKERS):
        return ("this page wants a login before it shows a stream — "
                "public media only, add cookies (VIEWER_COOKIES_B64)")
    if any(marker in low for marker in _DRM_MARKERS):
        return "this stream is DRM-protected — the viewer cannot play it"
    return None


def universal_extract(url, fetch_html, http_head, html=None, ua="",
                      note=None):
    """Run every probe, verify the best, return one info dict — or None.

    `fetch_html(url) -> (final_url, html) | None` and
    `http_head(url, headers, page, timeout) -> (status, headers)` come from
    server.py so this module stays free of HTTP plumbing.
    """
    if not _UNIVERSAL_ENABLED:
        return None
    deadline = time.time() + _DEADLINE_S
    fetched = 0
    refetch = html

    initial_reason = refusal_reason(refetch or "")
    if initial_reason:
        raise RuntimeError(initial_reason)

    if refetch is None:
        page = fetch_html(url)
        if not page:
            return None
        fetched += 1
        final_url, refetch = page
    else:
        final_url = url

    ctx = Context(final_url, refetch, deadline, None, final_url)
    for name, fn in PROBE_REGISTRY:
        run_probe(name, fn, ctx)

    # Follow one player frame, same-origin first.
    if ctx.frames and fetched < _MAX_FETCHES and ctx.time_left() > 0:
        frame = ctx.frames[0]
        try:
            frame_page = fetch_html(frame)
        except Exception:  # noqa: BLE001
            frame_page = None
        if frame_page:
            fetched += 1
            frame_final, frame_html = frame_page
            if refusal_reason(frame_html):
                pass
            else:
                nested = Context(frame_final, frame_html, deadline, None,
                                 frame_final)
                nested.trace = []
                for name, fn in PROBE_REGISTRY[:4]:
                    run_probe(name, fn, nested)
                for cand in nested.candidates:
                    cand.page = frame_final
                    ctx.candidates.append(cand)

    if not ctx.candidates:
        return None

    ranked = _ranked(ctx.candidates, final_url)

    if _VERIFY:
        checked, remaining = [], []
        for cand in ranked[:_VERIFY_N]:
            verdict = _verify(cand, http_head, deadline)
            if verdict is None:
                remaining.append(cand)
            else:
                checked.append(cand)
        for cand in ranked[_VERIFY_N:]:
            remaining.append(cand)
        ranked = checked + remaining

    title = _title(refetch, final_url)
    probe_name = ranked[0].probe if ranked else "none"
    if note:
        note(f"[universal] resolved via {probe_name}: "
             f"{len(ranked)} candidate(s) ({ctx.trace and ' | '.join(ctx.trace) or 'no trace'})")
    return _info_from_candidates(final_url, title, ranked, probe_name)
