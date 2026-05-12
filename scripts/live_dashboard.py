#!/usr/bin/env python3
"""
Live dashboard for an in-progress submission run.

Runs a tiny HTTP server on http://localhost:8765 that serves a live-updating
HTML view of:
  - The current submission-info.json (the source data being submitted)
  - submission-history.json (auto-refreshes every 2s)
  - Screenshots from shots/<name>.png linked from the table
  - A side-by-side row detail: source data vs. screenshot of the filled form

Usage:
    live_dashboard.py <workspace> [--port 8765] [--open]

The workspace must contain submission-info.json (input) and grows
submission-history.json + shots/ as the run progresses.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI Directory Submission Dashboard (live)</title>
<style>
  * { box-sizing: border-box; }
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; padding: 16px; background: #f6f8fa; color: #1f2328; }
  h1 { margin: 0 0 4px; font-size: 20px; display: inline-block; }
  .live { display: inline-block; padding: 2px 8px; border-radius: 12px; background: #1a7f37; color: #fff;
          font-size: 11px; font-weight: 600; margin-left: 8px; vertical-align: middle; }
  .live.idle { background: #6e7781; }
  .live.dot { animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  .meta { color: #57606a; font-size: 12px; margin-bottom: 14px; }
  .summary { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .pill { background: white; border: 1px solid #d0d7de; border-radius: 8px;
          padding: 8px 14px; min-width: 90px; }
  .pill .n { font-size: 20px; font-weight: 600; }
  .pill .l { font-size: 11px; color: #57606a; text-transform: uppercase; letter-spacing: 0.5px; }
  main { display: grid; grid-template-columns: 280px 1fr; gap: 16px; }
  .source { background: white; border: 1px solid #d0d7de; border-radius: 8px;
            padding: 12px; max-height: 80vh; overflow: auto; position: sticky; top: 16px; }
  .source h2 { margin: 0 0 8px; font-size: 13px; text-transform: uppercase; color: #57606a; letter-spacing: 0.5px; }
  .source dl { margin: 0; font-size: 12px; }
  .source dt { color: #57606a; font-weight: 600; margin-top: 8px; }
  .source dd { margin: 0; word-wrap: break-word; }
  table { width: 100%; background: white; border: 1px solid #d0d7de;
          border-radius: 8px; border-collapse: separate; border-spacing: 0; overflow: hidden; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eaeef2;
           vertical-align: top; font-size: 13px; }
  th { background: #f6f8fa; font-weight: 600; font-size: 11px;
       text-transform: uppercase; letter-spacing: 0.5px; color: #57606a; }
  tr:last-child td { border-bottom: none; }
  tr.row { cursor: pointer; }
  tr.row:hover td { background: #f6f8fa; }
  tr.detail td { background: #fbfcfd; padding: 16px; border-bottom: 2px solid #d0d7de; }
  .status { display: inline-block; padding: 1px 8px; border-radius: 10px;
            font-size: 11px; font-weight: 600; color: white; text-transform: capitalize; }
  .reason { color: #57606a; font-size: 12px; }
  a { color: #0969da; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .thumb { width: 60px; height: 38px; object-fit: cover; border: 1px solid #d0d7de; border-radius: 3px; }
  .detail-grid { display: grid; grid-template-columns: 1fr 2fr; gap: 16px; }
  .detail-grid h3 { margin: 0 0 6px; font-size: 12px; text-transform: uppercase; color: #57606a; }
  .detail-grid .info dl { margin: 0; font-size: 12px; }
  .detail-grid .info dt { color: #57606a; font-weight: 600; margin-top: 6px; }
  .detail-grid .info dd { margin: 0; }
  .detail-grid .shot img { max-width: 100%; border: 1px solid #d0d7de; border-radius: 6px; }
  .empty { padding: 40px; text-align: center; color: #57606a; }
</style>
</head>
<body>
  <h1>AI Directory Submission Dashboard</h1>
  <span id="liveBadge" class="live dot">LIVE</span>
  <div class="meta" id="meta">…</div>
  <div class="summary" id="summary"></div>
  <main>
    <aside class="source">
      <h2>Source data (submission-info.json)</h2>
      <dl id="info"></dl>
    </aside>
    <table>
      <thead>
        <tr>
          <th>#</th>
          <th>Directory</th>
          <th>Status</th>
          <th>Reason</th>
          <th>Submit URL</th>
          <th>Shot</th>
          <th>When</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </main>

<script>
const STATUS_COLORS = {
  submitted: "#1a7f37", skipped: "#9a6700", failed: "#cf222e",
  unknown: "#6e7781", pending: "#0969da"
};

let openRow = null;
let lastHistoryLen = -1;
let lastUpdate = null;

function esc(s) { return (s ?? "").toString().replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":"&#39;"}[c])); }

function tsAgo(iso) {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60000) return Math.floor(ms/1000) + "s ago";
  if (ms < 3600000) return Math.floor(ms/60000) + "m ago";
  return Math.floor(ms/3600000) + "h ago";
}

function renderInfo(info) {
  const el = document.getElementById('info');
  el.innerHTML = Object.entries(info).filter(([k]) => !k.startsWith('_')).map(([k, v]) => {
    const val = Array.isArray(v) ? v.join(', ') : (typeof v === 'object' ? JSON.stringify(v) : v);
    return `<dt>${esc(k)}</dt><dd>${esc(val || '—')}</dd>`;
  }).join('');
}

function renderSummary(history) {
  const counts = {};
  for (const e of history) counts[e.status] = (counts[e.status] || 0) + 1;
  const order = ['submitted', 'unknown', 'failed', 'skipped'];
  document.getElementById('summary').innerHTML = order.map(s => `
    <div class="pill">
      <div class="n" style="color:${STATUS_COLORS[s] || '#1f2328'}">${counts[s] || 0}</div>
      <div class="l">${s}</div>
    </div>
  `).join('') + `<div class="pill"><div class="n">${history.length}</div><div class="l">total</div></div>`;
}

function renderRows(history, info) {
  const tbody = document.getElementById('rows');
  if (history.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">Waiting for first submission…</td></tr>';
    return;
  }
  tbody.innerHTML = history.map((e, i) => {
    const color = STATUS_COLORS[e.status] || '#6e7781';
    return `
      <tr class="row" data-i="${i}">
        <td>${i+1}</td>
        <td><a href="${esc(e.directory_url)}" target="_blank" onclick="event.stopPropagation()">${esc(e.directory_name)}</a></td>
        <td><span class="status" style="background:${color}">${esc(e.status)}</span></td>
        <td class="reason">${esc(e.reason || '')}</td>
        <td>${e.submit_url ? `<a href="${esc(e.submit_url)}" target="_blank" onclick="event.stopPropagation()">${esc(e.submit_url).slice(0, 50)}</a>` : '—'}</td>
        <td>${e.screenshot ? `<img class="thumb" src="${esc(e.screenshot)}" onerror="this.style.display='none'">` : '—'}</td>
        <td class="reason">${esc(tsAgo(e.submitted_at))}</td>
      </tr>
      <tr class="detail" data-detail="${i}" style="display:${openRow === i ? '' : 'none'}">
        <td colspan="7">
          <div class="detail-grid">
            <div class="info">
              <h3>Source data sent</h3>
              <dl>${Object.entries(info).filter(([k]) => !k.startsWith('_')).map(([k, v]) => {
                const val = Array.isArray(v) ? v.join(', ') : (typeof v === 'object' ? JSON.stringify(v) : v);
                return `<dt>${esc(k)}</dt><dd>${esc(val || '—')}</dd>`;
              }).join('')}</dl>
            </div>
            <div class="shot">
              <h3>${e.screenshot ? 'What got submitted / current state' : 'No screenshot recorded'}</h3>
              ${e.screenshot ? `<img src="${esc(e.screenshot)}">` : ''}
              ${e.final_url ? `<p>Ended at: <a href="${esc(e.final_url)}" target="_blank">${esc(e.final_url)}</a></p>` : ''}
            </div>
          </div>
        </td>
      </tr>
    `;
  }).join('');
  tbody.querySelectorAll('tr.row').forEach(tr => {
    tr.onclick = () => {
      const i = parseInt(tr.dataset.i);
      openRow = (openRow === i) ? null : i;
      // toggle without re-fetching
      document.querySelectorAll('tr.detail').forEach(d => d.style.display = 'none');
      const det = document.querySelector(`tr.detail[data-detail="${i}"]`);
      if (det && openRow === i) det.style.display = '';
    };
  });
}

async function tick() {
  try {
    const [info, history] = await Promise.all([
      fetch('/info.json?ts=' + Date.now()).then(r => r.ok ? r.json() : {}),
      fetch('/history.json?ts=' + Date.now()).then(r => r.ok ? r.json() : []),
    ]);
    renderInfo(info);
    if (history.length !== lastHistoryLen) {
      renderSummary(history);
      renderRows(history, info);
      lastHistoryLen = history.length;
      lastUpdate = new Date();
    }
    const tool = info.name || info.tool_name || '—';
    const url = info.url || '—';
    document.getElementById('meta').textContent =
      `Submitting ${tool} (${url}) • ${history.length} attempts • updated ${lastUpdate ? lastUpdate.toLocaleTimeString() : 'never'}`;
    document.getElementById('liveBadge').className = 'live dot';
    document.getElementById('liveBadge').textContent = 'LIVE';
  } catch (e) {
    document.getElementById('liveBadge').className = 'live idle';
    document.getElementById('liveBadge').textContent = 'OFFLINE';
  }
}

tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


def make_handler(workspace: Path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **kw):
            return  # quiet

        def _send(self, status, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, HTML, "text/html; charset=utf-8")
                return
            if path == "/info.json":
                f = workspace / "submission-info.json"
                try:
                    self._send(200, f.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    self._send(200, "{}")
                return
            if path == "/history.json":
                f = workspace / "submission-history.json"
                try:
                    self._send(200, f.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    self._send(200, "[]")
                return
            if path.startswith("/shots/"):
                rel = path.lstrip("/")
                f = workspace / rel
                if f.exists() and f.is_file():
                    ctype = "image/png" if f.suffix == ".png" else "image/jpeg" if f.suffix in (".jpg", ".jpeg") else "application/octet-stream"
                    self._send(200, f.read_bytes(), ctype)
                else:
                    self._send(404, b"not found", "text/plain")
                return
            self._send(404, b"not found", "text/plain")

    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", help="Workspace directory (contains submission-info.json)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="Open in default browser")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    if not ws.is_dir():
        print(f"not a directory: {ws}", file=sys.stderr)
        return 1

    handler = make_handler(ws)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Dashboard: {url}  (workspace: {ws})", flush=True)

    if args.open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
