#!/usr/bin/env python3
"""
Build / update a directory access-type catalog from one or more submission histories.

The catalog records each directory's *access type* (independent of the tool being
submitted), so future runs can filter the live best-of-ai list without re-probing:

    no-login         Form actually submitted successfully without authentication
    has-form         Scout saw a form but didn't submit — candidate for a real attempt
    login-required   Needs a user sign-in (Google / GitHub / email)
    paid-only        Only paid submission tiers; no free path
    captcha          Bot wall (Cloudflare / reCAPTCHA / etc.)
    dead             Domain expired, parked, unreachable, or repurposed
    no-form          Site doesn't accept public submissions (curated)
    github-pr-only   Submit via a GitHub PR, not a web form
    email-only       Submit by emailing the operator
    off-topic        Niche directory that doesn't fit most tools
    complex-form     Form exists but our tooling can't drive it (rich-text, captcha, etc.)
    unknown          Outcome unverified — keep retrying

Usage:
    build_catalog.py <history.json> [<history.json> ...] [-o catalog.json]
        Reads one or more submission-history.json files, derives access types
        from each (status, reason) pair, and writes a merged catalog.

    build_catalog.py --history <history.json> --merge <existing-catalog.json> -o catalog.json
        Updates an existing catalog with new findings; newer entries win.

Default output: ~/.claude/skills/submit-to-ai-directories/data/directory-catalog.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_CATALOG = Path.home() / ".claude" / "skills" / "submit-to-ai-directories" / "data" / "directory-catalog.json"


# (substring → access type). First match wins. Substrings are matched case-insensitive
# against the `reason` field of the history entry.
REASON_PATTERNS: list[tuple[str, str]] = [
    # Scout heuristic: form detected on the submit page but not actually submitted yet.
    # Worth a real attempt — these are submission candidates.
    ("form detected on submit page", "has-form"),
    # captcha first because some captcha entries also contain "bot"
    ("cloudflare", "captcha"),
    ("captcha", "captcha"),
    ("recaptcha", "captcha"),
    ("turnstile", "captcha"),
    # login wall
    ("login-required", "login-required"),
    ("login required", "login-required"),
    ("sign in", "login-required"),
    # paid tiers
    ("paid-only", "paid-only"),
    ("paid only", "paid-only"),
    # github / email / niche
    ("github-pr-only", "github-pr-only"),
    ("github pr", "github-pr-only"),
    ("fork ", "github-pr-only"),
    ("email-only", "email-only"),
    ("email follow-up", "email-only"),
    ("off-topic", "off-topic"),
    ("niche", "off-topic"),
    # dead sites
    ("ssl cert", "dead"),
    ("unreachable", "dead"),
    ("not found", "dead"),
    ("vercel deployment", "dead"),
    ("deployment paused", "dead"),
    ("parked", "dead"),
    ("expired", "dead"),
    ("renew now", "dead"),
    ("hijacked", "dead"),
    ("repurposed", "dead"),
    ("404", "dead"),
    ("quic.cloud", "dead"),
    ("not a directory", "dead"),
    # form gap
    ("no submit form", "no-form"),
    ("no form", "no-form"),
    ("no own submit", "no-form"),
    ("curated", "no-form"),
    # tooling gap
    ("rich-text editor", "complex-form"),
    ("trix", "complex-form"),
    ("multi-step", "complex-form"),
    ("overlay", "complex-form"),
    ("not interactable", "complex-form"),
    ("validation state", "complex-form"),
    ("invalid captcha", "captcha"),
]


def normalize_url(url: str) -> str:
    """Lowercase host, strip trailing slash, drop www and fragment."""
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


def classify(entry: dict) -> str:
    """Map a history entry to an access-type bucket."""
    status = (entry.get("status") or "").lower()
    reason = (entry.get("reason") or "").lower()

    if status == "submitted":
        return "no-login"

    # Check reason patterns regardless of status — a scout entry with
    # status="unknown" but reason "form detected ..." should become has-form.
    for needle, access in REASON_PATTERNS:
        if needle in reason:
            return access

    # Fallback for `failed`/`skipped` with no recognised pattern.
    if status == "failed":
        return "complex-form"
    return "unknown"


def load_history(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"history file not found: {path}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"history file not valid JSON: {path}: {e}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        print(f"history file is not a JSON array: {path}", file=sys.stderr)
        return []
    return data


def merge_into_catalog(catalog: dict[str, dict], entries: list[dict]) -> None:
    """Apply entries to catalog in-place. Newer entries (by submitted_at) win."""
    for e in entries:
        if not isinstance(e, dict):
            continue
        key = normalize_url(e.get("directory_url", ""))
        if not key:
            continue
        access = classify(e)
        new = {
            "name": e.get("directory_name") or key,
            "directory_url": e.get("directory_url", ""),
            "submit_url": e.get("submit_url", ""),
            "access": access,
            "last_status": e.get("status", ""),
            "last_reason": e.get("reason", ""),
            "last_seen_at": e.get("submitted_at", ""),
        }
        existing = catalog.get(key)
        if existing:
            # Keep whichever has the more recent timestamp.
            if existing.get("last_seen_at", "") >= new["last_seen_at"]:
                # But upgrade a stale 'unknown' if the newer one is concrete.
                if existing["access"] == "unknown" and access != "unknown":
                    existing["access"] = access
                    existing["last_status"] = new["last_status"]
                    existing["last_reason"] = new["last_reason"]
                continue
        catalog[key] = new


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("history", nargs="*", help="submission-history.json file(s) to ingest")
    ap.add_argument("--merge", help="Existing catalog to merge into (preserves entries not seen in this run)")
    ap.add_argument("-o", "--output", help=f"Output catalog path (default: {DEFAULT_CATALOG})")
    ap.add_argument("--print-summary", action="store_true", help="Print per-bucket counts after building")
    args = ap.parse_args()

    if not args.history and not args.merge:
        ap.error("provide at least one history file or --merge an existing catalog")

    catalog: dict[str, dict] = {}
    if args.merge:
        merge_path = Path(args.merge).expanduser().resolve()
        try:
            existing = json.loads(merge_path.read_text(encoding="utf-8"))
            for k, v in existing.get("directories", {}).items():
                catalog[k] = v
        except FileNotFoundError:
            print(f"--merge file missing: {merge_path} (starting fresh)", file=sys.stderr)
        except Exception as e:
            print(f"failed to read --merge: {e}", file=sys.stderr)
            return 1

    for h in args.history:
        entries = load_history(Path(h).expanduser().resolve())
        merge_into_catalog(catalog, entries)
        print(f"ingested {len(entries)} entries from {h}", file=sys.stderr)

    output = Path(args.output).expanduser().resolve() if args.output else DEFAULT_CATALOG
    output.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(catalog),
        "directories": dict(sorted(catalog.items())),
    }
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {output} ({len(catalog)} directories)", file=sys.stderr)

    if args.print_summary:
        buckets: dict[str, int] = {}
        for v in catalog.values():
            buckets[v["access"]] = buckets.get(v["access"], 0) + 1
        print("\nAccess types:", file=sys.stderr)
        for k in sorted(buckets, key=lambda x: -buckets[x]):
            print(f"  {k:<16} {buckets[k]:>3}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
