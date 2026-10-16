"""Check whether a host is reachable, extractable and playable — per site.

    python3 tools/site_check.py                     # built-in list
    python3 tools/site_check.py https://example.com # your own URL

Prints structure only (extractor, format count, headers needed, error text) —
never titles or any media content. Run it on the *same machine* that runs
server.py: whether a host works depends on its IP, not on your code.

Exit code 0 = every host resolved, 1 = at least one did not.
"""

import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Homepage-level probes: enough to see the edge (Cloudflare/DataDome/age gate)
# without depending on any particular video URL.
HOSTS = [
    "https://www.xvideos.com/",
    "https://www.pornhub.com/",
    "https://www.youporn.com/",
    "https://www.redtube.com/",
    "https://www.spankbang.com/",
    "https://onlyfans.com/",
    # non-adult controls, useful to tell "site is down" from "IP is blocked"
    "https://www.youtube.com/",
    "https://www.tiktok.com/",
]

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
CHALLENGE = ("just a moment", "checking your browser", "cf-mitigated",
             "attention required", "captcha", "access denied", "are you a robot",
             "ddos-guard", "datadome", "pardon the interruption")


def edge(url: str, headers: dict) -> tuple[int | None, str, dict]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read(4096).decode("utf-8", "replace").lower()
            return r.status, body, dict(r.headers)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", "replace").lower()
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, body, dict(exc.headers or {})
    except Exception as exc:  # noqa: BLE001
        return None, str(exc).lower(), {}


def edge_check(url: str) -> str:
    status, body, headers = edge(url, {"User-Agent": UA})
    server = headers.get("Server", "-")
    via = "cloudflare" if headers.get("CF-RAY") or "cloudflare" in server.lower() else server[:22]
    verdict = "open" if status == 200 else f"HTTP {status}"
    hit = next((c for c in CHALLENGE if c in body), None)
    if hit:
        verdict += f" · challenge({hit})"
    if headers.get("CF-Mitigated"):
        verdict += f" · CF-Mitigated={headers['CF-Mitigated']}"
    return f"{verdict} via {via}"


def extraction(url: str) -> str:
    """Run the real backend extraction path and report the shape of the result."""
    import server

    if server.ytdlp_mode() == "none":
        return "yt-dlp not installed"
    started = time.time()
    try:
        info = server.get_info(url, playlist=True)
    except Exception as exc:  # noqa: BLE001
        return f"FAIL  {server.friendly(exc)[:150]}"
    fmts = server.browsable_formats(info)
    proxied = sum(1 for f in fmts if f["needsProxy"])
    headers = sorted({k for f in fmts for k in f["headers"]})
    bits = [f"ok   {len(fmts)} formats", f"{proxied} need the proxy"]
    if headers:
        bits.append("headers: " + ",".join(headers))
    bits.append(f"{time.time() - started:.1f}s")
    return " · ".join(bits)


def main(argv: list[str]) -> int:
    urls = argv[1:] or HOSTS
    import server

    print(f"yt-dlp {server.yt_dlp_version()} via {server.ytdlp_mode()} · "
          f"impersonate={server.IMPERSONATE or 'off'} · cookies="
          f"{'yes' if server.COOKIES else 'no'} · block_private="
          f"{int(server.BLOCK_PRIVATE)}")
    print()

    bad = 0
    for url in urls:
        print(f"{url}")
        print(f"  edge       {edge_check(url)}")
        result = extraction(url)
        print(f"  extraction {result}")
        print()
        if result.startswith("FAIL"):
            bad += 1
    print(f"{len(urls) - bad}/{len(urls)} extracted")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))