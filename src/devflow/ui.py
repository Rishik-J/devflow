"""Local web dashboard to track ticket stages and devflow queue progress."""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

from . import analytics
from .detect import repo_root
from .loadconfig import ConfigError, load_config
from .states import State
from .store import Store, state_db_path

HOST_TIMEOUT_S = 5.0
MAX_TRACE_SPANS = 500

LIFECYCLE = {
    State.NEW: "Refining", State.REFINING: "Refining",
    State.AWAITING_CLARIFICATION: "Refining", State.AWAITING_PLAN_APPROVAL: "Refining",
    State.TEST_DESIGN: "Implementing", State.IMPLEMENTING: "Implementing",
    State.CHANGES_REQUESTED: "Implementing", State.GATES: "Gates",
    State.IN_REVIEW: "Review", State.AWAITING_MERGE_APPROVAL: "Review",
    State.MERGED: "Merged", State.ESCALATED: "Failed", State.CANCELLED: "Failed",
}

HTML_TEMPLATE = r"""<!DOCTYPE html>
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


    /* ---- live flight deck ---- */
    #flight-deck { position: sticky; top: 0; z-index: 20; margin-bottom: 20px; background: #101512;
      border: 1px solid #222a24; border-radius: 10px; padding: 14px 16px; color: #cfd8d2; }
    #flight-deck[hidden] { display: none; }
    #flight-deck.active { min-height: 200px; }
    .fd-top { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
    .fd-issue { background: #222a24; color: #34d399; border: 1px solid #2b352e; border-radius: 12px; padding: 2px 10px; font-weight: 600; font-size: 12px; cursor: pointer; }
    .fd-title { font-weight: 600; color: #f0f6fc; overflow-wrap: anywhere; flex: 1; min-width: 0; }
    .fd-stage { color: #34d399; font-size: 12px; font-weight: 600; }
    .fd-more { background: #222a24; border: 1px solid #2b352e; border-radius: 12px; padding: 2px 10px; font-size: 12px; }
    .fd-more[hidden] { display: none; }
    .fd-stepper { display: flex; align-items: center; margin-bottom: 14px; }
    .fd-node { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #8b9a90; white-space: nowrap; }
    .fd-badge { width: 24px; height: 24px; border-radius: 50%; border: 1px solid #2b352e; display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; background: #161c18; }
    .fd-node.done .fd-badge { background: #238636; border-color: #238636; color: #fff; }
    .fd-node.done .fd-badge::before { content: "\2713"; }
    .fd-node.done .fd-badge span { display: none; }
    .fd-node.active { color: #34d399; font-weight: 600; }
    .fd-node.active .fd-badge { border-color: #34d399; color: #34d399; animation: fd-glow 1.8s ease-in-out infinite; }
    .fd-conn { flex: 1; min-width: 12px; height: 2px; margin: 0 8px; background: #2b352e; position: relative; overflow: hidden; }
    .fd-conn.done { background: #238636; }
    .fd-conn.active::after { content: ""; position: absolute; inset: 0; width: 40%; background: linear-gradient(90deg, transparent, #34d399, transparent); animation: fd-flow 1.4s linear infinite; }
    @keyframes fd-glow { 0%, 100% { box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.55); } 50% { box-shadow: 0 0 0 7px rgba(52, 211, 153, 0); } }
    @keyframes fd-flow { from { transform: translateX(-100%); } to { transform: translateX(250%); } }
    @keyframes fd-warn { 0%, 100% { opacity: 1; } 50% { opacity: 0.65; } }
    .fd-meta { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
    .fd-pill, .fd-copy { background: #161c18; border: 1px solid #2b352e; color: #cfd8d2; border-radius: 12px; padding: 2px 10px; font-size: 12px; font-family: inherit; }
    .fd-copy { cursor: pointer; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .fd-copy:hover { border-color: #34d399; }
    .fd-timer { display: inline-flex; gap: 10px; align-items: center; flex-wrap: wrap; border: 1px solid #34d399; color: #34d399; border-radius: 8px; padding: 4px 10px; font-size: 13px; }
    .fd-time { font-variant-numeric: tabular-nums; font-weight: 700; }
    .fd-bench { color: #8b9a90; font-size: 12px; }
    .fd-stall { display: none; font-weight: 600; }
    .fd-timer.amber { border-color: #d29922; color: #d29922; }
    .fd-timer.amber .fd-stall { display: inline; }
    .fd-timer.amber.stalled { animation: fd-warn 2s ease-in-out infinite; }
    .fd-toggle { background: none; border: none; color: #8b9a90; cursor: pointer; font-size: 12px; margin-top: 10px; padding: 0; }
    .fd-feed { background: #0a0e0b; border: 1px solid #222a24; border-radius: 6px; padding: 8px 10px; margin-top: 6px; max-height: 160px; overflow-y: auto; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
    .fd-feed[hidden] { display: none; }
    .fd-line.ok { color: #3fb950; } .fd-line.fail { color: #f85149; } .fd-line.info { color: #9aa7a0; }
    .fd-compact { display: none; }
    @media (max-width: 640px) {
      #flight-deck.active { min-height: 0; }
      .fd-stepper .fd-node .fd-label, .fd-meta, .fd-bench, .fd-title { display: none; }
      .fd-stepper { gap: 2px; }
      .fd-conn { margin: 0 2px; }
      .fd-compact { display: inline; }
    }
    @media (prefers-reduced-motion: reduce) {
      .fd-node.active .fd-badge, .fd-conn.active::after, .fd-timer.amber.stalled, .live-dot { animation: none; }
    }

    .item-card { cursor: pointer; }
    .item-card:focus-visible { outline: 2px solid #34d399; outline-offset: 2px; }
    #drawer-overlay {
      --c-bg: #101512; --c-panel: #161c18; --c-line: #222a24; --c-raised: #2b352e; --c-accent: #34d399;
      position: fixed; inset: 0; z-index: 50; background: rgba(5, 8, 6, 0.55);
      -webkit-backdrop-filter: blur(4px); backdrop-filter: blur(4px);
      opacity: 0; visibility: hidden; transition: opacity 0.2s ease, visibility 0.2s;
    }
    #drawer-overlay.open { opacity: 1; visibility: visible; }
    #drawer {
      position: absolute; top: 0; right: 0; bottom: 0; width: 640px; max-width: 100vw;
      background: var(--c-bg); border-left: 1px solid var(--c-line); color: #cfd8d2;
      display: flex; flex-direction: column; transform: translateX(100%);
      transition: transform 0.25s ease; outline: none;
    }
    #drawer-overlay.open #drawer { transform: translateX(0); }
    @media (max-width: 640px) { #drawer { width: 100vw; } }
    .dr-head { padding: 16px 20px; background: var(--c-panel); border-bottom: 1px solid var(--c-line); display: flex; gap: 12px; align-items: flex-start; }
    .dr-head-main { flex: 1; min-width: 0; }
    .dr-title { font-size: 17px; font-weight: 600; color: #f0f6fc; margin-bottom: 8px; overflow-wrap: anywhere; }
    .dr-badges { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .dr-issue { color: var(--c-accent); border: 1px solid var(--c-raised); border-radius: 12px; padding: 2px 10px; font-size: 12px; font-weight: 600; text-decoration: none; background: var(--c-line); }
    .dr-pill { border: 1px solid var(--c-accent); color: var(--c-accent); border-radius: 12px; padding: 2px 10px; font-size: 12px; font-weight: 600; }
    .dr-pill.Failed { color: #f85149; border-color: #f85149; }
    .dr-close { background: var(--c-line); color: #cfd8d2; border: 1px solid var(--c-raised); border-radius: 6px; width: 32px; height: 32px; font-size: 18px; cursor: pointer; }
    .dr-close:hover { background: var(--c-raised); }
    .dr-tabs { display: flex; background: var(--c-panel); border-bottom: 1px solid var(--c-line); padding: 0 12px; overflow-x: auto; }
    .dr-tab { background: none; border: none; border-bottom: 2px solid transparent; color: #8b9a90; padding: 10px 12px; font-size: 13px; cursor: pointer; white-space: nowrap; }
    .dr-tab[aria-selected="true"] { color: var(--c-accent); border-bottom-color: var(--c-accent); }
    .dr-body { flex: 1; overflow-y: auto; padding: 20px; }
    .dr-body h3 { font-size: 13px; text-transform: uppercase; letter-spacing: .5px; color: #8b9a90; margin: 18px 0 8px; }
    .dr-body h3:first-child { margin-top: 0; }
    .dr-body ul { margin: 0 0 8px 20px; }
    .dr-body code { background: var(--c-line); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
    .dr-body pre { background: var(--c-panel); border: 1px solid var(--c-line); padding: 10px; border-radius: 6px; overflow-x: auto; font-size: 12px; margin-bottom: 8px; }
    .dr-body a { color: var(--c-accent); }
    .dr-chip { display: inline-block; background: var(--c-line); border: 1px solid var(--c-raised); border-radius: 12px; padding: 2px 10px; font-size: 12px; margin: 0 6px 6px 0; }
    .dr-row { background: var(--c-panel); border: 1px solid var(--c-line); border-radius: 6px; padding: 10px 12px; margin-bottom: 8px; font-size: 13px; overflow-wrap: anywhere; }
    .dr-ok { color: var(--c-accent); font-weight: 600; }
    .dr-bad { color: #f85149; font-weight: 600; }
    .dr-muted { color: #8b9a90; font-size: 13px; }
    .dr-body details { margin: 8px 0; }

    /* ---- analytics view ---- */
    .nav { display: flex; gap: 6px; }
    .nav-btn { background: none; color: #8b949e; border: 1px solid transparent; padding: 6px 14px; border-radius: 6px; font-size: 13px; cursor: pointer; font-weight: 500; }
    .nav-btn:hover { color: var(--heading); }
    .nav-btn[aria-current="page"] { color: var(--heading); background: #21262d; border-color: var(--border); }
    .nav-btn:focus-visible, .win-btn:focus-visible { outline: 2px solid #34d399; outline-offset: 2px; }
    body.view-analytics #flight-deck, body.view-analytics .stats-bar, body.view-analytics #board-container { display: none; }
    body.view-setup #flight-deck, body.view-setup .stats-bar, body.view-setup #board-container { display: none; }
    #setup-view[hidden] { display: none; }
    #setup-view { max-width: 900px; margin: 0 auto; }
    #setup-view h2 { color: var(--heading); margin-bottom: 8px; }
    #setup-view > p { color: #8b949e; margin-bottom: 20px; }
    #setup-summary { margin-bottom: 16px; color: #8b949e; }
    .setup-row { display: grid; grid-template-columns: 110px 1fr auto; align-items: center; gap: 14px; padding: 14px 16px; margin-bottom: 9px; border: 1px solid var(--border); border-radius: 8px; background: var(--card-bg); }
    .setup-row strong { color: var(--heading); }
    .setup-row p { color: #8b949e; font-size: 13px; }
    .setup-row code { display: block; margin-top: 7px; color: #a5d6ff; font-size: 12px; overflow-wrap: anywhere; }
    .setup-state { font-size: 12px; font-weight: 700; color: #f0b94d; }
    .setup-state.ready { color: #3fb950; }
    @media (max-width: 650px) { .setup-row { grid-template-columns: 1fr; gap: 5px; } }
    #analytics-view[hidden] { display: none; }
    .an-bar-top { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
    .win-group { display: inline-flex; gap: 0; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
    .win-btn { background: #161b22; color: var(--text); border: none; border-right: 1px solid var(--border); padding: 6px 14px; font-size: 13px; cursor: pointer; }
    .win-btn:last-child { border-right: none; }
    .win-btn[aria-pressed="true"] { background: #1f6feb; color: #fff; font-weight: 600; }
    .an-msg { background: var(--card-bg); border: 1px dashed var(--border); border-radius: 8px; padding: 12px 16px; margin-bottom: 16px; color: #8b949e; font-size: 13px; }
    .an-msg[hidden] { display: none; }
    .an-kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }
    .an-sub { font-size: 12px; color: #8b949e; margin-top: 2px; }
    .an-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; margin-bottom: 16px; }
    .an-panel { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; min-width: 0; }
    .an-panel h2 { font-size: 14px; color: var(--heading); margin-bottom: 10px; font-weight: 600; }
    .an-panel svg { width: 100%; height: auto; display: block; }
    .an-note { font-size: 12px; color: #8b949e; margin-top: 8px; }
    .an-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px; font-variant-numeric: tabular-nums; }
    .an-table th, .an-table td { text-align: right; padding: 5px 8px; border-bottom: 1px solid var(--border); }
    .an-table th:first-child, .an-table td:first-child { text-align: left; }
    .an-table tr.bottleneck td { background: rgba(210, 153, 34, 0.16); font-weight: 600; }
    .an-tag { display: inline-block; font-size: 11px; font-weight: 700; border: 1px solid currentColor; border-radius: 4px; padding: 0 5px; margin-left: 6px; }
    .an-tag.warn { color: #e3b341; }
    .an-tag.low { color: #8b949e; font-weight: 500; }
    .an-list { list-style: none; font-size: 13px; }
    .an-list li { display: flex; justify-content: space-between; gap: 10px; padding: 4px 0; border-bottom: 1px solid var(--border); }
    .an-list li.elevated { color: #f0883e; font-weight: 600; }
    .an-hbar { display: grid; grid-template-columns: minmax(80px, 40%) 1fr auto; gap: 8px; align-items: center; font-size: 12px; margin-bottom: 6px; }
    .an-hbar .lbl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, Menlo, monospace; }
    .an-hbar .track { background: #21262d; border-radius: 3px; height: 12px; overflow: hidden; }
    .an-hbar .fill { height: 100%; background: #58a6ff; transition: width 0.3s ease; }
    .an-hbar .fill.blocking { background: #f85149; }
    .an-hbar .fill.nitpick { background: #8b949e; }
    @media (prefers-reduced-motion: reduce) { .an-hbar .fill { transition: none; } }
  </style>
</head>
<body>
  <header>
    <div class="title-group">
      <h1>Devflow</h1>
      <span class="badge" id="repo-badge">devflow</span>
      <span style="font-size: 13px; color: #8b949e; margin-left: 8px;">
        <span class="live-dot"></span>Live tracking (board every 3s, flight deck every 2.5s)
      </span>
    </div>
    <nav class="nav" aria-label="Views">
      <button type="button" class="nav-btn" data-view="board" aria-current="page">Board</button>
      <button type="button" class="nav-btn" data-view="analytics">Analytics</button>
      <button type="button" class="nav-btn" data-view="setup">Setup</button>
    </nav>
    <div>
      <button class="refresh-btn" onclick="fetchState()">Refresh</button>
    </div>
  </header>

  <section id="flight-deck" class="fd" aria-label="Live Flight Deck" hidden>
    <div class="fd-top">
      <button type="button" class="fd-issue" id="fd-issue"></button>
      <span class="fd-title" id="fd-title"></span>
      <span class="fd-stage" id="fd-stage" aria-live="polite"></span>
      <span class="fd-more" id="fd-more" hidden></span>
    </div>
    <div class="fd-stepper" id="fd-stepper"></div>
    <div class="fd-meta">
      <span class="fd-pill" id="fd-agent"></span>
      <button type="button" class="fd-copy" id="fd-worktree" title="Copy worktree path"></button>
      <button type="button" class="fd-copy" id="fd-branch" title="Copy branch"></button>
    </div>
    <div class="fd-timer" id="fd-timer">
      <span class="fd-compact" id="fd-compact"></span>
      <span class="fd-time" id="fd-time">00:00</span>
      <span class="fd-bench" id="fd-bench"></span>
      <span class="fd-stall">Stall Warning: Stage exceeding 99th percentile</span>
    </div>
    <div><button type="button" class="fd-toggle" id="fd-toggle" aria-expanded="false">&#9656; Activity</button></div>
    <div class="fd-feed" id="fd-feed" hidden></div>
  </section>

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

  <section id="setup-view" aria-label="Repository setup" hidden>
    <h2>Set up this repository</h2>
    <p>Follow each step in your terminal. Refresh after connecting a service. Credentials stay with GitHub, OpenClaw, and the coding CLIs.</p>
    <div id="setup-summary" role="status"></div>
    <div id="setup-steps"></div>
    <button type="button" class="refresh-btn" id="setup-refresh">Recheck connections</button>
  </section>


  <section id="analytics-view" aria-label="Workflow analytics" hidden>
    <div class="an-bar-top">
      <div class="win-group" role="group" aria-label="Timeframe" id="an-window">
        <button type="button" class="win-btn" data-window="24h" aria-pressed="false">Last 24 Hours</button>
        <button type="button" class="win-btn" data-window="7d" aria-pressed="false">Past 7 Days</button>
        <button type="button" class="win-btn" data-window="all" aria-pressed="true">All Time</button>
      </div>
      <button type="button" class="refresh-btn" id="an-refresh">Refresh analytics</button>
    </div>
    <div class="an-msg" id="an-message" role="status" hidden></div>
    <div class="an-kpis">
      <div class="stat-card" id="kpi-cycle"><div class="stat-label">Median cycle time</div><div class="stat-val" data-role="value">&mdash;</div><div class="an-sub" data-role="sub"></div></div>
      <div class="stat-card" id="kpi-firstpass"><div class="stat-label">First-pass approval</div><div class="stat-val" data-role="value">&mdash;</div><div class="an-sub" data-role="sub"></div></div>
      <div class="stat-card" id="kpi-short"><div class="stat-label">Blast short-circuit rate</div><div class="stat-val" data-role="value">&mdash;</div><div class="an-sub" data-role="sub"></div></div>
      <div class="stat-card" id="kpi-stalls"><div class="stat-label">Total stalls</div><div class="stat-val" data-role="value">&mdash;</div><div class="an-sub" data-role="sub"></div></div>
    </div>
    <div class="an-grid">
      <div class="an-panel" id="stage-panel">
        <h2>Stage latency (p50, with p99 marker)</h2>
        <svg id="stage-chart" viewBox="0 0 640 220" role="img" aria-label="Stage latency chart"></svg>
        <table class="an-table" id="stage-table" aria-label="Stage latency table">
          <thead><tr><th>Stage</th><th>n</th><th>p50</th><th>p90</th><th>p99</th><th>mean</th></tr></thead>
          <tbody></tbody>
        </table>
        <div class="an-note">Human wait states (awaiting_*) are excluded.</div>
      </div>
      <div class="an-panel" id="stall-panel">
        <h2>Stall radar</h2>
        <svg id="stall-radar" viewBox="0 0 320 280" role="img" aria-label="Stall radar"></svg>
        <ul class="an-list" id="stall-list"></ul>
        <div class="an-note" id="stall-total"></div>
      </div>
    </div>
    <div class="an-grid">
      <div class="an-panel" id="friction-panel">
        <h2>Review and gate friction</h2>
        <div id="friction-review"></div>
        <h2 style="margin-top:14px">Most frequent gate failures</h2>
        <div id="friction-gates"></div>
      </div>
      <div class="an-panel" id="blast-panel">
        <h2>Blast radius</h2>
        <ul class="an-list">
          <li><span>Avg hop-1 callers per ticket</span><strong id="blast-avg">&mdash;</strong></li>
          <li><span>Test coverage of impacted symbols</span><strong id="blast-coverage">&mdash;</strong></li>
          <li><span>Tickets with impact data</span><strong id="blast-tickets">&mdash;</strong></li>
        </ul>
      </div>
    </div>
  </section>

  <div id="drawer-overlay">
    <aside id="drawer" role="dialog" aria-modal="true" aria-labelledby="dr-title" tabindex="-1">
      <div class="dr-head">
        <div class="dr-head-main">
          <div class="dr-title" id="dr-title"></div>
          <div class="dr-badges" id="dr-badges"></div>
        </div>
        <button class="dr-close" id="dr-close" type="button" aria-label="Close">&times;</button>
      </div>
      <div class="dr-tabs" role="tablist" aria-label="Ticket details" id="dr-tabs"></div>
      <div class="dr-body" id="dr-body" role="tabpanel"></div>
    </aside>
  </div>

  <script>
    let lastData = null;
    let initialSynced = false;
    let openIssue = null;
    let activeTab = 'overview';
    let ticketData = null;
    let ticketJson = '';
    let lastFocus = null;

    async function fetchState() {
      try {
        const res = await fetch('/api/state');
        if (!res.ok) throw new Error('API error: ' + res.status);
        const data = await res.json();
        render(data);
        if (!initialSynced) { initialSynced = true; syncFromHash(); }
        else if (openIssue !== null) refreshTicket();
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
            card.tabIndex = 0;
            card.setAttribute('role', 'button');
            card.dataset.issue = String(item.issue);

            let stageBadge = '';
            if (item.stage) {
              stageBadge = `<span class="active-stage">${escapeHtml(item.stage)}</span>`;
            }

            let attemptInfo = '';
            if (item.implement_attempts > 0 || item.review_round > 0) {
              attemptInfo = `<span>Rounds: ${item.review_round} | Attempts: ${item.implement_attempts}</span>`;
            }

            card.innerHTML = `
              <div class="item-top">
                <a class="item-number" href="https://github.com/${escapeHtml(data.repo_slug || 'Rishik-J/devflow')}/issues/${encodeURIComponent(item.issue)}" target="_blank" rel="noopener">#${escapeHtml(item.issue)}</a>
                <span class="status-badge status-${colName.toLowerCase().replace(' ', '_')}">${colName}</span>
              </div>
              <div class="item-title">${escapeHtml(item.title)}</div>
              <div class="item-meta">
                ${stageBadge}
                ${item.priority ? `<span class="badge">${escapeHtml(item.priority)}</span>` : ''}
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
      if (str === null || str === undefined) return '';
      return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ---------------------------------------------------------------- inspector drawer
    const TABS = [
      ['overview', 'Overview'], ['plan', 'Plan & Decisions'],
      ['blast', 'Blast radius'], ['gates', 'Verification & Gates'],
    ];
    const overlay = document.getElementById('drawer-overlay');
    const drawer = document.getElementById('drawer');
    const drBody = document.getElementById('dr-body');

    function safeUrl(u) {
      const s = String(u === null || u === undefined ? '' : u).trim();
      return /^https?:\/\/[^\s<>"']+$/i.test(s) ? s : null;
    }

    function inlineMd(t) {
      // t is already HTML-escaped; only a fixed allow-list of tags is produced.
      return t
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
          const ok = safeUrl(url.replace(/&amp;/g, '&'));
          return ok ? `<a href="${escapeHtml(ok)}" target="_blank" rel="noopener noreferrer">${label}</a>` : label;
        });
    }

    function renderMarkdown(src) {
      const lines = escapeHtml(src || '').split(/\r?\n/);
      const out = [];
      let list = false, code = null, para = [];
      const flush = () => { if (para.length) { out.push('<p>' + inlineMd(para.join(' ')) + '</p>'); para = []; } };
      const endList = () => { if (list) { out.push('</ul>'); list = false; } };
      for (const line of lines) {
        if (code !== null) {
          if (/^```/.test(line)) { out.push('<pre><code>' + code.join('\n') + '</code></pre>'); code = null; }
          else code.push(line);
          continue;
        }
        if (/^```/.test(line)) { flush(); endList(); code = []; continue; }
        const h = line.match(/^(#{1,6})\s+(.*)$/);
        const li = line.match(/^\s*[-*]\s+(.*)$/);
        if (h) { flush(); endList(); const n = Math.min(h[1].length + 2, 6); out.push(`<h${n}>${inlineMd(h[2])}</h${n}>`); }
        else if (li) { flush(); if (!list) { out.push('<ul>'); list = true; } out.push('<li>' + inlineMd(li[1]) + '</li>'); }
        else if (!line.trim()) { flush(); endList(); }
        else { endList(); para.push(line.trim()); }
      }
      if (code !== null) out.push('<pre><code>' + code.join('\n') + '</code></pre>');
      flush(); endList();
      return out.join('');
    }

    function chip(label, value) {
      if (value === null || value === undefined || value === '') return '';
      return `<span class="dr-chip">${escapeHtml(label)}: ${escapeHtml(value)}</span>`;
    }
    function linkTag(url, label) {
      const u = safeUrl(url);
      return u ? `<a href="${escapeHtml(u)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>` : '';
    }
    function listOf(items, fn) {
      return items && items.length ? '<ul>' + items.map(fn).join('') + '</ul>' : '<div class="dr-muted">None recorded.</div>';
    }
    function text(x) { return typeof x === 'string' ? x : (x && (x.text || x.description || x.summary)) || JSON.stringify(x); }

    function overviewTab(t) {
      const brief = t.brief && t.brief.available && t.brief.body
        ? renderMarkdown(t.brief.body)
        : '<div class="dr-muted">Issue body unavailable (GitHub could not be reached).</div>';
      const hist = (t.history || []).slice(-12).map(h =>
        `<li><code>${escapeHtml(h.to || h.state || '')}</code> ${escapeHtml(h.actor || '')} ${escapeHtml(h.reason || '')}</li>`).join('');
      return `<h3>Issue</h3>${brief}
        <h3>Details</h3>
        ${chip('State', t.state)}${chip('Agent', t.agent)}${chip('Attempts', t.attempts)}${chip('Review rounds', t.review_round)}
        ${chip('Branch', t.branch)}${chip('Worktree', t.worktree)}
        <h3>Links</h3>
        <div>${linkTag(t.urls && t.urls.issue, 'GitHub issue')} ${linkTag(t.urls && t.urls.pr, 'Pull request')}</div>
        <h3>History</h3>${hist ? '<ul>' + hist + '</ul>' : '<div class="dr-muted">No transitions recorded.</div>'}`;
    }

    function planTab(t) {
      const p = t.plan || {};
      if (!p.goal && !(p.steps || []).length) return '<div class="dr-muted">No plan has been recorded yet.</div>';
      const reasoning = p.reasoning ? `<details><summary>Reasoning</summary>${renderMarkdown(p.reasoning)}</details>` : '';
      return `<h3>Objective</h3>${renderMarkdown(p.goal || '')}
        <h3>Acceptance criteria</h3>${listOf(p.acceptance_criteria, c => `<li><strong>${escapeHtml(c.id || '')}</strong> ${escapeHtml(text(c))}</li>`)}
        <h3>Steps</h3>${listOf(p.steps, s => `<li><strong>${escapeHtml(s.id || '')}</strong> ${escapeHtml(text(s))}
          ${(s.files || []).map(f => `<code>${escapeHtml(f)}</code>`).join(' ')}</li>`)}
        <h3>Test strategy</h3>${listOf(p.test_strategy, x => `<li><strong>${escapeHtml(x.id || '')}</strong> ${escapeHtml(x.kind || '')}: ${escapeHtml(x.description || '')}</li>`)}
        <h3>Out of scope</h3>${listOf(p.out_of_scope, x => `<li>${escapeHtml(text(x))}</li>`)}
        <h3>Risks</h3>${listOf(p.risks, x => `<li>${escapeHtml(text(x))}</li>`)}
        ${reasoning}`;
    }

    function blastTab(t) {
      const b = t.blast_radius || {};
      if (b.status !== 'available') return '<div class="dr-muted">Blast radius unavailable: no impact was recorded for this ticket.</div>';
      return `<h3>Impacted symbols</h3>${listOf(b.symbols, s => `<li><code>${escapeHtml(s.label)}</code> ${escapeHtml(s.role || '')} <span class="dr-muted">${escapeHtml(s.file)}:${escapeHtml(s.line)}</span></li>`)}
        <h3>Direct callers (hop 1)</h3>${listOf(b.callers, c => `<li><code>${escapeHtml(c.symbol)}</code> <span class="dr-muted">${escapeHtml(c.file)}</span>
          <span class="${c.verified ? 'dr-ok' : 'dr-bad'}">${c.verified ? 'verified' : 'unverified'}</span></li>`)}
        <h3>Dependent modules</h3>${listOf(b.dependent_modules, m => `<li><code>${escapeHtml(m.file || m.label || '')}</code></li>`)}
        <h3>Uncovered symbols</h3>${listOf(b.uncovered, u => `<li><code>${escapeHtml(u)}</code></li>`)}
        <h3>Impacted tests (run order)</h3>${listOf(b.impacted_tests, x => `<li><code>${escapeHtml(x)}</code></li>`)}`;
    }

    function gatesTab(t) {
      const g = t.gates || {}, attempts = g.attempts || [], latest = g.latest || {}, r = g.review || {};
      const rows = (latest.results || []).map(x => `<div class="dr-row"><span class="${x.ok ? 'dr-ok' : 'dr-bad'}">${x.ok ? 'PASS' : 'FAIL'}</span>
        <code>${escapeHtml(x.project)}:${escapeHtml(x.gate)}</code> <code>${escapeHtml(x.command)}</code>
        <span class="dr-muted">exit ${escapeHtml(x.exit_code)} &middot; ${escapeHtml(x.duration_s)}s${x.timed_out ? ' &middot; timed out' : ''}</span></div>`).join('')
        || '<div class="dr-muted">No per-command results recorded.</div>';
      const fails = (latest.failures || []).map(f => `<div class="dr-row"><code>${escapeHtml(f.project)}:${escapeHtml(f.gate)}</code>
        ${(f.new || []).map(n => `<div><code>${escapeHtml(n)}</code></div>`).join('')}</div>`).join('');
      const hist = attempts.map(a => `<li>Attempt ${escapeHtml(a.attempt)}: <span class="${a.ok ? 'dr-ok' : 'dr-bad'}">${a.ok ? 'passed' : 'failed'}</span></li>`).join('');
      const review = r.verdict ? `<div>${chip('Verdict', r.verdict)}${chip('Approved', r.approved)}${chip('Reviewer', r.agent)}${chip('Independent', r.independent)}</div>
        ${listOf(r.findings, f => `<li><strong>${escapeHtml(f.severity || '')}</strong>${f.blocking ? ' (blocking)' : ''} <code>${escapeHtml(f.file || '')}</code> ${escapeHtml(f.summary || '')}</li>`)}
        ${r.notes ? renderMarkdown(r.notes) : ''}` : '<div class="dr-muted">No review yet.</div>';
      return `<h3>Gate commands</h3>${rows}
        ${fails ? '<h3>New failures</h3>' + fails : ''}
        <h3>Gate attempts</h3>${hist ? '<ul>' + hist + '</ul>' : '<div class="dr-muted">No gate runs yet.</div>'}
        <h3>Review</h3>${review}`;
    }

    function renderDrawer(preserve) {
      const t = ticketData;
      const prev = preserve ? drBody.scrollTop : 0;
      const title = document.getElementById('dr-title');
      const badges = document.getElementById('dr-badges');
      if (!t) return;
      if (t.notFound) {
        title.textContent = 'Ticket #' + openIssue;
        badges.innerHTML = '';
        document.getElementById('dr-tabs').innerHTML = '';
        drBody.innerHTML = `<div class="dr-muted">Ticket #${escapeHtml(openIssue)} was not found.</div>`;
        return;
      }
      title.textContent = t.title || ('Ticket #' + t.issue);
      const issueUrl = safeUrl(t.urls && t.urls.issue);
      badges.innerHTML = (issueUrl ? `<a class="dr-issue" href="${escapeHtml(issueUrl)}" target="_blank" rel="noopener noreferrer">#${escapeHtml(t.issue)}</a>`
                                   : `<span class="dr-issue">#${escapeHtml(t.issue)}</span>`)
        + `<span class="dr-pill ${escapeHtml(t.lifecycle)}">${escapeHtml(t.lifecycle)}</span>`;
      document.getElementById('dr-tabs').innerHTML = TABS.map(([id, label]) =>
        `<button class="dr-tab" role="tab" type="button" data-tab="${id}" aria-selected="${id === activeTab}" tabindex="${id === activeTab ? 0 : -1}">${escapeHtml(label)}</button>`).join('');
      const view = { overview: overviewTab, plan: planTab, blast: blastTab, gates: gatesTab }[activeTab];
      drBody.innerHTML = view(t);
      drBody.scrollTop = prev;
    }

    async function loadTicket(n, isRefresh) {
      try {
        const res = await fetch('/api/ticket/' + encodeURIComponent(n) + (isRefresh ? '?brief=0' : ''), { cache: 'no-store' });
        if (n !== openIssue) return;
        let data;
        if (res.status === 404 || res.status === 400) data = { notFound: true };
        else if (!res.ok) return;
        else data = await res.json();
        // The issue body is fetched once per open; refreshes keep the one already loaded.
        if (isRefresh && ticketData && ticketData.brief && data.brief && data.brief.skipped) data.brief = ticketData.brief;
        const json = JSON.stringify(data);
        if (isRefresh && json === ticketJson) return;
        ticketJson = json;
        ticketData = data;
        renderDrawer(isRefresh);
      } catch (err) {
        console.error('Failed to load ticket:', err);
      }
    }
    function refreshTicket() { if (openIssue !== null) loadTicket(openIssue, true); }

    function openTicket(n, fromHash) {
      if (openIssue === n && overlay.classList.contains('open')) return;
      if (!overlay.classList.contains('open')) lastFocus = document.activeElement;
      openIssue = n;
      activeTab = 'overview';
      ticketData = null;
      ticketJson = '';
      document.getElementById('dr-title').textContent = 'Loading…';
      document.getElementById('dr-badges').innerHTML = '';
      document.getElementById('dr-tabs').innerHTML = '';
      drBody.innerHTML = '<div class="dr-muted">Loading…</div>';
      overlay.classList.add('open');
      drawer.focus();
      if (!fromHash && location.hash !== '#ticket-' + n) location.hash = 'ticket-' + n;
      loadTicket(n, false);
    }

    function closeDrawer(updateHash) {
      if (!overlay.classList.contains('open')) { openIssue = null; return; }
      const n = openIssue;
      overlay.classList.remove('open');
      openIssue = null;
      if (updateHash !== false && /^#ticket-/.test(location.hash)) {
        history.replaceState(null, '', location.pathname + location.search);
      }
      const card = n !== null ? document.querySelector(`.item-card[data-issue="${n}"]`) : null;
      const target = card || (lastFocus && document.contains(lastFocus) ? lastFocus : null);
      if (target) target.focus();
    }

    function syncFromHash() {
      const m = location.hash.match(/^#ticket-(\d+)$/);
      setView(location.hash === '#analytics' ? 'analytics' : location.hash === '#setup' ? 'setup' : 'board');
      if (m) openTicket(parseInt(m[1], 10), true);
      else closeDrawer(false);
    }

    window.addEventListener('hashchange', syncFromHash);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && overlay.classList.contains('open')) { e.preventDefault(); closeDrawer(true); return; }
      if (e.key === 'Tab' && overlay.classList.contains('open')) {
        const f = [...drawer.querySelectorAll('a[href], button, [tabindex="0"]')];
        if (!f.length) { e.preventDefault(); return; }
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === drawer)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeDrawer(true); });
    document.getElementById('dr-close').addEventListener('click', () => closeDrawer(true));
    document.getElementById('dr-tabs').addEventListener('click', (e) => {
      const b = e.target.closest('[data-tab]');
      if (b) { activeTab = b.dataset.tab; drBody.scrollTop = 0; renderDrawer(false); }
    });
    // Delegated on the (rebuilt-every-poll) board container, so cards never lose their handlers.
    const boardEl = document.getElementById('board-container');
    boardEl.addEventListener('click', (e) => {
      if (e.target.closest('a')) return;
      const card = e.target.closest('.item-card');
      if (card && /^\d+$/.test(card.dataset.issue || '')) openTicket(parseInt(card.dataset.issue, 10), false);
    });
    boardEl.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      if (e.target.closest('a')) return;
      const card = e.target.closest('.item-card');
      if (card && /^\d+$/.test(card.dataset.issue || '')) { e.preventDefault(); openTicket(parseInt(card.dataset.issue, 10), false); }
    });

    fetchState();
    setInterval(fetchState, 3000);

    // ---------------------------------------------------------------- live flight deck
    const FD_NODES = [['refine', '01', 'Refine'], ['plan', '02', 'Plan'], ['implement', '03', 'Implement'],
                      ['gates', '04', 'Verify Gates'], ['review', '05', 'Independent Review']];
    const FD_ACTIVE_MS = 2500, FD_IDLE_MS = 10000;
    const fd = {
      el: document.getElementById('flight-deck'), run: null, anchorElapsed: 0, anchorAt: 0,
      busy: false, feedSig: '', stepSig: '', open: false,
    };
    const fdEl = id => document.getElementById(id);
    function fdMmss(s) {
      s = Math.max(0, Math.floor(s));
      return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    }
    function fdCopy(text, btn) {
      const done = () => {
        const prev = btn.dataset.label || btn.textContent;
        btn.dataset.label = prev; btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = btn.dataset.label; }, 1200);
      };
      const fallback = () => {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch (e) { /* ignore */ }
        document.body.removeChild(ta); done();
      };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
      else fallback();
    }
    function fdBuildStepper() {
      const box = fdEl('fd-stepper');
      box.textContent = '';
      FD_NODES.forEach((n, i) => {
        if (i) { const c = document.createElement('span'); c.className = 'fd-conn'; box.appendChild(c); }
        const node = document.createElement('span'); node.className = 'fd-node'; node.dataset.node = n[0];
        const badge = document.createElement('span'); badge.className = 'fd-badge';
        const num = document.createElement('span'); num.textContent = n[1]; badge.appendChild(num);
        const label = document.createElement('span'); label.className = 'fd-label'; label.textContent = n[1] + ' ' + n[2];
        node.appendChild(badge); node.appendChild(label); box.appendChild(node);
      });
    }
    function fdPatchStepper(run) {
      const done = new Set(run.completed_nodes || []);
      const nodes = fdEl('fd-stepper').querySelectorAll('.fd-node');
      const conns = fdEl('fd-stepper').querySelectorAll('.fd-conn');
      nodes.forEach((n, i) => {
        n.classList.toggle('done', done.has(n.dataset.node));
        n.classList.toggle('active', n.dataset.node === run.node);
        if (i) {
          conns[i - 1].classList.toggle('done', done.has(n.dataset.node) || n.dataset.node === run.node);
          conns[i - 1].classList.toggle('active', n.dataset.node === run.node);
        }
      });
    }
    function fdRenderFeed(run) {
      const feed = fdEl('fd-feed');
      const sig = JSON.stringify(run.feed || []);
      if (sig === fd.feedSig) return;
      const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 8 || !fd.feedSig;
      fd.feedSig = sig;
      feed.textContent = '';
      (run.feed || []).forEach(e => {
        const line = document.createElement('div');
        line.className = 'fd-line ' + (['ok', 'fail', 'info'].includes(e.level) ? e.level : 'info');
        const t = typeof e.ts === 'number' ? new Date(e.ts * 1000).toLocaleTimeString() : '';
        line.textContent = t + '  ' + (e.text || '');
        feed.appendChild(line);
      });
      if (atBottom) feed.scrollTop = feed.scrollHeight;
    }
    function fdTick() {
      const run = fd.run;
      if (!run) return;
      const elapsed = fd.anchorElapsed + (performance.now() - fd.anchorAt) / 1000;
      const b = run.benchmark || {};
      const stalled = !!b.stalled || (b.p99 !== null && b.p99 !== undefined && (b.samples || 0) >= 5 && elapsed > b.p99);
      const t = fdMmss(elapsed);
      if (fdEl('fd-time').textContent !== t) fdEl('fd-time').textContent = t;
      fdEl('fd-compact').textContent = '#' + run.issue + ' ' + (FD_NODES.find(n => n[0] === run.node) || ['', '', ''])[2] + ' ';
      const timer = fdEl('fd-timer');
      timer.classList.toggle('amber', stalled);
      timer.classList.toggle('stalled', !!b.stalled);
    }
    function renderDeck(data) {
      const runs = (data && data.runs) || [];
      const deck = fd.el;
      if (!runs.length) { fd.run = null; deck.hidden = true; deck.classList.remove('active'); return; }
      const run = runs[0];
      const wasHidden = deck.hidden;
      deck.hidden = false; deck.classList.add('active');
      if (wasHidden || fd.stepSig !== 'built') { fdBuildStepper(); fd.stepSig = 'built'; }
      if (!fd.run || fd.run.issue !== run.issue) { fd.feedSig = ''; }
      fd.run = run; fd.anchorElapsed = run.elapsed_s || 0; fd.anchorAt = performance.now();
      fdEl('fd-issue').textContent = '#' + run.issue;
      fdEl('fd-title').textContent = run.title || '';
      const label = (FD_NODES.find(n => n[0] === run.node) || ['', '', run.node || ''])[2];
      fdEl('fd-stage').textContent = label;
      const more = fdEl('fd-more');
      more.hidden = runs.length < 2; more.textContent = '+' + (runs.length - 1) + ' more';
      fdPatchStepper(run);
      fdEl('fd-agent').textContent = run.agent || '—';
      const wt = fdEl('fd-worktree'), br = fdEl('fd-branch');
      wt.textContent = 'worktree: ' + (run.worktree || '—'); wt.dataset.label = wt.textContent; wt.dataset.value = run.worktree || '';
      br.textContent = 'branch: ' + (run.branch || '—') + (run.commit ? ' @ ' + run.commit : '');
      br.dataset.label = br.textContent; br.dataset.value = run.branch || '';
      const b = run.benchmark || {};
      fdEl('fd-bench').textContent = (b.p50 !== null && b.p50 !== undefined && b.p99 !== null && b.p99 !== undefined)
        ? 'p50 ' + fdMmss(b.p50) + ' · p99 ' + fdMmss(b.p99) : '';
      fdRenderFeed(run);
      fdTick();
    }
    fdEl('fd-issue').addEventListener('click', () => { if (fd.run) openTicket(fd.run.issue, false); });
    fdEl('fd-worktree').addEventListener('click', e => { if (e.currentTarget.dataset.value) fdCopy(e.currentTarget.dataset.value, e.currentTarget); });
    fdEl('fd-branch').addEventListener('click', e => { if (e.currentTarget.dataset.value) fdCopy(e.currentTarget.dataset.value, e.currentTarget); });
    fdEl('fd-toggle').addEventListener('click', () => {
      fd.open = !fd.open;
      fdEl('fd-feed').hidden = !fd.open;
      fdEl('fd-toggle').setAttribute('aria-expanded', String(fd.open));
      fdEl('fd-toggle').textContent = (fd.open ? '▾' : '▸') + ' Activity';
      if (fd.open) fdEl('fd-feed').scrollTop = fdEl('fd-feed').scrollHeight;
    });
    async function pollLive() {
      if (fd.busy) return;
      fd.busy = true;
      let active = !!fd.run;
      try {
        const res = await fetch('/api/live');
        if (!res.ok) throw new Error('live API error: ' + res.status);
        const data = await res.json();
        renderDeck(data);
        active = !!(data.runs && data.runs.length);
      } catch (err) {
        console.error('Failed to fetch live runs:', err); // keep the last good view
      } finally {
        fd.busy = false;
        setTimeout(pollLive, active ? FD_ACTIVE_MS : FD_IDLE_MS);
      }
    }
    setInterval(fdTick, 1000);
    pollLive();

    // ---------------------------------------------------------------- analytics view
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const AN_WINDOWS = ['24h', '7d', 'all'];
    const an = { window: 'all', seq: 0, view: 'board' };
    const DASH = '—';
    function fmtDuration(s) {
      if (s === null || s === undefined || !isFinite(s)) return DASH;
      if (s < 1) return s.toFixed(1) + 's';
      let t = Math.round(s);
      if (t < 60) return t + 's';
      const m = Math.floor(t / 60), sec = t % 60;
      if (m < 60) return m + 'm ' + sec + 's';
      return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
    }
    function fmtPct(r) {
      if (r === null || r === undefined || !isFinite(r)) return DASH;
      return (Math.round(r * 1000) / 10) + '%';
    }
    function fmtNum(n) { return n === null || n === undefined || !isFinite(n) ? DASH : String(Math.round(n * 100) / 100); }
    function svgEl(tag, attrs, text) {
      const e = document.createElementNS(SVG_NS, tag);
      for (const k in (attrs || {})) e.setAttribute(k, String(attrs[k]));
      if (text !== undefined) e.textContent = String(text);
      return e;
    }
    function htmlEl(tag, cls, text) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text !== undefined) e.textContent = String(text);
      return e;
    }
    function anMessage(msg) {
      const box = fdEl('an-message');
      box.hidden = !msg;
      box.textContent = msg || '';
    }
    function setKpi(id, value, sub) {
      const card = fdEl(id);
      card.querySelector('[data-role="value"]').textContent = value;
      card.querySelector('[data-role="sub"]').textContent = sub;
    }
    function renderKpis(k) {
      const n = x => (x && x.samples !== undefined ? 'n = ' + x.samples : '');
      setKpi('kpi-cycle', fmtDuration(k.median_cycle_seconds.value), n(k.median_cycle_seconds) + ' merged tickets');
      setKpi('kpi-firstpass', fmtPct(k.first_pass_approval_rate.value), n(k.first_pass_approval_rate) + ' merged tickets');
      setKpi('kpi-short', fmtPct(k.blast_short_circuit_rate.value), n(k.blast_short_circuit_rate) + ' gate orders');
      const st = k.total_stalls.value;
      setKpi('kpi-stalls', st === null || st === undefined ? DASH : String(st), 'stall.detected events');
    }
    function renderStages(stages, bottleneck) {
      const svg = fdEl('stage-chart');
      svg.textContent = '';
      const body = fdEl('stage-table').querySelector('tbody');
      body.textContent = '';
      const any = stages.some(s => s.count > 0);
      if (!any) {
        svg.appendChild(svgEl('text', { x: 320, y: 110, 'text-anchor': 'middle', fill: '#8b949e', 'font-size': 14 }, 'No stage spans in this window'));
      }
      const defs = svgEl('defs');
      const pat = svgEl('pattern', { id: 'hatch', width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' });
      pat.appendChild(svgEl('rect', { width: 6, height: 6, fill: '#d29922' }));
      pat.appendChild(svgEl('line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: '#0d1117', 'stroke-width': 2 }));
      defs.appendChild(pat);
      svg.appendChild(defs);
      const maxV = Math.max(1e-9, ...stages.map(s => Math.max(s.p99 || 0, s.p50 || 0)));
      const left = 110, width = 440;
      stages.forEach((s, i) => {
        const y = 12 + i * 40;
        const hot = s.stage === bottleneck;
        svg.appendChild(svgEl('text', { x: 6, y: y + 18, fill: hot ? '#e3b341' : '#c9d1d9', 'font-size': 13, 'font-weight': hot ? 700 : 400 }, s.stage));
        svg.appendChild(svgEl('rect', { x: left, y: y, width: width, height: 24, fill: '#21262d', rx: 3 }));
        if (s.count > 0) {
          const w = Math.max(2, (s.p50 / maxV) * width);
          svg.appendChild(svgEl('rect', { x: left, y: y, width: w, height: 24, rx: 3, fill: hot ? 'url(#hatch)' : '#58a6ff', stroke: hot ? '#e3b341' : 'none', 'stroke-width': 1.5 }));
          const px = left + (s.p99 / maxV) * width;
          svg.appendChild(svgEl('line', { x1: px, y1: y - 2, x2: px, y2: y + 26, stroke: '#c9d1d9', 'stroke-width': 2 }));
          svg.appendChild(svgEl('text', { x: Math.min(left + width - 2, Math.max(left + w, px) + 6), y: y + 17, fill: '#c9d1d9', 'font-size': 12 }, fmtDuration(s.p50) + (hot ? '  ▲ bottleneck' : '')));
        } else {
          svg.appendChild(svgEl('text', { x: left + 8, y: y + 17, fill: '#8b949e', 'font-size': 12 }, DASH + ' no samples'));
        }
      });
      stages.forEach(s => {
        const tr = document.createElement('tr');
        if (s.stage === bottleneck) tr.className = 'bottleneck';
        const first = document.createElement('td');
        first.textContent = s.stage;
        if (s.stage === bottleneck) first.appendChild(htmlEl('span', 'an-tag warn', 'Bottleneck'));
        if (s.count > 0 && s.low_samples) first.appendChild(htmlEl('span', 'an-tag low', 'low n'));
        tr.appendChild(first);
        [String(s.count), fmtDuration(s.p50), fmtDuration(s.p90), fmtDuration(s.p99), fmtDuration(s.mean)].forEach(v => tr.appendChild(htmlEl('td', '', v)));
        body.appendChild(tr);
      });
    }
    function renderRadar(stalls) {
      const svg = fdEl('stall-radar'), list = fdEl('stall-list');
      svg.textContent = ''; list.textContent = '';
      const rows = stalls.stages || [];
      const cx = 160, cy = 140, R = 90, n = rows.length || 1;
      const maxRate = Math.max(0.05, ...rows.map(r => r.rate || 0));
      const pt = (i, f) => [cx + Math.sin(2 * Math.PI * i / n) * R * f, cy - Math.cos(2 * Math.PI * i / n) * R * f];
      [0.33, 0.66, 1].forEach(f => svg.appendChild(svgEl('polygon', { points: rows.map((_, i) => pt(i, f).join(',')).join(' '), fill: 'none', stroke: '#30363d', 'stroke-width': 1 })));
      rows.forEach((r, i) => svg.appendChild(svgEl('line', { x1: cx, y1: cy, x2: pt(i, 1)[0], y2: pt(i, 1)[1], stroke: '#30363d', 'stroke-width': 1 })));
      svg.appendChild(svgEl('polygon', { points: rows.map((r, i) => pt(i, (r.rate || 0) / maxRate).join(',')).join(' '), fill: 'rgba(88,166,255,0.25)', stroke: '#58a6ff', 'stroke-width': 2 }));
      rows.forEach((r, i) => {
        const [x, y] = pt(i, (r.rate || 0) / maxRate);
        if (r.elevated) {
          svg.appendChild(svgEl('polygon', { points: [x, y - 8, x + 8, y + 6, x - 8, y + 6].join(','), fill: '#f0883e', stroke: '#fff', 'stroke-width': 1 }));
        } else {
          svg.appendChild(svgEl('circle', { cx: x, cy: y, r: 4, fill: '#58a6ff' }));
        }
        const [lx, ly] = pt(i, 1.28);
        svg.appendChild(svgEl('text', { x: lx, y: ly + 4, 'text-anchor': 'middle', fill: r.elevated ? '#f0883e' : '#c9d1d9', 'font-size': 12, 'font-weight': r.elevated ? 700 : 400 }, (r.elevated ? '⚠ ' : '') + r.stage));
        const li = htmlEl('li', r.elevated ? 'elevated' : '');
        li.appendChild(htmlEl('span', '', r.stage + (r.elevated ? ' — ELEVATED' : '')));
        li.appendChild(htmlEl('span', '', r.count + ' stall' + (r.count === 1 ? '' : 's') + ' · ' + fmtPct(r.rate)));
        list.appendChild(li);
      });
      fdEl('stall-total').textContent = 'Total stalls: ' + (stalls.total === undefined ? DASH : stalls.total) + '. Elevated means at least one stall and a rate above 1% of that stage’s spans.';
    }
    function hbars(box, items, cls) {
      const max = Math.max(1, ...items.map(i => i.count));
      items.forEach(i => {
        const row = htmlEl('div', 'an-hbar');
        const lbl = htmlEl('span', 'lbl', i.label); lbl.title = i.label;
        const track = htmlEl('span', 'track'); const fill = htmlEl('span', 'fill' + (i.cls ? ' ' + i.cls : ''));
        fill.style.width = (i.count / max * 100) + '%'; track.appendChild(fill);
        row.appendChild(lbl); row.appendChild(track); row.appendChild(htmlEl('span', '', i.count));
        box.appendChild(row);
      });
    }
    function renderFriction(f) {
      const rev = fdEl('friction-review'), gates = fdEl('friction-gates');
      rev.textContent = ''; gates.textContent = '';
      const r = f.review || {};
      if (!r.tickets) rev.appendChild(htmlEl('div', 'an-note', 'No review findings recorded in this window.'));
      else {
        hbars(rev, [{ label: 'Blocking findings', count: r.blocking, cls: 'blocking' }, { label: 'Nitpicks', count: r.nitpick, cls: 'nitpick' }]);
        rev.appendChild(htmlEl('div', 'an-note', 'Across ' + r.tickets + ' reviewed ticket' + (r.tickets === 1 ? '' : 's') + '.'));
      }
      const g = f.gate_failures || [];
      if (!g.length) gates.appendChild(htmlEl('div', 'an-note', 'No gate failures recorded in this window.'));
      else hbars(gates, g.map(x => ({ label: x.label, count: x.count })));
    }
    function renderBlast(b) {
      fdEl('blast-avg').textContent = fmtNum(b.avg_hop1_callers);
      fdEl('blast-coverage').textContent = fmtPct(b.coverage_ratio);
      fdEl('blast-tickets').textContent = b.tickets_with_impact === undefined ? DASH : String(b.tickets_with_impact);
    }
    function renderAnalytics(data) {
      renderKpis(data.kpis);
      renderStages(data.stages, data.bottleneck);
      renderRadar(data.stalls);
      renderFriction(data.friction);
      renderBlast(data.blast);
      if (!data.telemetry.available) anMessage('No telemetry recorded yet. Enable telemetry to see stage latency, stalls and short-circuit rates; review, gate and blast metrics come from the ticket store.');
      else if (!data.telemetry.spans) anMessage('No spans fall inside this timeframe. Try a wider window.');
      else anMessage('');
    }
    async function loadAnalytics() {
      const seq = ++an.seq;
      try {
        const res = await fetch('/api/analytics?window=' + encodeURIComponent(an.window), { cache: 'no-store' });
        const data = await res.json();
        if (seq !== an.seq) return;
        if (!res.ok) throw new Error(data && data.error ? data.error : 'HTTP ' + res.status);
        renderAnalytics(data);
      } catch (err) {
        if (seq === an.seq) anMessage('Analytics unavailable: ' + (err && err.message ? err.message : err));
      }
    }
    function setWindow(w) {
      if (!AN_WINDOWS.includes(w)) return;
      an.window = w;
      document.querySelectorAll('#an-window .win-btn').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.window === w)));
      loadAnalytics();
    }
    function setView(view) {
      if (an.view === view) return;
      an.view = view;
      document.body.classList.toggle('view-analytics', view === 'analytics');
      document.body.classList.toggle('view-setup', view === 'setup');
      fdEl('analytics-view').hidden = view !== 'analytics';
      fdEl('setup-view').hidden = view !== 'setup';
      document.querySelectorAll('.nav-btn').forEach(b => {
        if (b.dataset.view === view) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
      });
      if (view === 'analytics') loadAnalytics();
      if (view === 'setup') loadSetup();
    }
    document.querySelectorAll('.nav-btn').forEach(b => b.addEventListener('click', () => {
      if (b.dataset.view === 'analytics') { if (location.hash !== '#analytics') location.hash = 'analytics'; else setView('analytics'); }
      else if (b.dataset.view === 'setup') { if (location.hash !== '#setup') location.hash = 'setup'; else setView('setup'); }
      else { history.replaceState(null, '', location.pathname + location.search); syncFromHash(); }
    }));
    document.querySelectorAll('#an-window .win-btn').forEach(b => b.addEventListener('click', () => setWindow(b.dataset.window)));
    fdEl('an-refresh').addEventListener('click', loadAnalytics);
    async function loadSetup() {
      const summary = fdEl('setup-summary'), list = fdEl('setup-steps');
      summary.textContent = 'Checking local connections…';
      try {
        const res = await fetch('/api/setup', { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Setup check failed');
        list.replaceChildren();
        for (const item of data.steps) {
          const row = document.createElement('div'); row.className = 'setup-row';
          const state = document.createElement('span'); state.className = 'setup-state' + (item.ready ? ' ready' : '');
          state.textContent = item.ready ? 'READY' : item.required ? 'ACTION NEEDED' : 'OPTIONAL';
          const content = document.createElement('div');
          const title = document.createElement('strong'); title.textContent = item.name;
          const detail = document.createElement('p'); detail.textContent = item.detail;
          const command = document.createElement('code'); command.textContent = item.command;
          content.append(title, detail);
          if (!item.ready) content.append(command);
          row.append(state, content); list.append(row);
        }
        summary.textContent = data.ready ? data.repo + ' is ready for a dry run.' : data.steps.filter(s => s.required && s.ready).length + ' of ' + data.steps.filter(s => s.required).length + ' required checks ready for ' + data.repo + '.';
      } catch (err) { summary.textContent = 'Setup check failed: ' + err.message; }
    }
    fdEl('setup-refresh').addEventListener('click', loadSetup);
    if (location.hash === '#analytics') setView('analytics');
    if (location.hash === '#setup') setView('setup');

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
                    "lifecycle": LIFECYCLE.get(t.state, ""),
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


def _repo_slug(root: Path) -> str:
    try:
        cfg = load_config(root)
        return f"{cfg.board.owner}/{cfg.name}" if cfg.board.owner else cfg.name
    except ConfigError:
        return root.name or "devflow"


def _strip_reply(value: Any) -> Any:
    """Drop raw agent `reply` text at any depth."""
    if isinstance(value, dict):
        return {k: _strip_reply(v) for k, v in value.items() if k != "reply"}
    if isinstance(value, list):
        return [_strip_reply(v) for v in value]
    return value


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _plan_section(plan: Any, test_plan: Any) -> dict[str, Any]:
    plan, test_plan = _dict(plan), _dict(test_plan)
    if not plan and not test_plan:
        return {}
    reasoning = next((plan[k] for k in ("reasoning", "rationale", "analysis")
                      if isinstance(plan.get(k), str) and plan[k].strip()), None)
    return _strip_reply({
        "goal": plan.get("goal", ""),
        "acceptance_criteria": _list(plan.get("acceptance_criteria")),
        "steps": _list(plan.get("steps")),
        "out_of_scope": _list(plan.get("out_of_scope")),
        "risks": _list(plan.get("risks")),
        "test_strategy": _list(test_plan.get("tests")),
        "uncovered_criteria": _list(test_plan.get("uncovered_criteria")),
        "reasoning": reasoning[:4000] if reasoning else None,
    })


def _blast_section(impact: Any, impacted_tests: Any = None) -> dict[str, Any]:
    if not isinstance(impact, dict) or not impact:
        return {"status": "unavailable", "symbols": [], "callers": [], "dependent_modules": [],
                "uncovered": [], "impacted_tests": []}
    files = {f for f in _list(impact.get("files")) if isinstance(f, str)}
    callers: list[dict[str, Any]] = []
    for hop, cs in sorted(_dict(impact.get("callers")).items()):
        for c in _list(cs):
            c = _dict(c)
            if str(hop) != "1":
                continue
            file = c.get("file")
            verified = file in files or (isinstance(file, str) and "test" in file.lower())
            callers.append({"symbol": c.get("symbol"), "file": file, "hop": c.get("hop", 1),
                            "relation": c.get("relation"), "verified": bool(verified)})
    tests = _list(impact.get("impacted_tests")) or _list(impacted_tests)
    return {"status": "available", "graph_state": impact.get("graph_state"),
            "files": sorted(files), "symbols": [_dict(x) for x in _list(impact.get("symbols"))],
            "callers": callers,
            "dependent_modules": [_dict(x) for x in _list(impact.get("dependent_modules"))],
            "uncovered": _list(impact.get("uncovered")), "impacted_tests": tests}


def _gate_attempt(row: dict[str, Any]) -> dict[str, Any]:
    p = _dict(row.get("payload"))
    return _strip_reply({
        "attempt": row.get("attempt"), "ok": bool(row.get("ok")), "ts": row.get("ts"),
        "summary": p.get("summary", ""), "unconfigured": _list(p.get("unconfigured")),
        "findings": _list(p.get("findings")), "results": _list(p.get("results")),
        "failures": _list(p.get("failures"))})


def _review_section(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    p = _dict(row.get("payload"))
    keep = ("agent", "independent", "sha", "verdict", "approved", "findings",
            "engine_gates_ok", "facts", "notes")
    out = {k: p[k] for k in keep if k in p}
    out["attempt"] = row.get("attempt")
    return _strip_reply(out)


def read_trace(root: Path, issue: int) -> list[dict[str, Any]]:
    """Span names, durations and outcomes for one issue; empty on a missing or broken log."""
    from .stall import _attrs, _is_synthetic, _seconds
    from .telemetry_cli import _matches_issue

    path = root / ".devflow" / "state" / "traces.jsonl"
    spans: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict) or _is_synthetic(rec) or not _matches_issue(rec, issue):
                        continue
                    attrs = _attrs(rec)
                    outcome = attrs.get("devflow.result", rec.get("status"))
                    spans.append({"name": str(rec.get("name", "")), "duration_s": _seconds(rec),
                                  "outcome": outcome if isinstance(outcome, (str, int, float)) else None})
                except Exception:  # noqa: BLE001, S112 - skip a bad record
                    continue
                if len(spans) >= MAX_TRACE_SPANS:
                    break
    except OSError:
        return []
    return spans


def _default_host_factory(slug: str):
    from .host import GhHost
    return GhHost(slug)


def _fetch_brief(slug: str, issue: int, host_factory) -> dict[str, Any]:
    """The issue body from the host, bounded by a timeout; never raises."""
    unavailable: dict[str, Any] = {"body": None, "available": False}
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["body"] = host_factory(slug).get_issue(issue).body
        except Exception:  # noqa: BLE001
            box.clear()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(HOST_TIMEOUT_S)
    body = box.get("body")
    return {"body": body, "available": True} if isinstance(body, str) else unavailable


def collect_ticket(root: Path, issue: int, *, host_factory=None,
                   with_brief: bool = True) -> dict[str, Any] | None:
    """Everything the inspector drawer shows for one ticket, or None when unknown."""
    db = state_db_path(root)
    if not db.exists():
        return None
    with Store(db) as store:
        ticket = next((t for t in store.list_tickets() if t.issue == issue), None)
        if ticket is None:
            return None
        rows = {k: store.stage_results(ticket.id, k)
                for k in ("plan", "test_plan", "gates", "review", "implement")}
        events = store.events(ticket.id, "transition")
    last = {k: (v[-1] if v else None) for k, v in rows.items()}
    payload = {k: _dict(v["payload"]) if v else {} for k, v in last.items()}
    slug = _repo_slug(root)
    base = f"https://github.com/{slug}"
    pr = ticket.sessions.get("pr")
    gates_latest = payload["gates"]
    agent = payload["implement"].get("agent") or ticket.sessions.get("coder_agent") or ""
    history = [{"to": e.get("to"), "actor": e.get("actor"), "reason": e.get("reason", ""),
                "ts": e.get("ts")} for e in events] or _strip_reply(
        [h for h in ticket.history if isinstance(h, dict)])
    review = _review_section(last["review"])
    return {
        "issue": issue, "title": ticket.title, "state": ticket.state.value,
        "lifecycle": LIFECYCLE.get(ticket.state, ""), "branch": ticket.branch,
        "worktree": ticket.worktree, "attempts": ticket.implement_attempts,
        "review_round": ticket.review_round, "agent": agent,
        "urls": {"issue": f"{base}/issues/{issue}",
                 "pr": f"{base}/pull/{pr}" if pr else None},
        "history": history,
        "brief": (_fetch_brief(slug, issue, host_factory or _default_host_factory) if with_brief
                  else {"body": None, "available": False, "skipped": True}),
        "plan": _plan_section(payload["plan"], payload["test_plan"]),
        "blast_radius": _blast_section(gates_latest.get("impact")
                                       or _dict(payload["review"]).get("impact"),
                                       gates_latest.get("impacted_tests")),
        "gates": {"attempts": [_gate_attempt(r) for r in rows["gates"]],
                  "latest": _gate_attempt(last["gates"]) if last["gates"] else {},
                  "review": review},
        "review": review,
        "trace": read_trace(root, issue),
    }


# ---------------------------------------------------------------- live flight deck
NODES = ["refine", "plan", "implement", "gates", "review"]
STATE_NODE = {State.REFINING: "refine", State.TEST_DESIGN: "plan", State.IMPLEMENTING: "implement",
              State.GATES: "gates", State.IN_REVIEW: "review"}
ACTIVE_STATES = frozenset(STATE_NODE)
AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex", "openclaw": "OpenClaw Planner"}
FEED_MAX = 20


def _stage_benchmark(root: Path, state: State, elapsed: float) -> dict[str, Any]:
    from . import stall
    path = root / ".devflow" / "state" / "traces.jsonl"
    durations = stall.baseline_durations(stall.read_log_records(path), f"stage.{state.value}")
    n = len(durations)
    if n < stall.MIN_SAMPLES:
        return {"p50": None, "p99": None, "samples": n, "stalled": False}
    p50, p99 = stall.percentile(durations, 50), stall.percentile(durations, 99)
    return {"p50": round(p50, 3), "p99": round(p99, 3), "samples": n,
            "stalled": stall.check_stall(f"stage.{state.value}", elapsed, durations) is not None}


def _gate_text(r: dict[str, Any]) -> tuple[str, str]:
    cmd = str(r.get("command") or f"{r.get('project', '')}:{r.get('gate', '')}")
    dur = r.get("duration_s")
    took = f" in {float(dur):.1f}s" if isinstance(dur, (int, float)) else ""
    ok = r.get("exit_code") == 0 and not r.get("timed_out")
    return ("ok" if ok else "fail"), f"Gate: {cmd} ran{took} ({'PASS' if ok else 'FAIL'})"


def _feed(events: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest FEED_MAX sanitized entries, oldest first. Only whitelisted fields are read."""
    out: list[dict[str, Any]] = []

    def put(ts: Any, level: str, text: str) -> None:
        out.append({"ts": ts, "level": level, "text": text[:300]})

    for e in _strip_reply(events):
        kind = e.get("kind")
        if kind == "transition":
            reason = f" ({e['reason']})" if e.get("reason") else ""
            put(e.get("ts"), "info", f"{e.get('actor', 'engine')}: moved to {e.get('to', '?')}{reason}")
        elif kind == "error":
            put(e.get("ts"), "fail", f"Error in {e.get('state', '?')}: {e.get('error', '')}")
    for r in _strip_reply(results):
        p, stage, ts = _dict(r.get("payload")), r.get("stage"), r.get("ts")
        if stage == "plan":
            put(ts, "ok" if r.get("ok") else "fail",
                f"Plan {'ready' if r.get('ok') else 'rejected'} (attempt {r.get('attempt')})")
        elif stage == "gates":
            for g in _list(p.get("results")):
                level, text = _gate_text(_dict(g))
                put(ts, level, text)
        elif stage == "review":
            verdict = p.get("verdict") or ("approved" if p.get("approved") else "changes requested")
            put(ts, "ok" if r.get("ok") else "fail", f"Review verdict: {verdict}")
    out.sort(key=lambda x: x["ts"] if isinstance(x["ts"], (int, float)) else 0)
    return out[-FEED_MAX:]


def _head_sha(worktree: str) -> str | None:
    import subprocess
    if not worktree or not Path(worktree).is_dir():
        return None
    try:
        proc = subprocess.run(["git", "-C", worktree, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and sha else None


def _live_run(root: Path, store: Store, t: Any, now: float) -> dict[str, Any]:
    node = STATE_NODE[t.state]
    started = store.latest_transition_ts(t.id, t.state)
    if started is None:
        started = store.ticket_updated_at(t.id)
    elapsed = max(0.0, now - started) if started is not None else 0.0
    # The agent is unknown during the first call of a stage: usage is recorded after the call.
    usage = store.latest_usage(t.id)
    raw = usage["agent"] if usage and usage.get("agent") else None
    if raw is None:
        kind = {State.IMPLEMENTING: "implement", State.IN_REVIEW: "review"}.get(t.state)
        rows = store.stage_results(t.id, kind) if kind else []
        raw = (_dict(rows[-1]["payload"]).get("agent") if rows else None) \
            or t.sessions.get("coder_agent") or None
    raw = str(raw) if raw else None
    results = [r for k in ("plan", "gates", "review") for r in store.stage_results(t.id, k)]
    return {
        "issue": t.issue, "title": t.title, "state": t.state.value, "node": node,
        "completed_nodes": NODES[:NODES.index(node)], "stage_started_at": started,
        "elapsed_s": round(elapsed, 3), "agent": AGENT_LABELS.get(raw, raw) if raw else None,
        "agent_raw": raw, "branch": t.branch, "worktree": t.worktree,
        "commit": _head_sha(t.worktree) or (t.base_sha[:7] if t.base_sha else None),
        "attempt": t.implement_attempts, "review_round": t.review_round,
        "benchmark": _stage_benchmark(root, t.state, elapsed),
        "feed": _feed(store.events(t.id), results),
    }


def collect_live(root: Path, *, now: float | None = None) -> dict[str, Any]:
    """Read-only snapshot of in-flight tickets; never creates the db and skips a bad ticket."""
    import time

    from .runner import runner_status
    now = time.time() if now is None else now
    runs: list[tuple[float, dict[str, Any]]] = []
    db = state_db_path(root)
    if db.exists():
        with Store(db) as store:
            for t in store.list_tickets(*ACTIVE_STATES):
                try:
                    runs.append((store.ticket_updated_at(t.id) or 0.0, _live_run(root, store, t, now)))
                except Exception:  # noqa: BLE001, S112 - one bad ticket must not hide the rest
                    continue
    runs.sort(key=lambda x: x[0], reverse=True)
    return {"now": now, "runner": runner_status(root), "runs": [r for _, r in runs]}


class UiHandler(http.server.BaseHTTPRequestHandler):
    root: Path
    host_factory = staticmethod(_default_host_factory)

    def _json(self, code: int, data: Any, strict: bool = False) -> None:
        payload = json.dumps(data, allow_nan=not strict).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def _ticket(self, raw: str, with_brief: bool = True) -> None:
        if not raw.isascii() or not raw.isdigit() or int(raw) <= 0:
            self._json(400, {"error": "ticket id must be a positive integer"})
            return
        try:
            data = collect_ticket(self.root, int(raw), host_factory=self.host_factory,
                                  with_brief=with_brief)
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"error": str(exc)})
            return
        if data is None:
            self._json(404, {"error": f"no ticket for issue {raw}"})
        else:
            self._json(200, data)

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        if path.startswith("/api/ticket/") and len(path) > len("/api/ticket/"):
            query = urllib.parse.parse_qs(parts.query)
            self._ticket(path[len("/api/ticket/"):], query.get("brief") != ["0"])
            return
        if path == "/api/analytics":
            query = urllib.parse.parse_qs(parts.query)
            try:
                window = analytics.parse_window((query.get("window") or [""])[0])
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            try:
                self._json(200, analytics.collect_analytics(self.root, window), strict=True)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/api/live":
            try:
                self._json(200, collect_live(self.root))
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
        if path == "/api/setup":
            from .onboarding import collect_setup
            try:
                self._json(200, collect_setup(self.root), strict=True)
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
            return
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

        if self.path == "/favicon.ico":
            # No icon to serve; answering 204 keeps browsers from logging a 404 on every load.
            self.send_response(204)
            self.end_headers()
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
