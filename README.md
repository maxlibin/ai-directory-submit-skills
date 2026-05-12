# submit-to-ai-directories

A Claude Code skill that submits a website to the AI directories curated at
[github.com/best-of-ai/ai-directories](https://github.com/best-of-ai/ai-directories)
using the `agent-browser` CLI to fill and submit each directory's web form.

## What's in this repo

| Path | Purpose |
|---|---|
| `SKILL.md` | Workflow, rules, and quick start — entry point for Claude |
| `scripts/fetch_directories.py` | Pulls the live list of directories from the best-of-ai README |
| `scripts/find_submit_link.sh` | Resolves a directory's submission URL (curl-probes first, browser fallback) |
| `scripts/check_submitted.py` | Dedup: exits 0 if `(tool_url, directory_url)` was already submitted |
| `scripts/render_report.py` | Renders `submission-history.json` → self-contained HTML report |
| `scripts/run_batch.sh` | Unattended batch runner — `<workspace> [limit] [start-from]`. Good for chunked runs (10 at a time, etc.) |
| `references/submission-patterns.md` | Field mappings, what to skip, iframe-embedded form handling |
| `assets/submission-info.template.json` | User-facing template for tool metadata |
| `submit-to-ai-directories.skill` | Packaged distributable (zip with `.skill` extension) |

## How to use

1. Fill in a `submission-info.json` based on the template in `assets/`.
2. Tell Claude Code: _"submit my tool to AI directories using /path/to/submission-info.json"_ — the skill triggers from its description.
3. The skill iterates per directory: dedup check → resolve submit URL → snapshot the form → fill from the JSON → submit → record outcome.
4. At the end, `submission-history.json` is appended to and `submission-report.html` is generated and opened.

## Dependencies

- `agent-browser` CLI (installed via `npm install -g agent-browser` or Homebrew)
- Python 3
- `jq` (for history append in the workflow)

## Building the .skill bundle

```bash
python3 ~/.claude/skills/skill-creator/scripts/package_skill.py . ./dist
```

## Installing the skill

Drop the source directory at `~/.claude/skills/submit-to-ai-directories/`
(or unzip the `.skill` bundle to the same location). Claude Code auto-discovers
it on startup.
