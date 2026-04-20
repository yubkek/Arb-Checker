"""
Tennis Arb Scanner — Web Dashboard

Run alongside scanner.py (in a separate terminal):
    python dashboard.py

Then open http://localhost:5000 in your browser.
The page auto-refreshes every 10 seconds.
scanner.py writes scan_data.json after each scan; the dashboard reads it.
"""

import json
import os
import time

from flask import Flask, jsonify, render_template_string

app = Flask(__name__)
SCAN_DATA_FILE = "scan_data.json"

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tennis Arb Scanner</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body { background:#0b0f1a; color:#e2e8f0; font-family:'Inter',system-ui,sans-serif; }
  .mono { font-family:'JetBrains Mono','Fira Code','Courier New',monospace; }
  .arb-row { background:rgba(16,185,129,0.12); }
  .close-row { background:rgba(234,179,8,0.08); }
  .pulse { animation: pulse 2s cubic-bezier(0.4,0,0.6,1) infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
  ::-webkit-scrollbar { width:6px; height:6px; }
  ::-webkit-scrollbar-track { background:#1e293b; }
  ::-webkit-scrollbar-thumb { background:#334155; border-radius:3px; }
  th { position:sticky; top:0; background:#0f172a; z-index:10; }
  .badge { display:inline-block; padding:1px 8px; border-radius:9999px; font-size:11px; font-weight:600; letter-spacing:.04em; }
</style>
</head>
<body class="min-h-screen">

<!-- Header -->
<header class="border-b border-slate-800 px-6 py-4 flex items-center justify-between">
  <div class="flex items-center gap-3">
    <span class="text-2xl">🎾</span>
    <h1 class="text-xl font-bold text-white tracking-tight">Tennis Arb Scanner</h1>
    <span class="badge bg-slate-700 text-slate-300 ml-2">AU Markets</span>
  </div>
  <div class="flex items-center gap-2" id="status-dot">
    <div class="w-2 h-2 rounded-full bg-slate-600 pulse" id="dot"></div>
    <span class="text-sm text-slate-400" id="status-text">Connecting...</span>
  </div>
</header>

<!-- Stats bar -->
<div class="border-b border-slate-800 px-6 py-3 flex flex-wrap gap-6 text-sm">
  <div>
    <span class="text-slate-500">Matches</span>
    <span class="ml-2 font-bold text-white mono" id="stat-matches">—</span>
  </div>
  <div>
    <span class="text-slate-500">Sources</span>
    <span class="ml-2 font-bold text-white mono" id="stat-sources">—</span>
  </div>
  <div>
    <span class="text-slate-500">Arbs</span>
    <span class="ml-2 font-bold mono" id="stat-arbs">—</span>
  </div>
  <div>
    <span class="text-slate-500">Scan #</span>
    <span class="ml-2 font-bold text-white mono" id="stat-scan">—</span>
  </div>
  <div>
    <span class="text-slate-500">Last scan</span>
    <span class="ml-2 text-white mono" id="stat-last">—</span>
  </div>
  <div>
    <span class="text-slate-500">Next scan</span>
    <span class="ml-2 font-bold text-cyan-400 mono" id="stat-next">—</span>
  </div>
</div>

<!-- Arb alerts -->
<div id="arb-section" class="hidden px-6 pt-5">
  <h2 class="text-emerald-400 font-bold text-sm uppercase tracking-widest mb-3">⚡ Arb Opportunities</h2>
  <div id="arb-cards" class="grid gap-3 grid-cols-1 md:grid-cols-2 xl:grid-cols-3 mb-6"></div>
</div>

<!-- All matches table -->
<div class="px-6 py-5">
  <div class="flex items-center justify-between mb-3">
    <h2 class="text-slate-400 font-bold text-sm uppercase tracking-widest">All Matches</h2>
    <input id="search" type="text" placeholder="Filter player / bookmaker..."
      class="bg-slate-800 border border-slate-700 rounded px-3 py-1 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-cyan-500 w-64">
  </div>
  <div class="rounded-lg border border-slate-800 overflow-x-auto">
    <table class="w-full text-sm">
      <thead>
        <tr class="text-left text-xs uppercase tracking-wider text-slate-500">
          <th class="px-4 py-3 font-semibold">Match</th>
          <th class="px-4 py-3 font-semibold">Player 1</th>
          <th class="px-4 py-3 font-semibold text-right">Odds</th>
          <th class="px-4 py-3 font-semibold">Bookie</th>
          <th class="px-4 py-3 font-semibold">Player 2</th>
          <th class="px-4 py-3 font-semibold text-right">Odds</th>
          <th class="px-4 py-3 font-semibold">Bookie</th>
          <th class="px-4 py-3 font-semibold text-right">Margin</th>
          <th class="px-4 py-3 font-semibold text-center">Status</th>
        </tr>
      </thead>
      <tbody id="matches-body" class="divide-y divide-slate-800/60">
        <tr><td colspan="9" class="text-center py-12 text-slate-600">Waiting for first scan...</td></tr>
      </tbody>
    </table>
  </div>
  <p class="text-xs text-slate-600 mt-2" id="table-footer"></p>
</div>

<script>
let allMatches = [];
let nextScanAt = null;
let countdownTimer = null;

const BOOKIE_COLORS = {
  'Sportsbet':        'text-blue-400',
  'Neds':             'text-purple-400',
  'Ladbrokes':        'text-red-400',
  'Bet365':           'text-orange-400',
  'Betfair Exchange': 'text-cyan-400',
};

function bookieColor(name) {
  return BOOKIE_COLORS[name] || 'text-slate-300';
}

function bookieBadge(name) {
  const colors = {
    'Sportsbet':        'bg-blue-900/50 text-blue-300',
    'Neds':             'bg-purple-900/50 text-purple-300',
    'Ladbrokes':        'bg-red-900/50 text-red-300',
    'Bet365':           'bg-orange-900/50 text-orange-300',
    'Betfair Exchange': 'bg-cyan-900/50 text-cyan-300',
  };
  const cls = colors[name] || 'bg-slate-700 text-slate-300';
  return `<span class="badge ${cls}">${name}</span>`;
}

function marginBadge(m) {
  if (m.is_arb) {
    return `<span class="badge bg-emerald-900 text-emerald-300">+${m.margin.toFixed(2)}%</span>`;
  }
  const val = -m.margin;
  if (val < 5) return `<span class="badge bg-yellow-900/60 text-yellow-400">${m.margin.toFixed(1)}%</span>`;
  return `<span class="text-slate-600 mono text-xs">${m.margin.toFixed(1)}%</span>`;
}

function renderArbs(matches) {
  const arbs = matches.filter(m => m.is_arb);
  const section = document.getElementById('arb-section');
  const cards   = document.getElementById('arb-cards');
  if (!arbs.length) { section.classList.add('hidden'); return; }
  section.classList.remove('hidden');
  cards.innerHTML = arbs.map(m => `
    <div class="rounded-xl border border-emerald-500/30 bg-emerald-950/40 p-4">
      <div class="flex justify-between items-start mb-3">
        <p class="font-semibold text-white text-sm leading-tight">${m.match}</p>
        <span class="badge bg-emerald-500 text-black ml-3 shrink-0">+${m.margin.toFixed(3)}%</span>
      </div>
      <div class="space-y-2 text-sm">
        <div class="flex justify-between items-center">
          <span class="text-slate-300">${m.player1}</span>
          <div class="flex items-center gap-2">
            <span class="mono font-bold text-emerald-400">${m.odds1.toFixed(3)}</span>
            ${bookieBadge(m.bookie1)}
          </div>
        </div>
        <div class="flex justify-between items-center">
          <span class="text-slate-300">${m.player2}</span>
          <div class="flex items-center gap-2">
            <span class="mono font-bold text-emerald-400">${m.odds2.toFixed(3)}</span>
            ${bookieBadge(m.bookie2)}
          </div>
        </div>
      </div>
      ${m.profit != null ? `
      <div class="mt-3 pt-3 border-t border-emerald-800/40 grid grid-cols-3 gap-2 text-xs text-center">
        <div><p class="text-slate-500">Stake P1</p><p class="mono text-white font-semibold">$${m.stake1}</p></div>
        <div><p class="text-slate-500">Stake P2</p><p class="mono text-white font-semibold">$${m.stake2}</p></div>
        <div><p class="text-slate-500">Profit</p><p class="mono text-emerald-400 font-bold">$${m.profit}</p></div>
      </div>` : ''}
    </div>
  `).join('');
}

function renderTable(matches) {
  const filter = document.getElementById('search').value.toLowerCase();
  const filtered = filter
    ? matches.filter(m =>
        m.match.toLowerCase().includes(filter) ||
        m.player1.toLowerCase().includes(filter) ||
        m.player2.toLowerCase().includes(filter) ||
        m.bookie1.toLowerCase().includes(filter) ||
        m.bookie2.toLowerCase().includes(filter))
    : matches;

  const tbody = document.getElementById('matches-body');
  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="text-center py-8 text-slate-600">No matches found</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(m => {
    const rowCls = m.is_arb ? 'arb-row' : (-m.margin < 5 ? 'close-row' : '');
    return `
    <tr class="${rowCls} hover:bg-white/5 transition-colors">
      <td class="px-4 py-2.5 text-slate-200 max-w-[180px] truncate" title="${m.match}">${m.match}</td>
      <td class="px-4 py-2.5 text-slate-300 max-w-[140px] truncate">${m.player1}</td>
      <td class="px-4 py-2.5 text-right mono font-bold ${m.is_arb ? 'text-emerald-400' : 'text-slate-200'}">${m.odds1.toFixed(3)}</td>
      <td class="px-4 py-2.5">${bookieBadge(m.bookie1)}</td>
      <td class="px-4 py-2.5 text-slate-300 max-w-[140px] truncate">${m.player2}</td>
      <td class="px-4 py-2.5 text-right mono font-bold ${m.is_arb ? 'text-emerald-400' : 'text-slate-200'}">${m.odds2.toFixed(3)}</td>
      <td class="px-4 py-2.5">${bookieBadge(m.bookie2)}</td>
      <td class="px-4 py-2.5 text-right">${marginBadge(m)}</td>
      <td class="px-4 py-2.5 text-center">
        ${m.is_arb
          ? '<span class="badge bg-emerald-500 text-black">ARB</span>'
          : '<span class="text-slate-700 text-xs">—</span>'}
      </td>
    </tr>`;
  }).join('');

  document.getElementById('table-footer').textContent =
    `Showing ${filtered.length} of ${matches.length} matches`;
}

function startCountdown() {
  if (countdownTimer) clearInterval(countdownTimer);
  countdownTimer = setInterval(() => {
    if (!nextScanAt) return;
    const secs = Math.max(0, Math.round(nextScanAt - Date.now() / 1000));
    document.getElementById('stat-next').textContent = secs > 0 ? `${secs}s` : 'scanning...';
  }, 500);
}

async function fetchData() {
  try {
    const res  = await fetch('/api/data');
    const data = await res.json();

    if (data.error) {
      document.getElementById('status-text').textContent = 'Waiting for first scan...';
      return;
    }

    // Status dot → green
    document.getElementById('dot').className        = 'w-2 h-2 rounded-full bg-emerald-400';
    document.getElementById('status-text').textContent = 'Live';

    // Stats
    document.getElementById('stat-matches').textContent = data.total_matches;
    document.getElementById('stat-scan').textContent    = data.scan_num;
    document.getElementById('stat-last').textContent    = data.last_scan.split(' ')[1];
    document.getElementById('stat-arbs').textContent    = data.arb_count;
    document.getElementById('stat-arbs').className =
      data.arb_count > 0
        ? 'ml-2 font-bold mono text-emerald-400'
        : 'ml-2 font-bold mono text-slate-400';

    const sources = Object.entries(data.source_counts || {})
      .map(([k, v]) => `${k.split(' ')[0]}: ${v}`)
      .join(' · ');
    document.getElementById('stat-sources').textContent = sources || '—';

    nextScanAt = data.last_scan_ts + data.scan_interval;

    allMatches = data.matches || [];
    renderArbs(allMatches);
    renderTable(allMatches);

  } catch (e) {
    document.getElementById('status-text').textContent = 'Offline';
    document.getElementById('dot').className = 'w-2 h-2 rounded-full bg-red-500';
  }
}

document.getElementById('search').addEventListener('input', () => renderTable(allMatches));

fetchData();
startCountdown();
setInterval(fetchData, 10_000);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/data")
def api_data():
    if not os.path.exists(SCAN_DATA_FILE):
        return jsonify({"error": "no scan data yet"})
    try:
        with open(SCAN_DATA_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    print("Dashboard running at http://localhost:5000")
    print("Make sure scanner.py is also running in a separate terminal.")
    app.run(host="0.0.0.0", port=5000, debug=False)
