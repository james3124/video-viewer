"""Declarative extra-site adapter — one extractor, a host→recipe table.

Why this exists
---------------
yt-dlp ships ~1,750 extractors; this server adds a handful of bespoke adapters
for hosts whose player needs a decoder. The long tail is different: the host
is not in either list, but its video page hides the stream behind one of a
small set of shapes — `og:video`, a JSON-LD `VideoObject`, an HTML5
`<video>`/`<source>`, a JW Player/Video.js config, a plain media URL, or a
player frame. Writing a bespoke Python function per host means 100 decoders
that rot independently. This module keeps the *host list* here and the
*decoding* in one place:

    SiteRule(host, strategy, referer, allow_domains, patterns)

`strategy` names the page shape the host uses; `patterns` is the escape hatch
for a host whose page embeds the URL in a shape the generic strategies miss.
Adding a host is a data change, not a new function.

Scope, security and the ladder
------------------------------
Public streams only. The module never logs in, never touches DRM, and never
relaxes server.py's media redirect guard: a recipe may admit only the CDN
hops it names in `allow_domains`. `extract()` is pure — it takes the page URL
and HTML and returns candidate URLs; it performs no network I/O, so it is
testable offline and reusable by the resolver's fetch layer.

The server tries this adapter after the bespoke API/player adapters and before
the yt-dlp crawl (`_extract_sniffed`), so a recipe that goes stale falls back
to the universal pattern layer instead of failing the request.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field

VERSION_SNAPSHOT = 1


# --------------------------------------------------------------------------
# recipe table
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SiteRule:
    """How one host's video page names its stream.

    strategy: auto | direct | meta | jsonld | html5 | player | iframe | regex | embed
    referer:  sent with the media request; empty means "the page URL"
    allow_domains: CDN hosts the signed media may redirect to (never broadened
                   beyond this tuple; the default guard stays deny-by-default)
    patterns: regexes with one capture group, applied when strategy == "regex"
              or "embed" (the first match becomes the embed frame)
    api:      URL template for a page-token API; `{id}` comes from api_id
    api_id:   regex with one group that captures the API's id/episode token
    api_token: regex with one group that captures a request header value
    api_header: header name for api_token (default CSRF-Token)
    note:     one line for the supported-sites table
    """

    host: str
    strategy: str = "auto"
    referer: str = ""
    allow_domains: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    api: str = ""
    api_id: str = ""
    api_token: str = ""
    api_header: str = "CSRF-Token"
    note: str = ""


def _r(host: str, strategy: str = "auto", **kw) -> SiteRule:
    return SiteRule(host=host, strategy=strategy, **kw)


# Populated from a live probe of 247 adult/anime hosts (2026-11-07). Only
# hosts whose pages returned a video surface are listed; a parked/default page
# or a bot wall is not an adapter. Keep the list alphabetical by host.
SITES: dict[str, SiteRule] = {}


def _add(rule: SiteRule) -> None:
    SITES[rule.host] = rule


def rule_for(url: str) -> SiteRule | None:
    """The recipe for a URL's host, longest-suffix match (www. is ignored)."""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    best = None
    for candidate in SITES:
        if host == candidate or host.endswith("." + candidate):
            if best is None or len(candidate) > len(best):
                best = candidate
    return SITES[best] if best else None


# --------------------------------------------------------------------------
# candidate extraction (pure: URL + HTML -> URLs)
# --------------------------------------------------------------------------

_MEDIA_EXT_RE = re.compile(
    r"\.(?:mp4|m3u8|webm|m4v|mov|ogv|ogg|mp3|m4a|aac)(?:[?#]|$)", re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_OG_TITLE_RE = re.compile(
    r"<meta[^>]+(?:property|name)=[\"'](?:og:title|twitter:title)[\"']"
    r"[^>]+content=[\"']([^\"']+)[\"']", re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S | re.I)

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_ATTR_RE = re.compile(r"([\w:-]+)\s*=\s*[\"']([^\"']*)[\"']", re.I)
_OG_VIDEO = {
    "og:video", "og:video:secure_url", "og:video:url",
    "twitter:player:stream",
}

_VIDEO_TAG_RE = re.compile(r"<(?:video|source)\b[^>]*>", re.I)
_MEDIA_ATTR_RE = re.compile(
    r"\b(src|data-src|data-media-src|data-video-src|data-hls|data-file|"
    r"data-mp4|data-video)\s*=\s*[\"']([^\"']+)[\"']", re.I)

_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.S | re.I)
_LD_TYPE_RE = re.compile(r"[\"']@type[\"']\s*:\s*[\"'](?:[^\"']*VideoObject[^\"']*)[\"']", re.I)
_LD_URL_RE = re.compile(
    r"[\"'](?:contentUrl|embedUrl|url|thumbnailUrl)[\"']\s*:\s*[\"']([^\"']+)[\"']", re.I)

_SOURCES_RE = re.compile(r"(?:sources?|file)\s*[:=]\s*(\[[^\]]{0,6000}\])", re.S | re.I)
_FILE_RE = re.compile(
    r"\bfile\s*[:=]\s*[\"']([^\"']+)[\"']|\bsrc\s*[:=]\s*[\"']([^\"']+)[\"']",
    re.I)
_JSON_URL_RE = re.compile(
    r"[\"'](?:file|src|url|contentUrl|playlist|hls|mp4|video_url)[\"']\s*:\s*"
    r"[\"']([^\"']+)[\"']", re.I)
_DATA_CONFIG_RE = re.compile(
    r"\bdata-(?:config|setup|player)\s*=\s*[\"']([^\"']+)[\"']", re.I)

_IFRAME_RE = re.compile(
    r"<(?:iframe|embed)\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_OBJECT_RE = re.compile(
    r"<object\b[^>]*\bdata\s*=\s*[\"']([^\"']+)[\"']", re.I)
# Dean Edwards packer: eval(function(p,a,c,k,e,d){...}('...',62,63,'...'.split('|'),0,{}))
_PACKED_RE = re.compile(
    r"\('(.*?)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)", re.S)

# Trackers and ad slots are never media. Kept narrow: a legitimate CDN can use
# any of these words in a path, so match host labels, not substrings.
_AD_HOST_RE = re.compile(
    r"(?:^|\.)(?:doubleclick|googlesyndication|googleadservices|juicyads|"
    r"exoclick|trafficjunky|popads|propellerads|adsterra|adtng|gsrv|"
    r"whitetrafsa|kiki18|a-ads)\.", re.I)
_BAD_URL_RE = re.compile(
    r"^(?:data:|blob:|javascript:|mailto:|about:|#)", re.I)
# Thumbnails/trailers share the media extension but are not the feature.
_BAD_MEDIA_RE = re.compile(
    r"(?:preview|vthumb|thumb|screenshot|poster|trailer|teaser|sample|sprite|"
    r"banner|promo|/ad/|/ads/|/spot)", re.I)
# Last generic pass: a media URL mentioned anywhere in JS or markup.
_MEDIA_SCAN_RE = re.compile(
    r"""((?:https?:)?//[^"'()\s<>\\]{4,500}?\.(?:mp4|m3u8|webm|m4v|mov)"""
    r"""(?:\?[^"'()\s<>\\]*)?)""", re.I)
# Frames that are widgets, trackers, social buttons or ad zones, not players.
_BAD_FRAME_RE = re.compile(
    r"(?:youtube\.com/embed|youtu\.be|facebook\.com/plugins|"
    r"platform\.twitter\.com|instagram\.com|tiktok\.com|googletagmanager\.com|"
    r"google-analytics\.com|doubleclick|googlesyndication|adtng\.com|"
    r"exoclick|juicyads|trafficjunky|popads|propellerads|adsterra|"
    r"go\.gsrv\.dev|creative\.|whitetrafsa\.com|kiki18\.com/api/spots|"
    r"/zone/|/banner|/ads?/|/widget|/spots|/track|/pixel|like\.php|"
    r"/plugins/like|/orca/)", re.I)
_PLAYER_HINTS = ("player", "embed", "stream", "video", "watch", "play",
                 "hls", "m3u8", "mp4", "jwplayer", "videojs")
_PLAYER_HOSTS = ("dood", "voe", "mixdrop", "streamtape", "filemoon", "ok.ru",
                 "vk.com", "sibnet", "sendvid", "mp4upload", "yourupload",
                 "hydrax", "videocdn", "aniboom", "mega.nz", "drive.google.com",
                 "blogger.com", "googlevideo.com", "jwplatform", "jwplayer")


def _clean_url(raw: str, base: str) -> str | None:
    if not raw:
        return None
    raw = raw.strip().replace("&amp;", "&").replace("\\/", "/")
    if _BAD_URL_RE.match(raw):
        return None
    # A packed/JS token such as "1://host/path" is not a relative path; it is
    # an unexpanded placeholder. Reject it before urljoin can turn it into a
    # plausible-looking page URL.
    if ":" in raw.split("/", 1)[0] and not raw.lower().startswith(
            ("http://", "https://")):
        return None
    url = urllib.parse.urljoin(base, raw)
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return None
    if _AD_HOST_RE.search(host + "."):
        return None
    return url


def _is_media(url: str) -> bool:
    return bool(_MEDIA_EXT_RE.search(url))


def _ext_for(url: str) -> str:
    m = _MEDIA_EXT_RE.search(url)
    return m.group(0).lstrip(".").split("?")[0].split("#")[0].lower() if m else "mp4"


def _height_for(url: str, label: str = "") -> int | None:
    for text in (label or "", url):
        m = re.search(r"(\d{3,4})\s*[pP]", text or "")
        if m:
            return int(m.group(1))
        m = re.search(r"[_-](\d{3,4})(?:[_.-]|$)", text or "")
        if m:
            n = int(m.group(1))
            if 144 <= n <= 4320:
                return n
    return None


def _candidate(url: str, base: str, label: str = "", kind: str = "") -> dict | None:
    clean = _clean_url(url, base)
    if not clean:
        return None
    if kind in ("mp4", "hls", "webm", "mov", "m4v") or _is_media(clean):
        ext = _ext_for(clean)
        return {"url": clean, "ext": ext,
                "height": _height_for(clean, label),
                "label": label or ""}
    return None


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        if not item or item["url"] in seen:
            continue
        seen.add(item["url"])
        out.append(item)
    return out


def _parse_loose_json(blob: str):
    """json.loads with the two habits of hand-written player configs."""
    blob = blob.strip()
    try:
        return json.loads(blob)
    except Exception:
        pass
    fixed = re.sub(r",\s*([}\]])", r"\1", blob)
    fixed = fixed.replace("'", '"')
    try:
        return json.loads(fixed)
    except Exception:
        return None


def _walk_json(obj, base: str, out: list[dict], depth: int = 0) -> None:
    """Collect media-looking URLs from an arbitrary player payload."""
    if depth > 8:
        return
    if isinstance(obj, str):
        if _is_media(obj):
            item = _candidate(obj, base)
            if item:
                out.append(item)
    elif isinstance(obj, dict):
        label = str(obj.get("label") or obj.get("quality") or obj.get("res") or "")
        for key in ("file", "src", "url", "contentUrl", "hls", "mp4",
                    "video_url", "playlist"):
            value = obj.get(key)
            if isinstance(value, str):
                item = _candidate(value, base, label)
                if item:
                    out.append(item)
        for value in obj.values():
            _walk_json(value, base, out, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            _walk_json(value, base, out, depth + 1)


def _meta_candidates(page: str, base: str) -> list[dict]:
    out: list[dict] = []
    for tag in _META_TAG_RE.findall(page):
        attrs = {k.lower(): v for k, v in _META_ATTR_RE.findall(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "")
        if name in _OG_VIDEO and content:
            item = _candidate(content, base)
            if item:
                out.append(item)
    return out


def _html5_candidates(page: str, base: str) -> list[dict]:
    out: list[dict] = []
    for tag in _VIDEO_TAG_RE.findall(page):
        for attr, value in _MEDIA_ATTR_RE.findall(tag):
            item = _candidate(value, base)
            if item:
                out.append(item)
        # A <video> may name its only source in the poster-adjacent text.
    return out


def _jsonld_candidates(page: str, base: str) -> list[dict]:
    out: list[dict] = []
    for blob in _LD_RE.findall(page):
        if not _LD_TYPE_RE.search(blob):
            continue
        for raw in _LD_URL_RE.findall(blob):
            item = _candidate(raw, base)
            if item:
                out.append(item)
        parsed = _parse_loose_json(blob)
        if parsed is not None:
            _walk_json(parsed, base, out)
    return out


def _player_candidates(page: str, base: str) -> list[dict]:
    out: list[dict] = []
    for blob in _SOURCES_RE.findall(page):
        parsed = _parse_loose_json(blob)
        if parsed is not None:
            _walk_json(parsed, base, out)
        else:
            for raw in re.findall(r"[\"']([^\"']+)[\"']", blob):
                item = _candidate(raw, base)
                if item:
                    out.append(item)
    for a, b in _FILE_RE.findall(page):
        item = _candidate(a or b, base)
        if item:
            out.append(item)
    for raw in _JSON_URL_RE.findall(page):
        item = _candidate(raw, base)
        if item:
            out.append(item)
    for blob in _DATA_CONFIG_RE.findall(page):
        parsed = _parse_loose_json(blob)
        if parsed is not None:
            _walk_json(parsed, base, out)
    return out


def _scan_candidates(page: str, base: str) -> list[dict]:
    """Any media URL the page mentions in JS or markup, minus thumbnails."""
    out: list[dict] = []
    text = page.replace("\\/", "/")
    for raw in _MEDIA_SCAN_RE.findall(text):
        clean = _clean_url(raw.rstrip(",;"), base)
        if not clean:
            continue
        path = urllib.parse.urlparse(clean).path
        if _BAD_MEDIA_RE.search(path):
            continue
        item = _candidate(clean, base)
        if item:
            out.append(item)
    return out


def _frame_score(url: str, page_url: str) -> int:
    """Rank a frame as a player: same-site and player-shaped URLs win, ad and
    social widgets are rejected before scoring."""
    if _BAD_FRAME_RE.search(url):
        return -1
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    page_host = (urllib.parse.urlparse(page_url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if page_host.startswith("www."):
        page_host = page_host[4:]
    path = (parsed.path or "").lower()
    text = host + path
    score = 0
    if host and page_host and (host == page_host
                               or host.endswith("." + page_host)
                               or page_host.endswith("." + host)):
        score += 100
    if any(hint in text for hint in _PLAYER_HINTS):
        score += 40
    if any(hint in host for hint in _PLAYER_HOSTS):
        score += 60
    if path in ("", "/"):
        score -= 50
    return score


def _unpack_js(packed: str, base: int, count: int, words: list[str]) -> str:
    """Decode a Dean Edwards-packed string (no JS is executed)."""
    def base36(n: int) -> str:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = ""
        while n:
            out = digits[n % 36] + out
            n //= 36
        return out or "0"

    def token(n: int) -> str:
        head = "" if n < base else token(n // base)
        rem = n % base
        return head + (chr(rem + 29) if rem > 35 else base36(rem))

    text = packed.replace("\\'", "'").replace("\\\\", "\\")
    for i in range(count - 1, -1, -1):
        if i < len(words) and words[i]:
            text = re.sub(r"\b" + re.escape(token(i)) + r"\b",
                          lambda _match, word=words[i]: word, text)
    return text


def _packed_candidates(page: str, base: str,
                       page_url: str) -> tuple[list[dict], str | None]:
    """Unpack an obfuscated player config, then read it like ordinary markup."""
    match = _PACKED_RE.search(page)
    if not match:
        return [], None
    try:
        decoded = _unpack_js(match.group(1), int(match.group(2)),
                             int(match.group(3)), match.group(4).split("|"))
    except (ValueError, re.error):
        return [], None
    formats = _scan_candidates(decoded, base)
    frames, embed = _iframe_candidates(decoded, base, page_url)
    return formats + frames, embed


def _iframe_candidates(page: str, base: str,
                       page_url: str) -> tuple[list[dict], str | None]:
    """Player frames: a direct media frame is playable; anything else is an
    embed the browser can render. The best-scoring plausible frame wins, so a
    trailer/social/ ad iframe is never handed to the user as the video."""
    media: list[dict] = []
    scored: list[tuple[int, str]] = []
    for raw in _IFRAME_RE.findall(page) + _OBJECT_RE.findall(page):
        item = _candidate(raw, base)
        if item:
            media.append(item)
            continue
        clean = _clean_url(raw, base)
        if not clean:
            continue
        score = _frame_score(clean, page_url)
        if score >= 80:
            scored.append((score, clean))
    scored.sort(key=lambda pair: -pair[0])
    embed = scored[0][1] if scored else None
    return media, embed


def _regex_candidates(rule: SiteRule, page: str, base: str) -> list[dict]:
    out: list[dict] = []
    for pattern in rule.patterns:
        try:
            rx = re.compile(pattern, re.S | re.I)
        except re.error:
            continue
        for m in rx.finditer(page):
            raw = m.group(1) if m.groups() else m.group(0)
            item = _candidate(raw, base)
            if item:
                out.append(item)
    return out


def extract(rule: SiteRule, page_url: str, page: str) -> dict:
    """Apply a recipe. Pure; returns candidates and an optional embed frame."""
    strategy = (rule.strategy or "auto").lower()
    if strategy == "api":
        # The API call needs network I/O; the server wrapper makes it after
        # this pure pass finds nothing in the HTML.
        strategy = "auto"
    formats: list[dict] = []
    embed = None

    if strategy == "direct":
        item = _candidate(page_url, page_url)
        if item:
            formats.append(item)
    elif strategy == "meta":
        formats = _meta_candidates(page, page_url)
    elif strategy == "jsonld":
        formats = _jsonld_candidates(page, page_url)
    elif strategy == "html5":
        formats = _html5_candidates(page, page_url)
    elif strategy == "player":
        formats = _player_candidates(page, page_url)
    elif strategy == "regex":
        formats = _regex_candidates(rule, page, page_url)
    elif strategy == "embed":
        for pattern in rule.patterns:
            try:
                rx = re.compile(pattern, re.S | re.I)
            except re.error:
                continue
            match = rx.search(page)
            if not match:
                continue
            raw = match.group(1) if match.groups() else match.group(0)
            clean = _clean_url(raw, page_url)
            if clean:
                embed = clean
                break
    elif strategy == "iframe":
        formats, embed = _iframe_candidates(page, page_url, page_url)
    else:  # auto: every shape, most specific first
        formats += _meta_candidates(page, page_url)
        formats += _jsonld_candidates(page, page_url)
        formats += _html5_candidates(page, page_url)
        formats += _player_candidates(page, page_url)
        formats += _scan_candidates(page, page_url)
        extra, embed = _iframe_candidates(page, page_url, page_url)
        formats += extra
        if not formats and embed is None:
            extra, embed = _packed_candidates(page, page_url, page_url)
            formats += extra

    formats = _dedupe(formats)
    # HLS masters first, then highest quality, then progressive files.
    formats.sort(key=lambda f: (0 if f["ext"] == "m3u8" else 1,
                                -(f.get("height") or 0)))
    return {"formats": formats, "embed": embed, "title": _title(page)}


def api_request(rule: SiteRule, page_url: str, page: str) -> tuple[str, dict] | None:
    """Resolve a page-token API call from the HTML (pure; the caller fetches)."""
    if not rule.api:
        return None
    values: dict[str, str] = {}
    if rule.api_id:
        match = re.search(rule.api_id, page, re.S | re.I)
        if not match:
            return None
        values["id"] = match.group(1)
    try:
        url = rule.api.format_map(values)
    except (KeyError, ValueError):
        return None
    url = urllib.parse.urljoin(page_url, url)
    headers = {"Accept": "application/json",
               "X-Requested-With": "XMLHttpRequest",
               "Referer": page_url}
    if rule.api_token:
        match = re.search(rule.api_token, page, re.S | re.I)
        if match and rule.api_header:
            headers[rule.api_header] = match.group(1)
    return url, headers


def api_extract(payload: str, base: str) -> dict:
    """Collect media URLs from a JSON API response."""
    formats: list[dict] = []
    parsed = _parse_loose_json(payload)
    if parsed is not None:
        _walk_json(parsed, base, formats)
    if not formats:
        formats = _scan_candidates(payload, base)
    formats = _dedupe(formats)
    formats.sort(key=lambda f: (0 if f["ext"] == "m3u8" else 1,
                                -(f.get("height") or 0)))
    return {"formats": formats, "embed": None, "title": None}


def _title(page: str) -> str | None:
    for rx in (_OG_TITLE_RE, _TITLE_RE, _H1_RE):
        m = rx.search(page)
        if not m:
            continue
        text = re.sub(r"<[^>]+>", " ", m.group(1))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text[:160]
    return None


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
# Filled by _register_defaults() at import time. Kept as a function so the
# table reads as data (and so tests can monkeypatch SITES without fighting a
# module-level comprehension).
def _register_defaults() -> None:
    for rule in (
        # --- populated from the live probe (see docs/extra-sites.md) ---
        _r("9anime.me", "auto", note="generic pattern probe"),
        _r("9anime.pl", "auto", note="generic pattern probe"),
        _r("9anime.to", "auto", note="generic pattern probe"),
        _r("anilibria.tv", "auto", note="generic pattern probe"),
        _r("anime4up.com", "auto", note="generic pattern probe"),
        _r("animedao.com", "auto", note="generic pattern probe"),
        _r("animedao.to", "auto", note="generic pattern probe"),
        _r("animefenix.com", "auto", note="generic pattern probe"),
        _r("animeflix.io", "auto", note="generic pattern probe"),
        _r("animeforce.com", "auto", note="generic pattern probe"),
        _r("animefreak.tv", "auto", note="generic pattern probe"),
        _r("animefrenzy.com", "auto", note="generic pattern probe"),
        _r("animeheaven.com", "auto", note="generic pattern probe"),
        _r("animeheaven.eu", "auto", note="generic pattern probe"),
        _r("animehub.cc", "auto", note="generic pattern probe"),
        _r("animehub.com", "auto", note="generic pattern probe"),
        _r("animeid.com", "auto", note="generic pattern probe"),
        _r("animekisa.lol", "auto", note="generic pattern probe"),
        _r("animekisa.vip", "auto", note="generic pattern probe"),
        _r("animepahe.com", "auto", note="generic pattern probe"),
        _r("animepahe.ru", "auto", note="generic pattern probe"),
        _r("animepahe.si", "auto", note="generic pattern probe"),
        _r("animesdrive.com", "auto", note="generic pattern probe"),
        _r("animesgames.com", "auto", note="generic pattern probe"),
        _r("animeslatino.com", "auto", note="generic pattern probe"),
        _r("animesonline.cc", "auto", note="generic pattern probe"),
        _r("animesonline.top", "auto", note="generic pattern probe"),
        _r("animesonlinecc.com", "auto", note="generic pattern probe"),
        _r("animesroll.com", "auto", note="generic pattern probe"),
        _r("animestream.com", "auto", note="generic pattern probe"),
        _r("animesuge.io", "auto", note="generic pattern probe"),
        _r("animesup.com", "auto", note="generic pattern probe"),
        _r("animeszone.net", "auto", note="generic pattern probe"),
        _r("animeultima.com", "auto", note="generic pattern probe"),
        _r("animeunity.tv", "auto", note="generic pattern probe"),
        _r("animevost.org", "auto", note="generic pattern probe"),
        _r("animeworld.ac", "api",
           api="/api/episode/info?id={id}&alt=0",
           api_id=r'window\.episodeToken\s*=\s*"([^"]+)"',
           api_token=r'csrfToken\s*=\s*"([^"]+)"',
           note="page token + episode API"),
        _r("animeworld.one", "auto", note="generic pattern probe"),
        _r("animeworld.tv", "api",
           api="/api/episode/info?id={id}&alt=0",
           api_id=r'window\.episodeToken\s*=\s*"([^"]+)"',
           api_token=r'csrfToken\s*=\s*"([^"]+)"',
           note="page token + episode API"),
        _r("animexin.com", "auto", note="generic pattern probe"),
        _r("animeyt.com", "auto", note="generic pattern probe"),
        _r("anitaku.com", "auto", note="generic pattern probe"),
        _r("anitube.com", "auto", note="generic pattern probe"),
        _r("aniwatch.me", "auto", note="generic pattern probe"),
        _r("aniworld.to", "embed",
           patterns=(r'data-link-target="(/redirect/\d+)"',),
           note="first hoster redirect"),
        _r("anix.to", "auto", note="generic pattern probe"),
        _r("anixplayer.com", "auto", note="generic pattern probe"),
        _r("gogoanime.tel", "auto", note="generic pattern probe"),
        _r("gogoanime.vc", "auto", note="generic pattern probe"),
        _r("gogoanime.wiki", "auto", note="generic pattern probe"),
        _r("gogoanimes.co", "auto", note="generic pattern probe"),
        _r("hentaicity.com", "meta", note="og:video"),
        _r("hentaigasm.com", "auto", note="generic pattern probe"),
        _r("hentaihaven.com", "auto", note="generic pattern probe"),
        _r("hentaistream.com", "auto", note="generic pattern probe"),
        _r("hentaitube.tv", "auto", note="generic pattern probe"),
        _r("hqporner.com", "auto", note="generic pattern probe"),
        _r("hustler.com", "auto", note="generic pattern probe"),
        _r("jav321.com", "auto", note="generic pattern probe"),
        _r("javbangers.com", "auto", note="generic pattern probe"),
        _r("javbus.com", "auto", note="generic pattern probe"),
        _r("javcl.com", "auto", note="generic pattern probe"),
        _r("javcool.com", "auto", note="generic pattern probe"),
        _r("javdesu.com", "auto", note="generic pattern probe"),
        _r("javdna.com", "auto", note="generic pattern probe"),
        _r("javfinder.com", "auto", note="generic pattern probe"),
        _r("javfree.me", "auto", note="generic pattern probe"),
        _r("javfull.net", "auto", note="generic pattern probe"),
        _r("javfun.me", "auto", note="generic pattern probe"),
        _r("javguru.com", "auto", note="generic pattern probe"),
        _r("javhay.com", "auto", note="generic pattern probe"),
        _r("javhd.com", "auto", note="generic pattern probe"),
        _r("javhd.pro", "auto", note="generic pattern probe"),
        _r("javhihi.com", "auto", note="generic pattern probe"),
        _r("javkiss.com", "auto", note="generic pattern probe"),
        _r("javleak.com", "auto", note="generic pattern probe"),
        _r("javmix.tv", "auto", note="generic pattern probe"),
        _r("javporn.com", "auto", note="generic pattern probe"),
        _r("javpro.net", "auto", note="generic pattern probe"),
        _r("javqq.com", "auto", note="generic pattern probe"),
        _r("javrave.com", "auto", note="generic pattern probe"),
        _r("javsex.net", "auto", note="generic pattern probe"),
        _r("javsin.com", "auto", note="generic pattern probe"),
        _r("javstream.com", "auto", note="generic pattern probe"),
        _r("javsub.co", "auto", note="generic pattern probe"),
        _r("javsubtitle.com", "auto", note="generic pattern probe"),
        _r("javtiful.com", "player", note="player source list"),
        _r("javtube.com", "auto", note="generic pattern probe"),
        _r("javzoo.com", "auto", note="generic pattern probe"),
        _r("missav.com", "auto", note="generic pattern probe"),
        _r("monoshare.com", "auto", note="generic pattern probe"),
        _r("porn300.com", "auto", note="generic pattern probe"),
        _r("porndig.com", "auto", note="generic pattern probe"),
        _r("pornhat.com", "auto", note="generic pattern probe"),
        _r("pornone.com", "auto", note="generic pattern probe"),
        _r("porntrex.com", "auto", note="generic pattern probe"),
        _r("pornzog.com", "auto", note="generic pattern probe"),
        _r("sexvid.xxx", "auto", note="generic pattern probe"),
        _r("supjav.com", "auto", note="generic pattern probe"),
        _r("xozilla.com", "auto", note="generic pattern probe"),
        _r("zorox.to", "auto", note="generic pattern probe"),
    ):
        _add(rule)


_register_defaults()
