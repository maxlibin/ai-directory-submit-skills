# submit-to-ai-directories
<img width="867" height="468" alt="ai-directory-submit" src="https://github.com/user-attachments/assets/a128ac51-7cd9-477d-86a2-8862a5bc6b47" />

A [Claude Code](https://claude.com/claude-code) skill that submits an AI tool to
~300 AI tool directories from two curated sources:

- [github.com/best-of-ai/ai-directories](https://github.com/best-of-ai/ai-directories)
- [github.com/submitaitools/Free-AI-Directories](https://github.com/submitaitools/Free-AI-Directories)

It drives a real, visible browser via the
[`agent-browser`](https://www.npmjs.com/package/agent-browser) CLI — Claude looks at each
directory's actual submit form, fills it from your `submission-info.json`, hits submit,
and verifies the outcome with a screenshot. When a directory requires sign-in, the run
pauses and asks you to log in personally in the open browser window, then continues.

Every attempt is recorded so you can see exactly what got submitted, what got skipped,
and why. A **shared catalog** remembers each directory's *access type* across runs, so
the next time you submit a different tool you can say _"submit only the no-login ones"_
and skip directories that are already known to be paid / dead / login-walled.

## What it does (and doesn't)

- ✅ Auto-scrapes your tool's metadata (name, descriptions, logo, socials) from its URL
- ✅ Drives a headed browser through each directory's form
- ✅ Pauses for you to sign in when a directory needs it
- ✅ Records every attempt with screenshots and reasons
- ✅ Builds a shared **access-type catalog** so future runs skip known walls
- ✅ Provides a live terminal dashboard (`dirwatch`) with a full-summary view
- ❌ Doesn't pay for paid submissions (we just skip them and tell you which)
- ❌ Doesn't solve Cloudflare / reCAPTCHA
- ❌ Doesn't invent data — fields not in your `submission-info.json` stay blank

## Requirements

- macOS or Linux (the launcher script + zsh aliases assume a Unix shell)
- [`agent-browser`](https://www.npmjs.com/package/agent-browser) CLI on `PATH`
- Python 3.10+
- [`jq`](https://stedolan.github.io/jq/) (used for safe history-file appends)
- [Claude Code](https://claude.com/claude-code) installed (the skill runs inside Claude Code)

## Install

```bash
# 1. Drop the source at the Claude Code skills location (or unzip the .skill bundle)
git clone https://github.com/<you>/submit-to-ai-directories \
  ~/.claude/skills/submit-to-ai-directories

# 2. Optional but recommended: install the short launcher
install -m 755 ~/.claude/skills/submit-to-ai-directories/scripts/dirwatch \
  ~/.local/bin/dirwatch        # or anywhere on PATH

# 3. Restart Claude Code so it picks up the new skill
```

The skill auto-discovers from `SKILL.md` once it's in the right location. No build step.

## Quick start

```bash
# 1. Make a workspace folder
mkdir my-tool && cd my-tool

# 2. Auto-scrape your tool's metadata into submission-info.json
python3 ~/.claude/skills/submit-to-ai-directories/scripts/scrape_metadata.py \
  https://your-tool.com -o submission-info.json --download-logo --email you@example.com

# 3. Review the file, fill in any TODOs (usually just categories)

# 4. (Optional) See the live progress in another tab
dirwatch                        # auto-detects current dir as the workspace

# 5. In Claude Code, say:
#    "submit my tool to AI directories using ./submission-info.json"

# 6. At any time, get a categorised summary
dirwatch --summary
```

Claude will iterate through directories one at a time. Whenever a directory needs
your sign-in, the dashboard shows a red `NEEDS YOU` banner and Claude pauses for
you to log in.

## The shared catalog

After your first run, every directory is classified into an **access type**:

| Bucket | Meaning |
|---|---|
| `no-login` | Form was actually submitted without auth (confirmed working) |
| `has-form` | Scout saw a fillable form — candidate for a real attempt |
| `login-required` | Needs sign-in (Google / GitHub / email) |
| `paid-only` | Only paid submission tiers |
| `captcha` | Cloudflare / reCAPTCHA bot wall |
| `dead` | Domain expired / parked / unreachable |
| `no-form` | Curated — no public submission |
| `complex-form` | Form exists but tooling can't drive it (rich-text, multi-step) |
| `email-only` | Submit by email, not a web form |
| `github-pr-only` | Submit via GitHub PR |
| `off-topic` | Niche directory that doesn't fit |
| `unknown` | Submitted but no confirmation — worth retrying |

The catalog persists at
`~/.claude/skills/submit-to-ai-directories/data/directory-catalog.json` and is
**tool-independent**: once a directory is classified as `paid-only`, it stays that way
for every future tool you submit. No re-probing.

### Seed the catalog with a scout pass

When the upstream directory list grows (or the first time you set things up), run
the scout to classify every uncatalogued directory without filling forms:

```bash
mkdir /tmp/scout && cd /tmp/scout
python3 ~/.claude/skills/submit-to-ai-directories/scripts/scout_directories.py . --resume
# Watch progress in another tab:  dirwatch /tmp/scout

# After it finishes, fold findings into the catalog
python3 ~/.claude/skills/submit-to-ai-directories/scripts/build_catalog.py \
  ./submission-history.json \
  --merge ~/.claude/skills/submit-to-ai-directories/data/directory-catalog.json \
  -o ~/.claude/skills/submit-to-ai-directories/data/directory-catalog.json \
  --print-summary
```

A scout typically takes 20-30 minutes for ~200 uncatalogued directories. The browser
runs headless by default; pass `--headed` if you want to watch it work.

### Filter the live list

```bash
DIR=~/.claude/skills/submit-to-ai-directories/scripts/list_directories.py

# Confirmed-working free-form directories
python3 $DIR --access no-login

# Confirmed-working + scout-detected candidates (the full submission pool)
python3 $DIR --access no-login,has-form

# Login queue (sign in to these, then re-run)
python3 $DIR --access login-required

# Anything I haven't tested yet
python3 $DIR --uncatalogued

# Skip the obvious noise
python3 $DIR --exclude paid-only,dead,captcha,no-form,off-topic,github-pr-only

# Counts by access type
python3 $DIR --summary --format json
```

## Targeting subsets in chat

Once the catalog is seeded, ask Claude for narrower batches:

- *"submit my new tool to the no-login directories"* → uses `--access no-login`
- *"include has-form candidates too"* → uses `--access no-login,has-form`
- *"do the login batch"* → uses `--access login-required`, walks through with you
- *"skip paid and captcha-walled ones"* → `--exclude paid-only,captcha`
- *"what's still uncatalogued?"* → `--uncatalogued`

## The live dashboard

```bash
dirwatch                            # auto-detect workspace
dirwatch /path/to/workspace         # explicit
dirwatch --summary                  # one-shot full breakdown, then exit
```

Two views, toggle with `s`:

- **List view** — colored status pills, NOW panel (current step), and recent attempts
- **Summary view** — access-type breakdown + lists of submitted / has-form / login-queue

| Key | Action |
|---|---|
| `↑` / `k` | Scroll one row toward newer entries |
| `↓` / `j` | Scroll one row toward older entries |
| `PgUp` / `PgDn` | Scroll a page |
| `Home` / `g` | Jump to newest (resumes live tail) |
| `End` / `G` | Jump to oldest |
| `s` | Toggle list ↔ summary view |
| `q` / `Ctrl-C` | Quit |

## State files (in your workspace, not in this repo)

| File | Purpose |
|---|---|
| `submission-info.json` | Your tool's metadata (input — produced by `scrape_metadata.py` or hand-written) |
| `submission-history.json` | Append-only JSON array of every attempt — source of truth for dedup, dashboard, catalog |
| `submission-status.json` | Current step the skill is on — drives the dashboard's `NOW` panel |
| `submission-report.html` | Generated by `render_report.py` — self-contained, shareable |
| `shots/<directory>.png` | Pre- and post-submit screenshots, referenced from the report |

## Scripts reference

| Path | Purpose |
|---|---|
| `SKILL.md` | Workflow, rules, and quick start — entry point for Claude |
| `scripts/scrape_metadata.py` | Auto-fills `submission-info.json` from a tool URL |
| `scripts/fetch_directories.py` | Pulls the merged list from `best-of-ai` + `submitaitools` (--source flag to limit) |
| `scripts/list_directories.py` | Filter the live list by catalog access type |
| `scripts/scout_directories.py` | Scout-only classifier — probes uncatalogued directories without filling |
| `scripts/build_catalog.py` | Builds / merges the shared catalog from submission histories |
| `scripts/find_submit_link.sh` | Resolves a directory's submission URL (curl-probes first, browser fallback) |
| `scripts/check_submitted.py` | Dedup: exits 0 if `(tool_url, directory_url)` is already handled |
| `scripts/watch.py` | Terminal live dashboard (`dirwatch` is the short launcher) |
| `scripts/dirwatch` | Small bash wrapper for `watch.py` with auto-workspace detection |
| `scripts/render_report.py` | Renders `submission-history.json` → self-contained HTML report |
| `scripts/live_dashboard.py` | Optional browser-based live dashboard at `127.0.0.1:8765` |
| `scripts/run_batch.sh` | Legacy unattended batch runner (kept for backwards compat) |
| `references/submission-patterns.md` | Field mappings, what to skip, iframe-form handling, captcha patterns |
| `assets/submission-info.template.json` | User-facing template for tool metadata |

## Architecture (one paragraph)

The skill is a four-stage pipeline driven by `SKILL.md`: **(1)** the user provides
metadata via `submission-info.json` (auto-scraped by `scrape_metadata.py` then reviewed),
**(2)** `fetch_directories.py` pulls the merged directory list and `list_directories.py`
filters it by catalog access type, **(3)** for each remaining directory Claude drives
`agent-browser` to look at the form, fill what's there, click submit, and record the
outcome in `submission-history.json`, and **(4)** `build_catalog.py` folds the new
findings into the shared catalog so future runs skip known walls. State files live in
the user's workspace; the catalog and the skill source live under
`~/.claude/skills/submit-to-ai-directories/`.

## Contributing

The two upstream directory lists are external — to add a directory, open a PR on
[best-of-ai/ai-directories](https://github.com/best-of-ai/ai-directories) or
[submitaitools/Free-AI-Directories](https://github.com/submitaitools/Free-AI-Directories).
This skill picks them up automatically on the next `fetch_directories.py` run.

To improve the skill itself:
- **New classification patterns** → edit `REASON_PATTERNS` in
  `scripts/build_catalog.py` and `ACCESS_PATTERNS` in `scripts/watch.py`
  (keep these two lists in sync — they share semantics).
- **New scout heuristics** → edit `URL_PATTERNS` / `TEXT_PATTERNS` in
  `scripts/scout_directories.py`.
- **Form-handling edge cases** → document them in
  `references/submission-patterns.md`.

## Packaging the `.skill` bundle

```bash
python3 ~/.claude/skills/skill-creator/scripts/package_skill.py . ./dist
```

## License

Open source — pick whichever license fits your project and add it as `LICENSE`.
The skill itself uses only the standard library, `agent-browser`, and `jq`, all of
which have their own licenses.
