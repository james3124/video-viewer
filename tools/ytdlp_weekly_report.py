"""Aggregate the weekly yt-dlp sweep into the one PR-worthy report.

    python3 tools/ytdlp_weekly_report.py \
        --reports-dir reports \
        --prev docs/ytdlp-broken.md \
        --out docs/ytdlp-broken.md \
        --body body.md \
        --counts counts.json

Reads every per-site `ytdlp-report.json` under `--reports-dir` (one folder per
site, as uploaded by the `ytdlp-weekly` workflow), then:

* writes the new broken-site list (`--out`) in the canonical table shape:

  | Site | Broken | Extractor | Fixed |
  | --- | --- | --- | --- |
  | YoutubeIE | BROKEN: ... | Youtube | - [ ] |

* writes the PR body (`--body`) — the same table plus a
  "Fixed since last report" section for sites that recovered (broken last
  week, passing now) so the `Fixed` column means something in one glance;
* writes counts (`--counts`) for the workflow to decide the job's exit code.

Only BROKEN sites land in the file/PR. BLOCKED (bot/geo/login/datacenter IP)
and STALE (fixture video died upstream) are environmental or sample
maintenance — they go in the step summary, never in the PR. Default state
of every row is BROKEN with an unticked `- [ ]`; a human ticks `Fixed`
when the extractor is updated upstream or the sample is replaced.

Previous file (--prev) is the last report on `main`; absent means first run.
Exit code is always 0 (the workflow fails its job on BROKEN>0 itself, after
the PR is done).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys


def load_results(reports_dir: str) -> list[dict]:
    """Collectors rows that look like ytdlp_check report results."""
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(reports_dir, "*", "ytdlp-report.json"))):
        try:
            rep = json.load(open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt artifact is not a result
            continue
        folder = os.path.dirname(path)
        extractor_file = os.path.join(folder, "extractor.txt")
        corpus_extractor = ""
        if os.path.exists(extractor_file):
            corpus_extractor = open(extractor_file, encoding="utf-8").read().strip()
        for r in rep.get("results", []):
            if corpus_extractor and not r.get("extractor"):
                r["extractor"] = corpus_extractor
            rows.append(r)
    return rows


def parse_prev(text: str) -> list[tuple[str, str, str]]:
    """(site, broken, extractor) rows from the previous docs file."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or "---" in line:
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) < 3 or parts[0].lower() in ("site",):
            continue
        out.append((parts[0], parts[1], parts[2]))
    return out


def md_table(rows: list[tuple[str, str, str]]) -> str:
    lines = ["| Site | Broken | Extractor | Fixed |",
             "| --- | --- | --- | --- |"]
    for site, broken, extractor in rows:
        cell = (str(broken).replace("|", "\\|").replace("\n", " ")[:120] or "BROKEN")
        lines.append(f"| `{site}` | {cell} | {extractor} | - [ ] |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reports-dir", required=True)
    ap.add_argument("--prev", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--body", required=True)
    ap.add_argument("--counts", required=True)
    args = ap.parse_args(argv)

    rows = load_results(args.reports_dir)
    statuses = {r.get("status") for r in rows}
    prev_rows = parse_prev(open(args.prev, encoding="utf-8").read()) \
        if args.prev and os.path.exists(args.prev) else []
    prev_sites = {s for s, _, _ in prev_rows}

    broken = sorted({(r["site"],
                      "BROKEN: " + str(r.get("detail") or "")[:100],
                      r.get("extractor", "")) for r in rows
                     if r.get("status") in ("BROKEN",)})
    broken_sites = {s for s, _, _ in broken}
    # A site counts as "fixed" only if it was in the previous report AND was
    # checked this week AND extracted OK — a BLOCKED/STALE rerun is not a fix.
    fixed = sorted(r["site"] for r in rows
                   if r.get("site") in prev_sites and r.get("status") == "OK")
    # Keep rows for sites whose check didn't run this week (unchanged).
    seen = {r.get("site") for r in rows}
    carried = [(s, b, e) for s, b, e in prev_rows if s not in seen]

    counts = {
        "checked": len(rows),
        "ok": sum(1 for r in rows if r.get("status") == "OK"),
        "blocked": sum(1 for r in rows if r.get("status") == "BLOCKED"),
        "stale": sum(1 for r in rows if r.get("status") == "STALE"),
        "broken": len(broken),
        "unknown": sum(1 for r in rows if r.get("status") == "UNKNOWN"),
        "fixed_since_last": len(fixed),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
    with open(args.counts, "w", encoding="utf-8") as fh:
        json.dump(counts, fh, indent=2)

    day = counts["generated_at"]
    file_rows = broken + carried
    file_text = "\n".join([
        f"# yt-dlp broken sites — weekly sweep",
        "",
        f"Auto-generated by `tools/ytdlp_weekly_report.py` (`ytdlp-weekly` workflow) "
        f"on {day} from the full yt-dlp extractor corpus.",
        f"",
        f"- Checked: **{counts['checked']}** · OK: **{counts['ok']}** · "
        f"BLOCKED: {counts['blocked']} · STALE: {counts['stale']} · "
        f"**BROKEN: {counts['broken']}** · UNKNOWN: {counts['unknown']}",
        f"",
        f"`BLOCKED` (bot/geo/login/datacenter IP) and `STALE` (fixture video died) "
        f"are never listed here — only extractor rot is. The `Fixed` checkbox is "
        f"for the human who fixes/upstreams an extractor or replaces a dead sample.",
        f"",
        md_table(sorted(file_rows)),
        "",
    ])
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(file_text)

    body_chunks = [
        "## yt-dlp weekly sweep — broken sites report",
        "",
        f"_Generated {day} from the full yt-dlp extractor corpus via "
        f"`ytdlp-weekly` (`tools/ytdlp_corpus.py` → `tools/ytdlp_check.py`)._",
        "",
        f"- Checked: **{counts['checked']}** · OK: **{counts['ok']}** · "
        f"BLOCKED: {counts['blocked']} · STALE: {counts['stale']} · "
        f"**BROKEN: {counts['broken']}** · UNKNOWN: {counts['unknown']}",
        "",
        "Only sites whose extractor fails while the page *did* load are listed. "
        "Tick **Fixed** when the extractor is updated upstream or the sample is replaced.",
        "",
        md_table(broken),
    ]
    if fixed:
        body_chunks += ["", f"### Fixed since last report ({len(fixed)})", "",
                        *[f"- `{s}`" for s in fixed]]
    body_chunks += [
        "",
        "Reproduce one site locally:",
        "",
        "```bash",
        "python3 tools/ytdlp_check.py --only <Site> "
        "--url <Site>=<url-from-ytdlp_corpus> --strict",
        "```",
        "",
        "This PR is updated by the weekly workflow in place — one PR, not one per site.",
    ]
    with open(args.body, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body_chunks) + "\n")

    print(json.dumps(counts, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
