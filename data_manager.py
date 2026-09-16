"""
Загрузка и обновление OHLCV через CCXT.
  - Всегда отбрасываем последнюю (ещё формирующуюся) свечу перед отдачей наружу.
  - Кэш старшего таймфрейма обновляется по TTL (HIGHER_TF_REFRESH_SEC).
  - Ретраятся NetworkError/RequestTimeout/ExchangeError.
  - Отдельный get_live_price() для отображения — НИКОГДА для торговых решений.
  - Поддержка demo/testnet ключей Bybit (EXCHANGE_SANDBOX / BYBIT_DEMO_URL) --
    без этого live-ключи и demo-ключи несовместимы, биржа отвечает
    retCode=10003 "API key is invalid" при попытке подключиться не на тот домен.
"""
import time
import logging
from typing import Dict, Any, Optional

import ccxt
import pandas as pd
from ccxt.base.errors import NetworkError, ExchangeError, RequestTimeout

from config import (
    EXCHANGE_ID, API_KEY, API_SECRET, TIMEFRAME, HIGHER_TIMEFRAME,
    CANDLE_LIMIT, HIGHER_TF_LIMIT, HIGHER_TF_REFRESH_SEC, USE_CLOSED_BARS_ONLY,
    LEVERAGE, MARGIN_MODE, EXCHANGE_SANDBOX, BYBIT_DEMO_URL,
)

logger = logging.getLogger(__name__)


def build_exchange(exchange_id: str, api_key: str, api_secret: str) -> ccxt.Exchange:
    """Общая инициализация ccxt-биржи с поддержкой demo/testnet и понятной
    диагностикой при ошибке аутентификации (retCode 10003 и т.п.)."""
    if not api_key or not api_secret:
        logger.warning(
            "BYBIT_API_KEY/BYBIT_API_SECRET не заданы (пусто) -- бот сможет работать "
            "только в DRY_RUN без баланса/ордеров."
        )
    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({
        "apiKey": api_key,
        "secret": api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    if EXCHANGE_SANDBOX and hasattr(exchange, "set_sandbox_mode"):
        exchange.set_sandbox_mode(True)
        logger.info(f"{exchange_id}: включён ccxt sandbox/testnet режим (EXCHANGE_SANDBOX=true)")
    if BYBIT_DEMO_URL and exchange_id == "bybit":
        try:
            for key in list(exchange.urls.get("api", {}).keys()):
                exchange.urls["api"][key] = BYBIT_DEMO_URL
            logger.info(f"bybit: API-домен переопределён на demo URL {BYBIT_DEMO_URL}")
        except Exception as e:
            logger.warning(f"Не удалось переопределить demo URL для bybit: {e}")
    try:
        exchange.load_markets()
    except Exception as e:
        msg = str(e)
        if "10003" in msg or "invalid" in msg.lower():
            logger.critical(
                "Биржа отклонила API-ключ (retCode=10003 / invalid key). Проверьте: "
                "1) нет лишних пробелов/кавычек в BYBIT_API_KEY/BYBIT_API_SECRET в переменных окружения; "
                "2) ключ от LIVE-аккаунта, а не demo/testnet (или наоборот) -- см. EXCHANGE_SANDBOX/BYBIT_DEMO_URL в config.py; "
                "3) на ключе нет IP-whitelist, не включающего IP хостинга (или whitelist выключен); "
                "4) у ключа включены права Contract Trade / Unified Trading, а не только Read-only."
            )
        raise
    return exchange


class DataManager:
    def __init__(self):
        self.exchange = self._init_exchange()
        self._ohlcv_cache: Dict[str, pd.DataFrame] = {}
        self._higher_tf_cache: Dict[str, pd.DataFrame] = {}
        self._higher_tf_fetched_at: Dict[str, float] = {}

    def _init_exchange(self) -> ccxt.Exchange:
        try:
            exchange = build_exchange(EXCHANGE_ID, API_KEY, API_SECRET)
            logger.info(f"Биржа {EXCHANGE_ID} успешно инициализирована")
            return exchange
        except Exception as e:
            logger.error(f"Ошибка инициализации биржи: {e}")
            raise

    def _strip_forming(self, df: pd.DataFrame) -> pd.DataFrame:
        if not USE_CLOSED_BARS_ONLY or len(df) < 2:
            return df
        return df.iloc[:-1]

    def fetch_ohlcv(self, symbol: str, timeframe: str = TIMEFRAME,
                     limit: int = CANDLE_LIMIT, strip_forming: bool = True) -> pd.DataFrame:
        max_retries = 5
        last_exc = None
        for attempt in range(1, max_retries + 1):
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol=symbol, timeframe=timeframe, limit=limit)
                if not ohlcv:
                    raise ValueError(f"Пустой ответ OHLCV для {symbol}")
                df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                df.set_index("timestamp", inplace=True)
                df = df.astype({"open": "float64", "high": "float64", "low": "float64",
                                 "close": "float64", "volume": "float64"})
                df = df[~df.index.duplicated(keep="last")].sort_index()
                if strip_forming:
                    df = self._strip_forming(df)
                return df
            except (NetworkError, RequestTimeout, ExchangeError) as e:
                last_exc = e
                logger.warning(f"Сетевая/биржевая ошибка {symbol} (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    time.sleep(min(30, 2 ** attempt))
                else:
                    raise
            except Exception as e:
                logger.error(f"Ошибка загрузки {symbol}: {e}")
                raise
        raise last_exc

    def get_historical_data(self, symbol: str) -> pd.DataFrame:
        logger.info(f"Загрузка истории {symbol} ({TIMEFRAME})...")
        df = self.fetch_ohlcv(symbol, TIMEFRAME, CANDLE_LIMIT, strip_forming=True)
        self._ohlcv_cache[symbol] = df
        return df.copy()

    def get_higher_tf_data(self, symbol: str) -> pd.DataFrame:
        now = time.time()
        fetched_at = self._higher_tf_fetched_at.get(symbol, 0)
        cached = self._higher_tf_cache.get(symbol)
        if cached is not None and len(cached) > 50 and (now - fetched_at) < HIGHER_TF_REFRESH_SEC:
            return cached.copy()
        logger.info(f"(Пере)Загрузка старшего ТФ {HIGHER_TIMEFRAME} для {symbol}...")
        df = self.fetch_ohlcv(symbol, HIGHER_TIMEFRAME, HIGHER_TF_LIMIT, strip_forming=True)
        self._higher_tf_cache[symbol] = df
        self._higher_tf_fetched_at[symbol] = now
        return df.copy()

    def update_latest_candle(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._ohlcv_cache:
            return self.get_historical_data(symbol)
        latest_df = self.fetch_ohlcv(symbol, TIMEFRAME, 5, strip_forming=True)
        combined = pd.concat([self._ohlcv_cache[symbol], latest_df])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        if len(combined) > CANDLE_LIMIT + 50:
            combined = combined.iloc[-(CANDLE_LIMIT + 50):]
        self._ohlcv_cache[symbol] = combined
        return combined.copy()

    def get_live_price(self, symbol: str) -> Optional[float]:
        try:
            t = self.exchange.fetch_ticker(symbol)
            return float(t.get("last") or t.get("close") or 0) or None
        except Exception as e:
            logger.warning(f"Не удалось получить live-цену {symbol}: {e}")
            return None

    def get_balance(self) -> Dict[str, Any]:
        try:
            return self.exchange.fetch_balance()
        except Exception as e:
            logger.error(f"Ошибка баланса: {e}")
            raise

    def ensure_leverage(self, symbol: str):
        try:
            if hasattr(self.exchange, "set_margin_mode"):
                self.exchange.set_margin_mode(MARGIN_MODE, symbol)
        except Exception as e:
            logger.debug(f"set_margin_mode не поддержан/не удался для {symbol}: {e}")
        try:
            if hasattr(self.exchange, "set_leverage"):
                self.exchange.set_leverage(LEVERAGE, symbol)
        except Exception as e:
            logger.debug(f"set_leverage не поддержан/не удался для {symbol}: {e}")
