"""
Binance Futures Executor
Handles order placement, SL/TP, and position closing.

IMPORTANT: In PAPER_TRADING mode, NEVER contacts Binance.
Uses prices and quantities provided by the signal.
"""

import logging
import time
from datetime import datetime
from typing import Optional, Dict, Tuple

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    PAPER_TRADING, LEVERAGE,
)

logger = logging.getLogger(__name__)

# Lazy init — created only when LIVE trading is used
_exchange = None
_markets_loaded = False


def get_exchange():
    """Initialize the exchange client (LIVE mode only)."""
    global _exchange
    if _exchange is None:
        try:
            import ccxt
            opts = {
                'apiKey': BINANCE_API_KEY,
                'secret': BINANCE_API_SECRET,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                    'adjustForTimeDifference': True,
                },
            }
            _exchange = ccxt.binanceusdm(opts)
            if BINANCE_TESTNET:
                _exchange.set_sandbox_mode(True)
                logger.info("Binance Futures executor initialized in TESTNET")
            else:
                logger.info("Binance Futures executor initialized (LIVE)")
        except Exception as e:
            logger.error(f"Failed to init exchange: {e}")
            raise
    return _exchange


# ======================================================================
# Symbol mapping
# ======================================================================
def to_binance_symbol(symbol: str) -> str:
    return symbol.replace('/', '').upper()


# ======================================================================
# LIVE-only helpers (only called when PAPER_TRADING is False)
# ======================================================================
def _get_current_price_live(symbol: str) -> float:
    """Get current price from Binance (LIVE only)."""
    ex = get_exchange()
    ticker = ex.fetch_ticker(to_binance_symbol(symbol))
    return float(ticker['last'])


def _get_market_info_live(symbol: str) -> Dict:
    """Load market info from Binance (LIVE only)."""
    ex = get_exchange()
    markets = ex.load_markets()
    bs = to_binance_symbol(symbol)
    if bs in markets:
        return markets[bs]
    if symbol in markets:
        return markets[symbol]
    raise ValueError(f"Symbol {symbol} not found on Binance Futures")


def _round_qty_live(qty: float, market: Dict) -> float:
    try:
        precision = market.get('precision', {}).get('amount')
        if precision is None:
            return float(qty)
        step = float(precision) if isinstance(precision, (int, float)) else 0.001
        if step <= 0:
            step = 0.001
        result = round(qty / step) * step
        s = f"{step:.10f}".rstrip('0')
        decimals = len(s.split('.')[1]) if '.' in s else 0
        return float(f"{result:.{decimals}f}")
    except Exception:
        return float(qty)


def _round_price_live(price: float, market: Dict) -> float:
    try:
        precision = market.get('precision', {}).get('price')
        if precision is None:
            return float(price)
        step = float(precision) if isinstance(precision, (int, float)) else 0.01
        if step <= 0:
            step = 0.01
        result = round(price / step) * step
        s = f"{step:.10f}".rstrip('0')
        decimals = len(s.split('.')[1]) if '.' in s else 0
        return float(f"{result:.{decimals}f}")
    except Exception:
        return float(price)


# ======================================================================
# Position sizing
# ======================================================================
def calculate_quantity(symbol: str, margin_usd: float,
                       leverage: int, entry_price: float) -> float:
    """
    Quantity = (margin * leverage) / entry_price.
    In PAPER: pure math, no Binance call.
    In LIVE: also rounds to exchange precision.
    """
    notional = margin_usd * leverage
    qty = notional / entry_price

    if PAPER_TRADING:
        # Round to 8 decimals for cleanliness; no exchange call
        return round(qty, 8)

    market = _get_market_info_live(symbol)
    qty = _round_qty_live(qty, market)

    limits = market.get('limits', {}).get('amount', {})
    min_qty = limits.get('min')
    if min_qty and qty < float(min_qty):
        raise ValueError(
            f"Quantity {qty} below exchange minimum {min_qty} for {symbol}. "
            f"Increase POSITION_SIZE_USD."
        )
    return qty


# ======================================================================
# Set leverage
# ======================================================================
def set_leverage(symbol: str, leverage: int) -> bool:
    if PAPER_TRADING:
        logger.info(f"[PAPER] set_leverage {symbol} -> {leverage}x (skipped)")
        return True
    try:
        ex = get_exchange()
        ex.set_leverage(leverage, to_binance_symbol(symbol))
        logger.info(f"Leverage set to {leverage}x for {symbol}")
        return True
    except Exception as e:
        logger.warning(f"set_leverage failed for {symbol}: {e}")
        return False


# ======================================================================
# Open position
# ======================================================================
def open_position(symbol: str, side: str, margin_usd: float,
                  leverage: int, stop_loss: Optional[float],
                  take_profit: Optional[float],
                  entry_price_hint: Optional[float] = None) -> Tuple[Dict, float, float, float]:
    """
    Returns: (order_result, entry_price, quantity, notional_usd)
    side: 'long' or 'short'

    In PAPER mode: no Binance contact; uses entry_price_hint from signal.
    In LIVE mode: fetches from Binance, places market order + SL + TP.
    """
    # --------------------------------------------------------------
    # PAPER MODE — no Binance contact at all
    # --------------------------------------------------------------
    if PAPER_TRADING:
        if not entry_price_hint or entry_price_hint <= 0:
            raise ValueError(
                f"PAPER mode requires entry_price_hint for {symbol}"
            )

        entry_price = float(entry_price_hint)
        qty = calculate_quantity(symbol, margin_usd, leverage, entry_price)
        notional = qty * entry_price

        logger.info(
            f"[PAPER] OPEN {side.upper()} {symbol}: qty={qty}, "
            f"entry~{entry_price}, notional=${notional:.2f}, "
            f"margin=${margin_usd:.2f} @ {leverage}x, "
            f"SL={stop_loss}, TP={take_profit}"
        )
        return (
            {'id': f'paper_{int(time.time())}', 'status': 'filled', 'paper': True},
            entry_price, qty, notional,
        )

    # --------------------------------------------------------------
    # LIVE MODE — full execution
    # --------------------------------------------------------------
    bs = to_binance_symbol(symbol)
    entry_price = _get_current_price_live(symbol)
    qty = calculate_quantity(symbol, margin_usd, leverage, entry_price)
    notional = qty * entry_price

    set_leverage(symbol, leverage)

    try:
        ex = get_exchange()
        order_side = 'buy' if side == 'long' else 'sell'
        order = ex.create_order(
            symbol=bs,
            type='market',
            side=order_side,
            amount=qty,
        )
        logger.info(f"Market order placed: {order.get('id')}")

        filled_price = float(order.get('average') or order.get('price') or entry_price)

        sl_id = ''
        tp_id = ''
        market = _get_market_info_live(symbol)

        if stop_loss and stop_loss > 0:
            try:
                sl_order = ex.create_order(
                    symbol=bs,
                    type='STOP_MARKET',
                    side='sell' if side == 'long' else 'buy',
                    amount=qty,
                    params={
                        'stopPrice': _round_price_live(stop_loss, market),
                        'reduceOnly': True,
                        'workingType': 'MARK_PRICE',
                    },
                )
                sl_id = sl_order.get('id', '')
                logger.info(f"SL order placed: {sl_id} at {stop_loss}")
            except Exception as e:
                logger.error(f"Failed to place SL: {e}")

        if take_profit and take_profit > 0:
            try:
                tp_order = ex.create_order(
                    symbol=bs,
                    type='TAKE_PROFIT_MARKET',
                    side='sell' if side == 'long' else 'buy',
                    amount=qty,
                    params={
                        'stopPrice': _round_price_live(take_profit, market),
                        'reduceOnly': True,
                        'workingType': 'MARK_PRICE',
                    },
                )
                tp_id = tp_order.get('id', '')
                logger.info(f"TP order placed: {tp_id} at {take_profit}")
            except Exception as e:
                logger.error(f"Failed to place TP: {e}")

        order['sl_order_id'] = sl_id
        order['tp_order_id'] = tp_id
        return order, filled_price, qty, notional

    except Exception as e:
        logger.error(f"open_position failed for {symbol}: {e}")
        raise


# ======================================================================
# Close position
# ======================================================================
def close_position(symbol: str, side: str, quantity: float,
                   exit_price_hint: Optional[float] = None) -> Tuple[Dict, float]:
    """
    Close a position.

    In PAPER: uses exit_price_hint (from signal current price) or raises.
    In LIVE: places a reduce-only market order.
    """
    if PAPER_TRADING:
        if not exit_price_hint or exit_price_hint <= 0:
            raise ValueError(
                f"PAPER mode requires exit_price_hint for {symbol}"
            )
        current_price = float(exit_price_hint)

        logger.info(
            f"[PAPER] CLOSE {side.upper()} {symbol}: qty={quantity} @ ~{current_price}"
        )
        return (
            {'id': f'paper_close_{int(time.time())}', 'status': 'filled'},
            current_price,
        )

    # LIVE
    bs = to_binance_symbol(symbol)
    current_price = _get_current_price_live(symbol)

    try:
        ex = get_exchange()
        order_side = 'sell' if side == 'long' else 'buy'

        # Cancel SL/TP first
        try:
            open_orders = ex.fetch_open_orders(bs)
            for o in open_orders:
                try:
                    ex.cancel_order(o['id'], bs)
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"Could not cancel open orders: {e}")

        order = ex.create_order(
            symbol=bs,
            type='market',
            side=order_side,
            amount=quantity,
            params={'reduceOnly': True},
        )
        filled_price = float(order.get('average') or order.get('price') or current_price)
        logger.info(f"Position closed: {order.get('id')} @ {filled_price}")
        return order, filled_price

    except Exception as e:
        logger.error(f"close_position failed for {symbol}: {e}")
        raise


# ======================================================================
# PnL
# ======================================================================
def compute_pnl(side: str, entry: float, exit_price: float,
                quantity: float) -> Tuple[float, float]:
    """Return (pnl_usd, pnl_pct_on_notional)."""
    if side == 'long':
        pnl_usd = (exit_price - entry) * quantity
    else:
        pnl_usd = (entry - exit_price) * quantity

    notional = entry * quantity
    if notional <= 0:
        return pnl_usd, 0.0
    pnl_pct = (pnl_usd / notional) * 100.0
    return pnl_usd, pnl_pct
