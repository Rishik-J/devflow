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


class UiHandler(http.server.BaseHTTPRequestHandler):
    root: Path
    host_factory = staticmethod(_default_host_factory)

    def _json(self, code: int, data: Any) -> None:
        payload = json.dumps(data).encode("utf-8")
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
