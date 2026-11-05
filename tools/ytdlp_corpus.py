"""Emit the FULL yt-dlp corpus as a CI matrix — one sample URL per extractor.

    .venv/bin/python tools/ytdlp_corpus.py                    # full corpus
    .venv/bin/python tools/ytdlp_corpus.py --limit 20          # first 20 (smoke)

Every extractor class in the installed yt-dlp ships `_TEST`/`_TESTS` with a
sample URL — yt-dlp's own fixtures for proving extraction. This script reads
those fixtures and prints one entry per extractor class:

    {"include": [{"site": "YoutubeIE", "url": "https://…",
                  "extractor": "youtube", "sample": true}, ...]}

`tools/ytdlp_check.py` then runs each entry the same way it runs the curated
registry (`--url site=url --only site`) — pure yt-dlp, no adapters. The
`ytdlp-weekly` workflow consumes this matrix; expect ~1,400 entries, so the
weekly job count is high but fail-fast is off and every job has a 10-minute
cap. STALE results are common (fixture videos die upstream); only BROKEN is
filed to the weekly PR.

Nothing here is a claim the site is *supposed* to extract — if yt-dlp's own
fixture URL is dead, STALE teaches us that, not that the extractor rotted.
"""
from __future__ import annotations

import argparse
import json
import sys


def corpus() -> list[dict]:
    import yt_dlp.extractor as ex  # noqa: PLC0415  (imported lazily; --list stays cheap)
    entries: list[dict] = []
    for cls in ex.gen_extractor_classes():
        name = cls.__name__
        if name == "GenericIE":
            continue  # matches everything; proves nothing about a real site
        test: dict | None = getattr(cls, "_TEST", None)
        tests: list[dict] = list(getattr(cls, "_TESTS", []) or [])
        candidates: list[dict] = ([test] if test else []) + tests
        url = None
        for cand in candidates:
            if isinstance(cand, dict):
                raw = cand.get("url")
                if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                    url = raw
                    break
        if url is None:
            continue
        entries.append({
            "site": name,
            "url": url,
            "extractor": getattr(cls, "IE_NAME", name),
            "sample": True,
        })
    entries.sort(key=lambda e: e["site"])
    return entries


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=0,
                    help="emit only the first N entries (smoke tests)")
    args = ap.parse_args(argv)
    import yt_dlp  # noqa: PLC0415
    entries = corpus()
    if args.limit:
        entries = entries[: args.limit]
    print(json.dumps({"include": entries, "count": len(entries),
                      "ytdlp": yt_dlp.version.__version__}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
