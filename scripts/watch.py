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
import shutil
import signal
import sys
import time
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

    def render(self) -> None:
        cols, rows = term_size()
        info = load_json(self.info_path, {})
        history = load_json(self.history_path, [])
        status = load_json(self.status_path, {})
        if not isinstance(history, list):
            history = []

        out: list[str] = [HOME]

        out.append(self._header(cols, info, status, len(history)))
        out.append(self._summary(cols, history))
        out.append(self._current(cols, status))
        out.append(self._divider(cols, "RECENT"))
        out.extend(self._table(cols, rows, history))
        out.append(self._footer(cols))

        # Clear-to-end-of-screen on each frame to wipe stale lines
        sys.stdout.write(CLEAR + "".join(out))
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

    def _table(self, cols: int, rows: int, history: list) -> list[str]:
        # how many rows of history fit
        used = 6  # header + summary + divider + footer + margin
        available = max(5, rows - used - 4)
        recent = list(reversed(history))[:available]

        # column widths
        idx_w = 4
        status_w = 11
        time_w = 6
        # remaining split between directory and reason
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
        if not recent:
            out.append(f"{DIM}  (no submissions yet — waiting…){RESET}\n")
            return out

        for i, e in enumerate(recent):
            if not isinstance(e, dict):
                continue
            n = len(history) - i
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

    def _footer(self, cols: int) -> str:
        msg = f" {DIM}watching {self.workspace} · refresh on file change · Ctrl-C to exit{RESET} "
        return "\n" + trunc(msg, cols)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("workspace", help="Workspace directory with submission-info.json + submission-history.json")
    ap.add_argument("--interval", type=float, default=0.5, help="Min seconds between checks (default 0.5)")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"not a directory: {ws}", file=sys.stderr)
        return 1

    r = Renderer(ws)

    # Use alt screen + hide cursor so we don't pollute the user's scrollback
    sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR + CLEAR)

    def restore(*_):
        sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, restore)
    signal.signal(signal.SIGTERM, restore)

    last_mtime = (0.0, 0.0, 0.0)
    try:
        # Initial render
        r.render()
        while True:
            time.sleep(args.interval)
            current = mtimes(r.history_path, r.info_path, r.status_path)
            # Render every 2s even if no file change, so "ago" values stay fresh
            if current != last_mtime or int(time.time()) % 2 == 0:
                r.render()
                last_mtime = current
    except KeyboardInterrupt:
        restore()
    finally:
        sys.stdout.write(SHOW_CURSOR + ALT_SCREEN_OFF)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
