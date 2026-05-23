#!/usr/bin/env python3
"""
Live terminal dashboard for an in-progress submission run.

Run in a second terminal tab while Claude drives submissions in the first:

    python3 scripts/watch.py /path/to/workspace

Watches:
  - submission-info.json   (the tool being submitted — header)
  - submission-history.json (the event log — body)
  - submission-status.json  (optional, current step — top-of-body)

Auto-refreshes when files change. Ctrl-C to exit.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import signal
import sys
import termios
import time
import tty
from datetime import datetime, timezone
from pathlib import Path

# ANSI
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
INV = "\033[7m"
CLEAR = "\033[2J\033[H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
HOME = "\033[H"
CLEAR_LINE = "\033[2K"

# colors
FG = {
    "green":   "\033[38;5;34m",
    "yellow":  "\033[38;5;178m",
    "red":     "\033[38;5;160m",
    "blue":    "\033[38;5;39m",
    "gray":    "\033[38;5;244m",
    "white":   "\033[38;5;255m",
    "purple":  "\033[38;5;141m",
    "orange":  "\033[38;5;208m",
}
BG = {
    "green":   "\033[48;5;34m",
    "yellow":  "\033[48;5;178m",
    "red":     "\033[48;5;160m",
    "gray":    "\033[48;5;244m",
    "blue":    "\033[48;5;39m",
}

STATUS_COLOR = {
    "submitted": ("green",  "✓"),
    "failed":    ("red",    "✗"),
    "skipped":   ("yellow", "↷"),
    "unknown":   ("gray",   "?"),
    "pending":   ("blue",   "…"),
}

# Map history (status, reason) → access-type bucket (same buckets as build_catalog.py).
# Order matters: first match wins.
ACCESS_PATTERNS: list[tuple[str, str]] = [
    ("form detected on submit page", "has-form"),
    ("cloudflare", "captcha"),
    ("captcha", "captcha"),
    ("recaptcha", "captcha"),
    ("turnstile", "captcha"),
    ("invalid captcha", "captcha"),
    ("login-required", "login-required"),
    ("login required", "login-required"),
    ("sign in", "login-required"),
    ("paid-only", "paid-only"),
    ("paid only", "paid-only"),
    ("github-pr-only", "github-pr-only"),
    ("github pr", "github-pr-only"),
    ("fork ", "github-pr-only"),
    ("email-only", "email-only"),
    ("email follow-up", "email-only"),
    ("off-topic", "off-topic"),
    ("niche", "off-topic"),
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
    ("no submit form", "no-form"),
    ("no form", "no-form"),
    ("no own submit", "no-form"),
    ("curated", "no-form"),
    ("rich-text editor", "complex-form"),
    ("trix", "complex-form"),
    ("multi-step", "complex-form"),
    ("overlay", "complex-form"),
    ("not interactable", "complex-form"),
    ("validation state", "complex-form"),
]

ACCESS_COLOR = {
    "no-login":       ("green",  "✓"),
    "has-form":       ("blue",   "◇"),
    "login-required": ("yellow", "⏸"),
    "paid-only":      ("red",    "$"),
    "captcha":        ("orange", "🛡"),
    "dead":           ("gray",   "✗"),
    "no-form":        ("gray",   "—"),
    "off-topic":      ("gray",   "≠"),
    "complex-form":   ("red",    "⚙"),
    "email-only":     ("purple", "✉"),
    "github-pr-only": ("blue",   "⌥"),
    "unknown":        ("gray",   "?"),
}

ACCESS_DESCRIPTION = {
    "no-login":       "Submitted successfully, no sign-in needed",
    "has-form":       "Scout saw a form — candidate for a real attempt",
    "login-required": "Needs sign-in (revisit when ready)",
    "paid-only":      "Only paid submission tiers",
    "captcha":        "Bot wall (Cloudflare / reCAPTCHA)",
    "dead":           "Domain expired / parked / unreachable",
    "no-form":        "Curated — no public submission",
    "off-topic":      "Niche directory, doesn't fit",
    "complex-form":   "Form exists but tooling can't drive it",
    "email-only":     "Submit by email, not a web form",
    "github-pr-only": "Submit via GitHub PR",
    "unknown":        "Submitted but no confirmation",
}


def classify_access(entry: dict) -> str:
    status = (entry.get("status") or "").lower()
    reason = (entry.get("reason") or "").lower()
    if status == "submitted":
        return "no-login"
    # Check reason patterns regardless of status; e.g. scout's "form detected"
    # arrives as status=unknown but should bucket as has-form.
    for needle, access in ACCESS_PATTERNS:
        if needle in reason:
            return access
    if status == "failed":
        return "complex-form"
    return "unknown"


def term_size() -> tuple[int, int]:
    try:
        s = shutil.get_terminal_size((100, 30))
        return s.columns, s.lines
    except Exception:
        return 100, 30


def visible_len(s: str) -> int:
    """Length ignoring ANSI escapes."""
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", s))


def trunc(s: str, n: int) -> str:
    if visible_len(s) <= n:
        return s
    # simple truncate ignoring ANSI mid-string is OK because we only color whole tokens
    return s[: max(0, n - 1)] + "…"


def pad(s: str, n: int) -> str:
    pad_n = n - visible_len(s)
    return s + " " * max(0, pad_n)


def ago(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return ""
    now = datetime.now(timezone.utc)
    sec = int((now - t).total_seconds())
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        # Partial write — fall back to last known good
        return default


def mtimes(*paths: Path) -> tuple[float, ...]:
    out = []
    for p in paths:
        try:
            out.append(p.stat().st_mtime)
        except FileNotFoundError:
            out.append(0.0)
    return tuple(out)


class Renderer:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.history_path = workspace / "submission-history.json"
        self.info_path = workspace / "submission-info.json"
        self.status_path = workspace / "submission-status.json"
        self.start = time.time()
        self.last_total = -1
        self.recent_event_ts: float | None = None
        # Scroll: offset from the top of the reversed (newest-first) list.
        # 0 = follow tail (newest visible). >0 = scrolled back into history.
        self.scroll_offset = 0
        self.follow_tail = True
        # Cached page size for input handling
        self.page_size = 10
        # View mode: "list" = live table, "summary" = full breakdown
        self.view_mode = "list"

    def _available_rows(self, term_rows: int, has_status: bool) -> int:
        # header(1) + summary(1) + status block(~0-4) + divider(1) + table_head(1) + footer(2)
        status_lines = 0
        if has_status:
            # be conservative; the status block can be up to 4 lines
            status_lines = 4
        used = 1 + 1 + status_lines + 1 + 1 + 2
        return max(5, term_rows - used)

    def scroll_by(self, delta: int, history_len: int, term_rows: int, has_status: bool) -> None:
        avail = self._available_rows(term_rows, has_status)
        self.page_size = avail
        max_offset = max(0, history_len - avail)
        self.scroll_offset = max(0, min(max_offset, self.scroll_offset + delta))
        # If user scrolled away from tail, stop following
        if delta < 0 or self.scroll_offset > 0:
            self.follow_tail = (self.scroll_offset == 0)
        else:
            self.follow_tail = (self.scroll_offset == 0)

    def scroll_to_top(self) -> None:
        # Top = newest entry visible (offset 0, follows the live tail).
        self.scroll_offset = 0
        self.follow_tail = True

    def scroll_to_bottom(self, history_len: int, term_rows: int, has_status: bool) -> None:
        # Bottom = oldest entry visible (max offset into the reversed list).
        avail = self._available_rows(term_rows, has_status)
        self.scroll_offset = max(0, history_len - avail)
        self.follow_tail = False

    def render(self) -> None:
        cols, rows = term_size()
        info = load_json(self.info_path, {})
        history = load_json(self.history_path, [])
        status = load_json(self.status_path, {})
        if not isinstance(history, list):
            history = []

        has_status = bool(status and any(status.get(k) for k in ("directory", "step", "message", "prompt")))
        avail = self._available_rows(rows, has_status)
        self.page_size = avail

        # Auto-follow tail: if new entries arrive and we're at the bottom, keep showing newest
        if self.follow_tail:
            self.scroll_offset = 0
        else:
            # Clamp in case history grew
            max_offset = max(0, len(history) - avail)
            self.scroll_offset = min(self.scroll_offset, max_offset)

        # Render in place: HOME + content, then "\033[J" (clear to end of screen)
        # erases any leftover lines from a previously-taller frame. Avoids the
        # full-screen wipe ("\033[2J") that produces a visible flicker each tick.
        out: list[str] = [HOME]

        out.append(self._header(cols, info, status, len(history)))
        out.append(self._summary(cols, history))

        if self.view_mode == "summary":
            out.append(self._divider(cols, "FULL SUMMARY"))
            summary_lines = self._build_summary_body(cols, info, history)
            out.extend(self._scrollable_block(summary_lines, avail))
            out.append(self._footer(cols, history, total_lines=len(summary_lines)))
        else:
            out.append(self._current(cols, status))
            out.append(self._divider(cols, "RECENT"))
            out.extend(self._table(cols, avail, history))
            out.append(self._footer(cols, history))

        # Clear-to-EOL before every line break so a shorter line on this frame
        # doesn't leave a tail of stale chars from the previous (longer) line.
        # Then clear-to-end-of-screen wipes any leftover lines below.
        frame = "".join(out).replace("\n", "\033[K\n") + "\033[K\033[J"
        sys.stdout.write(frame)
        sys.stdout.flush()

    def _header(self, cols: int, info: dict, status: dict, total: int) -> str:
        name = info.get("name", "—")
        url = info.get("url", "—")
        title = f" {BOLD}AI Directory Submissions{RESET}  {FG['white']}{name}{RESET} {DIM}{url}{RESET} "
        title = trunc(title, cols)
        bar = f"{BG['blue']}{FG['white']}{pad(title, cols)}{RESET}\n"
        return bar

    def _summary(self, cols: int, history: list) -> str:
        counts = {"submitted": 0, "skipped": 0, "failed": 0, "unknown": 0, "pending": 0}
        for e in history:
            s = (e or {}).get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1
        elapsed = int(time.time() - self.start)
        rate = (counts["submitted"] / max(1, len(history))) * 100 if history else 0
        order = ["submitted", "skipped", "failed", "unknown"]
        pills = []
        for s in order:
            col, glyph = STATUS_COLOR[s]
            pills.append(f" {FG[col]}{BOLD}{counts[s]:>3}{RESET} {FG[col]}{glyph}{RESET} {DIM}{s}{RESET} ")
        total_pill = f" {BOLD}{len(history)}{RESET} {DIM}total{RESET} "
        success_pill = f" {FG['green']}{rate:.0f}%{RESET} {DIM}success{RESET} "
        elapsed_pill = f" {DIM}elapsed{RESET} {elapsed//60}m{elapsed%60:02d}s "
        line = "│".join(pills + [total_pill, success_pill, elapsed_pill])
        return f"{line}\n"

    def _current(self, cols: int, status: dict) -> str:
        if not status:
            return ""
        directory = status.get("directory", "")
        step = status.get("step", "")
        msg = status.get("message", "")
        url = status.get("url", "")
        prompt = status.get("prompt", "")
        updated = status.get("updated_at", "")
        if not (directory or step or msg or prompt):
            return ""
        lines = []
        head = f" {BG['yellow']}{FG['white']}{BOLD} NOW {RESET}  {BOLD}{directory or '—'}{RESET}  {DIM}{step or ''}{RESET}"
        if updated:
            head += f"  {DIM}(updated {ago(updated)} ago){RESET}"
        lines.append(trunc(head, cols))
        if url:
            lines.append(f"      {FG['blue']}{trunc(url, cols - 6)}{RESET}")
        if msg:
            lines.append(f"      {trunc(msg, cols - 6)}")
        if prompt:
            lines.append(f"      {BG['red']}{FG['white']} NEEDS YOU {RESET} {BOLD}{trunc(prompt, cols - 18)}{RESET}")
        return "\n".join(lines) + "\n"

    def _divider(self, cols: int, label: str) -> str:
        line = f"{DIM}─ {label} {'─' * max(0, cols - len(label) - 3)}{RESET}\n"
        return line

    def _table(self, cols: int, available: int, history: list) -> list[str]:
        # Reversed view (newest first); apply scroll offset
        reversed_hist = list(reversed(history))
        start = self.scroll_offset
        end = start + available
        page = reversed_hist[start:end]

        # column widths
        idx_w = 4
        status_w = 11
        time_w = 6
        remaining = cols - idx_w - status_w - time_w - 4
        dir_w = max(20, int(remaining * 0.4))
        reason_w = max(20, remaining - dir_w)

        head = (
            f"{DIM}{pad('  #', idx_w)} "
            f"{pad('STATUS', status_w)} "
            f"{pad('DIRECTORY', dir_w)} "
            f"{pad('REASON', reason_w)} "
            f"{pad('AGO', time_w)}{RESET}\n"
        )
        out = [head]
        if not page:
            if history:
                out.append(f"{DIM}  (scrolled past end — press End to return){RESET}\n")
            else:
                out.append(f"{DIM}  (no submissions yet — waiting…){RESET}\n")
            return out

        for offset_i, e in enumerate(page):
            if not isinstance(e, dict):
                continue
            # i = offset in the reversed list = (history_len - 1 - original_index)
            i = start + offset_i
            n = len(history) - i  # original 1-based index
            s = e.get("status", "unknown")
            col, glyph = STATUS_COLOR.get(s, ("gray", "?"))
            status_cell = f"{FG[col]}{glyph} {s:<8}{RESET}"
            dir_name = e.get("directory_name", "?")
            reason = e.get("reason", "")
            if not reason and e.get("final_url"):
                reason = e["final_url"]
            t = ago(e.get("submitted_at"))
            out.append(
                f"{pad(str(n).rjust(3), idx_w)} "
                f"{pad(status_cell, status_w + len(status_cell) - visible_len(status_cell))} "
                f"{pad(trunc(dir_name, dir_w), dir_w)} "
                f"{DIM}{pad(trunc(reason, reason_w), reason_w)}{RESET} "
                f"{DIM}{pad(t, time_w)}{RESET}\n"
            )
        return out

    def _footer(self, cols: int, history: list, total_lines: int | None = None) -> str:
        if self.view_mode == "summary" and total_lines is not None:
            shown_top = self.scroll_offset + 1
            shown_bot = min(self.scroll_offset + self.page_size, total_lines)
            if total_lines == 0:
                range_str = "summary empty"
            else:
                range_str = f"lines {shown_top}–{shown_bot} of {total_lines}"
            mode_str = f"{BG['blue']}{FG['white']} SUMMARY {RESET}"
        else:
            total = len(history)
            start = self.scroll_offset
            end = min(start + self.page_size, total)
            shown_newest = total - start
            shown_oldest = max(1, total - end + 1)
            if total == 0:
                range_str = "no entries"
            elif self.follow_tail:
                range_str = f"showing {shown_oldest}–{shown_newest} of {total} (tail)"
            else:
                range_str = f"showing {shown_oldest}–{shown_newest} of {total}"
            mode_str = f"{DIM}LIST{RESET}"

        keys = (
            f"{BOLD}↑↓{RESET} scroll  "
            f"{BOLD}PgUp/PgDn{RESET} page  "
            f"{BOLD}Home/End{RESET} jump  "
            f"{BOLD}s{RESET} {'list' if self.view_mode == 'summary' else 'summary'}  "
            f"{BOLD}q{RESET} quit"
        )
        msg = f" {mode_str}  {DIM}{range_str}{RESET}  ·  {keys} "
        return "\n" + trunc(msg, cols)

    def _scrollable_block(self, lines: list[str], available: int) -> list[str]:
        """Page through an arbitrary list of pre-rendered lines using scroll_offset."""
        start = self.scroll_offset
        end = start + available
        page = lines[start:end]
        if not page:
            return [f"{DIM}  (scrolled past end — press Home/End){RESET}\n"]
        # Each line already includes its own '\n' — preserve them.
        return [ln if ln.endswith("\n") else ln + "\n" for ln in page]

    def _build_summary_body(self, cols: int, info: dict, history: list) -> list[str]:
        """Return one string per rendered line (newline-terminated each)."""
        lines: list[str] = []

        # Group by access type
        buckets: dict[str, list[dict]] = {}
        for e in history:
            if not isinstance(e, dict):
                continue
            buckets.setdefault(classify_access(e), []).append(e)

        # Bucket order: actionable first, then terminal
        bucket_order = [
            "no-login", "has-form", "login-required", "unknown",
            "complex-form", "captcha",
            "paid-only", "dead", "no-form", "off-topic",
            "github-pr-only", "email-only",
        ]
        total = len(history)

        # Section 1: access-type counts table
        lines.append(f"{BOLD}ACCESS TYPE BREAKDOWN{RESET}  {DIM}({total} directories processed){RESET}\n")
        lines.append("\n")
        for access in bucket_order:
            entries = buckets.get(access, [])
            if not entries:
                continue
            col, glyph = ACCESS_COLOR.get(access, ("gray", "·"))
            desc = ACCESS_DESCRIPTION.get(access, "")
            lines.append(
                f"  {FG[col]}{glyph}{RESET} "
                f"{FG[col]}{access:<16}{RESET}  "
                f"{BOLD}{len(entries):>3}{RESET}  "
                f"{DIM}{desc}{RESET}\n"
            )
        lines.append("\n")

        # Section 2: per-bucket details for the actionable buckets
        DETAILED_BUCKETS = [
            ("no-login",       "SUBMITTED",       "Submission landed; check inbox for any verification"),
            ("has-form",       "FORM CANDIDATES", "Scout saw a form here — worth a real submission attempt"),
            ("login-required", "LOGIN QUEUE",     "Sign in to these and re-run; high-leverage queue"),
            ("unknown",        "NEEDS REVISIT",   "Form was sent or page didn't classify cleanly — manual look"),
            ("complex-form",   "TOOLING GAP",     "Form exists but our automation couldn't drive it"),
            ("captcha",        "CAPTCHA WALLS",   "Cloudflare / reCAPTCHA — manual submission may work"),
        ]
        for access, title, hint in DETAILED_BUCKETS:
            entries = buckets.get(access, [])
            if not entries:
                continue
            col, glyph = ACCESS_COLOR.get(access, ("gray", "·"))
            lines.append(f"{BOLD}{title}{RESET}  {DIM}({len(entries)}) — {hint}{RESET}\n")
            for e in entries:
                name = e.get("directory_name", "?")[:30]
                url = e.get("submit_url") or e.get("directory_url", "")
                reason = e.get("reason", "")
                if access == "no-login" and reason:
                    detail = f"{DIM}— {trunc(reason, cols - 50)}{RESET}"
                else:
                    detail = f"{FG['blue']}{trunc(url, cols - 36)}{RESET}"
                lines.append(f"  {FG[col]}{glyph}{RESET} {pad(name, 30)}  {detail}\n")
            lines.append("\n")

        # Section 3: terminal-skip categories (just counts, no listing — they're noise)
        terminal_total = sum(
            len(buckets.get(b, []))
            for b in ("paid-only", "dead", "no-form", "off-topic", "github-pr-only", "email-only")
        )
        if terminal_total:
            lines.append(f"{DIM}{terminal_total} directories are terminal skips (paid / dead / off-topic / no-form / pr-only / email-only). Not worth retrying.{RESET}\n")

        return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspace", help="Workspace directory with submission-info.json + submission-history.json")
    ap.add_argument("--interval", type=float, default=0.5, help="Min seconds between checks (default 0.5)")
    ap.add_argument("--summary", action="store_true",
                    help="Print full summary once (counts, submitted, login queue, etc.) and exit. "
                         "Non-interactive — pipeable / scriptable.")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"not a directory: {ws}", file=sys.stderr)
        return 1

    r = Renderer(ws)

    if args.summary:
        # One-shot summary mode: print to plain stdout, no alt-screen, no colors-off.
        info = load_json(r.info_path, {})
        history = load_json(r.history_path, [])
        if not isinstance(history, list):
            history = []
        cols, _ = term_size()

        name = info.get("name", "—")
        url = info.get("url", "—")
        print(f"\n{BOLD}AI Directory Submissions{RESET}  {FG['white']}{name}{RESET} {DIM}{url}{RESET}\n")
        # Status counts
        counts: dict[str, int] = {}
        for e in history:
            counts[(e or {}).get("status", "unknown")] = counts.get((e or {}).get("status", "unknown"), 0) + 1
        order = ["submitted", "skipped", "failed", "unknown"]
        pills = []
        for s in order:
            col, glyph = STATUS_COLOR[s]
            pills.append(f"{FG[col]}{BOLD}{counts.get(s, 0):>3}{RESET} {FG[col]}{glyph}{RESET} {DIM}{s}{RESET}")
        rate = (counts.get("submitted", 0) / max(1, len(history))) * 100 if history else 0
        pills.append(f"{BOLD}{len(history)}{RESET} {DIM}total{RESET}")
        pills.append(f"{FG['green']}{rate:.0f}%{RESET} {DIM}success{RESET}")
        print("  " + " │ ".join(pills) + "\n")

        # Body lines from the same builder used by interactive mode
        for line in r._build_summary_body(cols, info, history):
            sys.stdout.write(line)
        print()
        return 0

    # Use alt screen + hide cursor so we don't pollute the user's scrollback
    sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR + CLEAR)

    # Switch stdin to cbreak mode so we can read single keypresses without Enter.
    # Save the original state so we can restore on exit.
    stdin_fd = sys.stdin.fileno()
    is_tty = sys.stdin.isatty()
    old_term_settings = None
    if is_tty:
        try:
            old_term_settings = termios.tcgetattr(stdin_fd)
            tty.setcbreak(stdin_fd)
        except Exception:
            is_tty = False

    cleaned = {"done": False}

    def restore(*_):
        if cleaned["done"]:
            return
        cleaned["done"] = True
        if is_tty and old_term_settings is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term_settings)
            except Exception:
                pass
        sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
        sys.stdout.flush()

    def restore_and_exit(*_):
        restore()
        sys.exit(0)

    signal.signal(signal.SIGINT, restore_and_exit)
    signal.signal(signal.SIGTERM, restore_and_exit)

    def read_key(timeout: float) -> str | None:
        """Read a keypress (or escape sequence) with timeout. Returns a symbolic name or single char.
        Returns None for unrecognized / partial sequences — never silently quits."""
        if not is_tty:
            time.sleep(timeout)
            return None
        rlist, _, _ = select.select([sys.stdin], [], [], timeout)
        if not rlist:
            return None
        ch = sys.stdin.read(1)
        if ch != "\x1b":
            return ch
        # ESC byte. Wait generously for the rest of an escape sequence.
        # Some terminals (macOS Terminal.app, slow SSH) buffer arrow keys with delay;
        # being too aggressive here turns arrow presses into spurious quits.
        rlist, _, _ = select.select([sys.stdin], [], [], 0.25)
        if not rlist:
            # Truly a bare ESC. Treat as no-op (we only quit on 'q'/Ctrl-C).
            return None
        seq = sys.stdin.read(1)
        if seq != "[":
            # ESC + something we don't recognise — also no-op.
            return None
        body = ""
        while True:
            rlist, _, _ = select.select([sys.stdin], [], [], 0.25)
            if not rlist:
                break
            c = sys.stdin.read(1)
            body += c
            if c.isalpha() or c == "~":
                break
            if len(body) > 6:
                break
        return {
            "A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT",
            "H": "HOME", "F": "END",
            "5~": "PGUP", "6~": "PGDN",
            "1~": "HOME", "4~": "END",
        }.get(body, None)

    last_mtime = (0.0, 0.0, 0.0)
    last_render = 0.0
    try:
        r.render()
        last_render = time.time()
        while True:
            key = read_key(timeout=0.1)
            # Get current state for key handlers
            history = load_json(r.history_path, [])
            if not isinstance(history, list):
                history = []
            status = load_json(r.status_path, {})
            has_status = bool(status and any(status.get(k) for k in ("directory", "step", "message", "prompt")))
            _, term_rows = term_size()

            need_render = False
            if key == "q" or key == "Q":
                restore_and_exit()
            elif key == "UP" or key == "k":
                # Up = toward newer entries (toward the top of the displayed list).
                r.scroll_by(-1, len(history), term_rows, has_status)
                need_render = True
            elif key == "DOWN" or key == "j":
                # Down = toward older entries (further into history).
                r.scroll_by(+1, len(history), term_rows, has_status)
                need_render = True
            elif key == "PGUP":
                r.scroll_by(-r.page_size, len(history), term_rows, has_status)
                need_render = True
            elif key == "PGDN":
                r.scroll_by(+r.page_size, len(history), term_rows, has_status)
                need_render = True
            elif key == "HOME" or key == "g":
                # Home = top of list = newest, resume live tail.
                r.scroll_to_top()
                need_render = True
            elif key == "END" or key == "G":
                # End = bottom of list = oldest.
                r.scroll_to_bottom(len(history), term_rows, has_status)
                need_render = True
            elif key == "s" or key == "S":
                # Toggle list ↔ summary view, reset scroll so the toggle is obvious.
                r.view_mode = "list" if r.view_mode == "summary" else "summary"
                r.scroll_offset = 0
                r.follow_tail = (r.view_mode == "list")
                need_render = True

            # File-change re-render
            current = mtimes(r.history_path, r.info_path, r.status_path)
            now = time.time()
            if current != last_mtime:
                need_render = True
                last_mtime = current
            # Keep "ago" fresh every 2s
            if now - last_render >= 2.0:
                need_render = True

            if need_render:
                r.render()
                last_render = now
    except KeyboardInterrupt:
        restore_and_exit()
    finally:
        restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
