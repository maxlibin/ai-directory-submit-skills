#!/usr/bin/env python3
"""
Scout-only classifier: open every uncatalogued directory's submit URL, detect
the wall (login / paid / captcha / dead / has-form), and record the outcome
*without* filling or submitting anything.

Writes results to <workspace>/submission-history.json so you can watch with
`dirwatch <workspace>` and then merge into the shared catalog via
`build_catalog.py`.

Usage:
    scout_directories.py <workspace> [--limit N] [--resume]
                                     [--session-name <name>]
                                     [--per-dir-timeout 20]
                                     [--rate-limit 2.5]

Examples:
    # Scout the first 50 uncatalogued directories
    scout_directories.py /tmp/riggd-scout --limit 50

    # Resume after interruption (skips directories already in the history)
    scout_directories.py /tmp/riggd-scout --resume

The script keeps one agent-browser session open for the whole run and restarts
it if it crashes. After each directory, the workspace history file is updated
atomically so the dashboard always sees a valid array.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import fetch_directories  # noqa: E402

DEFAULT_CATALOG = Path.home() / ".claude" / "skills" / "submit-to-ai-directories" / "data" / "directory-catalog.json"

# Patterns checked against the *final URL* after redirects.
URL_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"chrome-error://", re.I), "dead", "site unreachable (chrome-error)"),
    (re.compile(r"/(login|signin|sign-in|signup|sign-up|register|auth/)", re.I), "login-required", "login-required (URL redirect)"),
    (re.compile(r"(stripe\.com|checkout\.|paypal\.com|gumroad\.com|lemonsqueezy\.com)", re.I), "paid-only", "paid-only (redirect to payment processor)"),
    (re.compile(r"/(upgrade|pricing|plan|billing|payment)\b", re.I), "paid-only", "paid-only (redirect to pricing/billing)"),
]

# Patterns checked against page TITLE / TEXT (lowercased).
TEXT_PATTERNS: list[tuple[str, str, str]] = [
    # Walls
    ("cloudflare", "captcha", "captcha (Cloudflare bot challenge)"),
    ("checking your browser", "captcha", "captcha (browser check)"),
    ("verify you are a human", "captcha", "captcha (human verification)"),
    ("recaptcha", "captcha", "captcha (reCAPTCHA)"),
    ("turnstile", "captcha", "captcha (Cloudflare Turnstile)"),
    # Dead / parked
    ("renew now", "dead", "domain expired / parked"),
    ("this domain is for sale", "dead", "domain for sale / parked"),
    ("deployment cannot be found", "dead", "Vercel deployment not found"),
    ("deployment is temporarily paused", "dead", "Vercel deployment paused"),
    ("404", "dead", "404 page"),
    ("page not found", "dead", "page not found"),
    ("this page could not be found", "dead", "page not found"),
    ("nginx", "dead", "raw nginx default page"),
    # Auth UI
    ("sign in with google", "login-required", "login wall (Sign in with Google button)"),
    ("sign in with github", "login-required", "login wall (Sign in with GitHub button)"),
    ("continue with google", "login-required", "login wall (Continue with Google)"),
    ("don't have an account? sign up", "login-required", "login form visible"),
    # Paid signals (look for $ + 'submit'/'fee'/'plan' nearby)
    ("submit tool & pay", "paid-only", "paid submission gate"),
    ("submit for $", "paid-only", "paid submission gate"),
    ("submission fee", "paid-only", "paid submission gate"),
    ("verification fee", "paid-only", "paid verification fee"),
    ("submit your tool - $", "paid-only", "paid submission gate"),
    ("get listed for $", "paid-only", "paid listing fee"),
    ("$5/month", "paid-only", "paid subscription"),
    # Submission patterns
    ("github.com", "github-pr-only", "submits via GitHub PR (mentioned on page)"),
    ("pull request", "github-pr-only", "submits via GitHub PR"),
    ("fork the", "github-pr-only", "submits via GitHub fork+PR"),
]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_key(url: str) -> str:
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


def load_catalog(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("directories", {})
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def load_history(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_history_atomic(path: Path, history: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_status(path: Path, **kwargs: str) -> None:
    payload = {
        "directory": kwargs.get("directory", ""),
        "step": kwargs.get("step", ""),
        "url": kwargs.get("url", ""),
        "message": kwargs.get("message", ""),
        "prompt": "",
        "updated_at": now(),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Browser:
    """Wraps agent-browser CLI invocations with one persistent --session."""

    def __init__(self, session: str, headed: bool = False) -> None:
        self.session = session
        self.headed = headed
        self.crashes = 0

    def _run(self, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
        cmd = ["agent-browser", "--session", self.session, *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def close(self) -> None:
        try:
            self._run(["close"], timeout=10)
        except Exception:
            pass

    def open(self, url: str, timeout: int = 20) -> bool:
        cmd = ["agent-browser"]
        if self.headed:
            cmd.append("--headed")
        cmd += ["--session", self.session, "open", url]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False

    def wait_load(self, timeout: int = 12) -> None:
        try:
            self._run(["wait", "--load", "networkidle"], timeout=timeout)
        except Exception:
            pass

    def get_url(self) -> str:
        try:
            r = self._run(["get", "url"], timeout=8)
            return (r.stdout or "").strip()
        except Exception:
            return ""

    def get_text(self, max_chars: int = 4000) -> str:
        try:
            r = self._run(["get", "text", "body"], timeout=10)
            return (r.stdout or "")[:max_chars]
        except Exception:
            return ""

    def snapshot_summary(self, max_chars: int = 2000) -> str:
        """Return a small accessibility snapshot for form-detection heuristics."""
        try:
            r = self._run(["snapshot", "-i"], timeout=12)
            return (r.stdout or "")[:max_chars]
        except Exception:
            return ""


def classify(final_url: str, text: str, snapshot: str) -> tuple[str, str]:
    """Return (access_bucket, human_reason) for a probed directory."""
    if not final_url or final_url == "about:blank":
        return "dead", "site unreachable (no URL)"

    # URL-based wall detection first (fast, high-confidence)
    for rx, bucket, reason in URL_PATTERNS:
        if rx.search(final_url):
            return bucket, reason

    text_l = (text or "").lower()
    snap_l = (snapshot or "").lower()
    combined = text_l + "\n" + snap_l

    for needle, bucket, reason in TEXT_PATTERNS:
        if needle in combined:
            return bucket, reason

    # If the snapshot has interactive form elements, treat as has-form (worth attempting)
    if re.search(r"\btextbox\b", snap_l) and re.search(r"\bbutton\b", snap_l):
        if "submit" in combined or "post" in combined or "add" in combined or "list" in combined:
            return "no-login", "form detected on submit page (scout heuristic)"
    # Snapshot shows nothing actionable
    if "no interactive elements" in snap_l or len(snap_l) < 50:
        return "no-form", "no interactive elements on submit page"

    return "unknown", "could not classify from scout snapshot"


def scout_one(b: Browser, entry: dict, per_dir_timeout: int) -> dict:
    name = entry.get("name", "?")
    dir_url = entry.get("url") or ""
    submit_url = entry.get("submit_url") or dir_url

    deadline = time.time() + per_dir_timeout
    opened = b.open(submit_url, timeout=min(per_dir_timeout, 15))
    if not opened:
        return {
            "directory_name": name, "directory_url": dir_url, "submit_url": submit_url,
            "status": "skipped", "reason": "navigation timeout",
            "screenshot": "", "final_url": "", "submitted_at": now(),
        }
    # Best-effort load wait, but don't blow the budget
    remaining = max(2, int(deadline - time.time()))
    b.wait_load(timeout=min(remaining, 12))

    final_url = b.get_url()
    text = b.get_text(max_chars=3000)
    snap = b.snapshot_summary(max_chars=2000)

    bucket, reason = classify(final_url, text, snap)

    # Map bucket → (status, reason). Buckets we treat as terminal/known go in `skipped`.
    if bucket == "no-login":
        status = "unknown"  # scout never actually submits — flag as worth probing
        reason = "scout: " + reason
    elif bucket == "unknown":
        status = "unknown"
        reason = "scout: " + reason
    else:
        status = "skipped"
        reason = "scout: " + reason

    return {
        "directory_name": name,
        "directory_url": dir_url,
        "submit_url": submit_url,
        "status": status,
        "reason": reason,
        "screenshot": "",
        "final_url": final_url,
        "submitted_at": now(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspace", help="Where to write submission-history.json + submission-status.json")
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG),
                    help=f"Catalog file to mark already-classified directories (default: {DEFAULT_CATALOG})")
    ap.add_argument("--limit", type=int, default=0, help="Cap probes at N directories (0 = all uncatalogued)")
    ap.add_argument("--resume", action="store_true",
                    help="Skip directories already present in the workspace history")
    ap.add_argument("--session-name", default="dirscout", help="agent-browser session name")
    ap.add_argument("--headed", action="store_true", help="Show the scout browser window")
    ap.add_argument("--per-dir-timeout", type=int, default=20, help="Max seconds per directory (default 20)")
    ap.add_argument("--rate-limit", type=float, default=2.0, help="Min seconds between directories (default 2.0)")
    args = ap.parse_args()

    ws = Path(args.workspace).expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)
    history_path = ws / "submission-history.json"
    status_path = ws / "submission-status.json"

    # Header for the dashboard
    info_path = ws / "submission-info.json"
    if not info_path.exists():
        info_path.write_text(json.dumps({"name": "Scout Pass (no tool)", "url": ""}, indent=2), encoding="utf-8")

    catalog = load_catalog(Path(args.catalog).expanduser().resolve())
    print(f"catalog: {len(catalog)} directories", file=sys.stderr)

    history = load_history(history_path)
    already_done_keys = {normalize_key(e.get("directory_url", "")) for e in history}

    all_dirs = fetch_directories.fetch_all()
    targets = []
    for d in all_dirs:
        key = normalize_key(d.get("url", ""))
        if key in catalog:
            continue
        if args.resume and key in already_done_keys:
            continue
        targets.append(d)
    if args.limit > 0:
        targets = targets[: args.limit]

    print(f"will scout {len(targets)} uncatalogued directories", file=sys.stderr)
    if not targets:
        print("nothing to do.", file=sys.stderr)
        return 0

    b = Browser(args.session_name, headed=args.headed)
    last_open = 0.0
    try:
        for i, d in enumerate(targets, 1):
            # Rate limit between sites
            wait = args.rate_limit - (time.time() - last_open)
            if wait > 0:
                time.sleep(wait)

            save_status(status_path,
                        directory=d.get("name", "?"),
                        step=f"scout {i}/{len(targets)}",
                        url=d.get("submit_url") or d.get("url", ""),
                        message="probing for walls (no form fill)")
            try:
                entry = scout_one(b, d, args.per_dir_timeout)
            except subprocess.TimeoutExpired:
                entry = {
                    "directory_name": d.get("name", "?"),
                    "directory_url": d.get("url", ""),
                    "submit_url": d.get("submit_url", ""),
                    "status": "skipped",
                    "reason": "scout: timeout",
                    "screenshot": "",
                    "final_url": "",
                    "submitted_at": now(),
                }
                # Restart the session — a hung browser tends to stay hung
                b.close()
                b = Browser(args.session_name, headed=args.headed)
            except Exception as e:
                entry = {
                    "directory_name": d.get("name", "?"),
                    "directory_url": d.get("url", ""),
                    "submit_url": d.get("submit_url", ""),
                    "status": "failed",
                    "reason": f"scout error: {e}",
                    "screenshot": "",
                    "final_url": "",
                    "submitted_at": now(),
                }
            history.append(entry)
            save_history_atomic(history_path, history)
            last_open = time.time()

            # Compact one-line progress to stderr
            print(f"[{i:>3}/{len(targets)}]  {entry['directory_name'][:32]:<32}  "
                  f"{entry['status']:<8}  {entry['reason']}", file=sys.stderr, flush=True)
    finally:
        save_status(status_path, directory="", step="done", url="", message=f"scouted {len(history)} dirs")
        try:
            b.close()
        except Exception:
            pass

    print("\ndone.", file=sys.stderr)
    print(f"history: {history_path}", file=sys.stderr)
    print(f"next: python3 scripts/build_catalog.py {history_path} --merge {args.catalog} -o {args.catalog} --print-summary",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
