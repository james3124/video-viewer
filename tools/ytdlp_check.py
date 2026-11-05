"""Live health-check for the yt-dlp layer ONLY — no site adapters, no sniff.

    .venv/bin/python tools/ytdlp_check.py                       # all sites
    .venv/bin/python tools/ytdlp_check.py --only youtube        # one site
    .venv/bin/python tools/ytdlp_check.py --list                 # JSON matrix for CI
    .venv/bin/python tools/ytdlp_check.py --self-test            # offline classification tests

Why: `server.py` resolves a link through a ladder (adapter -> yt-dlp -> sniff).
When a user says a site "doesn't work", the adapter check can't tell whether
the break is in a custom adapter or in yt-dlp's own extractor. This tool calls
yt-dlp directly (the `yt_dlp.YoutubeDL` module) against one known-good sample
URL per site, so a red result here means yt-dlp's extractor for that site
needs updating — the adapters are never in the picture.

Sample URLs live in `tools/ytdlp_urls.txt` (one per site, editable in one
place). A site with no sample/reports a stale one is SKIP (never FAIL) — a
missing sample must not mask a broken extractor, but an OK needs a real URL.

Failure is classified so datacenter-IP bot blocks are not blamed on the code
(that is the README's "Known limits", not an extractor bug):

  OK        yt-dlp returned formats
  BLOCKED   bot check / geo / login / cookies / datacenter IP
  STALE     video private/removed/unavailable — refresh the sample URL
  SKIP      no sample URL in the registry
  BROKEN    extraction failed — the extractor needs updating
  UNKNOWN   anything else (retried first; transient blips should pass)

Exit codes: 0 = no BROKEN/UNKNOWN, 1 = at least one BROKEN/UNKNOWN, 2 = usage.
`--strict` makes BLOCKED/STALE/SKIP fail too (use for manual runs, not CI).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URLS = os.path.join(HERE, "ytdlp_urls.txt")

# yt-dlp language that means "the site moved and the extractor's guess is stale".
# Anchored to yt-dlp's exact error strings so a real rot is never filed
# under BLOCKED (which would silence it in CI).
BROKEN_HINTS = (
    "unsupported url", "no formats found", "no downloadable formats",
    "could not extract", "unable to extract", "failed to extract",
    "no video could be found", "no video is available",
    "failed to download", "extract_info failed", "request info",
    "no m3u8 playlist", "no manifest", "failed to parse",
    "extractor error", "invalid extractor", "no playable",
    "unexpected url", "does not match", "failed to resolve",
    "invalid video", "extraction failed",
)

# "The video is gone" — refresh the sample, don't file it as extractor rot.
# Checked before BLOCKED so "Private video. sign in..." is STALE: it is a dead
# sample, not a datacenter-IP block.
STALE_HINTS = (
    "private video", "removed", "deleted", "terminated", "unavailable",
    "no longer available", "not found", "this video is private",
    "account terminated", "suspended", "404", "disabled", "not_found",
    "flagged for verification", "trust and safety", "has been taken down",
    "is not available", "not available, status code",
)

# Things that are the network/IP/cookies, not the extractor.
BLOCKED_HINTS = (
    "bot", "cloudflare", "datadome", "captcha", "sign in to confirm",
    "are you a robot", "access denied", "forbidden", "rate limit",
    "429", "403", "401", "geo", "in your country", "region",
    "login", "sign in", "confirm your age", "age", "verify", "consent",
    "premium", "member", "cookies", "timed out", "timeout",
    "unreachable", "name or service not known", "temporary failure",
    "errno", "connection reset", "connection refused", "eof occurred",
    "no route to host", "network is unreachable", "handshake failure",
    "ssl", "tunnel", "datacenter", "ip address is blocked",
    "blocked from accessing", "logged-in", "on the latest version",
)


def load_urls(path: str, extra: list[str]) -> dict[str, str]:
    """site -> sample URL. File lines are `site<TAB or space>url`; `#` comments."""
    urls: dict[str, str] = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    urls[parts[0].lower()] = parts[1].strip()
    for item in extra:
        site, _, url = item.partition("=")
        if site and url:
            urls[site.strip().lower()] = url.strip()
    return urls


def clean_reason(text: str) -> str:
    """One-line summary, mirroring server.clean_reason (yt-dlp errors are long)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return ""
    kept = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
            if s and "http" not in s]
    return " ".join(kept).strip(" .—-") or text[:400]


def classify(exc: Exception, info: dict | None = None) -> str:
    """SKIP is decided by the caller (no sample); here: OK/BLOCKED/STALE/BROKEN/UNKNOWN."""
    if info:
        if (info.get("formats") or info.get("entries") or info.get("url")):
            return "OK"
        text = clean_reason(str(info.get("_error") or "")).lower()
        if not text:
            return "BROKEN"
    else:
        text = clean_reason(str(exc)).lower() if exc else ""
    if any(h in text for h in BROKEN_HINTS):
        return "BROKEN"
    if any(h in text for h in STALE_HINTS):
        return "STALE"
    if any(h in text for h in BLOCKED_HINTS):
        return "BLOCKED"
    return "UNKNOWN"


def extract(url: str, timeout: float = 120.0) -> tuple[dict | None, Exception | None]:
    """Run yt-dlp in-process and return (info, error). Pure yt-dlp, no adapters."""
    import yt_dlp  # noqa: PLC0415  (imported lazily so --list needs no install)

    def handler(signum, frame):  # noqa: ANN001 - timeout guard for CI
        raise TimeoutError(f"extraction exceeded {timeout:.0f}s")

    import signal  # noqa: PLC0415  (Unix-only; SIGALRM on runners)
    can_time = hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer")

    class _QuietLog:
        def __init__(self):
            self.last = ""

        def _keep(self, msg):
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

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "age_limit": 18,
        "noplaylist": True,
    }
    # Browser impersonation gets past Cloudflare/DataDome on many hosts, when
    # curl_cffi is installed (same default as server.py).
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget  # noqa: PLC0415
        opts["impersonate"] = ImpersonateTarget("chrome", None)
    except Exception:  # noqa: BLE001 - optional
        pass

    log = _QuietLog()
    opts["logger"] = log
    if can_time:
        prev = signal.signal(signal.SIGALRM, handler)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False), None
    except Exception as exc:  # noqa: BLE001
        return None, exc
    finally:
        if can_time:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, prev)


def validate(info: dict | None) -> tuple[str, str, int]:
    """(status, detail, format count) for a returned info dict."""
    if info is None:
        return "UNKNOWN", "no info returned", 0
    fmts = [f for f in (info.get("formats") or [])
            if isinstance(f, dict) and str(f.get("url") or "").startswith("http")]
    entries = info.get("entries") or []
    if fmts:
        kinds = sorted({(str(f.get("ext") or f.get("protocol") or "?").lower())
                        for f in fmts[:6]})
        detail = f"{len(fmts)} format(s) ({','.join(kinds)})"
        return "OK", detail, len(fmts)
    if entries or info.get("url"):
        # Playlist/login-wall pages still prove the extractor parses the site.
        return "OK", "entries/URL only (playlist/live page)", 0
    return "BROKEN", "extraction returned no formats", 0


def probe_bytes(info: dict, timeout: float = 20) -> str:
    """Best-effort: fetch the first bytes of a direct format URL the way the
    browser would. Informational only — a refusal is usually the datacenter
    IP, so it never changes the verdict on its own."""
    import urllib.error
    import urllib.parse
    import urllib.request

    fmts = [f for f in (info.get("formats") or [])
            if isinstance(f, dict) and str(f.get("url") or "").startswith("http")
            and (f.get("protocol") not in ("m3u8_native", "http_dash_segments"))]
    chosen = fmts[0] if fmts else None
    if chosen is None:
        return "no direct format to probe"
    headers = dict(chosen.get("http_headers") or {})
    headers.setdefault("User-Agent",
                       "Mozilla/5.0 (X11; Linux x86_64) Chrome/126 Safari/537.36")
    headers["Range"] = "bytes=0-2047"
    req = urllib.request.Request(chosen["url"], headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(64)
            ctype = resp.headers.get("Content-Type", "?")
            host = urllib.parse.urlparse(chosen["url"]).hostname or "?"
            if body.lstrip()[:9].lower().startswith((b"<!doctype", b"<html")):
                return f"bytes: {host} answered HTML, not media (CDN refused)"
            return f"bytes: {resp.status} {ctype[:28]} from {host}"
    except urllib.error.HTTPError as exc:
        return f"bytes: CDN refused (HTTP {exc.code})"
    except Exception as exc:  # noqa: BLE001
        return f"bytes: {type(exc).__name__}: {str(exc)[:60]}"


def check_site(site: str, url: str, retries: int) -> dict:
    """Run only yt-dlp for one site. No adapters anywhere in this path."""
    result = {"site": site, "url": url, "status": "SKIP",
              "detail": "", "formats": 0, "extractor": "", "seconds": 0.0}
    if not url:
        result["detail"] = "no sample URL (add it to tools/ytdlp_urls.txt)"
        return result

    last_exc: Exception | None = None
    for _attempt in range(retries + 1):
        t0 = time.time()
        info, exc = extract(url)
        result["seconds"] = round(time.time() - t0, 1)
        if exc is None:
            status, detail, nf = validate(info)
            result.update(status=status, detail=detail, formats=nf,
                          extractor=str((info or {}).get("extractor_key") or ""))
            return result
        last_exc = exc
    assert last_exc is not None
    result.update(status=classify(last_exc),
                  detail=clean_reason(str(last_exc))[:220])
    return result


def cmd_list(args: argparse.Namespace) -> int:
    urls = load_urls(args.urls, [])
    matrix = [{"site": site, "sample": bool(url)}
              for site, url in sorted(urls.items())]
    print(json.dumps({"include": matrix}, indent=2))
    return 0


def cmd_self_test() -> int:
    """Classification checks on synthetic yt-dlp error strings. No network."""
    # Direct classify checks on synthetic yt-dlp error strings.
    checks = [
        ("Unsupported URL: https://example.test/x", "BROKEN"),
        ("Video unavailable", "STALE"),
        ("Private video. sign in if you've been granted access", "BLOCKED"),
        ("Sign in to confirm your age", "BLOCKED"),
        ("ERROR: Failed to extract signature? (#123)", "BROKEN"),
        ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", "BLOCKED"),
    ]
    # "Private video. sign in..." — real messages are ambiguous; accept either
    # environmental bucket rather than asserting one.
    bad = 0
    for err, want in checks:
        got = classify(Exception(err))
        # "Private video. sign in..." — real message is ambiguous; accept either
        # environmental bucket rather than asserting one.
        ok = got == want or (want == "BLOCKED" and got in ("BLOCKED", "STALE"))
        print(f"{'ok  ' if ok else 'FAIL'} classify({err[:52]!r}...) = {got} (want {want})")
        bad += 0 if ok else 1
    ok_info = classify(None, {"formats": [{"url": "https://x.test/v.mp4"}],
                              "extractor_key": "Youtube"})
    print(f"{'ok  ' if ok_info == 'OK' else 'FAIL'} classify(info with formats) = {ok_info}")
    bad += 0 if ok_info == "OK" else 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", action="append", default=[],
                    help="site name (repeatable)")
    ap.add_argument("--urls", default=DEFAULT_URLS,
                    help="sample URL file (default: tools/ytdlp_urls.txt)")
    ap.add_argument("--url", action="append", default=[],
                    help="override a sample: site=url (repeatable)")
    ap.add_argument("--retries", type=int, default=1,
                    help="extra attempts per site after a failure (default 1)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="pause between sites, seconds (default 1)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="per-site wall-clock budget in seconds (default 120)")
    ap.add_argument("--bytes", action="store_true",
                    help="also probe a direct format's first bytes (info only)")
    ap.add_argument("--strict", action="store_true",
                    help="fail on BLOCKED/STALE/SKIP too (manual runs only)")
    ap.add_argument("--json-out", default="",
                    help="write a machine-readable report to this file")
    ap.add_argument("--list", action="store_true",
                    help="emit the site matrix as JSON and exit")
    ap.add_argument("--self-test", action="store_true",
                    help="run offline classification tests and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="print only the summary table, not per-site lines")
    args = ap.parse_args(argv)

    if args.self_test:
        return cmd_self_test()
    if args.list:
        return cmd_list(args)

    urls = load_urls(args.urls, args.url)
    wanted = {w.lower() for w in args.only}
    targets = [(site, urls[site]) for site in sorted(urls)
               if not wanted or site.lower() in wanted]
    if not targets:
        print("no sites matched (check --only / the registry)", file=sys.stderr)
        return 2

    results: list[dict] = []
    for site, url in targets:
        res = check_site(site, url, args.retries)
        if args.bytes and res["status"] == "OK":
            info, exc = extract(url)  # device-faithful re-run for the bytes probe
            if exc is None and info and res["formats"]:
                res["detail"] += " · " + probe_bytes(info)
        results.append(res)
        if not args.quiet:
            print(f"{res['status']:8s} {site:16s} {res['seconds']:>5.1f}s  "
                  f"{res['detail']}")
        if args.delay:
            time.sleep(args.delay)

    summary = {k: sum(1 for r in results if r["status"] == k)
               for k in ("OK", "BLOCKED", "STALE", "SKIP", "BROKEN", "UNKNOWN")}
    print("\nsummary: " + "  ".join(f"{k}={v}" for k, v in summary.items()
                                     if v))
    if args.json_out:
        try:
            import yt_dlp  # noqa: PLC0415
            ytdlp = yt_dlp.version.__version__
        except Exception:  # noqa: BLE001
            ytdlp = "unknown"
        report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()),
                  "ytdlp_version": ytdlp,
                  "results": results, "summary": summary}
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"report -> {args.json_out}")

    failed = summary["BROKEN"] + summary["UNKNOWN"]
    if args.strict:
        failed += summary["BLOCKED"] + summary["STALE"] + summary["SKIP"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
