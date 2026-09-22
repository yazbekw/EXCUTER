"""
Execution Bot - Configuration
Reads from environment variables.
"""

import os


def _env(key: str, default: str = '') -> str:
    return os.environ.get(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).lower() in ('true', '1', 'yes', 'on')


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except Exception:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except Exception:
        return default


# ======================================================================
# Binance Futures credentials
# ======================================================================
BINANCE_API_KEY = _env('BINANCE_API_KEY', '')
BINANCE_API_SECRET = _env('BINANCE_API_SECRET', '')
BINANCE_TESTNET = _env_bool('BINANCE_TESTNET', False)  # testnet for testing

# ======================================================================
# Execution mode
# ======================================================================
PAPER_TRADING = _env_bool('PAPER_TRADING', True)   # ← مهم: افتراضي = ورق
LEVERAGE = _env_int('LEVERAGE', 20)
POSITION_SIZE_USD = _env_float('POSITION_SIZE_USD', 5.0)   # الهامش لكل صفقة
MAX_CONCURRENT_POSITIONS = _env_int('MAX_CONCURRENT_POSITIONS', 1)

# ======================================================================
# Webhook security
# ======================================================================
SHARED_SECRET = _env('EXECUTION_BOT_SECRET', '')   # يجب أن يطابق EXTERNAL_BOT_SECRET في المحلل

# ======================================================================
# Notifications
# ======================================================================
NTFY_TOPIC = _env('EXECUTION_NTFY_TOPIC', 'crypto_execution_alerts')
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

# ======================================================================
# Safety limits
# ======================================================================
MAX_DAILY_LOSS_USD = _env_float('MAX_DAILY_LOSS_USD', 20.0)
MAX_DAILY_TRADES = _env_int('MAX_DAILY_TRADES', 20)
MIN_CONFIDENCE = _env_float('MIN_CONFIDENCE', 30.0)   # النسبة المئوية الدنيا
MAX_SLIPPAGE_PCT = _env_float('MAX_SLIPPAGE_PCT', 0.5)  # 0.5%

# ======================================================================
# Storage
# ======================================================================
DATA_DIR = _env('DATA_DIR', '/data')
if not os.path.isdir(DATA_DIR):
    DATA_DIR = '.'
DB_PATH = os.path.join(DATA_DIR, 'execution.db')

# ======================================================================
# Server
# ======================================================================
PORT = _env_int('PORT', 5001)
LOG_LEVEL = _env('LOG_LEVEL', 'INFO').upper()


# ======================================================================
# Validation
# ======================================================================
def validate():
    errors = []
    if not PAPER_TRADING:
        if not BINANCE_API_KEY:
            errors.append('BINANCE_API_KEY is required for live trading')
        if not BINANCE_API_SECRET:
            errors.append('BINANCE_API_SECRET is required for live trading')
    if not SHARED_SECRET:
        errors.append('EXECUTION_BOT_SECRET is required for webhook authentication')
    return errors
