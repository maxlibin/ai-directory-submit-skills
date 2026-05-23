#!/usr/bin/env python3
"""
Filter the live best-of-ai directory list by catalog access type.

Reads:
  - The live README via fetch_directories.py
  - A directory-catalog.json (built by build_catalog.py)
And emits a filtered list — useful when you want "submit only no-login ones"
without re-probing each directory.

Usage:
    list_directories.py --access no-login
    list_directories.py --access login-required
    list_directories.py --access no-login,login-required
    list_directories.py --exclude paid-only,dead,off-topic,captcha,no-form,github-pr-only
    list_directories.py --uncatalogued        # directories not in the catalog yet
    list_directories.py --summary             # count by access type

Output: JSONL by default ({"name","url","section","access","submit_url"}),
or JSON array with --format json.

Default catalog: ~/.claude/skills/submit-to-ai-directories/data/directory-catalog.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_CATALOG = Path.home() / ".claude" / "skills" / "submit-to-ai-directories" / "data" / "directory-catalog.json"

# Sibling to this script in the installed skill
FETCH_DIR_SCRIPT = Path(__file__).resolve().parent / "fetch_directories.py"


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
    except Exception:
        return url.strip().rstrip("/").lower()
    host = (p.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    return f"{host}{path}".rstrip("/").lower()


def load_catalog(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"catalog not found: {path}", file=sys.stderr)
        print("Run scripts/build_catalog.py <history.json> first.", file=sys.stderr)
        sys.exit(2)
    return data.get("directories", {})


def fetch_live_list(source: str = "all") -> list[dict]:
    """Re-use fetch_directories.py by importing it."""
    sys.path.insert(0, str(FETCH_DIR_SCRIPT.parent))
    try:
        import fetch_directories  # type: ignore
    except ImportError as e:
        print(f"could not import fetch_directories: {e}", file=sys.stderr)
        sys.exit(3)
    if source == "best-of-ai":
        return list(fetch_directories.parse_best_of_ai(
            fetch_directories.fetch(fetch_directories.SOURCES["best-of-ai"])))
    if source == "submitaitools":
        return list(fetch_directories.parse_submitaitools(
            fetch_directories.fetch(fetch_directories.SOURCES["submitaitools"])))
    return list(fetch_directories.fetch_all())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--access", help="Comma-separated access types to include (e.g. no-login,login-required)")
    ap.add_argument("--exclude", help="Comma-separated access types to exclude")
    ap.add_argument("--uncatalogued", action="store_true", help="Only directories missing from the catalog")
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG), help=f"Catalog file (default: {DEFAULT_CATALOG})")
    ap.add_argument("--summary", action="store_true", help="Print per-access-type counts (filtered list, not all)")
    ap.add_argument("--source", choices=("all", "best-of-ai", "submitaitools"), default="all",
                    help="Which source to draw from (default: all — merges both)")
    ap.add_argument("--format", choices=["jsonl", "json", "table"], default="table",
                    help="Output format (default: table)")
    ap.add_argument("--limit", type=int, default=0, help="Cap output at N entries (0 = no limit)")
    args = ap.parse_args()

    catalog = load_catalog(Path(args.catalog).expanduser().resolve())
    directories = fetch_live_list(args.source)

    include = set(s.strip() for s in (args.access or "").split(",") if s.strip())
    exclude = set(s.strip() for s in (args.exclude or "").split(",") if s.strip())

    out: list[dict] = []
    for d in directories:
        key = normalize_url(d.get("url", ""))
        cat = catalog.get(key, {})
        access = cat.get("access", "uncatalogued")
        if args.uncatalogued and access != "uncatalogued":
            continue
        if include and access not in include:
            continue
        if exclude and access in exclude:
            continue
        # Prefer the cached submit_url from past runs; fall back to one provided by
        # the source list (submitaitools includes direct submit URLs for many).
        submit_url = cat.get("submit_url") or d.get("submit_url", "")
        out.append({
            "name": d.get("name"),
            "url": d.get("url"),
            "section": d.get("section", ""),
            "access": access,
            "submit_url": submit_url,
            "source": d.get("source", ""),
            "domain_rating": d.get("domain_rating"),
            "last_reason": cat.get("last_reason", ""),
        })

    if args.limit > 0:
        out = out[: args.limit]

    if args.summary:
        buckets: dict[str, int] = {}
        for r in out:
            buckets[r["access"]] = buckets.get(r["access"], 0) + 1
        print(f"{len(out)} directories matched. By access type:", file=sys.stderr)
        for k in sorted(buckets, key=lambda x: -buckets[x]):
            print(f"  {k:<16} {buckets[k]:>4}", file=sys.stderr)
        if args.format == "table" and not (include or exclude or args.uncatalogued):
            return 0

    if args.format == "jsonl":
        for r in out:
            print(json.dumps(r, ensure_ascii=False))
    elif args.format == "json":
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:  # table
        for r in out:
            name = (r["name"] or "")[:34]
            url = (r["url"] or "")[:38]
            access = r["access"]
            print(f"  {access:<16}  {name:<34}  {url}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
