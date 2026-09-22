"""
Position Manager
Orchestrates open/close flows, calls executor + storage + notifier.
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
    """Handle an incoming 'entry' event."""
    symbol = signal.get('symbol', '')
    signal_type = signal.get('signal_type', '')
    direction = signal.get('direction', 'long')  # 'long' or 'short'

    # Risk check
    decision = risk_evaluate(signal)
    if not decision.allow:
        logger.warning(f"Entry rejected for {symbol}: {decision.reason}")
        notify_rejected(symbol, decision.reason, signal_type)
        return {'status': 'rejected', 'reason': decision.reason}

    # Extract SL/TP
    stop_loss = float(signal.get('stop_loss') or 0.0)
    take_profit = float(signal.get('take_profit') or 0.0)

    if stop_loss <= 0:
        reason = "no stop_loss provided"
        notify_rejected(symbol, reason, signal_type)
        return {'status': 'rejected', 'reason': reason}

    try:
        order, entry_price, quantity, notional = executor.open_position(
            symbol=symbol,
            side=direction,
            margin_usd=POSITION_SIZE_USD,
            leverage=LEVERAGE,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
    except Exception as e:
        logger.error(f"Execution failed for {symbol}: {e}")
        notify_rejected(symbol, f"execution error: {e}", signal_type)
        return {'status': 'failed', 'error': str(e)}

    # Save to DB
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

    # Notify
    notify_entry(
        symbol=symbol, side=direction,
        entry_price=entry_price, quantity=quantity,
        notional_usd=notional, margin_usd=POSITION_SIZE_USD,
        leverage=LEVERAGE, stop_loss=stop_loss, take_profit=take_profit,
        signal_type=signal_type, confidence=float(signal.get('percentage', 0.0)),
        paper=PAPER_TRADING,
    )

    logger.info(
        f"Position opened #{position_id}: {direction.upper()} {symbol} "
        f"@ {entry_price}, qty={quantity}"
    )
    return {
        'status': 'opened',
        'position_id': position_id,
        'entry_price': entry_price,
        'quantity': quantity,
    }


# ======================================================================
# Close on state change
# ======================================================================
def handle_exit(signal: Dict) -> Dict:
    """Handle an incoming 'state_change' event (close position)."""
    symbol = signal.get('symbol', '')
    reason = signal.get('reason', 'signal_change')

    position = PositionStore.get_open_by_symbol(symbol)
    if not position:
        logger.info(f"No open position for {symbol}, nothing to close")
        return {'status': 'no_position'}

    try:
        order, exit_price = executor.close_position(
            symbol=symbol,
            side=position['side'],
            quantity=float(position['quantity']),
        )
    except Exception as e:
        logger.error(f"Close failed for {symbol}: {e}")
        return {'status': 'failed', 'error': str(e)}

    pnl_usd, pnl_pct = executor.compute_pnl(
        side=position['side'],
        entry=float(position['entry_price']),
        exit_price=exit_price,
        quantity=float(position['quantity']),
    )

    PositionStore.close_position(
        position_id=position['id'],
        exit_price=exit_price,
        exit_reason=reason,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
    )
    PositionStore.increment_daily(
        closed=1, pnl_delta=pnl_usd, is_win=(pnl_usd >= 0)
    )

    # Duration
    try:
        opened = datetime.fromisoformat(position['opened_at'])
        duration_min = (datetime.now() - opened).total_seconds() / 60.0
    except Exception:
        duration_min = 0.0

    notify_exit(
        symbol=symbol, side=position['side'],
        entry_price=float(position['entry_price']),
        exit_price=exit_price,
        pnl_usd=pnl_usd, pnl_pct=pnl_pct,
        reason=reason,
        paper=PAPER_TRADING,
        duration_min=duration_min,
    )

    logger.info(
        f"Position closed #{position['id']}: {symbol} "
        f"PnL=${pnl_usd:+.2f} ({pnl_pct:+.2f}%)"
    )
    return {
        'status': 'closed',
        'position_id': position['id'],
        'exit_price': exit_price,
        'pnl_usd': pnl_usd,
        'pnl_pct': pnl_pct,
    }


# ======================================================================
# Emergency close all
# ======================================================================
def close_all(reason: str = 'manual') -> Dict:
    open_positions = PositionStore.get_open()
    closed = []
    for p in open_positions:
        try:
            result = handle_exit({
                'symbol': p['symbol'],
                'reason': reason,
            })
            closed.append(result)
        except Exception as e:
            logger.error(f"Failed to close {p['symbol']}: {e}")
    return {'closed': len(closed), 'details': closed}
