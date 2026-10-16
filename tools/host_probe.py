"""End-to-end check of the real chain against yt-dlp's own extractor test URLs.

    venv/bin/python tools/host_probe.py            # structure only, no content

Prints host, extractor, format count, whether the proxy is required and what the
CDN answered. Never prints titles, slugs or any media content.
"""

import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


def probe(url: str, play: bool = True) -> None:
    host = urllib.parse.urlparse(url).hostname
    try:
        info = server.get_info(url, playlist=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{host:22s} extraction FAIL  {server.friendly(exc)[:120]}")
        return

    fmts = server.browsable_formats(info)
    proxied = [f for f in fmts if f["needsProxy"]]
    hdrs = sorted({k for f in fmts for k in f["headers"] if k.lower() != "user-agent"})
    chosen = server.best_format(fmts) or (fmts[0] if fmts else None)
    print(f"{host:22s} extractor={str(info.get('extractor_key')):10s} "
          f"formats={len(fmts):3d} proxy={len(proxied):3d} "
          f"headers={','.join(hdrs) or '-'}")
    if not chosen or not play:
        return

    # Now prove the bytes actually flow, exactly like <video src=proxyUrl> does.
    try:
        target = server.resolve_target(url, chosen["id"])
    except Exception as exc:  # noqa: BLE001
        print(f"{'':22s} resolve FAIL {server.friendly(exc)[:90]}")
        return
    req = urllib.request.Request(target["url"], headers={
        "User-Agent": (target["headers"].get("User-Agent") or server.DEFAULT_UA),
        "Range": "bytes=0-2047",
        **{k: v for k, v in target["headers"].items() if k.lower() != "user-agent"},
    })
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read(64)
            ctype = r.headers.get("Content-Type", "?")
            ok = not body.lstrip()[:9].lower().startswith((b"<!doctype", b"<html"))
            print(f"{'':22s} bytes={r.status} {ctype[:26]:26s} "
                  f"looks-like-media={ok}")
    except urllib.error.HTTPError as exc:
        print(f"{'':22s} bytes=HTTP {exc.code} (CDN refused)")
    except Exception as exc:  # noqa: BLE001
        print(f"{'':22s} bytes FAIL {str(exc)[:80]}")


def main() -> int:
    urls = [u.strip() for u in open(sys.argv[1]) if u.strip()]
    print(f"yt-dlp {server.yt_dlp_version()} · {server.ytdlp_mode()} · "
          f"impersonate={server.IMPERSONATE or 'off'} · "
          f"cookies={'yes' if server.COOKIES else 'no'}\n")
    for url in urls:
        probe(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())