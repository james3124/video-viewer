"""Ask the backend which layer answers for a URL, and how fast.

    .venv/bin/python tools/site_check.py <url> [more urls...]

Why: the viewer resolves a link through a ladder —

  1. a site adapter (only for hosts registered in _PLAYER_SITES / _API_SITES)
  2. yt-dlp's ~1,800 extractors (YouTube, TikTok, Facebook, and most sites)
  3. a bounded page sniff for direct media links yt-dlp missed
  4. an error

This tool runs the real extraction once per URL and prints which layer
caught it, the playable formats, and the timing. Use it when a site
"doesn't work": the output is the bug report.
"""

import json
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 2)[0])

import server  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    bad = 0
    for url in argv[1:]:
        host = server._host_of(url) or "?"
        print(f"\n{url}")
        print(f"  host: {host}  adapter: {'yes' if server._known_adapter_host(url) else 'no'}")
        t0 = time.time()
        try:
            info = server.get_info(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL after {time.time() - t0:.1f}s: {server.clean_reason(str(exc))[:180]}")
            bad += 1
            continue
        dt = time.time() - t0
        fmts = server.browsable_formats(info)
        labels = [f"{f['id']} ⟶ {f['label']}" for f in fmts[:5]]
        why = server.clean_reason(str(info.get("reason") or ""))
        print(f"  resolved in {dt:.1f}s: title={info.get('title')!r} formats={len(fmts)}")
        if labels:
            print(f"  first choices: {', '.join(labels)} ..."
                  if len(fmts) > 5 else f"  choices: {', '.join(labels)}")
        if why:
            print(f"  reason: {why[:180]}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
