#!/usr/bin/env python3
"""
Fetch the AI directory list from github.com/best-of-ai/ai-directories.

Parses the README and emits one JSON object per line (JSONL) with:
    {"name": "...", "url": "https://...", "section": "A"}

Usage:
    fetch_directories.py                 # all directories, JSONL to stdout
    fetch_directories.py --limit 20      # first 20
    fetch_directories.py --section A     # only section A
    fetch_directories.py --format json   # single JSON array instead of JSONL
    fetch_directories.py --raw           # dump the raw README to stdout

The list is fetched live from the GitHub raw README on every run, so the
caller always gets the up-to-date set.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

RAW_URLS = [
    "https://raw.githubusercontent.com/best-of-ai/ai-directories/main/README.md",
    "https://raw.githubusercontent.com/best-of-ai/ai-directories/master/README.md",
]

LINK_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\(([^)]+)\)")
SECTION_RE = re.compile(r"^##+\s*(?:Section\s+)?([A-Z0-9])\s*$", re.IGNORECASE)


def fetch_readme() -> str:
    last_err: Exception | None = None
    for url in RAW_URLS:
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
            continue
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    raise SystemExit(f"Failed to fetch README: {last_err}")


def parse(readme: str) -> list[dict]:
    out: list[dict] = []
    section = ""
    seen: set[str] = set()
    for line in readme.splitlines():
        sm = SECTION_RE.match(line.strip())
        if sm:
            section = sm.group(1).upper()
            continue
        m = LINK_RE.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        url = m.group(2).strip().split()[0]  # drop title "url \"title\""
        if not url.startswith(("http://", "https://")):
            continue
        # Skip GitHub repo entries — these need a PR not a form
        if url.startswith("https://github.com/"):
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "url": url, "section": section})
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=0, help="Max number of entries (0 = all)")
    p.add_argument("--section", default="", help="Only this section letter (e.g. A)")
    p.add_argument(
        "--format",
        choices=("jsonl", "json"),
        default="jsonl",
        help="Output format (default: jsonl, one record per line)",
    )
    p.add_argument("--raw", action="store_true", help="Print raw README to stdout and exit")
    args = p.parse_args()

    readme = fetch_readme()

    if args.raw:
        sys.stdout.write(readme)
        return 0

    entries = parse(readme)
    if args.section:
        section = args.section.upper()
        entries = [e for e in entries if e["section"] == section]
    if args.limit > 0:
        entries = entries[: args.limit]

    if args.format == "json":
        json.dump(entries, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        for e in entries:
            sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")

    print(f"# {len(entries)} directories", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
