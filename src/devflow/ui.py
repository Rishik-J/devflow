"""Local web dashboard to track ticket stages and devflow queue progress."""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

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

  <script>
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
    setInterval(fetchState, 3000);
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

    def do_GET(self) -> None:
        if self.path == "/api/state":
            try:
                data = collect_state(self.root)
                payload = json.dumps(data).encode("utf-8")
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
