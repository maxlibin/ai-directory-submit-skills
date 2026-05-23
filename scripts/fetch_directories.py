#!/usr/bin/env python3
"""
Fetch the AI directory list from one or more curated GitHub sources.

Sources:
  best-of-ai     github.com/best-of-ai/ai-directories       (~150 entries, sectioned)
  submitaitools  github.com/submitaitools/Free-AI-Directories (~175 entries, table format,
                 includes direct submit URLs and Domain Rating where available)

Emits one record per line (JSONL by default) with:
    {"name": "...", "url": "https://...", "submit_url": "...", "section": "A",
     "source": "best-of-ai|submitaitools", "domain_rating": <number or null>}

Usage:
    fetch_directories.py                    # both sources, deduplicated (default)
    fetch_directories.py --source best-of-ai
    fetch_directories.py --source submitaitools
    fetch_directories.py --limit 20         # first 20
    fetch_directories.py --section A        # only best-of-ai section A
    fetch_directories.py --format json      # single JSON array instead of JSONL
    fetch_directories.py --raw              # dump the raw README of the chosen source

The lists are fetched live on every run.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse


SOURCES = {
    "best-of-ai": [
        "https://raw.githubusercontent.com/best-of-ai/ai-directories/main/README.md",
        "https://raw.githubusercontent.com/best-of-ai/ai-directories/master/README.md",
    ],
    "submitaitools": [
        "https://raw.githubusercontent.com/submitaitools/Free-AI-Directories/main/README.md",
        "https://raw.githubusercontent.com/submitaitools/Free-AI-Directories/master/README.md",
    ],
}

LINK_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\(([^)]+)\)")
SECTION_RE = re.compile(r"^##+\s*(?:Section\s+)?([A-Z0-9])\s*$", re.IGNORECASE)

# submitaitools row: | Name | [Name](url) | [Submit](submit_url) | DR | Visits |
TABLE_ROW_RE = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*\[[^\]]+\]\((https?://[^)\s]+)[^)]*\)\s*\|\s*\[[^\]]+\]\((https?://[^)\s]+)[^)]*\)\s*\|(?:\s*([0-9.]+)\s*\|)?"
)


def fetch(urls: list[str]) -> str:
    last_err: Exception | None = None
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise SystemExit(f"Failed to fetch README from {urls}: {last_err}")


def normalize_key(url: str) -> str:
    """For dedup. Lowercase host (drop www), strip trailing slash."""
    if not url:
        return ""
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return url.strip().lower().rstrip("/")
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    return f"{host}{path}".rstrip("/").lower()


def parse_best_of_ai(readme: str) -> list[dict]:
    """Bullet-list format with H2 section headers."""
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
        url = m.group(2).strip().split()[0]
        if not url.startswith(("http://", "https://")):
            continue
        # GitHub-hosted "directories" need a PR not a form
        if url.startswith("https://github.com/"):
            continue
        key = normalize_key(url)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "name": name,
            "url": url,
            "submit_url": "",
            "section": section,
            "source": "best-of-ai",
            "domain_rating": None,
        })
    return out


def parse_submitaitools(readme: str) -> list[dict]:
    """Markdown table format with direct submit URLs + Domain Rating."""
    out: list[dict] = []
    seen: set[str] = set()
    for line in readme.splitlines():
        if not line.strip().startswith("|") or "---" in line:
            continue
        m = TABLE_ROW_RE.match(line)
        if not m:
            continue
        raw_name, url, submit_url, dr = m.group(1), m.group(2), m.group(3), m.group(4)
        # Strip leading emoji / asterisks the table sometimes has
        name = re.sub(r"^[\W_]+", "", raw_name).strip()
        if not name or name.lower().startswith("name"):
            continue
        if url.startswith("https://github.com/"):
            continue
        key = normalize_key(url)
        if key in seen:
            continue
        seen.add(key)
        # If submit_url is just the homepage, treat as unknown
        if normalize_key(submit_url) == key:
            submit_url = ""
        try:
            domain_rating = float(dr) if dr else None
        except ValueError:
            domain_rating = None
        out.append({
            "name": name,
            "url": url,
            "submit_url": submit_url,
            "section": "",
            "source": "submitaitools",
            "domain_rating": domain_rating,
        })
    return out


def merge(entries_lists: list[list[dict]]) -> list[dict]:
    """Dedup by normalized URL. When merging duplicates, fill in missing fields from
    the later source (e.g. submitaitools often has a submit_url that best-of-ai lacks)."""
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for entries in entries_lists:
        for e in entries:
            key = normalize_key(e["url"])
            if not key:
                continue
            if key not in by_key:
                by_key[key] = dict(e)
                order.append(key)
            else:
                existing = by_key[key]
                # Fill in anything the existing entry is missing
                if not existing.get("submit_url") and e.get("submit_url"):
                    existing["submit_url"] = e["submit_url"]
                if not existing.get("section") and e.get("section"):
                    existing["section"] = e["section"]
                if existing.get("domain_rating") is None and e.get("domain_rating") is not None:
                    existing["domain_rating"] = e["domain_rating"]
                # Track which sources had this entry
                src = existing.get("source", "")
                if e.get("source") and e["source"] not in src:
                    existing["source"] = ",".join(filter(None, [src, e["source"]]))
    return [by_key[k] for k in order]


# Back-compat shim — list_directories.py and other callers import this.
def parse(readme: str) -> list[dict]:
    return parse_best_of_ai(readme)


def fetch_readme() -> str:
    """Back-compat: best-of-ai README only."""
    return fetch(SOURCES["best-of-ai"])


def fetch_all() -> list[dict]:
    """Fetch and merge both sources."""
    best = parse_best_of_ai(fetch(SOURCES["best-of-ai"]))
    sat = parse_submitaitools(fetch(SOURCES["submitaitools"]))
    return merge([best, sat])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=("all", "best-of-ai", "submitaitools"), default="all",
                   help="Which source to use (default: all — merges both with dedup)")
    p.add_argument("--limit", type=int, default=0, help="Max number of entries (0 = all)")
    p.add_argument("--section", default="", help="Only this section letter (best-of-ai only)")
    p.add_argument("--format", choices=("jsonl", "json"), default="jsonl",
                   help="Output format (default: jsonl, one record per line)")
    p.add_argument("--raw", action="store_true", help="Print raw README of the source and exit")
    p.add_argument("--summary", action="store_true", help="Print counts per source (to stderr)")
    args = p.parse_args()

    if args.raw:
        if args.source == "all":
            print("--raw needs a single source (best-of-ai or submitaitools)", file=sys.stderr)
            return 2
        sys.stdout.write(fetch(SOURCES[args.source]))
        return 0

    if args.source == "best-of-ai":
        entries = parse_best_of_ai(fetch(SOURCES["best-of-ai"]))
    elif args.source == "submitaitools":
        entries = parse_submitaitools(fetch(SOURCES["submitaitools"]))
    else:
        entries = fetch_all()

    if args.section:
        section = args.section.upper()
        entries = [e for e in entries if e.get("section") == section]
    if args.limit > 0:
        entries = entries[: args.limit]

    if args.format == "json":
        json.dump(entries, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        for e in entries:
            sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")

    if args.summary:
        by_source: dict[str, int] = {}
        for e in entries:
            by_source[e.get("source", "?")] = by_source.get(e.get("source", "?"), 0) + 1
        print(f"# {len(entries)} directories", file=sys.stderr)
        for src, n in sorted(by_source.items(), key=lambda x: -x[1]):
            print(f"#   {src:<24} {n}", file=sys.stderr)
    else:
        print(f"# {len(entries)} directories", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
