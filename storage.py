"""
Storage for the execution bot — SQLite.
Tracks open positions, closed trades, daily stats.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from threading import Lock

from config import DB_PATH

logger = logging.getLogger(__name__)


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10.0)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,                    -- 'long' or 'short'
                status TEXT NOT NULL,                  -- 'open' or 'closed'
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                notional_usd REAL NOT NULL,
                leverage INTEGER NOT NULL,
                margin_usd REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                signal_type TEXT,
                signal_score REAL,
                signal_confidence REAL,
                signal_id INTEGER,
                -- exit info
                exit_price REAL,
                exit_reason TEXT,
                pnl_usd REAL,
                pnl_pct REAL,
                opened_at TEXT NOT NULL,
                closed_at TEXT,
                -- exchange info
                exchange_order_id TEXT,
                sl_order_id TEXT,
                tp_order_id TEXT,
                paper INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_positions_status
                ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_positions_symbol
                ON positions(symbol);

            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                trades_opened INTEGER DEFAULT 0,
                trades_closed INTEGER DEFAULT 0,
                realized_pnl REAL DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS webhook_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                event TEXT,
                symbol TEXT,
                payload TEXT,
                action TEXT,
                reason TEXT
            );
        """)
    logger.info(f"Storage initialized at {DB_PATH}")


# ======================================================================
# Positions
# ======================================================================
class PositionStore:
    _lock = Lock()

    @staticmethod
    def open_position(symbol: str, side: str, entry_price: float,
                      quantity: float, notional_usd: float,
                      leverage: int, margin_usd: float,
                      stop_loss: Optional[float],
                      take_profit: Optional[float],
                      signal_type: str, signal_score: float,
                      signal_confidence: float,
                      signal_id: Optional[int],
                      exchange_order_id: str = '',
                      sl_order_id: str = '',
                      tp_order_id: str = '',
                      paper: bool = True) -> int:
        with PositionStore._lock, _conn() as c:
            cur = c.execute("""
                INSERT INTO positions
                    (symbol, side, status, entry_price, quantity, notional_usd,
                     leverage, margin_usd, stop_loss, take_profit,
                     signal_type, signal_score, signal_confidence, signal_id,
                     opened_at, exchange_order_id, sl_order_id, tp_order_id, paper)
                VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                symbol, side, entry_price, quantity, notional_usd,
                leverage, margin_usd, stop_loss, take_profit,
                signal_type, signal_score, signal_confidence, signal_id,
                datetime.now().isoformat(),
                exchange_order_id, sl_order_id, tp_order_id,
                1 if paper else 0,
            ))
            return cur.lastrowid

    @staticmethod
    def close_position(position_id: int, exit_price: float,
                       exit_reason: str, pnl_usd: float, pnl_pct: float):
        with PositionStore._lock, _conn() as c:
            c.execute("""
                UPDATE positions
                SET status='closed', exit_price=?, exit_reason=?,
                    pnl_usd=?, pnl_pct=?, closed_at=?
                WHERE id=?
            """, (exit_price, exit_reason, pnl_usd, pnl_pct,
                  datetime.now().isoformat(), position_id))

    @staticmethod
    def get_open() -> List[Dict]:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM positions WHERE status='open' ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def get_open_count() -> int:
        with _conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS cnt FROM positions WHERE status='open'"
            ).fetchone()
            return int(row['cnt'] or 0)

    @staticmethod
    def get_open_by_symbol(symbol: str) -> Optional[Dict]:
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM positions WHERE status='open' AND symbol=? LIMIT 1",
                (symbol,)
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def get_recent(limit: int = 50) -> List[Dict]:
        with _conn() as c:
            rows = c.execute(
                "SELECT * FROM positions ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def get_today_stats() -> Dict:
        today = datetime.now().strftime('%Y-%m-%d')
        with _conn() as c:
            row = c.execute(
                "SELECT * FROM daily_stats WHERE date=?", (today,)
            ).fetchone()
            if not row:
                return {
                    'date': today, 'trades_opened': 0, 'trades_closed': 0,
                    'realized_pnl': 0.0, 'wins': 0, 'losses': 0,
                }
            return dict(row)

    @staticmethod
    def increment_daily(opened: int = 0, closed: int = 0,
                        pnl_delta: float = 0.0, is_win: Optional[bool] = None):
        today = datetime.now().strftime('%Y-%m-%d')
        with PositionStore._lock, _conn() as c:
            c.execute("""
                INSERT INTO daily_stats (date, trades_opened, trades_closed, realized_pnl, wins, losses)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    trades_opened = trades_opened + excluded.trades_opened,
                    trades_closed = trades_closed + excluded.trades_closed,
                    realized_pnl = realized_pnl + excluded.realized_pnl,
                    wins = wins + excluded.wins,
                    losses = losses + excluded.losses
            """, (
                today, opened, closed, pnl_delta,
                1 if is_win is True else 0,
                1 if is_win is False else 0,
            ))


# ======================================================================
# Webhook log
# ======================================================================
def log_webhook(event: str, symbol: str, payload: dict,
                action: str, reason: str = ''):
    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO webhook_log (received_at, event, symbol, payload, action, reason)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(), event, symbol,
                json.dumps(payload, default=str), action, reason,
            ))
    except Exception as e:
        logger.debug(f"log_webhook failed: {e}")
