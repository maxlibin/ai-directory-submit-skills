# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The source for the `submit-to-ai-directories` Claude Code skill. The installed copy lives at `~/.claude/skills/submit-to-ai-directories/`; this repo is the canonical source — keep them in sync (the installed copy is what Claude Code actually loads at runtime).

## Common commands

```bash
# List directories from the live best-of-ai README
python3 scripts/fetch_directories.py --limit 10 --format json

# Resolve a directory's submission URL (tries common paths, then scans homepage via agent-browser)
bash scripts/find_submit_link.sh https://example-directory.com

# Check if a tool was already submitted to a directory
python3 scripts/check_submitted.py ./submission-history.json https://your-tool.com https://directory.com

# Render an HTML report from a history file
python3 scripts/render_report.py ./submission-history.json --open

# Run the full submission loop unattended (e.g. directories 1–10)
bash scripts/run_batch.sh /path/to/workspace 10 1

# Validate skill structure
python3 ~/.claude/skills/skill-creator/scripts/quick_validate.py .

# Repackage the .skill bundle (writes to current dir by default; pass a path for elsewhere)
python3 ~/.claude/skills/skill-creator/scripts/package_skill.py .
```

## Architecture

The skill is a four-stage pipeline driven by `SKILL.md`:

1. **Collect info** — user supplies `submission-info.json` (template in `assets/`).
2. **Fetch list** — `fetch_directories.py` parses the live GitHub README into `{name, url, section}` records. GitHub-hosted entries are dropped (those need PRs, not forms).
3. **Loop** — for each directory: dedup (`check_submitted.py`) → resolve URL (`find_submit_link.sh`) → open + snapshot via `agent-browser` → detect iframe forms (Tally / Typeform / Google Forms / Fillout / etc.) and switch to the iframe `src` if found → fill from `submission-info.json` → submit → classify outcome → append to `submission-history.json` via `jq`.
4. **Report** — `render_report.py` renders the history into a self-contained, sortable, filterable HTML report.

### State files (live in user's working dir, not in this repo)

- `submission-info.json` — input
- `submission-history.json` — append-only log of every attempt (the source of truth for dedup and the report)
- `submission-report.html` — generated each run
- `shots/<directory>.png` — screenshots referenced by the report (relative paths)

### Dedup semantics

`check_submitted.py` only counts `status == "submitted"` entries as "done." `failed`, `skipped`, and `unknown` are retried on the next run by design. URLs are normalized before comparison (case, `www.` prefix, trailing `/`, scheme).

### Iframe forms

The single most important non-obvious gotcha: many directories embed Tally/Typeform/Google Forms in an `<iframe>`. `agent-browser snapshot` doesn't traverse iframes — the snapshot will look empty even when a form is visible. The workflow detects this with `eval` over `document.querySelectorAll("iframe")` and opens the iframe `src` directly. Verified end-to-end with aitoolslist.io (Tally).

### What to skip (don't waste cycles)

`references/submission-patterns.md` is authoritative. In short: pay-to-list, GitHub-PR-only, OAuth-walled, email-only, dead/404, and unsolveable captcha pages.

## Conventions

- Browser session: always `agent-browser --session submitdir` (one session per run), close at the end.
- History writes: use `jq` to keep the JSON array valid. Never `echo >>` to the file.
- Don't invent data. If a field isn't in `submission-info.json`, leave the form field blank or skip the directory. Real users have real brands; junk data sticks around.
