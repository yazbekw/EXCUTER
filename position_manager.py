"""
Position Manager
Orchestrates open/close flows, calls executor + storage + notifier.

IMPORTANT: This module works in two modes:
  - PAPER: uses prices from the incoming signal payload (never contacts Binance)
  - LIVE: contacts Binance via executor
"""

import logging
from datetime import datetime
from typing import Optional, Dict

from config import (
    PAPER_TRADING, LEVERAGE, POSITION_SIZE_USD,
)
from storage import PositionStore
from notifier import notify_entry, notify_exit, notify_rejected
from risk_guard import evaluate as risk_evaluate
import executor

logger = logging.getLogger(__name__)


# ======================================================================
# Open from a signal
# ======================================================================
def handle_entry(signal: Dict) -> Dict:
    """
    Handle an incoming 'entry' event.

    Expected signal keys:
      - symbol            e.g. "SOL/USDT"
      - signal_type       e.g. "STRONG BUY"
      - direction         "long" or "short"
      - entry_price       reference price from the analyzer
      - stop_loss         from the analyzer (ATR-based)
      - take_profit       from the analyzer
      - percentage        signal confidence (0-100)
      - score             raw signal score
      - signal_id         DB id in analyzer's Supabase (optional)
    """
    symbol = signal.get('symbol', '')
    signal_type = signal.get('signal_type', '')
    direction = signal.get('direction', 'long')

    # --------------------------------------------------------------
    # 1. Risk check
    # --------------------------------------------------------------
    decision = risk_evaluate(signal)
    if not decision.allow:
        logger.warning(f"Entry rejected for {symbol}: {decision.reason}")
        notify_rejected(symbol, decision.reason, signal_type)
        return {'status': 'rejected', 'reason': decision.reason}

    # --------------------------------------------------------------
    # 2. Extract SL/TP/entry hint from signal
    # --------------------------------------------------------------
    stop_loss = float(signal.get('stop_loss') or 0.0)
    take_profit = float(signal.get('take_profit') or 0.0)
    entry_price_hint = float(signal.get('entry_price') or 0.0)

    if stop_loss <= 0:
        reason = "no stop_loss provided"
        notify_rejected(symbol, reason, signal_type)
        return {'status': 'rejected', 'reason': reason}

    if entry_price_hint <= 0:
        reason = "no entry_price provided"
        notify_rejected(symbol, reason, signal_type)
        return {'status': 'rejected', 'reason': reason}

    # --------------------------------------------------------------
    # 3. Execute (paper: no Binance; live: full execution)
    # --------------------------------------------------------------
    try:
        order, entry_price, quantity, notional = executor.open_position(
            symbol=symbol,
            side=direction,
            margin_usd=POSITION_SIZE_USD,
            leverage=LEVERAGE,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_price_hint=entry_price_hint,
        )
    except Exception as e:
        logger.error(f"Execution failed for {symbol}: {e}")
        notify_rejected(symbol, f"execution error: {e}", signal_type)
        return {'status': 'failed', 'error': str(e)}

    # --------------------------------------------------------------
    # 4. Save to DB
    # --------------------------------------------------------------
    try:
        position_id = PositionStore.open_position(
            symbol=symbol,
            side=direction,
            entry_price=entry_price,
            quantity=quantity,
            notional_usd=notional,
            leverage=LEVERAGE,
            margin_usd=POSITION_SIZE_USD,
            stop_loss=stop_loss,
            take_profit=take_profit,
            signal_type=signal_type,
            signal_score=float(signal.get('score', 0.0)),
            signal_confidence=float(signal.get('percentage', 0.0)),
            signal_id=signal.get('signal_id'),
            exchange_order_id=str(order.get('id', '')),
            sl_order_id=str(order.get('sl_order_id', '')),
            tp_order_id=str(order.get('tp_order_id', '')),
            paper=PAPER_TRADING,
        )
        PositionStore.increment_daily(opened=1)
    except Exception as e:
        logger.error(f"DB save failed for {symbol}: {e}")
        # Position already opened on exchange — we still report partial success
        # so the caller knows about the situation, but we continue to notify.
        position_id = None

    # --------------------------------------------------------------
    # 5. Notify
    # --------------------------------------------------------------
    try:
        notify_entry(
            symbol=symbol,
            side=direction,
            entry_price=entry_price,
            quantity=quantity,
            notional_usd=notional,
            margin_usd=POSITION_SIZE_USD,
            leverage=LEVERAGE,
            stop_loss=stop_loss,
            take_profit=take_profit,
            signal_type=signal_type,
            confidence=float(signal.get('percentage', 0.0)),
            paper=PAPER_TRADING,
        )
    except Exception as e:
        logger.error(f"Notification failed for {symbol}: {e}")

    logger.info(
        f"Position opened #{position_id}: {direction.upper()} {symbol} "
        f"@ {entry_price}, qty={quantity}, "
        f"mode={'PAPER' if PAPER_TRADING else 'LIVE'}"
    )

    return {
        'status': 'opened',
        'position_id': position_id,
        'entry_price': entry_price,
        'quantity': quantity,
        'notional_usd': notional,
        'paper': PAPER_TRADING,
    }


# ======================================================================
# Close on state change
# ======================================================================
def handle_exit(signal: Dict) -> Dict:
    """
    Handle an incoming 'state_change' event → close the open position.

    Expected signal keys:
      - symbol            e.g. "SOL/USDT"
      - current_price     latest price from analyzer (used in paper mode)
      - reason            e.g. "signal_now_neutral"
      - action            "close_position"
    """
    symbol = signal.get('symbol', '')
    reason = signal.get('reason', 'signal_change')
    exit_price_hint = float(signal.get('current_price') or 0.0)

    # --------------------------------------------------------------
    # 1. Find open position
    # --------------------------------------------------------------
    position = PositionStore.get_open_by_symbol(symbol)
    if not position:
        logger.info(f"No open position for {symbol}, nothing to close")
        return {'status': 'no_position'}

    # In paper mode, we need an exit price hint
    if PAPER_TRADING and exit_price_hint <= 0:
        # Fall back to entry price (worst case: no PnL)
        exit_price_hint = float(position.get('entry_price') or 0.0)
        logger.warning(
            f"No current_price in state_change for {symbol}, "
            f"using entry_price as fallback"
        )

    # --------------------------------------------------------------
    # 2. Close (paper: no Binance; live: full execution)
    # --------------------------------------------------------------
    try:
        order, exit_price = executor.close_position(
            symbol=symbol,
            side=position['side'],
            quantity=float(position['quantity']),
            exit_price_hint=exit_price_hint,
        )
    except Exception as e:
        logger.error(f"Close failed for {symbol}: {e}")
        # Do NOT close in DB — position may still be open on exchange
        return {'status': 'failed', 'error': str(e)}

    # --------------------------------------------------------------
    # 3. Compute PnL
    # --------------------------------------------------------------
    try:
        pnl_usd, pnl_pct = executor.compute_pnl(
            side=position['side'],
            entry=float(position['entry_price']),
            exit_price=exit_price,
            quantity=float(position['quantity']),
        )
    except Exception as e:
        logger.error(f"PnL computation failed for {symbol}: {e}")
        pnl_usd, pnl_pct = 0.0, 0.0

    # --------------------------------------------------------------
    # 4. Update DB
    # --------------------------------------------------------------
    try:
        PositionStore.close_position(
            position_id=position['id'],
            exit_price=exit_price,
            exit_reason=reason,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
        )
        PositionStore.increment_daily(
            closed=1,
            pnl_delta=pnl_usd,
            is_win=(pnl_usd >= 0),
        )
    except Exception as e:
        logger.error(f"DB update on close failed for {symbol}: {e}")

    # --------------------------------------------------------------
    # 5. Duration
    # --------------------------------------------------------------
    try:
        opened = datetime.fromisoformat(position['opened_at'])
        duration_min = (datetime.now() - opened).total_seconds() / 60.0
    except Exception:
        duration_min = 0.0

    # --------------------------------------------------------------
    # 6. Notify
    # --------------------------------------------------------------
    try:
        notify_exit(
            symbol=symbol,
            side=position['side'],
            entry_price=float(position['entry_price']),
            exit_price=exit_price,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            reason=reason,
            paper=PAPER_TRADING,
            duration_min=duration_min,
        )
    except Exception as e:
        logger.error(f"Notification failed for {symbol}: {e}")

    logger.info(
        f"Position closed #{position['id']}: {symbol} "
        f"PnL=${pnl_usd:+.2f} ({pnl_pct:+.2f}%) "
        f"mode={'PAPER' if PAPER_TRADING else 'LIVE'} "
        f"reason={reason}"
    )

    return {
        'status': 'closed',
        'position_id': position['id'],
        'exit_price': exit_price,
        'pnl_usd': pnl_usd,
        'pnl_pct': pnl_pct,
        'reason': reason,
        'paper': PAPER_TRADING,
    }


# ======================================================================
# Emergency close all
# ======================================================================
def close_all(reason: str = 'manual') -> Dict:
    """Close all open positions. Used for emergency stops."""
    open_positions = PositionStore.get_open()
    results = []
    closed_count = 0
    failed_count = 0

    for p in open_positions:
        try:
            result = handle_exit({
                'symbol': p['symbol'],
                'reason': reason,
                'current_price': 0.0,   # will fall back to entry price
            })
            if result.get('status') == 'closed':
                closed_count += 1
            else:
                failed_count += 1
            results.append({
                'symbol': p['symbol'],
                'result': result.get('status'),
                'pnl_usd': result.get('pnl_usd'),
            })
        except Exception as e:
            logger.error(f"Failed to close {p['symbol']}: {e}")
            failed_count += 1
            results.append({
                'symbol': p['symbol'],
                'result': 'error',
                'error': str(e),
            })

    logger.info(
        f"close_all: {closed_count} closed, {failed_count} failed "
        f"(reason={reason})"
    )
    return {
        'status': 'ok',
        'total_open': len(open_positions),
        'closed': closed_count,
        'failed': failed_count,
        'details': results,
    }
