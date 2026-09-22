"""
Risk guard — decides whether to accept or reject a signal.
"""

import logging
from datetime import datetime

from config import (
    MAX_CONCURRENT_POSITIONS, MAX_DAILY_LOSS_USD,
    MAX_DAILY_TRADES, MIN_CONFIDENCE,
)
from storage import PositionStore

logger = logging.getLogger(__name__)


class RiskDecision:
    def __init__(self, allow: bool, reason: str = ''):
        self.allow = allow
        self.reason = reason


def evaluate(signal: dict) -> RiskDecision:
    """
    Return RiskDecision(allow=True/False, reason).
    signal is the parsed webhook payload.
    """
    # 1. Confidence filter
    confidence = abs(float(signal.get('percentage', 0.0)))
    if confidence < MIN_CONFIDENCE:
        return RiskDecision(False,
                            f"confidence {confidence:.1f}% below minimum {MIN_CONFIDENCE}%")

    # 2. Max concurrent positions
    open_count = PositionStore.get_open_count()
    if open_count >= MAX_CONCURRENT_POSITIONS:
        return RiskDecision(False,
                            f"already {open_count} open position(s), max is {MAX_CONCURRENT_POSITIONS}")

    # 3. Same symbol already open
    symbol = signal.get('symbol', '')
    if PositionStore.get_open_by_symbol(symbol):
        return RiskDecision(False, f"{symbol} already has an open position")

    # 4. Daily loss limit
    stats = PositionStore.get_today_stats()
    if stats['realized_pnl'] <= -MAX_DAILY_LOSS_USD:
        return RiskDecision(False,
                            f"daily loss limit reached ({stats['realized_pnl']:.2f} USD)")

    # 5. Daily trades limit
    if stats['trades_opened'] >= MAX_DAILY_TRADES:
        return RiskDecision(False,
                            f"daily trades limit reached ({stats['trades_opened']})")

    # 6. SL/TP sanity
    entry = float(signal.get('entry_price', 0.0))
    sl = float(signal.get('stop_loss', 0.0))
    tp = float(signal.get('take_profit', 0.0))
    direction = signal.get('direction', 'long')

    if entry <= 0:
        return RiskDecision(False, "entry_price missing")

    if sl > 0:
        if direction == 'long' and sl >= entry:
            return RiskDecision(False, "invalid SL for long (SL >= entry)")
        if direction == 'short' and sl <= entry:
            return RiskDecision(False, "invalid SL for short (SL <= entry)")
    else:
        return RiskDecision(False, "stop_loss missing")

    if tp > 0:
        if direction == 'long' and tp <= entry:
            return RiskDecision(False, "invalid TP for long (TP <= entry)")
        if direction == 'short' and tp >= entry:
            return RiskDecision(False, "invalid TP for short (TP >= entry)")

    return RiskDecision(True, 'ok')
