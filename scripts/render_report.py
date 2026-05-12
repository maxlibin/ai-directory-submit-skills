#!/usr/bin/env python3
"""
Render a submission-history.json into a self-contained HTML report.

Usage:
    render_report.py <history.json> [-o report.html] [--open]

The HTML file inlines all CSS/JS, so it works offline and can be emailed or
zipped. Screenshots are linked by relative path; keep them next to the HTML
file (or absolute paths in the history).

History entry shape (extra keys are tolerated and shown in the detail panel):
    {
      "tool_url": "https://yourtool.com",
      "tool_name": "Your Tool",
      "directory_name": "AI Tools List",
      "directory_url": "https://aitoolslist.io",
      "submit_url": "https://aitoolslist.io/submit",
      "status": "submitted",        # submitted | skipped | failed | unknown
      "reason": "",                  # short string for skipped/failed/unknown
      "screenshot": "./shots/x.png",
      "final_url": "https://aitoolslist.io/thank-you",
      "submitted_at": "2026-05-12T18:00:00Z"
    }
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from collections import Counter
from pathlib import Path


STATUS_ORDER = ["submitted", "unknown", "failed", "skipped"]
STATUS_COLORS = {
    "submitted": "#1a7f37",
    "skipped":   "#9a6700",
    "failed":    "#cf222e",
    "unknown":   "#6e7781",
}


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI Directory Submission Report</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    margin: 0; padding: 24px; color: #1f2328; background: #f6f8fa;
  }}
  h1 {{ margin: 0 0 4px; font-size: 22px; }}
  .meta {{ color: #57606a; margin-bottom: 20px; }}
  .summary {{
    display: flex; gap: 12px; margin-bottom: 24px; flex-wrap: wrap;
  }}
  .pill {{
    background: white; border: 1px solid #d0d7de; border-radius: 8px;
    padding: 12px 16px; min-width: 120px;
  }}
  .pill .n {{ font-size: 24px; font-weight: 600; }}
  .pill .l {{ font-size: 12px; color: #57606a; text-transform: uppercase; letter-spacing: 0.5px; }}
  .controls {{ margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap; }}
  .controls button {{
    background: white; border: 1px solid #d0d7de; padding: 6px 12px;
    border-radius: 6px; cursor: pointer; font-size: 13px;
  }}
  .controls button.active {{ background: #0969da; color: white; border-color: #0969da; }}
  .controls input {{
    border: 1px solid #d0d7de; padding: 6px 10px; border-radius: 6px;
    font-size: 13px; flex: 1; min-width: 200px;
  }}
  table {{
    width: 100%; background: white; border: 1px solid #d0d7de;
    border-radius: 8px; border-collapse: separate; border-spacing: 0;
    overflow: hidden;
  }}
  th, td {{
    text-align: left; padding: 10px 12px; border-bottom: 1px solid #d0d7de;
    vertical-align: top;
  }}
  th {{
    background: #f6f8fa; font-weight: 600; font-size: 12px;
    text-transform: uppercase; letter-spacing: 0.5px; color: #57606a;
    cursor: pointer; user-select: none;
  }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: #f6f8fa; }}
  .status {{
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 12px; font-weight: 600; color: white; text-transform: capitalize;
  }}
  .reason {{ color: #57606a; font-size: 12px; }}
  a {{ color: #0969da; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .thumb {{
    width: 80px; height: 50px; object-fit: cover; border: 1px solid #d0d7de;
    border-radius: 4px; cursor: zoom-in;
  }}
  .hidden {{ display: none; }}
  dialog {{
    border: none; border-radius: 8px; padding: 0; max-width: 90vw; max-height: 90vh;
  }}
  dialog img {{ display: block; max-width: 100%; max-height: 90vh; }}
  dialog::backdrop {{ background: rgba(0,0,0,0.6); }}
  .empty {{ padding: 40px; text-align: center; color: #57606a; }}
</style>
</head>
<body>
  <h1>AI Directory Submission Report</h1>
  <div class="meta">{tool_line} &middot; generated {generated_at}</div>

  <div class="summary">
    {summary_pills}
  </div>

  <div class="controls">
    <button data-filter="all" class="active">All ({total})</button>
    {filter_buttons}
    <input id="search" placeholder="Filter by name, URL, or reason…" />
  </div>

  <table id="report">
    <thead>
      <tr>
        <th data-sort="directory_name">Directory</th>
        <th data-sort="status">Status</th>
        <th>Reason</th>
        <th>Submit URL</th>
        <th>Final URL</th>
        <th>Screenshot</th>
        <th data-sort="submitted_at">When</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>

  <dialog id="lightbox"><img id="lightbox-img" /></dialog>

<script>
  const dialog = document.getElementById('lightbox');
  const lbImg = document.getElementById('lightbox-img');
  document.querySelectorAll('.thumb').forEach(t => {{
    t.addEventListener('click', () => {{ lbImg.src = t.src; dialog.showModal(); }});
  }});
  dialog.addEventListener('click', () => dialog.close());

  const search = document.getElementById('search');
  const rows = Array.from(document.querySelectorAll('#report tbody tr'));
  let activeFilter = 'all';

  function apply() {{
    const q = search.value.trim().toLowerCase();
    rows.forEach(r => {{
      const matchesFilter = activeFilter === 'all' || r.dataset.status === activeFilter;
      const hay = r.textContent.toLowerCase();
      const matchesQuery = !q || hay.includes(q);
      r.classList.toggle('hidden', !(matchesFilter && matchesQuery));
    }});
  }}
  search.addEventListener('input', apply);
  document.querySelectorAll('.controls button[data-filter]').forEach(b => {{
    b.addEventListener('click', () => {{
      document.querySelectorAll('.controls button[data-filter]').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      activeFilter = b.dataset.filter;
      apply();
    }});
  }});

  document.querySelectorAll('th[data-sort]').forEach(th => {{
    let asc = true;
    th.addEventListener('click', () => {{
      const key = th.dataset.sort;
      const tbody = document.querySelector('#report tbody');
      const sorted = [...rows].sort((a, b) => {{
        const av = (a.dataset[key] || '').toLowerCase();
        const bv = (b.dataset[key] || '').toLowerCase();
        return asc ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      asc = !asc;
      sorted.forEach(r => tbody.appendChild(r));
    }});
  }});
</script>
</body>
</html>
"""


def row_html(entry: dict) -> str:
    status = entry.get("status", "unknown")
    color = STATUS_COLORS.get(status, "#6e7781")
    name = entry.get("directory_name") or entry.get("directory_url", "")
    home = entry.get("directory_url", "")
    submit_url = entry.get("submit_url", "")
    final_url = entry.get("final_url", "")
    shot = entry.get("screenshot", "")
    reason = entry.get("reason", "")
    when = entry.get("submitted_at", "")

    name_cell = (
        f'<a href="{html.escape(home)}" target="_blank">{html.escape(name)}</a>'
        if home else html.escape(name)
    )
    submit_cell = (
        f'<a href="{html.escape(submit_url)}" target="_blank">{html.escape(submit_url)}</a>'
        if submit_url else "—"
    )
    final_cell = (
        f'<a href="{html.escape(final_url)}" target="_blank">{html.escape(final_url)}</a>'
        if final_url else "—"
    )
    shot_cell = (
        f'<img class="thumb" src="{html.escape(shot)}" alt="screenshot">'
        if shot else "—"
    )

    return (
        f'<tr data-status="{html.escape(status)}" data-directory_name="{html.escape(name)}" '
        f'data-submitted_at="{html.escape(when)}">'
        f"<td>{name_cell}</td>"
        f'<td><span class="status" style="background:{color}">{html.escape(status)}</span></td>'
        f'<td><span class="reason">{html.escape(reason)}</span></td>'
        f"<td>{submit_cell}</td>"
        f"<td>{final_cell}</td>"
        f"<td>{shot_cell}</td>"
        f'<td><span class="reason">{html.escape(when)}</span></td>'
        f"</tr>"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("history", help="Path to submission-history.json")
    ap.add_argument("-o", "--output", default="", help="Output HTML path (default: alongside history)")
    ap.add_argument("--open", action="store_true", help="Open report in default browser")
    args = ap.parse_args()

    history_path = Path(args.history)
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"history file not found: {history_path}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"failed to read history: {e}", file=sys.stderr)
        return 1

    if not isinstance(history, list):
        print("history must be a JSON array", file=sys.stderr)
        return 1

    counts = Counter(e.get("status", "unknown") for e in history if isinstance(e, dict))
    total = sum(counts.values())

    summary_pills = "".join(
        f'<div class="pill"><div class="n" style="color:{STATUS_COLORS.get(s, "#1f2328")}">{counts.get(s, 0)}</div>'
        f'<div class="l">{s}</div></div>'
        for s in STATUS_ORDER
    )

    filter_buttons = "".join(
        f'<button data-filter="{s}">{s.capitalize()} ({counts.get(s, 0)})</button>'
        for s in STATUS_ORDER if counts.get(s, 0) > 0
    )

    rows = "\n".join(row_html(e) for e in history if isinstance(e, dict))
    if not rows:
        rows = '<tr><td colspan="7" class="empty">No entries yet.</td></tr>'

    tool_urls = sorted({e.get("tool_url", "") for e in history if isinstance(e, dict) and e.get("tool_url")})
    tool_line = (
        ", ".join(html.escape(u) for u in tool_urls) if tool_urls else "(no tool URL recorded)"
    )

    from datetime import datetime, timezone
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    out_html = HTML_TEMPLATE.format(
        tool_line=tool_line,
        generated_at=generated_at,
        summary_pills=summary_pills,
        filter_buttons=filter_buttons,
        total=total,
        rows=rows,
    )

    output_path = Path(args.output) if args.output else history_path.with_suffix(".html").with_name("submission-report.html")
    output_path.write_text(out_html, encoding="utf-8")
    print(f"wrote {output_path}")

    if args.open:
        webbrowser.open(output_path.resolve().as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(main())
