"""Live health-check for the site adapters ONLY — no yt-dlp, no sniff, no universal.

    .venv/bin/python tools/adapter_check.py                  # all hosts
    .venv/bin/python tools/adapter_check.py --only hanime.tv  # one host
    .venv/bin/python tools/adapter_check.py --extra           # sites_extra.py hosts
    .venv/bin/python tools/adapter_check.py --list            # JSON matrix for CI
    .venv/bin/python tools/adapter_check.py --self-test       # decoder round-trips, offline

Why: `server.py` resolves a link through a ladder (adapter -> yt-dlp -> sniff).
A site that stops extracting is usually the adapter's markup/API guess going
stale, so this tool calls the adapter directly (`player_site_extract`,
which has no yt-dlp fallback) against one known-good sample URL per host and
reports whether the adapter still produces something playable.

Live sample URLs live in `tools/adapter_urls.txt` (one per host, editable in
one place). A host with no sample/reports a stale one is SKIP, never FAIL —
a missing sample must not mask a broken adapter, but a PASS needs a real URL.

Failure is classified so datacenter-IP bot blocks are not blamed on the code
(that is the README's "Known limits", not an adapter bug):

  PASS / PASS_EMBED  adapter returned formats (or an embed frame, by design)
  SKIP               sample missing/stale, or the page is not a single video
  BLOCKED            host unreachable, bot-checked, geo/login/cooked-gated
  BROKEN             adapter ran and read the page, but its guess went stale
                     ("markup changed", "player carried no source list", ...)
  UNKNOWN            anything else (retried first; transient blips should pass)

Exit codes: 0 = no BROKEN/UNKNOWN, 1 = at least one BROKEN/UNKNOWN, 2 = usage.
`--strict` makes BLOCKED and SKIP fail too (use for manual runs, not CI).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_URLS = os.path.join(HERE, "adapter_urls.txt")

MEDIA_EXTS = ("mp4", "webm", "m4v", "mov", "m3u8", "mpd", "ogv", "ogg", "mkv",
              "avi", "mp3", "m4a", "aac", "wav", "flac")

# Adapter language that means "the site moved and the adapter's guess is stale".
# Anchored to server.py's exact error strings so a real rot is never filed
# under BLOCKED (which would silence it in CI).
BROKEN_HINTS = (
    "markup changed", "needs updating", "player changed", "carried no",
    "no source list", "did not parse", "decoder needs updating",
    "decoder did not", "named no stream", "named no playable",
    "unsupported stream type", "mint a play token", "did not mint",
    "carried no muxed rendition", "bootstrap data changed",
    "decoded to nothing", "no playable source", "offered no playable",
    "no player data", "player frame carried no", "source list did not",
    "handshake", "signer", "sources: sources", "no rendition",
    "carries no source", "changed its player", "changed, so the adapter",
    "no alternate", "only ad playlists",
)

# Things that are the network/IP/cookies, not the adapter.
BLOCKED_HINTS = (
    "did not serve", "refused the request", "try again", "bot",
    "cloudflare", "datadome", "captcha", "forbidden", "access denied",
    "403", "401", "geo", "in your country", "region", "login", "sign in",
    "premium", "member", "cookies", "age", "verify", "consent",
    "timed out", "timeout", "unreachable", "name or service not known",
    "temporary failure", "errno", "connection reset", "connection refused",
    "eof occurred", "no route to host", "network is unreachable",
    "server selection", "tunnel", "handshake failure", "ssl",
    "private, removed", "unavailable", "gone", "404",
)


def adapter_registry(mode: str = "none") -> list[tuple[str, list[str]]]:
    """(adapter function name, hosts) pairs from server.py's own tables.

    mode: "none" (default) keeps the declarative extra-site hosts out so the
    CI matrix keeps its current shape; "merge" adds them; "only" returns just
    the extra-site table.
    """
    by_fn: dict[str, list[str]] = {}
    if mode != "only":
        for host, fn in server._PLAYER_SITES.items():
            by_fn.setdefault(getattr(fn, "__name__", str(fn)), []).append(host)
        for host, entry in server._API_SITES.items():
            by_fn.setdefault("api_site_extract", []).append(host)
    if mode in ("merge", "only"):
        for host in getattr(server, "_EXTRA_SITES", {}):
            by_fn.setdefault("extra_site_extract", []).append(host)
    return sorted((name, sorted(hosts)) for name, hosts in by_fn.items())


def load_urls(path: str, extra: list[str]) -> dict[str, str]:
    """host -> sample URL. File lines are `host<TAB or space>url`; `#` comments."""
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
        host, _, url = item.partition("=")
        if host and url:
            urls[host.strip().lower()] = url.strip()
    return urls


def classify(exc: Exception) -> str:
    if isinstance(exc, server.NotAVideoPage):
        return "SKIP"
    text = server.clean_reason(str(exc)).lower()
    if any(h in text for h in BROKEN_HINTS):
        return "BROKEN"
    if any(h in text for h in BLOCKED_HINTS):
        return "BLOCKED"
    return "UNKNOWN"


def validate(info: dict | None) -> tuple[str, str, int]:
    """(status, detail, format count) for a returned info dict."""
    if info is None:
        return "SKIP", "adapter returned None (URL did not match its shape)", 0
    embed = info.get("embed")
    fmts = server.browsable_formats(info)
    if fmts:
        bad = [f for f in fmts
               if not str(f.get("url", "")).startswith("http")]
        if len(bad) == len(fmts):
            return "BROKEN", "every format lacks an http(s) URL", len(fmts)
        kinds = sorted({f.get("kind") or "?" for f in fmts})
        detail = f"{len(fmts)} format(s) ({','.join(kinds)})"
        return "PASS", detail, len(fmts)
    if embed and str(embed).startswith("http"):
        return "PASS_EMBED", f"embed frame: {str(embed)[:64]}", 0
    return "BROKEN", "adapter returned no formats and no embed", 0


def probe_bytes(info: dict, timeout: float = 20) -> str:
    """Best-effort: fetch the first bytes of the best format the way the
    browser would (headers included) and report what the CDN answered.
    Informational only — a refusal here is usually the datacenter IP, so it
    never changes the verdict on its own."""
    import urllib.error
    import urllib.parse
    import urllib.request

    fmts = server.browsable_formats(info)
    chosen = server.best_format(fmts) or (fmts[0] if fmts else None)
    if chosen is None:
        return "no format to probe"
    headers = dict(chosen.get("headers") or {})
    headers.setdefault("User-Agent", server.DEFAULT_UA)
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


def check_host(host: str, url: str, retries: int) -> dict:
    """Run only the adapter for one host. No yt-dlp anywhere in this path."""
    result = {"adapter": "", "host": host, "url": url, "status": "SKIP",
              "detail": "", "formats": 0, "seconds": 0.0}
    fn = None
    for name, hosts in adapter_registry("merge"):
        if host in hosts:
            result["adapter"] = name
            fn = name
            break
    if fn is None:
        result["detail"] = "host not registered in _PLAYER_SITES/_API_SITES/_EXTRA_SITES"
        return result
    if not url:
        result["detail"] = "no sample URL (add it to tools/adapter_urls.txt)"
        return result

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            info = server.player_site_extract(url)
            if info is None:
                info = server.api_site_extract(url)
            if info is None:
                extra = getattr(server, "extra_site_extract", None)
                if extra is not None:
                    info = extra(url)
            status, detail, nf = validate(info)
            result.update(status=status, detail=detail, formats=nf,
                          seconds=round(time.time() - t0, 1))
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            result["seconds"] = round(time.time() - t0, 1)
    assert last_exc is not None
    result.update(status=classify(last_exc),
                  detail=server.clean_reason(str(last_exc))[:220])
    return result


def cmd_list(args: argparse.Namespace) -> int:
    urls = load_urls(args.urls, [])
    matrix = []
    for adapter, hosts in adapter_registry("only" if args.extra else "none"):
        for host in hosts:
            matrix.append({"adapter": adapter, "host": host,
                           "sample": bool(urls.get(host))})
    print(json.dumps({"include": matrix}, indent=2))
    return 0


def cmd_self_test() -> int:
    """Decoder round-trips — the parts that rot silently. No network."""
    cases: list[tuple[str, bool, str]] = []

    # watchhentai: base64(xor(reverse-b64)) decode == encode round-trip
    try:
        import base64 as _b64
        plain = "https://cdn.example.test/720.mp4"
        # Encode via the exact inverse of wh_decode_media_url's operations:
        # reverse( b64(utf8) ) -> xor -> b64.
        rev_b = _b64.b64encode(plain.encode())[::-1]
        x = bytes(c ^ ((13 + i % 17) & 0xFF) for i, c in enumerate(rev_b))
        blob = _b64.b64encode(x).decode().replace("+", "-").replace("/", "_")
        cases.append(("watchhentai wh_decode_media_url",
                      server.wh_decode_media_url(blob) == plain, ""))
    except Exception as exc:  # noqa: BLE001
        cases.append(("watchhentai wh_decode_media_url", False, str(exc)[:80]))

    # zoko: xor(base64) round-trip behind hianimes
    try:
        import base64 as _b64
        import json as _json
        payload = {"sources": [{"src": "https://x.test/master.m3u8"}]}
        raw = _json.dumps(payload).encode()
        key = server._ZOKO_XOR_KEY
        blob = _b64.b64encode(bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))).decode()
        cases.append(("hianimes _zoko_deobfuscate",
                      server._zoko_deobfuscate(blob) == payload, ""))
    except Exception as exc:  # noqa: BLE001
        cases.append(("hianimes _zoko_deobfuscate", False, str(exc)[:80]))

    # javhdporn: rc4 round-trip
    try:
        data = b"hello-adapter"
        key = b"viewer-check-key"
        cases.append(("javhdporn _jvd_rc4",
                      server._jvd_rc4(key, server._jvd_rc4(key, data)) == data, ""))
    except Exception as exc:  # noqa: BLE001
        cases.append(("javhdporn _jvd_rc4", False, str(exc)[:80]))

    # hanime: seal/unseal round-trip (only if the AES helper is present)
    seal = getattr(server, "_htv_seal", None)
    unseal = getattr(server, "_htv_unseal", None)
    if seal and unseal:
        try:
            obj = {"sources": [{"src": "/hls/1/x"}]}
            cases.append(("hanime _htv_seal/_htv_unseal",
                          unseal(seal(obj)) == obj, ""))
        except Exception as exc:  # noqa: BLE001
            cases.append(("hanime _htv_seal/_htv_unseal", False, str(exc)[:80]))

    # kisskh: kkey derivation is deterministic
    try:
        kkey = getattr(server, "_kisskh_kkey", None)
        cases.append(("kisskh _kisskh_kkey",
                      kkey is not None and kkey(7, "g") == kkey(7, "g") != kkey(8, "g"),
                      "" if kkey else "helper missing"))
    except Exception as exc:  # noqa: BLE001
        cases.append(("kisskh _kisskh_kkey", False, str(exc)[:80]))

    bad = 0
    for name, ok, extra in cases:
        print(f"{'ok  ' if ok else 'FAIL'} {name}{'  — ' + extra if extra else ''}")
        bad += 0 if ok else 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", action="append", default=[],
                    help="host or adapter function name (repeatable)")
    ap.add_argument("--urls", default=DEFAULT_URLS,
                    help="sample URL file (default: tools/adapter_urls.txt)")
    ap.add_argument("--url", action="append", default=[],
                    help="override a sample: host=url (repeatable)")
    ap.add_argument("--retries", type=int, default=1,
                    help="extra attempts per host after a failure (default 1)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="pause between hosts, seconds (default 1)")
    ap.add_argument("--bytes", action="store_true",
                    help="also probe the best format's first bytes (info only)")
    ap.add_argument("--strict", action="store_true",
                    help="fail on BLOCKED/SKIP too (manual runs only)")
    ap.add_argument("--json-out", default="",
                    help="write a machine-readable report to this file")
    ap.add_argument("--list", action="store_true",
                    help="emit the adapter/host matrix as JSON and exit")
    ap.add_argument("--extra", action="store_true",
                    help="restrict to the declarative sites_extra.py hosts")
    ap.add_argument("--self-test", action="store_true",
                    help="run offline decoder round-trips and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="print only the summary table, not server logs")
    args = ap.parse_args(argv)

    if args.self_test:
        return cmd_self_test()
    if args.list:
        return cmd_list(args)

    registry = adapter_registry(
        "only" if args.extra else ("merge" if args.only else "none"))
    urls = load_urls(args.urls, args.url)
    wanted = {w.lower() for w in args.only}
    targets: list[tuple[str, str]] = []
    for adapter, hosts in registry:
        for host in hosts:
            if wanted and host.lower() not in wanted \
                    and adapter.lower() not in wanted:
                continue
            targets.append((host, urls.get(host, "")))
    if not targets:
        print("no adapters matched (check --only / the registry)", file=sys.stderr)
        return 2

    results: list[dict] = []
    for host, url in targets:
        res = check_host(host, url, args.retries)
        if args.bytes and res["status"] in ("PASS",):
            try:
                info = server.player_site_extract(url) \
                    or server.api_site_extract(url)
                if info is None:
                    extra = getattr(server, "extra_site_extract", None)
                    if extra is not None:
                        info = extra(url)
                if info:
                    res["detail"] += " · " + probe_bytes(info)
            except Exception:  # noqa: BLE001 - informational only
                pass
        results.append(res)
        if not args.quiet:
            print(f"{res['status']:10s} {host:20s} {res['seconds']:>5.1f}s  "
                  f"{res['detail']}")
        if args.delay:
            time.sleep(args.delay)

    summary = {k: sum(1 for r in results if r["status"] == k)
               for k in ("PASS", "PASS_EMBED", "BLOCKED", "SKIP", "BROKEN",
                         "UNKNOWN")}
    print("\nsummary: " + "  ".join(f"{k}={v}" for k, v in summary.items()
                                     if v))
    if args.json_out:
        report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()),
                  "results": results, "summary": summary}
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"report -> {args.json_out}")

    failed = summary["BROKEN"] + summary["UNKNOWN"]
    if args.strict:
        failed += summary["BLOCKED"] + summary["SKIP"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
