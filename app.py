"""
Execution Bot - Webhook Receiver
Receives signals from the Crypto Signal Analyzer and executes trades.

Receives:
  - 'entry' event         → open position
  - 'state_change' event  → close position
"""

import os
import hmac
import json
import logging
import hashlib
import threading
from datetime import datetime

from flask import Flask, request, jsonify

from config import SHARED_SECRET, PAPER_TRADING, PORT, LOG_LEVEL, LEVERAGE, POSITION_SIZE_USD, validate
from storage import init_db, PositionStore, log_webhook
from position_manager import handle_entry, handle_exit, close_all
from notifier import notify_system
from dashboard import register_dashboard

# ======================================================================
# Logging
# ======================================================================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ======================================================================
# Flask app
# ======================================================================
app = Flask(__name__)
register_dashboard(app)


# ======================================================================
# HMAC signature verification
# ======================================================================
def verify_signature(body_bytes: bytes, provided_sig: str) -> bool:
    if not SHARED_SECRET:
        return False
    expected = hmac.new(
        SHARED_SECRET.encode('utf-8'),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided_sig or '')


# ======================================================================
# Webhook
# ======================================================================
@app.route('/webhook', methods=['POST'])
def webhook():
    body_bytes = request.get_data()
    sig = request.headers.get('X-Signature', '')

    if not verify_signature(body_bytes, sig):
        logger.warning("Invalid signature — rejecting")
        return jsonify({'status': 'error', 'message': 'invalid signature'}), 401

    try:
        payload = json.loads(body_bytes.decode('utf-8'))
    except Exception as e:
        logger.error(f"Invalid JSON: {e}")
        return jsonify({'status': 'error', 'message': 'invalid json'}), 400

    event = payload.get('event', '')
    symbol = payload.get('symbol', '')

    logger.info(f"Webhook received: event={event}, symbol={symbol}")

    if event == 'entry':
        # Run in background so webhook returns fast
        def _bg():
            try:
                result = handle_entry(payload)
                log_webhook(event, symbol, payload,
                            result.get('status', 'unknown'),
                            result.get('reason', ''))
            except Exception as e:
                logger.exception(f"handle_entry failed: {e}")
                log_webhook(event, symbol, payload, 'error', str(e))
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({'status': 'accepted', 'event': event}), 202

    if event == 'state_change':
        def _bg():
            try:
                result = handle_exit(payload)
                log_webhook(event, symbol, payload,
                            result.get('status', 'unknown'),
                            result.get('reason', ''))
            except Exception as e:
                logger.exception(f"handle_exit failed: {e}")
                log_webhook(event, symbol, payload, 'error', str(e))
        threading.Thread(target=_bg, daemon=True).start()
        return jsonify({'status': 'accepted', 'event': event}), 202

    return jsonify({'status': 'ignored', 'event': event}), 200


# ======================================================================
# API
# ======================================================================
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'paper_trading': PAPER_TRADING,
        'leverage': LEVERAGE,
        'position_size_usd': POSITION_SIZE_USD,
        'open_positions': PositionStore.get_open_count(),
        'today': PositionStore.get_today_stats(),
        'timestamp': datetime.now().isoformat(),
    })
  
@app.route('/api/close_all_browser', methods=['GET'])
def close_all_browser():
    """Browser-friendly close_all — GET instead of POST."""
    result = close_all('manual')
    return jsonify(result)

@app.route('/api/positions', methods=['GET'])
def positions():
    return jsonify({
        'open': PositionStore.get_open(),
        'recent': PositionStore.get_recent(20),
        'today': PositionStore.get_today_stats(),
    })


@app.route('/api/close_all', methods=['POST'])
def close_all_endpoint():
    result = close_all('manual')
    return jsonify(result)


@app.route('/api/test_notify', methods=['GET'])
def test_notify():
    ok = notify_system('Test notification from execution bot', 'Execution Bot Test')
    return jsonify({'success': ok})


# ======================================================================
# Startup
# ======================================================================
def _startup():
    errors = validate()
    if errors:
        for e in errors:
            logger.error(f"Config error: {e}")
        logger.error("Startup aborted due to config errors")
        return
    init_db()
    mode = 'PAPER TRADING' if PAPER_TRADING else 'LIVE TRADING'
    logger.info(f"Execution Bot starting in {mode} mode")
    logger.info(f"Leverage: {LEVERAGE}x | Position size: ${POSITION_SIZE_USD}")
    try:
        notify_system(
            f"Execution Bot started\n"
            f"Mode: {mode}\n"
            f"Leverage: {LEVERAGE}x\n"
            f"Position size: ${POSITION_SIZE_USD}\n"
            f"Max concurrent positions: 1",
            'Execution Bot Started'
        )
    except Exception as e:
        logger.error(f"Startup notify failed: {e}")


_startup()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
