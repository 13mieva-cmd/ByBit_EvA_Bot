"""TTM Squeeze Bot — Carter + crypto (15m+4H), closed-bar only, no HTF lookahead."""
import os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0") or 0)
TELEGRAM_ALLOWED_IDS = os.getenv("TELEGRAM_ALLOWED_IDS", "").strip()

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api-demo.bybit.com")

DATA_DIR = os.getenv("DATA_DIR", "/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."
STATE_FILE = os.path.join(DATA_DIR, "ttm_state.json")
METRICS_CSV = os.path.join(DATA_DIR, "ttm_trades.csv")

MIN_TURNOVER_USD = float(os.getenv("MIN_TURNOVER_USD", "8000000"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))
MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "30"))
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
BLACKLIST = {"BTC", "ETH", "XRP", "SOL", "BNB", "ADA", "DOGE", "TRX", "AVAX", "DOT", "LINK", "LTC", "BCH", "TON"}

BB_PERIOD = 20
BB_MULT = 2.0
KC_EMA = 20
KC_ATR = 20
KC_MULT = 1.5
MIN_SQUEEZE_BARS = int(os.getenv("MIN_SQUEEZE_BARS", "6"))
MAX_SQUEEZE_BARS = int(os.getenv("MAX_SQUEEZE_BARS", "18"))
MOM_LENGTH = int(os.getenv("MOM_LENGTH", "20"))
VOL_SPIKE_MIN = float(os.getenv("VOL_SPIKE_MIN", "1.7"))
BW_EXPAND_MIN = float(os.getenv("BW_EXPAND_MIN", "1.04"))
MOM_MIN_PCT = float(os.getenv("MOM_MIN_PCT", "0.08"))

EMA_FAST = int(os.getenv("EMA_FAST", "50"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "200"))
REQUIRE_EMA_STACK = os.getenv("REQUIRE_EMA_STACK", "true").lower() == "true"

HTF_INTERVAL = os.getenv("HTF_INTERVAL", "240")  # 4H
HTF_EMA = int(os.getenv("HTF_EMA", "50"))
REQUIRE_HTF = os.getenv("REQUIRE_HTF", "true").lower() == "true"

BTC_15M_MIN = float(os.getenv("BTC_15M_MIN", "-0.9"))
REQUIRE_BTC_TREND = os.getenv("REQUIRE_BTC_TREND", "true").lower() == "true"
BTC_HTF_INTERVAL = os.getenv("BTC_HTF_INTERVAL", "60")

DEPOSIT_USD = float(os.getenv("DEPOSIT_USD", "500"))
LEVERAGE = float(os.getenv("LEVERAGE", "7"))
RISK_PCT = float(os.getenv("RISK_PCT", "1.0"))
RISK_USD = float(os.getenv("RISK_USD", str(DEPOSIT_USD * RISK_PCT / 100)))
SIZE_MIN_USD = float(os.getenv("SIZE_MIN_USD", "40"))
SIZE_MAX_USD = float(os.getenv("SIZE_MAX_USD", "200"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "20"))
DAILY_LOSS_USD = float(os.getenv("DAILY_LOSS_USD", "20"))
CONSEC_LOSS_BLOCK = int(os.getenv("CONSEC_LOSS_BLOCK", "3"))
SL_TRIGGER = os.getenv("SL_TRIGGER", "MarkPrice")

SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.6"))
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "0.12"))
SL_CAP_PCT = float(os.getenv("SL_CAP_PCT", "2.2"))
TP_R_MULTIPLE = float(os.getenv("TP_R_MULTIPLE", "2.0"))
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "1.0"))
BE_TRIGGER_R = float(os.getenv("BE_TRIGGER_R", "0.8"))
PARTIAL_PCT = float(os.getenv("PARTIAL_PCT", "50"))
RECONCILE_SEC = int(os.getenv("RECONCILE_SEC", "20"))
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "24"))
MAX_HOLD_BARS = int(os.getenv("MAX_HOLD_BARS", "12"))
MOM_FADE_BARS = int(os.getenv("MOM_FADE_BARS", "2"))

ALLOW_SHORT = os.getenv("ALLOW_SHORT", "false").lower() == "true"
USE_CLOSED_BARS_ONLY = os.getenv("USE_CLOSED_BARS_ONLY", "true").lower() == "true"


# ============================================================================
# Pullback entry engine (v2): avoid chasing breakouts / buying at local highs.
#
# Research basis:
#  - Carter/TTM Squeeze: fire = first bar BB re-emerges outside KC, direction from
#    momentum histogram (rising & positive = long). Squeeze should run >=5-6 bars.
#  - Classic trend-following literature (pullback playbooks): never buy the initial
#    breakout — wait for price to retrace toward the fast EMA / broken level and
#    confirm with a bullish (or bearish) reversal candle before entering.
#  - Overextension filters: reject entries where price is already too many ATRs
#    away from its EMA (a stretched candle = chasing, not a low-risk entry).
#  - RSI sanity: avoid arming/entering when RSI is already at an exhausted extreme,
#    since strong trends can still hold overbought/oversold but the edge is worse.
# ============================================================================
ENTRY_MODE = os.getenv("ENTRY_MODE", "pullback")  # "pullback" (recommended) or "breakout" (legacy instant-fire entry)

# How long (in closed 15m bars) we keep watching a fired squeeze for a valid pullback+reclaim
PULLBACK_MAX_BARS = int(os.getenv("PULLBACK_MAX_BARS", "8"))
PULLBACK_MIN_BARS = int(os.getenv("PULLBACK_MIN_BARS", "1"))

# Reject a fresh squeeze-fire from being armed at all if price is already this many
# ATRs away from EMA_FAST -- i.e. the move already ran too far to call it a low-risk entry.
EXT_ATR_MAX_FIRE = float(os.getenv("EXT_ATR_MAX_FIRE", "2.5"))

# RSI sanity bands (14-period). Reject/­don't-confirm entries when RSI is at an exhausted extreme.
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_LONG_MIN = float(os.getenv("RSI_LONG_MIN", "45"))
RSI_LONG_MAX = float(os.getenv("RSI_LONG_MAX", "78"))
RSI_SHORT_MIN = float(os.getenv("RSI_SHORT_MIN", "22"))
RSI_SHORT_MAX = float(os.getenv("RSI_SHORT_MAX", "55"))

# Confirmation bar (the bar that actually triggers entry after the pullback) must show
# renewed participation: its volume must be at least this multiple of the 20-bar average.
PULLBACK_CONFIRM_VOL_MIN = float(os.getenv("PULLBACK_CONFIRM_VOL_MIN", "1.0"))

# Trailing-stop distance is now ATR-based (1.5-2x ATR is the literature-standard range)
# instead of a flat percent, so it adapts per-symbol volatility. Set to 0 to keep legacy TRAIL_PCT.
TRAIL_ATR_MULT = float(os.getenv("TRAIL_ATR_MULT", "1.8"))
