"""
Execution Bot Dashboard
========================
Single-file dashboard for the execution bot.
- Live KPIs
- Open positions with real-time PnL (from Binance public API)
- Closed positions with filters
- Daily PnL chart (last 14 days)
- Webhook log
- CSV export
- Emergency close-all button

Integrates into execution_bot by importing and registering a Blueprint.
"""

import os
import csv
import io
import json
import logging
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, List, Optional, Any

from flask import (
    Blueprint, render_template_string, jsonify,
    request, Response, abort,
)

logger = logging.getLogger(__name__)


# ======================================================================
# Dashboard Blueprint — mount at /dashboard
# ======================================================================
dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')


# ======================================================================
# Price fetching (from Binance public API via ccxt)
# ======================================================================
_price_cache: Dict[str, Dict[str, Any]] = {}
_price_cache_ttl = 30  # seconds
_ccxt_client = None


def _get_public_ccxt():
    """Lazy-init a public ccxt client (no API keys)."""
    global _ccxt_client
    if _ccxt_client is None:
        try:
            import ccxt
            _ccxt_client = ccxt.binanceusdm({
                'enableRateLimit': True,
                'options': {'defaultType': 'future'},
            })
            logger.info("Dashboard: public ccxt client initialized")
        except Exception as e:
            logger.error(f"Dashboard: ccxt init failed: {e}")
            return None
    return _ccxt_client


def _to_symbol(symbol: str) -> str:
    """BTC/USDT -> BTCUSDT"""
    return symbol.replace('/', '').upper()


def get_current_price(symbol: str) -> Optional[float]:
    """Get current price from Binance with a 30s cache."""
    now = datetime.now()
    cached = _price_cache.get(symbol)
    if cached and (now - cached['at']).total_seconds() < _price_cache_ttl:
        return cached['price']

    ex = _get_public_ccxt()
    if not ex:
        return cached['price'] if cached else None

    try:
        ticker = ex.fetch_ticker(_to_symbol(symbol))
        price = float(ticker.get('last') or ticker.get('close') or 0)
        if price > 0:
            _price_cache[symbol] = {'price': price, 'at': now}
            return price
    except Exception as e:
        logger.warning(f"Dashboard: price fetch failed for {symbol}: {e}")

    return cached['price'] if cached else None


def get_prices_bulk(symbols: List[str]) -> Dict[str, float]:
    """Fetch prices for multiple symbols in one batch."""
    out: Dict[str, float] = {}
    for s in symbols:
        p = get_current_price(s)
        if p is not None:
            out[s] = p
    return out


# ======================================================================
# Analytics
# ======================================================================
def compute_open_position_pnl(pos: Dict, current_price: Optional[float]) -> Dict:
    """Add current price + unrealized PnL to an open position."""
    result = dict(pos)
    result['current_price'] = current_price
    result['unrealized_pnl_usd'] = None
    result['unrealized_pnl_pct'] = None
    result['age_minutes'] = None

    try:
        opened = datetime.fromisoformat(pos['opened_at'])
        age_sec = (datetime.now() - opened).total_seconds()
        result['age_minutes'] = round(age_sec / 60.0, 1)
    except Exception:
        pass

    if current_price is None:
        return result

    try:
        entry = float(pos['entry_price'])
        qty = float(pos['quantity'])
        side = pos['side']

        if side == 'long':
            pnl_usd = (current_price - entry) * qty
        else:
            pnl_usd = (entry - current_price) * qty

        notional = entry * qty
        pnl_pct = (pnl_usd / notional * 100.0) if notional > 0 else 0.0

        result['unrealized_pnl_usd'] = round(pnl_usd, 4)
        result['unrealized_pnl_pct'] = round(pnl_pct, 4)
    except Exception:
        pass

    return result


def get_stats() -> Dict:
    """Compute dashboard KPIs."""
    from storage import PositionStore

    open_positions = PositionStore.get_open()
    recent = PositionStore.get_recent(limit=100)
    today = PositionStore.get_today_stats()

    # All closed trades (for win rate, avg PnL, etc.)
    closed = [p for p in recent if p.get('status') == 'closed']
    wins = [p for p in closed if (p.get('pnl_usd') or 0) > 0]
    losses = [p for p in closed if (p.get('pnl_usd') or 0) < 0]

    total_pnl = sum(float(p.get('pnl_usd') or 0) for p in closed)
    avg_win = (
        sum(float(p['pnl_usd']) for p in wins) / len(wins)
        if wins else 0.0
    )
    avg_loss = (
        sum(float(p['pnl_usd']) for p in losses) / len(losses)
        if losses else 0.0
    )
    best = max(closed, key=lambda p: float(p.get('pnl_usd') or 0), default=None)
    worst = min(closed, key=lambda p: float(p.get('pnl_usd') or 0), default=None)

    # Per-symbol performance
    by_symbol: Dict[str, Dict[str, float]] = {}
    for p in closed:
        s = p['symbol']
        if s not in by_symbol:
            by_symbol[s] = {'trades': 0, 'wins': 0, 'pnl': 0.0}
        by_symbol[s]['trades'] += 1
        by_symbol[s]['pnl'] += float(p.get('pnl_usd') or 0)
        if float(p.get('pnl_usd') or 0) > 0:
            by_symbol[s]['wins'] += 1

    top_symbols = sorted(
        [{'symbol': k, **v} for k, v in by_symbol.items()],
        key=lambda x: x['pnl'], reverse=True
    )[:5]

    return {
        'paper_trading': os.environ.get('PAPER_TRADING', 'true').lower() in ('true', '1', 'yes', 'on'),
        'leverage': int(os.environ.get('LEVERAGE', 20)),
        'position_size_usd': float(os.environ.get('POSITION_SIZE_USD', 5.0)),
        'open_count': len(open_positions),
        'closed_count': len(closed),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': (len(wins) / len(closed) * 100.0) if closed else 0.0,
        'total_pnl_usd': round(total_pnl, 4),
        'avg_win_usd': round(avg_win, 4),
        'avg_loss_usd': round(avg_loss, 4),
        'best_trade': {
            'symbol': best['symbol'],
            'pnl_usd': float(best.get('pnl_usd') or 0),
        } if best else None,
        'worst_trade': {
            'symbol': worst['symbol'],
            'pnl_usd': float(worst.get('pnl_usd') or 0),
        } if worst else None,
        'today': {
            'date': today.get('date'),
            'trades_opened': int(today.get('trades_opened') or 0),
            'trades_closed': int(today.get('trades_closed') or 0),
            'realized_pnl': round(float(today.get('realized_pnl') or 0), 4),
            'wins': int(today.get('wins') or 0),
            'losses': int(today.get('losses') or 0),
        },
        'top_symbols': top_symbols,
    }


def get_daily_chart_data(days: int = 14) -> List[Dict]:
    """Return daily PnL for the last N days."""
    from storage import _conn
    since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT date, realized_pnl, trades_opened, trades_closed, wins, losses "
                "FROM daily_stats WHERE date >= ? ORDER BY date ASC",
                (since,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_daily_chart_data failed: {e}")
        return []


def get_webhook_log(limit: int = 50) -> List[Dict]:
    from storage import _conn
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM webhook_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error(f"get_webhook_log failed: {e}")
        return []


# ======================================================================
# HTML template (inline — single-file design)
# ======================================================================
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Execution Bot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0d1117; color: #e6edf3;
    padding: 24px; min-height: 100vh;
  }
  h1 { font-size: 24px; margin-bottom: 4px; }
  h2 { font-size: 18px; margin: 24px 0 12px; color: #58a6ff; }
  .subtitle { color: #8b949e; font-size: 13px; margin-bottom: 20px; }
  .grid {
    display: grid; gap: 16px;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    margin-bottom: 24px;
  }
  .card {
    background: #161b22; border: 1px solid #30363d;
    border-radius: 8px; padding: 16px;
  }
  .card .label { color: #8b949e; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
  .card .value { font-size: 24px; font-weight: 600; margin-top: 6px; }
  .card .extra { color: #8b949e; font-size: 12px; margin-top: 4px; }
  .pos { color: #3fb950; }
  .neg { color: #f85149; }
  .neutral { color: #8b949e; }
  .tag {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 11px; font-weight: 600; text-transform: uppercase;
  }
  .tag-paper { background: #1f6feb; color: white; }
  .tag-live  { background: #da3633; color: white; }
  .tag-open  { background: #1f6feb; color: white; }
  .tag-closed { background: #484f58; color: #e6edf3; }
  .tag-long  { background: #238636; color: white; }
  .tag-short { background: #a40e26; color: white; }
  table {
    width: 100%; border-collapse: collapse; font-size: 13px;
    background: #161b22; border-radius: 8px; overflow: hidden;
  }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #21262d; }
  th { background: #0d1117; color: #8b949e; font-weight: 600; font-size: 12px; text-transform: uppercase; }
  tr:hover td { background: #1c2128; }
  .btn {
    background: #21262d; color: #e6edf3; border: 1px solid #30363d;
    padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px;
    transition: all 0.15s;
  }
  .btn:hover { background: #30363d; }
  .btn-danger { background: #da3633; color: white; border-color: #da3633; }
  .btn-danger:hover { background: #f85149; }
  .btn-primary { background: #1f6feb; color: white; border-color: #1f6feb; }
  .btn-primary:hover { background: #388bfd; }
  .controls { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
  .controls input, .controls select {
    background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
    padding: 6px 10px; border-radius: 6px; font-size: 13px;
  }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .dot-live { background: #3fb950; animation: pulse 1.5s infinite; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
  .empty { color: #8b949e; padding: 20px; text-align: center; font-style: italic; }
  .chart-wrap { background: #161b22; border-radius: 8px; padding: 16px; height: 280px; position: relative; }
  .refresh-badge {
    display: inline-flex; align-items: center;
    background: #21262d; padding: 4px 10px; border-radius: 12px;
    font-size: 11px; color: #8b949e;
  }
</style>
</head>
<body>

<h1>Execution Bot Dashboard <span id="mode-badge"></span></h1>
<div class="subtitle">
  <span class="refresh-badge"><span class="dot dot-live"></span> Live · refresh every 5s · <span id="last-update">–</span></span>
</div>

<!-- KPI GRID -->
<div class="grid" id="kpi-grid"></div>

<!-- CONTROLS -->
<div class="controls">
  <button class="btn btn-primary" onclick="refreshAll(true)">Refresh Now</button>
  <button class="btn" onclick="exportCsv()">Export CSV</button>
  <button class="btn btn-danger" onclick="closeAll()">Emergency Close All</button>
  <select id="filter-status">
    <option value="all">All Status</option>
    <option value="open">Open Only</option>
    <option value="closed">Closed Only</option>
  </select>
  <input id="filter-symbol" type="text" placeholder="Filter symbol..." style="width: 140px;">
</div>

<!-- OPEN POSITIONS -->
<h2>Open Positions <span id="open-count" class="neutral"></span></h2>
<div id="open-positions"></div>

<!-- CLOSED POSITIONS -->
<h2>Recent Closed Positions</h2>
<div id="closed-positions"></div>

<!-- CHART -->
<h2>Daily Realized PnL (Last 14 Days)</h2>
<div class="chart-wrap">
  <canvas id="pnl-chart"></canvas>
</div>

<!-- TOP SYMBOLS -->
<h2>Top Performing Symbols</h2>
<div id="top-symbols"></div>

<!-- WEBHOOK LOG -->
<h2>Recent Webhook Activity</h2>
<div id="webhook-log"></div>

<script>
// ======================================================================
// Auto-refresh logic
// ======================================================================
const REFRESH_MS = 5000;
let chart = null;

async function fetchJSON(url) {
  const r = await fetch(url);
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('json')) throw new Error('non-JSON response');
  return r.json();
}

async function refreshStats() {
  try {
    const data = await fetchJSON('/dashboard/api/stats');
    renderKpis(data.stats);
    renderOpenPositions(data.open_positions);
    renderClosedPositions(data.recent_closed);
    renderTopSymbols(data.stats.top_symbols);
    updateChart(data.daily_chart);
    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
  } catch (e) {
    console.warn('refreshStats failed:', e);
  }
}

async function refreshWebhooks() {
  try {
    const data = await fetchJSON('/dashboard/api/webhooks');
    renderWebhookLog(data.log);
  } catch (e) { /* silent */ }
}

function refreshAll(manual) {
  refreshStats();
  refreshWebhooks();
  if (manual) console.log('Manual refresh triggered');
}

// ======================================================================
// Renderers
// ======================================================================
function fmtUsd(v) {
  if (v == null) return '–';
  const sign = v >= 0 ? '+' : '−';
  return `${sign}$${Math.abs(v).toFixed(2)}`;
}

function fmtPct(v) {
  if (v == null) return '–';
  const sign = v >= 0 ? '+' : '−';
  return `${sign}${Math.abs(v).toFixed(2)}%`;
}

function pnlClass(v) {
  if (v == null) return 'neutral';
  return v > 0 ? 'pos' : (v < 0 ? 'neg' : 'neutral');
}

function renderKpis(s) {
  const mode = s.paper_trading
    ? '<span class="tag tag-paper">PAPER</span>'
    : '<span class="tag tag-live">LIVE</span>';
  document.getElementById('mode-badge').innerHTML = mode;

  const cards = [
    { label: 'Open Positions', value: s.open_count, extra: `Max concurrent: 1` },
    { label: 'Total PnL', value: fmtUsd(s.total_pnl_usd), cls: pnlClass(s.total_pnl_usd), extra: `${s.closed_count} trades` },
    { label: 'Win Rate', value: `${s.win_rate.toFixed(1)}%`, cls: s.win_rate >= 50 ? 'pos' : 'neg', extra: `${s.wins} W / ${s.losses} L` },
    { label: 'Today PnL', value: fmtUsd(s.today.realized_pnl), cls: pnlClass(s.today.realized_pnl), extra: `${s.today.trades_opened} opened · ${s.today.trades_closed} closed` },
    { label: 'Avg Win', value: fmtUsd(s.avg_win_usd), cls: 'pos' },
    { label: 'Avg Loss', value: fmtUsd(s.avg_loss_usd), cls: 'neg' },
    { label: 'Leverage', value: `${s.leverage}x`, extra: `Size: $${s.position_size_usd.toFixed(2)}` },
  ];

  document.getElementById('kpi-grid').innerHTML = cards.map(c => `
    <div class="card">
      <div class="label">${c.label}</div>
      <div class="value ${c.cls || ''}">${c.value}</div>
      ${c.extra ? `<div class="extra">${c.extra}</div>` : ''}
    </div>
  `).join('');
}

function renderOpenPositions(positions) {
  const root = document.getElementById('open-positions');
  document.getElementById('open-count').textContent = `(${positions.length})`;

  if (!positions.length) {
    root.innerHTML = '<div class="empty">No open positions.</div>';
    return;
  }

  root.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Symbol</th><th>Side</th><th>Entry</th><th>Current</th>
          <th>Qty</th><th>Margin</th><th>PnL</th><th>Age</th>
          <th>SL / TP</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>
        ${positions.map(p => `
          <tr>
            <td><strong>${p.symbol}</strong></td>
            <td><span class="tag tag-${p.side}">${p.side.toUpperCase()}</span></td>
            <td>${p.entry_price}</td>
            <td>${p.current_price != null ? p.current_price : '–'}</td>
            <td>${p.quantity}</td>
            <td>$${parseFloat(p.margin_usd).toFixed(2)} @ ${p.leverage}x</td>
            <td class="${pnlClass(p.unrealized_pnl_usd)}">
              ${fmtUsd(p.unrealized_pnl_usd)}
              <br><small>${fmtPct(p.unrealized_pnl_pct)}</small>
            </td>
            <td>${p.age_minutes != null ? p.age_minutes.toFixed(1) + 'm' : '–'}</td>
            <td>
              <small>SL: ${p.stop_loss || '–'}<br>TP: ${p.take_profit || '–'}</small>
            </td>
            <td>
              <button class="btn btn-danger" onclick="closePosition(${p.id})">
                Close
              </button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function renderClosedPositions(positions) {
  const root = document.getElementById('closed-positions');
  const filterSym = (document.getElementById('filter-symbol').value || '').toLowerCase();

  let filtered = positions;
  if (filterSym) {
    filtered = positions.filter(p => p.symbol.toLowerCase().includes(filterSym));
  }

  if (!filtered.length) {
    root.innerHTML = '<div class="empty">No closed positions yet.</div>';
    return;
  }

  root.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Symbol</th><th>Side</th><th>Entry</th><th>Exit</th>
          <th>PnL</th><th>Reason</th><th>Closed At</th>
        </tr>
      </thead>
      <tbody>
        ${filtered.map(p => `
          <tr>
            <td><strong>${p.symbol}</strong></td>
            <td><span class="tag tag-${p.side}">${p.side.toUpperCase()}</span></td>
            <td>${p.entry_price}</td>
            <td>${p.exit_price}</td>
            <td class="${pnlClass(p.pnl_usd)}">
              ${fmtUsd(p.pnl_usd)}
              <br><small>${fmtPct(p.pnl_pct)}</small>
            </td>
            <td><small>${p.exit_reason || '–'}</small></td>
            <td><small>${(p.closed_at || '').replace('T', ' ').split('.')[0]}</small></td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function renderTopSymbols(symbols) {
  const root = document.getElementById('top-symbols');
  if (!symbols || !symbols.length) {
    root.innerHTML = '<div class="empty">Not enough data yet.</div>';
    return;
  }
  root.innerHTML = `
    <table>
      <thead>
        <tr><th>Symbol</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>Total PnL</th></tr>
      </thead>
      <tbody>
        ${symbols.map(s => `
          <tr>
            <td><strong>${s.symbol}</strong></td>
            <td>${s.trades}</td>
            <td>${s.wins}</td>
            <td class="${s.trades ? (s.wins/s.trades >= 0.5 ? 'pos' : 'neg') : 'neutral'}">
              ${s.trades ? ((s.wins/s.trades*100).toFixed(0) + '%') : '–'}
            </td>
            <td class="${pnlClass(s.pnl)}">${fmtUsd(s.pnl)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function renderWebhookLog(log) {
  const root = document.getElementById('webhook-log');
  if (!log || !log.length) {
    root.innerHTML = '<div class="empty">No webhook activity.</div>';
    return;
  }
  root.innerHTML = `
    <table>
      <thead>
        <tr><th>Time</th><th>Event</th><th>Symbol</th><th>Action</th><th>Reason</th></tr>
      </thead>
      <tbody>
        ${log.map(e => `
          <tr>
            <td><small>${(e.received_at || '').replace('T', ' ').split('.')[0]}</small></td>
            <td><span class="tag tag-closed">${e.event || '–'}</span></td>
            <td>${e.symbol || '–'}</td>
            <td>${e.action || '–'}</td>
            <td><small>${e.reason || '–'}</small></td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function updateChart(chartData) {
  const labels = chartData.map(d => d.date);
  const values = chartData.map(d => parseFloat(d.realized_pnl || 0));

  const ctx = document.getElementById('pnl-chart').getContext('2d');

  if (chart) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    chart.update('none');
    return;
  }

  chart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Realized PnL (USD)',
        data: values,
        borderColor: '#58a6ff',
        backgroundColor: 'rgba(88, 166, 255, 0.1)',
        borderWidth: 2,
        tension: 0.3,
        fill: true,
        pointRadius: 3,
        pointBackgroundColor: '#58a6ff',
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#e6edf3' } },
        tooltip: {
          callbacks: {
            label: (ctx) => ` PnL: ${ctx.parsed.y >= 0 ? '+' : '−'}$${Math.abs(ctx.parsed.y).toFixed(2)}`,
          },
        },
      },
      scales: {
        x: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } },
      },
    },
  });
}

// ======================================================================
// Actions
// ======================================================================
async function closePosition(id) {
  if (!confirm(`Close position #${id}?`)) return;
  try {
    const r = await fetch(`/dashboard/api/close/${id}`, { method: 'POST' });
    const d = await r.json();
    console.log('close result:', d);
    refreshAll();
  } catch (e) {
    alert('Close failed: ' + e);
  }
}

async function closeAll() {
  if (!confirm('EMERGENCY: Close ALL open positions? This cannot be undone.')) return;
  try {
    const r = await fetch('/dashboard/api/close_all', { method: 'POST' });
    const d = await r.json();
    alert(`Closed ${d.closed} position(s).`);
    refreshAll();
  } catch (e) {
    alert('Close-all failed: ' + e);
  }
}

function exportCsv() {
  window.location.href = '/dashboard/api/export.csv';
}

document.getElementById('filter-symbol').addEventListener('input', refreshStats);
document.getElementById('filter-status').addEventListener('change', refreshStats);

// ======================================================================
// Start auto-refresh
// ======================================================================
refreshAll();
setInterval(refreshAll, REFRESH_MS);
</script>

</body>
</html>
"""


# ======================================================================
# Routes
# ======================================================================
@dashboard_bp.route('/')
def dashboard_home():
    return render_template_string(DASHBOARD_HTML)


@dashboard_bp.route('/api/stats')
def api_stats():
    from storage import PositionStore

    # Fetch open positions with live PnL
    open_pos = PositionStore.get_open()
    symbols = list({p['symbol'] for p in open_pos})
    prices = get_prices_bulk(symbols)

    enriched_open = [
        compute_open_position_pnl(p, prices.get(p['symbol']))
        for p in open_pos
    ]

    recent = PositionStore.get_recent(limit=100)
    recent_closed = [p for p in recent if p.get('status') == 'closed'][:20]

    return jsonify({
        'status': 'success',
        'stats': get_stats(),
        'open_positions': enriched_open,
        'recent_closed': recent_closed,
        'daily_chart': get_daily_chart_data(14),
        'timestamp': datetime.now().isoformat(),
    })


@dashboard_bp.route('/api/webhooks')
def api_webhooks():
    return jsonify({
        'status': 'success',
        'log': get_webhook_log(30),
    })


@dashboard_bp.route('/api/close/<int:position_id>', methods=['POST'])
def api_close_position(position_id):
    from storage import PositionStore
    from position_manager import handle_exit

    pos = None
    for p in PositionStore.get_open():
        if int(p['id']) == int(position_id):
            pos = p
            break
    if not pos:
        return jsonify({'status': 'error', 'message': 'position not found or not open'}), 404

    try:
        result = handle_exit({
            'symbol': pos['symbol'],
            'reason': f'manual_dashboard_{position_id}',
        })
        return jsonify({'status': 'success', 'result': result})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@dashboard_bp.route('/api/close_all', methods=['POST'])
def api_close_all():
    from position_manager import close_all
    try:
        result = close_all('dashboard_emergency')
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@dashboard_bp.route('/api/export.csv')
def api_export_csv():
    from storage import PositionStore
    rows = PositionStore.get_recent(limit=1000)

    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    else:
        output.write('no data\n')

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=positions.csv'}
    )


# ======================================================================
# Register function
# ======================================================================
def register_dashboard(app):
    """
    Call this from execution_bot/app.py:
        from dashboard import register_dashboard
        register_dashboard(app)
    """
    app.register_blueprint(dashboard_bp)
    logger.info("Dashboard registered at /dashboard/")
