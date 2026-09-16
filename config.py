"""
Конфигурация "Super Trader Bot" (v3) — полная переработка бота-предшественника.
Все параметры вынесены в ENV с безопасными дефолтами.
"""
import os
from typing import List


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


EXCHANGE_ID = os.getenv("EXCHANGE_ID", "bybit")
API_KEY = os.getenv("BYBIT_API_KEY", os.getenv("API_KEY", ""))
API_SECRET = os.getenv("BYBIT_API_SECRET", os.getenv("API_SECRET", ""))

_default_symbols = "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT"
SYMBOLS: List[str] = [s.strip() for s in os.getenv("SYMBOLS", _default_symbols).split(",") if s.strip()]

TIMEFRAME = os.getenv("TIMEFRAME", "15m")
HIGHER_TIMEFRAME = os.getenv("HIGHER_TIMEFRAME", "1h")
CANDLE_LIMIT = int(os.getenv("CANDLE_LIMIT", "300"))
HIGHER_TF_LIMIT = int(os.getenv("HIGHER_TF_LIMIT", "300"))
HIGHER_TF_REFRESH_SEC = int(os.getenv("HIGHER_TF_REFRESH_SEC", "900"))
USE_CLOSED_BARS_ONLY = _b("USE_CLOSED_BARS_ONLY", "true")

BB_PERIOD = int(os.getenv("BB_PERIOD", "20"))
BB_MULT = float(os.getenv("BB_MULT", "2.0"))
KC_EMA = int(os.getenv("KC_EMA", "20"))
KC_ATR = int(os.getenv("KC_ATR", "20"))
KC_MULT = float(os.getenv("KC_MULT", "1.5"))
MIN_SQUEEZE_BARS = int(os.getenv("MIN_SQUEEZE_BARS", "6"))
MAX_SQUEEZE_BARS = int(os.getenv("MAX_SQUEEZE_BARS", "30"))
MOM_LENGTH = int(os.getenv("MOM_LENGTH", "20"))

EMA_FAST = int(os.getenv("EMA_FAST", "50"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "200"))
REQUIRE_EMA_STACK = _b("REQUIRE_EMA_STACK", "true")
REQUIRE_HTF = _b("REQUIRE_HTF", "true")
HTF_EMA = int(os.getenv("HTF_EMA", "50"))

EXT_ATR_MAX_FIRE = float(os.getenv("EXT_ATR_MAX_FIRE", "2.5"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_MIN = float(os.getenv("RSI_LONG_MIN", "45"))
RSI_LONG_MAX = float(os.getenv("RSI_LONG_MAX", "78"))
RSI_SHORT_MIN = float(os.getenv("RSI_SHORT_MIN", "22"))
RSI_SHORT_MAX = float(os.getenv("RSI_SHORT_MAX", "55"))

ENTRY_MODE = os.getenv("ENTRY_MODE", "pullback")
PULLBACK_MIN_BARS = int(os.getenv("PULLBACK_MIN_BARS", "1"))
PULLBACK_MAX_BARS = int(os.getenv("PULLBACK_MAX_BARS", "8"))
PULLBACK_CONFIRM_VOL_MIN = float(os.getenv("PULLBACK_CONFIRM_VOL_MIN", "1.0"))

SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "4.0"))
WEIGHTS = {
    "trend_ema": 1.5, "hammer": 1.0, "shooting_star": 1.0,
    "engulfing": 1.5, "squeeze": 2.0, "triangle": 1.5,
}

MIN_ATR_PERCENT = float(os.getenv("MIN_ATR_PERCENT", "0.25"))
VOLUME_MA_PERIOD = int(os.getenv("VOLUME_MA_PERIOD", "20"))
VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", "1.3"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "60"))

RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.01"))
MIN_RR_RATIO = float(os.getenv("MIN_RR_RATIO", "2.0"))
MAX_POSITION_PCT_OF_BALANCE = float(os.getenv("MAX_POSITION_PCT_OF_BALANCE", "0.20"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))
DAILY_LOSS_PCT = float(os.getenv("DAILY_LOSS_PCT", "0.04"))
CONSEC_LOSS_BLOCK = int(os.getenv("CONSEC_LOSS_BLOCK", "3"))
CONSEC_LOSS_BLOCK_HOURS = int(os.getenv("CONSEC_LOSS_BLOCK_HOURS", "24"))
DAILY_LOSS_BLOCK_HOURS = int(os.getenv("DAILY_LOSS_BLOCK_HOURS", "20"))

LEVERAGE = float(os.getenv("LEVERAGE", "3"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")

SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.6"))
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "0.12"))
SL_CAP_PCT = float(os.getenv("SL_CAP_PCT", "3.0"))
TP_R_MULTIPLE = float(os.getenv("TP_R_MULTIPLE", "2.0"))
BE_TRIGGER_R = float(os.getenv("BE_TRIGGER_R", "0.8"))
TRAIL_ATR_MULT = float(os.getenv("TRAIL_ATR_MULT", "1.8"))
PARTIAL_PCT = float(os.getenv("PARTIAL_PCT", "50"))
MAX_HOLD_BARS = int(os.getenv("MAX_HOLD_BARS", "24"))
MOM_FADE_BARS = int(os.getenv("MOM_FADE_BARS", "2"))

ALLOW_SHORT = _b("ALLOW_SHORT", "true")

LOOP_INTERVAL = int(os.getenv("LOOP_INTERVAL", "60"))
RECONCILE_INTERVAL = int(os.getenv("RECONCILE_INTERVAL", "30"))
DRY_RUN = _b("DRY_RUN", "true")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

DATA_DIR = os.getenv("DATA_DIR", ".")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."
STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
SIGNALS_CSV = os.path.join(DATA_DIR, "signals.csv")
TRADES_CSV = os.path.join(DATA_DIR, "trades.csv")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
