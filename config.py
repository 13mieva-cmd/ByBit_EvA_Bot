"""
Configuration for Bybit Open Interest LONG Scanner v2.
Detects birth of an uptrend via Price + OI confluence.
Three signal types: Standard, OI Surge, Pullback Continuation.
"""
import os

# ---------- Telegram ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# ---------- Scanner core ----------
SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MIN", "10"))
ALERT_COOLDOWN_HOURS = int(os.getenv("ALERT_COOLDOWN_HOURS", "6"))
MAX_ALERTS_PER_SCAN = int(os.getenv("MAX_ALERTS_PER_SCAN", "6"))
MIN_STARS_TO_ALERT = int(os.getenv("MIN_STARS_TO_ALERT", "1"))

# ---------- Pre-filter ----------
MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "60"))
MIN_VOLUME_USD_24H = float(os.getenv("MIN_VOLUME_USD_24H", "5000000"))

# ---------- STANDARD signal (the original — UB-quality) ----------
PRICE_CHANGE_4H_MIN = float(os.getenv("PRICE_CHANGE_4H_MIN", "3.0"))   # back to 3% (was lowered to 2 — caused early entries)
PRICE_CHANGE_4H_MAX = float(os.getenv("PRICE_CHANGE_4H_MAX", "8.0"))
OI_CHANGE_4H_MIN = float(os.getenv("OI_CHANGE_4H_MIN", "10.0"))
OI_CHANGE_24H_2STAR = float(os.getenv("OI_CHANGE_24H_2STAR", "20.0"))
VOLUME_SPIKE_MIN = float(os.getenv("VOLUME_SPIKE_MIN", "1.5"))
VOLUME_SPIKE_2STAR = float(os.getenv("VOLUME_SPIKE_2STAR", "2.0"))
RSI_4H_MIN = float(os.getenv("RSI_4H_MIN", "50"))
RSI_4H_MAX = float(os.getenv("RSI_4H_MAX", "70"))

# ---------- OI SURGE signal (catches faster moves) ----------
ENABLE_OI_SURGE = os.getenv("ENABLE_OI_SURGE", "true").lower() == "true"
SURGE_PRICE_1H_MIN = float(os.getenv("SURGE_PRICE_1H_MIN", "1.5"))    # +1.5% in 1h
SURGE_PRICE_1H_MAX = float(os.getenv("SURGE_PRICE_1H_MAX", "5.0"))    # not more than +5%
SURGE_OI_1H_MIN = float(os.getenv("SURGE_OI_1H_MIN", "5.0"))          # OI +5% in 1h
SURGE_OI_24H_MIN = float(os.getenv("SURGE_OI_24H_MIN", "5.0"))        # NEW: OI 24h ≥ +5% — protects from closing-shorts squeeze
SURGE_RSI_1H_MAX = float(os.getenv("SURGE_RSI_1H_MAX", "65"))

# ---------- PULLBACK CONTINUATION signal ----------
ENABLE_PULLBACK = os.getenv("ENABLE_PULLBACK", "true").lower() == "true"
PULLBACK_RSI_1H_MIN = float(os.getenv("PULLBACK_RSI_1H_MIN", "45"))   # tightened from 40 — was catching trend-breaks
PULLBACK_RSI_1H_MAX = float(os.getenv("PULLBACK_RSI_1H_MAX", "55"))
PULLBACK_EMA_DISTANCE_PCT = float(os.getenv("PULLBACK_EMA_DISTANCE_PCT", "1.5"))  # tightened from 2.0
PULLBACK_OI_24H_MIN = float(os.getenv("PULLBACK_OI_24H_MIN", "15.0"))
PULLBACK_OI_1H_MIN = float(os.getenv("PULLBACK_OI_1H_MIN", "0.0"))    # NEW: OI 1h ≥ 0% — current OI not falling

# ---------- EMA filter ----------
USE_EMA_FILTER = os.getenv("USE_EMA_FILTER", "true").lower() == "true"
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "50"))
EMA_PULLBACK_PERIOD = int(os.getenv("EMA_PULLBACK_PERIOD", "21"))

# ---------- BTC filter ----------
BTC_MIN_1H_CHANGE = float(os.getenv("BTC_MIN_1H_CHANGE", "-0.5"))

# ---------- Trade parameters ----------
TP1_PCT = float(os.getenv("TP1_PCT", "2.0"))
TP2_PCT = float(os.getenv("TP2_PCT", "5.0"))
HARD_SL_PCT = float(os.getenv("HARD_SL_PCT", "10.0"))   # Aviation-style emergency only
OI_DROP_WARNING_PCT = float(os.getenv("OI_DROP_WARNING_PCT", "5.0"))   # warn if OI -5% in 1h
POSITION_TIMEOUT_HOURS = int(os.getenv("POSITION_TIMEOUT_HOURS", "24"))  # extended
POSITION_CHECK_INTERVAL_MIN = int(os.getenv("POSITION_CHECK_INTERVAL_MIN", "5"))

# Smart hold: when in +X% profit, monitor OI health intensively
SMART_HOLD_THRESHOLD_PCT = float(os.getenv("SMART_HOLD_THRESHOLD_PCT", "3.0"))

# ---------- Local ignore ----------
IGNORE_DURATION_HOURS = int(os.getenv("IGNORE_DURATION_HOURS", "24"))

# ---------- Storage ----------
# ВАЖНО: на Railway файловая система ЭФЕМЕРНАЯ — при редеплое всё стирается.
# Без volume состояние (открытые позиции!) терялось, а сделки на бирже оставались.
# DATA_DIR должен указывать на смонтированный volume (обычно /data).
DATA_DIR = os.getenv("DATA_DIR", "/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = "."          # локальный запуск без volume

POSITIONS_FILE = os.getenv("POSITIONS_FILE", os.path.join(DATA_DIR, "oi_positions.json"))
IGNORE_FILE = os.getenv("IGNORE_FILE", os.path.join(DATA_DIR, "oi_ignore.json"))
STATS_FILE = os.getenv("STATS_FILE", os.path.join(DATA_DIR, "oi_stats.json"))

# ---------- Daily report ----------
DAILY_REPORT_HOUR_UTC = int(os.getenv("DAILY_REPORT_HOUR_UTC", "20"))

# ---------- Blacklist ----------
BLACKLIST = {
    "BTC", "ETH", "XRP", "SOL", "BNB", "ADA", "DOGE", "TRX",
    "AVAX", "DOT", "LINK", "MATIC", "LTC", "BCH", "TON",
    "USDC", "USDT", "DAI", "TUSD", "FDUSD", "FOLKS"
}


# ============================================================
# AUTO-TRADING CONFIGURATION
# ============================================================

# Bybit API credentials — set in Railway Variables, NEVER hardcode
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")

# ---------- РИСК: депозит 500$, плечо 10x ----------
DEPOSIT_USD = float(os.getenv("DEPOSIT_USD", "500"))
LEVERAGE = float(os.getenv("LEVERAGE", "10"))          # ФИКСИРОВАННОЕ плечо (было max_leverage биржи!)
RISK_PCT_PER_TRADE = float(os.getenv("RISK_PCT_PER_TRADE", "1.5"))  # % депозита на сделку

# Номинал позиции. При 500$ и плече 10 разумный размер 250-500$.
POSITION_SIZE_USD = float(os.getenv("POSITION_SIZE_USD", "250"))

# ВАЖНО про стопы:
# Раньше стоял SL = 30% при МАКСИМАЛЬНОМ плече биржи. При плече 25-75x ликвидация
# наступает на 1-4%, то есть стоп на 30% не срабатывал НИКОГДА — позицию просто
# ликвидировало. Теперь плечо 10x (ликвидация ~10%), а стоп 2% — он реально сработает.
AUTO_TP_PCT = float(os.getenv("AUTO_TP_PCT", "3.0"))          # было 2.15
AUTO_HARD_SL_PCT = float(os.getenv("AUTO_HARD_SL_PCT", "2.0"))# было 30.0 (!)

# PULLBACK — быстрый скальп, цели ближе
AUTO_PULLBACK_TP_PCT = float(os.getenv("AUTO_PULLBACK_TP_PCT", "1.8"))  # было 1.15
AUTO_PULLBACK_SL_PCT = float(os.getenv("AUTO_PULLBACK_SL_PCT", "1.2"))  # было 30.0 (!)

# Limits
MAX_AUTO_POSITIONS = int(os.getenv("MAX_AUTO_POSITIONS", "2"))
# Дневной лимит: раньше 300$ при убытке 300$ с одной сделки -> блок после ПЕРВОГО стопа.
# Теперь стоп 2% от 250$ = 5$, лимит 25$ = примерно 5 стопов подряд.
DAILY_LOSS_LIMIT_USD = float(os.getenv("DAILY_LOSS_LIMIT_USD", "25"))
CONSECUTIVE_LOSS_BLOCK = int(os.getenv("CONSECUTIVE_LOSS_BLOCK", "3"))

# Which signal types are eligible for auto-trade (comma-separated)
AUTO_TRADE_SIGNAL_TYPES = os.getenv("AUTO_TRADE_SIGNAL_TYPES", "STANDARD,SURGE,PULLBACK")

# Reconciliation interval (sec)
RECONCILE_INTERVAL_SEC = int(os.getenv("RECONCILE_INTERVAL_SEC", "30"))

# Post-trade cooldown — block auto-trade on a symbol after it just closed (any reason)
POST_TRADE_COOLDOWN_HOURS = int(os.getenv("POST_TRADE_COOLDOWN_HOURS", "48"))

# BTC trend filter for auto-entry (blocks ONLY auto-entry, alerts still come)
BTC_FILTER_ENABLED = os.getenv("BTC_FILTER_ENABLED", "true").lower() == "true"
BTC_FILTER_15M_DROP_MAX = float(os.getenv("BTC_FILTER_15M_DROP_MAX", "0.8"))      # block if BTC dropped > 0.8% in 15m
BTC_FILTER_15M_PUMP_MAX = float(os.getenv("BTC_FILTER_15M_PUMP_MAX", "1.5"))      # block if BTC pumped > 1.5% in 15m
BTC_FILTER_1H_VOLATILITY_MAX = float(os.getenv("BTC_FILTER_1H_VOLATILITY_MAX", "1.2"))  # block if BTC stddev > 1.2% in 1h

# Storage
AUTO_STATE_FILE = os.getenv("AUTO_STATE_FILE", os.path.join(DATA_DIR, "auto_state.json"))
