"""TTM Squeeze Bot — literature defaults (Carter + crypto risk canon)."""
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

# --- Universe ---
MIN_TURNOVER_USD = float(os.getenv("MIN_TURNOVER_USD", "8000000"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "80"))
MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "30"))
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
BLACKLIST = {"BTC", "ETH", "XRP", "SOL", "BNB", "ADA", "DOGE", "TRX", "AVAX", "DOT", "LINK", "LTC", "BCH", "TON"}

# --- TTM classic (Carter) ---
BB_PERIOD = 20
BB_MULT = 2.0
KC_EMA = 20
KC_ATR = 20
KC_MULT = 1.5
MIN_SQUEEZE_BARS = int(os.getenv("MIN_SQUEEZE_BARS", "5"))  # literature 5–8; crypto 15m: 4–6
MOM_LENGTH = 12
VOL_SPIKE_MIN = float(os.getenv("VOL_SPIKE_MIN", "1.15"))

# --- Risk (crypto futures canon: 1% risk, modest leverage) ---
DEPOSIT_USD = float(os.getenv("DEPOSIT_USD", "500"))
LEVERAGE = float(os.getenv("LEVERAGE", "7"))
RISK_PCT = float(os.getenv("RISK_PCT", "1.0"))          # % of deposit per trade
RISK_USD = float(os.getenv("RISK_USD", str(DEPOSIT_USD * RISK_PCT / 100)))
SIZE_MIN_USD = float(os.getenv("SIZE_MIN_USD", "40"))
SIZE_MAX_USD = float(os.getenv("SIZE_MAX_USD", "200"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "2"))
DAILY_LOSS_USD = float(os.getenv("DAILY_LOSS_USD", "20"))
CONSEC_LOSS_BLOCK = int(os.getenv("CONSEC_LOSS_BLOCK", "3"))
SL_TRIGGER = os.getenv("SL_TRIGGER", "MarkPrice")

# --- Exit (Carter: structure SL + momentum fade + trail) ---
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "0.12"))
SL_CAP_PCT = float(os.getenv("SL_CAP_PCT", "1.8"))       # max stop distance
TP_R_MULTIPLE = float(os.getenv("TP_R_MULTIPLE", "2.0")) # first target 2R
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "1.0"))
BE_TRIGGER_R = float(os.getenv("BE_TRIGGER_R", "0.8"))   # move BE after 0.8R
PARTIAL_PCT = float(os.getenv("PARTIAL_PCT", "50"))
RECONCILE_SEC = int(os.getenv("RECONCILE_SEC", "20"))
COOLDOWN_HOURS = int(os.getenv("COOLDOWN_HOURS", "24"))

# BTC crash gate
BTC_15M_MIN = float(os.getenv("BTC_15M_MIN", "-0.9"))
ALLOW_SHORT = os.getenv("ALLOW_SHORT", "false").lower() == "true"
