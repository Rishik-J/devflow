"""Local web dashboard to track ticket stages and devflow queue progress."""

from __future__ import annotations

import argparse
import http.server
import json
import re
import secrets
import socketserver
import sys
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from typing import Any

from . import command_center
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
    .cc { display: none; grid-template-columns: 300px 1fr; gap: 16px; align-items: start; }
    .cc-list { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    .cc-item { padding: 12px 14px; border-bottom: 1px solid var(--border); cursor: pointer; }
    .cc-item:last-child { border-bottom: 0; }
    .cc-item:hover, .cc-item.sel { background: #1c2128; }
    .cc-item .t { font-size: 13px; color: var(--heading); margin: 2px 0; }
    .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; background: #6e7681; }
    .tone-ok { background: #3fb950; } .tone-warn { background: var(--yellow); }
    .tone-bad { background: var(--red); } .tone-info { background: var(--accent); }
    .diag { border-left: 3px solid var(--accent); }
    .diag.warn { border-left-color: var(--yellow); } .diag.bad { border-left-color: var(--red); }
    .diag.ok { border-left-color: #3fb950; }
    .diag h2 { font-size: 18px; color: var(--heading); margin-bottom: 6px; }
    .needs { margin-top: 12px; padding: 10px 12px; background: #0d1117; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
    .needs code, .evidence code { background: #21262d; padding: 2px 6px; border-radius: 4px; color: var(--heading); }
    .evidence { margin: 12px 0 0 18px; font-size: 12.5px; color: #8b949e; }
    .flow { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
    .flow-inputs, .flow-out { display: flex; gap: 14px; font-size: 12px; color: #8b949e; flex-wrap: wrap; }
    .flow-inputs small, .flow-out small { color: #6e7681; }
    .flow-nodes { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; margin: 14px 0; }
    .fnode { position: relative; background: #1c2128; border: 1px solid var(--border); padding: 12px; margin-right: 22px;
      border-radius: 8px; text-align: left; min-height: 84px; }
    .fnode:last-child { margin-right: 0; }
    .fnode:not(:last-child)::after { content: '→'; position: absolute; right: -19px; top: 50%; transform: translateY(-50%); color: #6e7681; }
    .fnode .n { font-size: 11px; color: #6e7681; }
    .fnode strong { display: block; color: var(--heading); font-size: 14px; }
    .fnode small { color: #8b949e; font-size: 12px; }
    .fnode .st { position: absolute; top: 8px; right: 10px; font-size: 11px; color: #8b949e; }
    .fnode.done { border-color: rgba(63,185,80,.5); } .fnode.done .st { color: #3fb950; }
    .fnode.working { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
    .fnode.waiting_human, .fnode.stalled { border-color: var(--yellow); box-shadow: 0 0 0 1px var(--yellow); }
    .fnode.blocked, .fnode.escalated { border-color: var(--red); box-shadow: 0 0 0 1px var(--red); }
    .fnode.pending { opacity: .55; }
    .fnode.current .st { font-weight: 600; color: var(--heading); }
    .runlog { background: #0d1117; border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-top: 10px;
      font: 12px/1.5 ui-monospace, Menlo, monospace; max-height: 220px; overflow: auto; white-space: pre-wrap; }
    .btn { background: #238636; color: #fff; border: 1px solid rgba(240,246,252,.1); padding: 7px 14px; border-radius: 6px;
      font-size: 13px; font-weight: 500; cursor: pointer; margin-right: 8px; }
    .btn.secondary { background: #21262d; color: var(--heading); border-color: var(--border); }
    .btn:disabled { opacity: .45; cursor: not-allowed; }
    .cc-main > div { margin-bottom: 16px; }
    @media (max-width: 900px) { .cc { grid-template-columns: 1fr; } .flow-nodes { grid-template-columns: 1fr; gap: 10px; }
      .fnode { margin-right: 0; } .fnode::after { display: none; } }
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
    <button class="tab" id="tab-cc" onclick="showView('cc')">Command Center</button>
    <button class="tab" id="tab-analytics" onclick="showView('analytics')">Analytics</button>
  </div>

  <div id="board-view">
  <div id="board-banner" class="a-card" style="display:none; margin-bottom:16px; border-left:3px solid var(--yellow)"></div>
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

  <div class="cc" id="cc-view">
    <div class="cc-list" id="cc-list"></div>
    <div class="cc-main" id="cc-main"><div class="muted">Select a ticket.</div></div>
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
    const TOKEN = '__DEVFLOW_TOKEN__';
    let ccSel = null;
    const STATUS_TEXT = {working: 'working', waiting_human: 'needs you', stalled: 'stalled', blocked: 'blocked',
      escalated: 'escalated', done: 'done', cancelled: 'cancelled', visited: 'visited', pending: 'upcoming'};

    async function fetchCC() {
      try {
        const list = await (await fetch('/api/tickets')).json();
        const box = document.getElementById('cc-list');
        if (ccSel === null && list.length) ccSel = list[0].issue;
        box.innerHTML = list.length ? list.map(t => `<div class="cc-item ${t.issue === ccSel ? 'sel' : ''}" onclick="selectTicket(${t.issue})">` +
          `<span class="dot tone-${escapeHtml(t.tone)}"></span><strong>#${t.issue}</strong> <span class="muted">${escapeHtml(STATUS_TEXT[t.status] || t.status)} · ${fmtDur(t.since_seconds)}</span>` +
          `<div class="t">${escapeHtml(t.title)}</div><div class="muted">${escapeHtml(t.headline)}</div></div>`).join('') :
          '<div class="cc-item muted">No tickets in the local store yet.</div>';
        if (ccSel !== null) await fetchCCDetail();
      } catch (err) { console.error('Failed to fetch tickets:', err); }
    }
    function selectTicket(n) { ccSel = n; fetchCC(); }

    async function fetchCCDetail() {
      const res = await fetch('/api/tickets/' + ccSel);
      if (!res.ok) { document.getElementById('cc-main').innerHTML = '<div class="muted">Ticket not found.</div>'; return; }
      renderCC(await res.json());
    }

    function copyText(text) { try { navigator.clipboard.writeText(text); } catch (e) {} }

    function renderCC(d) {
      const g = d.diagnosis, loops = d.graph.loops;
      const needs = g.needs ? `<div class="needs">Needs <strong>${escapeHtml(g.needs.who)}</strong>: comment <code>${escapeHtml(g.needs.command)}</code> on ${escapeHtml(g.needs.where)}.` +
        (g.needs.note ? `<div class="muted" style="margin-top:4px">${escapeHtml(g.needs.note)}</div>` : '') +
        ` <button class="btn secondary" style="margin:6px 0 0" onclick="copyText(${escapeHtml(JSON.stringify(g.needs.command.split(' ')[0]))})">Copy command</button></div>` : '';
      const ev = g.evidence.length ? '<ul class="evidence">' + g.evidence.map(e => `<li>${escapeHtml(e)}</li>`).join('') + '</ul>' : '';
      const nodes = d.graph.nodes.map(n => `<div class="fnode ${n.status} ${n.current ? 'current' : ''}">` +
        `<span class="st">${n.current ? '● ' : ''}${escapeHtml(STATUS_TEXT[n.status] || n.status)}${n.visits > 1 ? ' ×' + n.visits : ''}</span>` +
        `<span class="n">${escapeHtml(n.num)}</span><strong>${escapeHtml(n.title)}</strong><small>${escapeHtml(n.sub)}</small></div>`).join('');
      const inputs = d.graph.inputs.map(i => `<span>◦ ${escapeHtml(i.title)} <small>${escapeHtml(i.sub)}</small></span>`).join('');
      const run = d.run;
      const running = run.state === 'running';
      const canResume = g.resumable !== false && !['done', 'cancelled'].includes(g.status);
      const rl = run.state === 'idle' ? '' : `<div class="muted" style="margin-top:8px">Last UI run: ${escapeHtml(run.state)}${run.dry_run ? ' (dry run)' : ''}${run.exit_code !== null && run.exit_code !== undefined ? ', exit ' + run.exit_code : ''}</div><div class="runlog">${escapeHtml(run.log || '')}</div>`;
      const rows = d.timeline.slice().reverse().map(r => `<tr><td>${escapeHtml(r.to)}</td><td>${escapeHtml(r.actor)}</td><td style="text-align:left">${escapeHtml(r.reason)}</td><td>${fmtDur(r.seconds_in_state)}${r.open ? ' (now)' : ''}</td></tr>`).join('');
      document.getElementById('cc-main').innerHTML =
        `<div class="a-card diag ${escapeHtml(g.tone)}"><div class="muted">#${d.issue} · ${escapeHtml(d.title)} · in <code>${escapeHtml(g.state)}</code> for ${fmtDur(g.since_seconds)}</div>` +
        `<h2>${escapeHtml(g.headline)}</h2><div>${escapeHtml(g.detail)}</div>${needs}${ev}</div>` +
        `<div class="flow"><div class="flow-inputs">${inputs}</div><div class="flow-nodes">${nodes}</div>` +
        `<div class="flow-out"><span>↶ Review findings return to the coding agent <small>round ${loops.review_to_coder}, attempt ${loops.gate_retries}</small></span><span>◦ Langfuse <small>optional local trace</small></span></div></div>` +
        `<div class="a-card"><div class="panel-title" style="margin-top:0">Resume workflow</div>` +
        `<div class="muted" style="margin-bottom:8px">Runs <code>devflow ticket ${d.issue}</code> once on this machine. Human gates still need their GitHub comment first.</div>` +
        `<button class="btn" ${canResume && !running ? '' : 'disabled'} onclick="resumeTicket(${d.issue}, false)">${running ? 'Running…' : 'Resume now'}</button>` +
        `<button class="btn secondary" ${canResume && !running ? '' : 'disabled'} onclick="resumeTicket(${d.issue}, true)">Dry run</button>` +
        (g.resumable === false ? '<span class="muted">Another runner owns this repository.</span>' : '') + rl + `</div>` +
        `<div><div class="panel-title">History</div><table class="a-table"><tr><th>State</th><th>Actor</th><th style="text-align:left">Reason</th><th>Time in state</th></tr>${rows}</table></div>`;
    }

    async function resumeTicket(n, dry) {
      if (!dry && !confirm('Resume #' + n + '? This runs agents and may write to GitHub.')) return;
      const res = await fetch('/api/tickets/' + n + '/resume' + (dry ? '?dry_run=1' : ''), {method: 'POST', headers: {'X-Devflow-Token': TOKEN}});
      if (!res.ok) { const e = await res.json().catch(() => ({})); alert(e.error || 'Resume failed'); }
      fetchCC();
    }

    let view = 'board';
    function showView(name) {
      view = name;
      document.getElementById('board-view').style.display = name === 'board' ? '' : 'none';
      document.getElementById('analytics-view').style.display = name === 'analytics' ? 'block' : 'none';
      document.getElementById('cc-view').style.display = name === 'cc' ? 'grid' : 'none';
      document.getElementById('tab-cc').classList.toggle('active', name === 'cc');
      document.getElementById('tab-board').classList.toggle('active', name === 'board');
      document.getElementById('tab-analytics').classList.toggle('active', name === 'analytics');
      if (name === 'analytics') fetchAnalytics();
      if (name === 'cc') fetchCC();
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
      
      const banner = document.getElementById('board-banner');
      if (data.board_error) {
        banner.style.display = '';
        banner.innerText = (data.board_stale ? 'GitHub is unavailable; showing the last board it returned. ' :
          'GitHub is unavailable; showing local ticket state instead of the Project board. ') + data.board_error;
      } else { banner.style.display = 'none'; }

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
    setInterval(() => { fetchState(); if (view === 'analytics') fetchAnalytics(); if (view === 'cc') fetchCC(); }, 3000);
  </script>
</body>
</html>
"""


BOARD_TTL_SECONDS = 60.0
_BOARD_CACHE: dict[tuple[str, int], dict[str, Any]] = {}
_BOARD_LOCK = threading.Lock()

# Where a ticket belongs on the board when GitHub cannot be asked.
_STATE_COLUMN = {
    "new": "Ready",
    "merged": "Done", "cancelled": "Done",
    "in_review": "In review", "awaiting_merge_approval": "In review", "changes_requested": "In review",
}


def _board_items(slug: str, owner: str, project: int, now: float | None = None) -> tuple[list[Any], str | None]:
    """Project items, cached so a polling page does not spend GitHub's rate limit.

    Failures are cached too, so a rate-limited GitHub is retried once per TTL rather than every poll.
    Returns (items, error); on error the last good items are returned when there are any.
    """
    from .board import GhBoard
    now = time.monotonic() if now is None else now
    key = (slug, project)
    with _BOARD_LOCK:
        entry = _BOARD_CACHE.get(key)
        if entry and now - entry["at"] < BOARD_TTL_SECONDS:
            return entry["items"], entry["error"]
        try:
            items = list(GhBoard(slug, owner, project).items())
            entry = {"at": now, "items": items, "error": None}
        except Exception as exc:  # noqa: BLE001
            entry = {"at": now, "items": entry["items"] if entry else [], "error": str(exc)[:300] or type(exc).__name__}
        _BOARD_CACHE[key] = entry
        return entry["items"], entry["error"]


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
                    "local_title": t.title,
                }

    columns: dict[str, list[dict[str, Any]]] = {
        "Backlog": [],
        "Ready": [],
        "In progress": [],
        "In review": [],
        "Done": [],
    }
    source, board_error, stale = "local", None, False

    def local_fallback() -> None:
        for issue, details in local_tickets.items():
            column = _STATE_COLUMN.get(details["stage"], "In progress")
            columns[column].append({
                "issue": issue,
                "title": details.get("local_title") or f"Ticket #{issue}",
                "status": column,
                **{k: v for k, v in details.items() if k != "local_title"},
            })

    if cfg and cfg.board.project:
        items, board_error = _board_items(slug, cfg.board.owner, cfg.board.project)
        if items:
            source, stale = "github", board_error is not None
            for item in items:
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
                    info.update({k: v for k, v in local_tickets[item.issue].items() if k != "local_title"})
                columns[status].append(info)
        else:
            local_fallback()
    else:
        local_fallback()

    return {
        "repo": name,
        "repo_slug": slug,
        "project": project,
        "columns": columns,
        "source": source,
        "board_error": board_error,
        "board_stale": stale,
    }


_TICKET = re.compile(r"^/api/tickets/(\d+)(/resume)?$")


class UiHandler(http.server.BaseHTTPRequestHandler):
    root: Path
    token: str = secrets.token_urlsafe(24)

    def _send_json(self, produce, status: int = 200) -> None:
        try:
            payload = json.dumps(produce()).encode("utf-8")
            self.send_response(status)
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
        if url.path == "/api/tickets":
            self._send_json(lambda: command_center.list_tickets(self.root))
            return
        found = _TICKET.match(url.path)
        if found and not found.group(2):
            detail = command_center.ticket_detail(self.root, int(found.group(1)))
            if detail is None:
                self._send_json(lambda: {"error": "ticket not found"}, 404)
            else:
                self._send_json(lambda: detail)
            return
        if url.path == "/api/analytics":
            repo = parse_qs(url.query).get("repo", [None])[0]
            self._send_json(lambda: collect_analytics(self.root, repo))
            return

        if self.path == "/" or self.path.startswith("/?"):
            payload = HTML_TEMPLATE.replace("__DEVFLOW_TOKEN__", self.token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        found = _TICKET.match(url.path)
        if not (found and found.group(2)):
            self.send_response(404)
            self.end_headers()
            return
        host = self.headers.get("Host", "").rsplit(":", 1)[0]
        if (self.headers.get("X-Devflow-Token") != self.token
                or host not in ("127.0.0.1", "localhost", "[::1]")):
            self._send_json(lambda: {"error": "forbidden"}, 403)
            return
        issue = int(found.group(1))
        dry = parse_qs(url.query).get("dry_run", ["0"])[0] == "1"
        if command_center.ticket_detail(self.root, issue) is None:
            self._send_json(lambda: {"error": "ticket not found"}, 404)
            return
        try:
            result = command_center.start_resume(self.root, issue, dry_run=dry)
        except RuntimeError as exc:
            self._send_json(lambda: {"error": str(exc)}, 409)
            return
        self._send_json(lambda: result, 202)

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
