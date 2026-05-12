#!/usr/bin/env python3
"""
Check whether a tool has already been submitted to a directory.

Reads a submission-history.json (JSON array of entries) and looks for an
entry matching (tool_url, directory_url) that should not be re-attempted:
either status == "submitted", or status == "skipped" with a terminal reason
(paid-only, github-pr-only, email-only) that won't change between runs.

Usage:
    check_submitted.py <history.json> <tool_url> <directory_url>

Exit codes:
    0  - already handled (prints the existing entry as JSON to stdout)
    1  - not yet handled (no output)
    2  - bad input / unreadable history

The match is exact on tool_url and directory_url after stripping trailing
slashes and lowercasing the host. This is intentionally strict so that
http vs https and www vs no-www are normalized but path/query are not.

Retryable on next run (NOT considered handled): failed, unknown,
skipped/login-required, skipped/captcha, skipped/no submit form found.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import urlparse, urlunparse

# Skip reasons that won't fix themselves between runs — treat as "done"
# alongside successful submissions so we don't keep hitting paywalls.
TERMINAL_SKIP_REASONS = {"paid-only", "github-pr-only", "email-only"}


def normalize(u: str) -> str:
    try:
        p = urlparse(u.strip())
    except Exception:  # noqa: BLE001
        return u.strip().rstrip("/").lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = "https" if p.scheme in ("http", "https", "") else p.scheme
    netloc = host + (f":{p.port}" if p.port else "")
    path = p.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", p.query, "")).rstrip("/")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2

    history_path, tool_url, dir_url = sys.argv[1], sys.argv[2], sys.argv[3]

    try:
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
    except FileNotFoundError:
        return 1  # no history yet -> not submitted
    except Exception as e:  # noqa: BLE001
        print(f"unreadable history: {e}", file=sys.stderr)
        return 2

    if not isinstance(history, list):
        print("history must be a JSON array", file=sys.stderr)
        return 2

    want_tool = normalize(tool_url)
    want_dir = normalize(dir_url)

    for entry in history:
        if not isinstance(entry, dict):
            continue
        status = entry.get("status")
        is_handled = status == "submitted" or (
            status == "skipped" and entry.get("reason", "") in TERMINAL_SKIP_REASONS
        )
        if not is_handled:
            continue
        if normalize(entry.get("tool_url", "")) != want_tool:
            continue
        if normalize(entry.get("directory_url", "")) != want_dir:
            continue
        json.dump(entry, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
