"""
Binance Futures Executor
Handles order placement, SL/TP, and position closing.
Supports paper trading mode.
"""

import logging
import time
from datetime import datetime
from typing import Optional, Dict, Tuple
from decimal import Decimal, ROUND_DOWN

from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET, BINANCE_TESTNET,
    PAPER_TRADING, LEVERAGE,
)

logger = logging.getLogger(__name__)

# Lazy init — don't connect until first use
_exchange = None


def get_exchange():
    global _exchange
    if _exchange is None:
        try:
            import ccxt
            opts = {
                'apiKey': BINANCE_API_KEY,
                'secret': BINANCE_API_SECRET,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',   # USDT-M Futures
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
# Symbol mapping: BTC/USDT -> BTCUSDT
# ======================================================================
def to_binance_symbol(symbol: str) -> str:
    return symbol.replace('/', '').upper()


# ======================================================================
# Market data
# ======================================================================
def get_current_price(symbol: str) -> float:
    """Get current mark price."""
    try:
        ex = get_exchange()
        ticker = ex.fetch_ticker(to_binance_symbol(symbol))
        return float(ticker['last'])
    except Exception as e:
        logger.error(f"get_current_price failed for {symbol}: {e}")
        raise


def get_market_info(symbol: str) -> Dict:
    """Return market info (precision, min qty, tick size)."""
    try:
        ex = get_exchange()
        markets = ex.load_markets()
        bs = to_binance_symbol(symbol)
        if bs in markets:
            return markets[bs]
        # try with slash
        if symbol in markets:
            return markets[symbol]
        raise ValueError(f"Symbol {symbol} not found")
    except Exception as e:
        logger.error(f"get_market_info failed: {e}")
        raise


# ======================================================================
# Quantity / price normalization
# ======================================================================
def _round_qty(qty: float, market: Dict) -> float:
    """Round quantity to exchange precision."""
    try:
        precision = market.get('precision', {}).get('amount')
        if precision is None:
            return float(qty)
        step = float(precision) if isinstance(precision, (int, float)) else 0.001
        if step <= 0:
            step = 0.001
        # for ccxt binanceusdm, precision.amount is often the step size
        result = round(qty / step) * step
        # Determine decimals
        s = f"{step:.10f}".rstrip('0')
        decimals = len(s.split('.')[1]) if '.' in s else 0
        return float(f"{result:.{decimals}f}")
    except Exception:
        return float(qty)


def _round_price(price: float, market: Dict) -> float:
    """Round price to exchange precision."""
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
    """
    notional = margin_usd * leverage
    qty = notional / entry_price
    market = get_market_info(symbol)
    qty = _round_qty(qty, market)

    # Check min quantity
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
        logger.info(f"[PAPER] set_leverage {symbol} -> {leverage}x")
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
                  take_profit: Optional[float]) -> Tuple[Dict, float, float, float]:
    """
    Returns: (order_result, entry_price, quantity, notional_usd)
    side: 'long' or 'short'
    """
    bs = to_binance_symbol(symbol)
    entry_price = get_current_price(symbol)
    qty = calculate_quantity(symbol, margin_usd, leverage, entry_price)
    notional = qty * entry_price

    # Set leverage first
    set_leverage(symbol, leverage)

    if PAPER_TRADING:
        logger.info(
            f"[PAPER] {side.upper()} {symbol}: qty={qty}, "
            f"entry~{entry_price}, notional=${notional:.2f}, "
            f"SL={stop_loss}, TP={take_profit}"
        )
        return (
            {'id': f'paper_{int(time.time())}', 'status': 'filled', 'paper': True},
            entry_price, qty, notional,
        )

    # LIVE
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

        # Try to get filled price
        filled_price = float(order.get('average') or order.get('price') or entry_price)

        # Place SL and TP
        sl_id = ''
        tp_id = ''
        if stop_loss and stop_loss > 0:
            try:
                sl_order = ex.create_order(
                    symbol=bs,
                    type='STOP_MARKET',
                    side='sell' if side == 'long' else 'buy',
                    amount=qty,
                    params={
                        'stopPrice': _round_price(stop_loss, get_market_info(symbol)),
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
                        'stopPrice': _round_price(take_profit, get_market_info(symbol)),
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
def close_position(symbol: str, side: str, quantity: float) -> Tuple[Dict, float]:
    """
    Close a position with a market order.
    Returns: (order_result, exit_price)
    """
    bs = to_binance_symbol(symbol)
    current_price = get_current_price(symbol)

    if PAPER_TRADING:
        logger.info(
            f"[PAPER] CLOSE {side.upper()} {symbol}: qty={quantity} @ ~{current_price}"
        )
        return ({'id': f'paper_close_{int(time.time())}', 'status': 'filled'},
                current_price)

    try:
        ex = get_exchange()
        # Close side: opposite
        order_side = 'sell' if side == 'long' else 'buy'

        # Cancel any SL/TP orders first
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
# Helpers for PnL
# ======================================================================
def compute_pnl(side: str, entry: float, exit_price: float,
                quantity: float) -> Tuple[float, float]:
    """Return (pnl_usd, pnl_pct_on_margin)."""
    if side == 'long':
        pnl_usd = (exit_price - entry) * quantity
    else:
        pnl_usd = (entry - exit_price) * quantity

    notional = entry * quantity
    if notional <= 0:
        return pnl_usd, 0.0
    pnl_pct = (pnl_usd / notional) * 100.0
    return pnl_usd, pnl_pct
