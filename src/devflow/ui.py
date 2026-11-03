"""Local web dashboard to track ticket stages and devflow queue progress."""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import threading
import webbrowser
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from typing import Any

from .analytics import collect_analytics
from .detect import repo_root
from .loadconfig import ConfigError, load_config
from .store import Store, state_db_path

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Devflow Dashboard</title>
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --text: #c9d1d9;
      --heading: #f0f6fc;
      --accent: #58a6ff;
      --green: #238636;
      --purple: #8957e5;
      --yellow: #d29922;
      --red: #da3633;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      padding: 24px;
      line-height: 1.5;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }
    .title-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    h1 {
      font-size: 24px;
      color: var(--heading);
      font-weight: 600;
    }
    .badge {
      display: inline-block;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 500;
      border-radius: 12px;
      background: #21262d;
      color: var(--accent);
      border: 1px solid var(--border);
    }
    .status-badge {
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .status-in_progress { background: rgba(56, 139, 253, 0.15); color: #58a6ff; border: 1px solid rgba(56, 139, 253, 0.4); }
    .status-ready { background: rgba(210, 153, 34, 0.15); color: #d29922; border: 1px solid rgba(210, 153, 34, 0.4); }
    .status-in_review { background: rgba(137, 87, 229, 0.15); color: #a371f7; border: 1px solid rgba(137, 87, 229, 0.4); }
    .status-done { background: rgba(35, 134, 54, 0.15); color: #3fb950; border: 1px solid rgba(35, 134, 54, 0.4); }
    .status-backlog { background: #21262d; color: #8b949e; border: 1px solid var(--border); }
    
    .stats-bar {
      display: flex;
      gap: 20px;
      margin-bottom: 24px;
    }
    .stat-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 20px;
      flex: 1;
    }
    .stat-label {
      font-size: 12px;
      color: #8b949e;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 4px;
    }
    .stat-val {
      font-size: 22px;
      font-weight: 600;
      color: var(--heading);
    }
    
    .board {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
    }
    .column {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      max-height: calc(100vh - 200px);
    }
    .col-header {
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-weight: 600;
      color: var(--heading);
    }
    .col-count {
      background: #21262d;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 12px;
    }
    .col-items {
      padding: 12px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .item-card {
      background: #1c2128;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 14px;
      transition: transform 0.1s, border-color 0.1s;
    }
    .item-card:hover {
      border-color: #8b949e;
    }
    .item-top {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 8px;
    }
    .item-number {
      font-weight: 600;
      color: var(--accent);
      text-decoration: none;
    }
    .item-number:hover {
      text-decoration: underline;
    }
    .item-title {
      font-size: 14px;
      font-weight: 500;
      color: var(--heading);
      margin-bottom: 8px;
    }
    .item-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: center;
      font-size: 12px;
      color: #8b949e;
    }
    .active-stage {
      background: #388bfd26;
      color: #58a6ff;
      border: 1px solid #388bfd66;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 600;
      font-size: 11px;
    }
    .refresh-btn {
      background: #21262d;
      color: var(--heading);
      border: 1px solid var(--border);
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
      font-weight: 500;
    }
    .refresh-btn:hover {
      background: #30363d;
    }
    .tabs { display: flex; gap: 8px; margin-bottom: 20px; }
    .tab {
      background: transparent; color: #8b949e; border: 1px solid var(--border);
      padding: 6px 16px; border-radius: 6px; font-size: 13px; font-weight: 500; cursor: pointer;
    }
    .tab.active { background: #21262d; color: var(--heading); border-color: #8b949e; }
    .panel-title { font-size: 14px; font-weight: 600; color: var(--heading); margin: 24px 0 10px; }
    .a-table { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--card-bg);
      border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    .a-table th, .a-table td { padding: 8px 12px; text-align: right; border-bottom: 1px solid var(--border); }
    .a-table th:first-child, .a-table td:first-child { text-align: left; }
    .a-table th { color: #8b949e; font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
    .a-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
    .a-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px; }
    .bar-row { display: flex; align-items: center; gap: 10px; font-size: 12px; margin: 6px 0; }
    .bar-label { width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .bar-track { flex: 1; background: #21262d; border-radius: 4px; height: 10px; overflow: hidden; display: flex; }
    .bar-fill { height: 100%; background: var(--accent); }
    .bar-fill.human { background: var(--yellow); }
    .bar-val { width: 90px; text-align: right; color: #8b949e; }
    .muted { color: #8b949e; font-size: 12px; }
    #analytics-view { display: none; }
    .live-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #3fb950;
      display: inline-block;
      margin-right: 6px;
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.9); }
      100% { opacity: 1; transform: scale(1); }
    }
  </style>
</head>
<body>
  <header>
    <div class="title-group">
      <h1>Devflow</h1>
      <span class="badge" id="repo-badge">devflow</span>
      <span style="font-size: 13px; color: #8b949e; margin-left: 8px;">
        <span class="live-dot"></span>Live tracking (polls every 3s)
      </span>
    </div>
    <div>
      <button class="refresh-btn" onclick="fetchState()">Refresh</button>
    </div>
  </header>

  <div class="tabs">
    <button class="tab active" id="tab-board" onclick="showView('board')">Board</button>
    <button class="tab" id="tab-analytics" onclick="showView('analytics')">Analytics</button>
  </div>

  <div id="board-view">
  <div class="stats-bar">
    <div class="stat-card">
      <div class="stat-label">Working Now</div>
      <div class="stat-val" id="stat-working">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Ready Queue</div>
      <div class="stat-val" id="stat-ready">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">In Review</div>
      <div class="stat-val" id="stat-review">0</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Done</div>
      <div class="stat-val" id="stat-done">0</div>
    </div>
  </div>

  <div class="board" id="board-container">
    <!-- Rendered via JavaScript -->
  </div>
  </div>

  <div id="analytics-view">
    <div class="stats-bar" id="a-tiles"></div>
    <div class="panel-title">Tickets</div>
    <table class="a-table" id="a-tickets"></table>
    <div class="a-grid">
      <div><div class="panel-title">Agent latency (mean seconds)</div><div class="a-card" id="a-agents"></div></div>
      <div><div class="panel-title">Agent time vs human wait per ticket</div><div class="a-card" id="a-split"></div></div>
      <div><div class="panel-title">Gates</div><div class="a-card" id="a-gates"></div></div>
      <div><div class="panel-title">Review and routing</div><div class="a-card" id="a-review"></div></div>
      <div><div class="panel-title">Error spans</div><div class="a-card" id="a-errors"></div></div>
    </div>
    <p class="muted" id="a-foot" style="margin-top:16px"></p>
  </div>

  <script>
    let view = 'board';
    function showView(name) {
      view = name;
      document.getElementById('board-view').style.display = name === 'board' ? '' : 'none';
      document.getElementById('analytics-view').style.display = name === 'analytics' ? 'block' : 'none';
      document.getElementById('tab-board').classList.toggle('active', name === 'board');
      document.getElementById('tab-analytics').classList.toggle('active', name === 'analytics');
      if (name === 'analytics') fetchAnalytics();
    }

    async function fetchAnalytics() {
      try {
        const res = await fetch('/api/analytics');
        if (!res.ok) throw new Error('API error: ' + res.status);
        renderAnalytics(await res.json());
      } catch (err) {
        console.error('Failed to fetch analytics:', err);
      }
    }

    function fmtDur(s) {
      if (!s) return '0s';
      if (s < 90) return Math.round(s) + 's';
      if (s < 5400) return (s / 60).toFixed(1) + 'm';
      return (s / 3600).toFixed(1) + 'h';
    }
    function fmtNum(n) { return Number(n || 0).toLocaleString(); }

    function bars(rows, max) {
      if (!rows.length) return '<div class="muted">No data</div>';
      return rows.map(r => `<div class="bar-row"><span class="bar-label" title="${escapeHtml(r.label)}">${escapeHtml(r.label)}</span>` +
        `<span class="bar-track">${(r.parts || [{v: r.v}]).map(p => `<span class="bar-fill ${p.cls || ''}" style="width:${max ? (100 * p.v / max) : 0}%"></span>`).join('')}</span>` +
        `<span class="bar-val">${escapeHtml(r.text)}</span></div>`).join('');
    }

    function renderAnalytics(a) {
      const t = a.totals;
      const tiles = [
        ['Tickets', `${t.completed} / ${t.tickets} done`],
        ['Total cost', '$' + t.cost_usd.toFixed(2)],
        ['Median ticket', fmtDur(t.median_ticket_seconds)],
        ['Agent calls', fmtNum(t.agent_calls)],
        ['Gate failures', `${t.gate_failures} / ${t.gates}`],
        ['Error spans', fmtNum(t.error_spans)],
      ];
      document.getElementById('a-tiles').innerHTML = tiles.map(([l, v]) =>
        `<div class="stat-card"><div class="stat-label">${l}</div><div class="stat-val">${escapeHtml(String(v))}</div></div>`).join('');

      const head = ['Ticket', 'Runs', 'Wall', 'Agent', 'Human wait', 'Calls', 'Reviews', 'Gate fails', 'Errors', 'Cost', 'Tokens in/out'];
      const rows = a.tickets.map(k => [
        `${k.repo ? escapeHtml(k.repo) + ' ' : ''}#${k.issue}${k.done ? ' ✓' : ''}`, k.runs, fmtDur(k.total_seconds),
        fmtDur(k.agent_seconds), fmtDur(k.human_wait_seconds), k.agent_calls, k.review_rounds,
        k.gate_failures, k.error_spans, '$' + k.cost_usd.toFixed(2), `${fmtNum(k.tokens_in)} / ${fmtNum(k.tokens_out)}`]);
      document.getElementById('a-tickets').innerHTML = '<tr>' + head.map(h => `<th>${h}</th>`).join('') + '</tr>' +
        (rows.length ? rows.map(r => '<tr>' + r.map(c => `<td>${c}</td>`).join('') + '</tr>').join('') :
          `<tr><td colspan="${head.length}" class="muted">No traces found. Enable telemetry to collect them.</td></tr>`);

      const agents = Object.entries(a.agent_latency_seconds);
      const maxAgent = Math.max(0, ...agents.map(([, d]) => d.mean));
      document.getElementById('a-agents').innerHTML = bars(agents.map(([k, d]) =>
        ({label: k, v: d.mean, text: `${d.mean.toFixed(0)}s · n=${d.count}`})), maxAgent);

      const maxTotal = Math.max(0, ...a.tickets.map(k => k.agent_seconds + k.human_wait_seconds));
      document.getElementById('a-split').innerHTML = bars(a.tickets.map(k => ({
        label: '#' + k.issue, parts: [{v: k.agent_seconds}, {v: k.human_wait_seconds, cls: 'human'}],
        text: `${fmtDur(k.agent_seconds)} / ${fmtDur(k.human_wait_seconds)}`})), maxTotal) +
        '<div class="muted">blue: agent time · yellow: human wait</div>';

      const gates = Object.entries(a.gates);
      document.getElementById('a-gates').innerHTML = gates.length ? gates.map(([k, g]) =>
        `<div class="bar-row"><span class="bar-label">${escapeHtml(k)}</span><span>${g.pass} pass · ${g.fail} fail · mean ${g.mean_seconds.toFixed(0)}s</span></div>`).join('') :
        '<div class="muted">No data</div>';

      const r = a.review;
      const routing = Object.entries(a.routing).map(([k, v]) => `${escapeHtml(k)} ${v}`).join(' · ') || 'none';
      const costs = Object.entries(a.cost_by_agent_usd).map(([k, v]) => `${escapeHtml(k)} $${v.toFixed(2)}`).join(' · ') || 'none';
      document.getElementById('a-review').innerHTML =
        `<div class="bar-row">Reviews: ${r.results} · with blocking findings: ${r.with_blocking} · findings: ${r.findings} (${r.blocking} blocking)</div>` +
        `<div class="bar-row">Routing: ${routing}</div><div class="bar-row">Cost: ${costs}</div>` +
        `<div class="bar-row">Schema repairs: ${t.schema_repairs}</div>`;

      const errs = Object.entries(a.errors_by_span);
      document.getElementById('a-errors').innerHTML = errs.length ?
        errs.map(([k, v]) => `<div class="bar-row"><span class="bar-label">${escapeHtml(k)}</span><span>${v}</span></div>`).join('') :
        '<div class="muted">None</div>';
      document.getElementById('a-foot').innerText =
        `${a.spans_analyzed} spans analyzed, ${a.spans_skipped} skipped (synthetic or not tied to a ticket).`;
    }

    let lastData = null;

    async function fetchState() {
      try {
        const res = await fetch('/api/state');
        if (!res.ok) throw new Error('API error: ' + res.status);
        const data = await res.json();
        render(data);
      } catch (err) {
        console.error('Failed to fetch state:', err);
      }
    }

    function render(data) {
      lastData = data;
      document.getElementById('repo-badge').innerText = data.repo || 'devflow';
      
      const counts = {
        working: (data.columns['In progress'] || []).length,
        ready: (data.columns['Ready'] || []).length,
        review: (data.columns['In review'] || []).length,
        done: (data.columns['Done'] || []).length,
      };
      
      document.getElementById('stat-working').innerText = counts.working;
      document.getElementById('stat-ready').innerText = counts.ready;
      document.getElementById('stat-review').innerText = counts.review;
      document.getElementById('stat-done').innerText = counts.done;

      const order = ['Ready', 'In progress', 'In review', 'Done', 'Backlog'];
      const container = document.getElementById('board-container');
      container.innerHTML = '';

      for (const colName of order) {
        const items = data.columns[colName] || [];
        const col = document.createElement('div');
        col.className = 'column';

        const header = document.createElement('div');
        header.className = 'col-header';
        header.innerHTML = `<span>${colName}</span><span class="col-count">${items.length}</span>`;
        col.appendChild(header);

        const list = document.createElement('div');
        list.className = 'col-items';

        if (items.length === 0) {
          list.innerHTML = '<div style="color: #6e7681; font-size: 13px; text-align: center; padding: 20px;">No tickets</div>';
        } else {
          for (const item of items) {
            const card = document.createElement('div');
            card.className = 'item-card';

            let stageBadge = '';
            if (item.stage) {
              stageBadge = `<span class="active-stage">${item.stage}</span>`;
            }

            let attemptInfo = '';
            if (item.implement_attempts > 0 || item.review_round > 0) {
              attemptInfo = `<span>Rounds: ${item.review_round} | Attempts: ${item.implement_attempts}</span>`;
            }

            card.innerHTML = `
              <div class="item-top">
                <a class="item-number" href="https://github.com/${data.repo_slug || 'Rishik-J/devflow'}/issues/${item.issue}" target="_blank">#${item.issue}</a>
                <span class="status-badge status-${colName.toLowerCase().replace(' ', '_')}">${colName}</span>
              </div>
              <div class="item-title">${escapeHtml(item.title)}</div>
              <div class="item-meta">
                ${stageBadge}
                ${item.priority ? `<span class="badge">${item.priority}</span>` : ''}
                ${attemptInfo}
              </div>
            `;
            list.appendChild(card);
          }
        }

        col.appendChild(list);
        container.appendChild(col);
      }
    }

    function escapeHtml(str) {
      if (!str) return '';
      return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    fetchState();
    setInterval(() => { fetchState(); if (view === 'analytics') fetchAnalytics(); }, 3000);
  </script>
</body>
</html>
"""


def collect_state(root: Path) -> dict[str, Any]:
    try:
        cfg = load_config(root)
        name = cfg.name
        slug = f"{cfg.board.owner}/{cfg.name}" if cfg.board.owner else cfg.name
        project = cfg.board.project
    except ConfigError:
        cfg = None
        name = root.name or "devflow"
        slug = name
        project = None

    # Read local store tickets
    local_tickets: dict[int, dict[str, Any]] = {}
    db = state_db_path(root)
    if db.exists():
        with Store(db) as store:
            for t in store.list_tickets():
                local_tickets[t.issue] = {
                    "stage": str(t.state.value if hasattr(t.state, "value") else t.state),
                    "review_round": t.review_round,
                    "implement_attempts": t.implement_attempts,
                    "branch": t.branch,
                    "worktree": t.worktree,
                }

    columns: dict[str, list[dict[str, Any]]] = {
        "Backlog": [],
        "Ready": [],
        "In progress": [],
        "In review": [],
        "Done": [],
    }

    if cfg and cfg.board.project:
        from .board import GhBoard
        try:
            board = GhBoard(slug, cfg.board.owner, cfg.board.project)
            for item in board.items():
                status = item.status or "Backlog"
                if status not in columns:
                    columns[status] = []
                info = {
                    "issue": item.issue,
                    "title": item.title,
                    "priority": item.priority,
                    "labels": item.labels,
                    "status": status,
                }
                if item.issue in local_tickets:
                    info.update(local_tickets[item.issue])
                columns[status].append(info)
        except Exception:  # noqa: BLE001
            # Fall back to local tickets
            for issue, details in local_tickets.items():
                columns["In progress"].append({
                    "issue": issue,
                    "title": f"Ticket #{issue}",
                    "status": "In progress",
                    **details,
                })
    else:
        for issue, details in local_tickets.items():
            columns["In progress"].append({
                "issue": issue,
                "title": f"Ticket #{issue}",
                "status": "In progress",
                **details,
            })

    return {
        "repo": name,
        "repo_slug": slug,
        "project": project,
        "columns": columns,
    }


class UiHandler(http.server.BaseHTTPRequestHandler):
    root: Path

    def _send_json(self, produce) -> None:
        try:
            payload = json.dumps(produce()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)
        except Exception as exc:  # noqa: BLE001
            err = json.dumps({"error": str(exc)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/api/state":
            self._send_json(lambda: collect_state(self.root))
            return
        if url.path == "/api/analytics":
            repo = parse_qs(url.query).get("repo", [None])[0]
            self._send_json(lambda: collect_analytics(self.root, repo))
            return

        if self.path == "/" or self.path.startswith("/?"):
            payload = HTML_TEMPLATE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine HTTP request logging to keep console clean
        pass


def serve_ui(root: Path, host: str = "127.0.0.1", port: int = 8788, open_browser: bool = True) -> int:
    class CustomServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    class BoundHandler(UiHandler):
        pass
    BoundHandler.root = root

    try:
        server = CustomServer((host, port), BoundHandler)
    except OSError as exc:
        print(f"error: failed to bind to {host}:{port}: {exc}", file=sys.stderr)
        return 1

    url = f"http://{host}:{port}/"
    print(f"Devflow UI serving at: {url}")
    print("Press Ctrl+C to stop.")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def command(args: argparse.Namespace) -> int:
    root = repo_root(Path(getattr(args, "path", ".")).resolve())
    if root is None:
        print("error: not inside a git repository", file=sys.stderr)
        return 2

    return serve_ui(
        root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
