"""
Аналитическое ядро (v3). Тренд, свечные паттерны, TTM Squeeze, треугольники,
ATR/объём/RSI/экстеншн-фильтры. Работает только с уже закрытыми барами.
"""
import logging
from typing import Dict, Any, Tuple
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

from config import (
    MIN_ATR_PERCENT, VOLUME_MA_PERIOD, VOLUME_MULTIPLIER,
    BB_PERIOD, BB_MULT, KC_EMA, KC_ATR, KC_MULT,
    MIN_SQUEEZE_BARS, MAX_SQUEEZE_BARS, MOM_LENGTH,
    EMA_FAST, EMA_SLOW, RSI_PERIOD,
)
from indicators import (
    ema, rsi, atr as atr_ind, bollinger, keltner, bb_inside_kc,
    momentum_hist, momentum_series, squeeze_flags_series,
)

logger = logging.getLogger(__name__)


class Analyzer:
    def __init__(self, order: int = 5):
        self.order = order

    def calculate_trend(self, df: pd.DataFrame) -> Dict[str, Any]:
        try:
            if len(df) < EMA_SLOW + 10:
                return {"direction": "neutral", "score": 0.0}
            close = df["close"]
            ema50 = close.ewm(span=EMA_FAST, adjust=False).mean()
            ema200 = close.ewm(span=EMA_SLOW, adjust=False).mean()
            last_ema50 = ema50.iloc[-1]
            last_ema200 = ema200.iloc[-1]
            last_close = close.iloc[-1]

            if last_ema50 > last_ema200 and last_close > last_ema50:
                return {"direction": "long", "score": 1.5, "ema50": float(last_ema50), "ema200": float(last_ema200)}
            elif last_ema50 > last_ema200:
                return {"direction": "long", "score": 0.7, "ema50": float(last_ema50), "ema200": float(last_ema200)}
            elif last_ema50 < last_ema200 and last_close < last_ema50:
                return {"direction": "short", "score": 1.5, "ema50": float(last_ema50), "ema200": float(last_ema200)}
            elif last_ema50 < last_ema200:
                return {"direction": "short", "score": 0.7, "ema50": float(last_ema50), "ema200": float(last_ema200)}
            return {"direction": "neutral", "score": 0.0, "ema50": float(last_ema50), "ema200": float(last_ema200)}
        except Exception as e:
            logger.error(f"Ошибка тренда: {e}")
            return {"direction": "neutral", "score": 0.0}

    def detect_candlestick_patterns(self, df: pd.DataFrame) -> Dict[str, Any]:
        result = {
            "hammer": False, "shooting_star": False,
            "bullish_engulfing": False, "bearish_engulfing": False,
            "score_long": 0.0, "score_short": 0.0,
        }
        try:
            if len(df) < 3:
                return result
            c2 = df.iloc[-2]
            c3 = df.iloc[-1]

            def body(c):
                return abs(c["close"] - c["open"])

            def upper(c):
                return c["high"] - max(c["open"], c["close"])

            def lower(c):
                return min(c["open"], c["close"]) - c["low"]

            body3 = body(c3)
            lower3 = lower(c3)
            upper3 = upper(c3)
            range3 = c3["high"] - c3["low"]

            if range3 > 0 and body3 > 0:
                if lower3 >= body3 * 2.0 and upper3 <= body3 * 0.4 and body3 / range3 <= 0.35:
                    result["hammer"] = True
                    result["score_long"] += 1.0
                if upper3 >= body3 * 2.0 and lower3 <= body3 * 0.4 and body3 / range3 <= 0.35:
                    result["shooting_star"] = True
                    result["score_short"] += 1.0

            if c2["close"] < c2["open"] and c3["close"] > c3["open"]:
                if c3["open"] < c2["close"] and c3["close"] > c2["open"] and body(c3) > body(c2) * 1.1:
                    result["bullish_engulfing"] = True
                    result["score_long"] += 1.5

            if c2["close"] > c2["open"] and c3["close"] < c3["open"]:
                if c3["open"] > c2["close"] and c3["close"] < c2["open"] and body(c3) > body(c2) * 1.1:
                    result["bearish_engulfing"] = True
                    result["score_short"] += 1.5

            return result
        except Exception as e:
            logger.error(f"Ошибка свечных паттернов: {e}")
            return result

    def find_extrema(self, df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
        try:
            highs = df["high"].values
            lows = df["low"].values
            max_idx = argrelextrema(highs, np.greater_equal, order=self.order)[0]
            min_idx = argrelextrema(lows, np.less_equal, order=self.order)[0]
            return max_idx, min_idx
        except Exception as e:
            logger.error(f"Ошибка экстремумов: {e}")
            return np.array([]), np.array([])

    def detect_squeeze(self, df: pd.DataFrame) -> Dict[str, Any]:
        result = {
            "is_squeeze": False, "fired": False, "direction": "neutral", "score": 0.0,
            "resistance": None, "support": None, "squeeze_bars": 0,
            "zone_low": None, "zone_high": None, "bandwidth": None, "momentum": None,
        }
        try:
            closes = df["close"].tolist()
            highs = df["high"].tolist()
            lows = df["low"].tolist()
            need = max(BB_PERIOD, KC_EMA, KC_ATR, MOM_LENGTH) + MIN_SQUEEZE_BARS + 5
            if len(closes) < need:
                return result

            flags = squeeze_flags_series(highs, lows, closes, BB_PERIOD, BB_MULT,
                                          KC_EMA, KC_ATR, KC_MULT, lookback=MAX_SQUEEZE_BARS + 20)
            if len(flags) < 2:
                return result

            bb_sig = bollinger(closes, BB_PERIOD, BB_MULT)
            kc_sig = keltner(highs, lows, closes, KC_EMA, KC_ATR, KC_MULT)
            if not bb_sig or not kc_sig:
                return result

            in_sq_sig = bb_inside_kc(bb_sig, kc_sig)
            in_sq_prev = flags[-2]
            result["is_squeeze"] = in_sq_sig
            result["bandwidth"] = round(bb_sig["bandwidth"], 3)

            fired = in_sq_prev and not in_sq_sig
            if not fired:
                return result

            run = 0
            j = len(flags) - 2
            while j >= 0 and flags[j]:
                run += 1
                j -= 1
            if run < MIN_SQUEEZE_BARS or run > MAX_SQUEEZE_BARS:
                return result

            zone_lows, zone_highs = [], []
            n = len(closes)
            start_idx = n - len(flags)
            i = len(flags) - 2
            while i >= 0 and flags[i]:
                pi = start_idx + i
                if 0 <= pi < n:
                    zone_lows.append(lows[pi])
                    zone_highs.append(highs[pi])
                i -= 1

            mom_series = momentum_series(closes, MOM_LENGTH, lookback=2)
            mom = mom_series[-1]
            mom_prev = mom_series[-2] if len(mom_series) >= 2 else None
            if mom is None:
                return result

            direction = "neutral"
            if mom > 0 and (mom_prev is None or mom > mom_prev):
                direction = "long"
            elif mom < 0 and (mom_prev is None or mom < mom_prev):
                direction = "short"
            if direction == "neutral":
                return result

            result.update({
                "fired": True, "direction": direction, "score": 2.0, "squeeze_bars": run,
                "zone_low": min(zone_lows) if zone_lows else float(df["low"].iloc[-MIN_SQUEEZE_BARS:].min()),
                "zone_high": max(zone_highs) if zone_highs else float(df["high"].iloc[-MIN_SQUEEZE_BARS:].max()),
                "momentum": round(mom, 8),
                "resistance": max(zone_highs) if zone_highs else None,
                "support": min(zone_lows) if zone_lows else None,
            })
            return result
        except Exception as e:
            logger.error(f"Ошибка squeeze: {e}")
            return result

    def detect_triangle(self, df: pd.DataFrame, max_idx: np.ndarray, min_idx: np.ndarray) -> Dict[str, Any]:
        result = {
            "is_triangle": False, "type": None, "direction": "neutral", "score": 0.0,
            "upper_slope": None, "lower_slope": None,
        }
        try:
            if len(max_idx) < 4 or len(min_idx) < 4:
                return result
            recent_max = max_idx[-6:] if len(max_idx) >= 6 else max_idx
            recent_min = min_idx[-6:] if len(min_idx) >= 6 else min_idx
            x_max = recent_max.astype(float)
            y_max = df["high"].iloc[recent_max].values.astype(float)
            x_min = recent_min.astype(float)
            y_min = df["low"].iloc[recent_min].values.astype(float)

            if len(x_max) >= 3 and len(x_min) >= 3:
                upper_slope = np.polyfit(x_max, y_max, 1)[0]
                lower_slope = np.polyfit(x_min, y_min, 1)[0]
                price = df["close"].iloc[-1]
                upper_norm = upper_slope / price
                lower_norm = lower_slope / price

                if abs(upper_norm) < 0.00015 and lower_norm > 0.0002:
                    result.update({"is_triangle": True, "type": "ascending", "direction": "long", "score": 1.5})
                elif abs(lower_norm) < 0.00015 and upper_norm < -0.0002:
                    result.update({"is_triangle": True, "type": "descending", "direction": "short", "score": 1.5})
                elif upper_norm < -0.00001 and lower_norm > 0.00001:
                    result.update({"is_triangle": True, "type": "symmetrical", "direction": "neutral", "score": 0.0})

                result["upper_slope"] = float(upper_slope)
                result["lower_slope"] = float(lower_slope)
            return result
        except Exception as e:
            logger.error(f"Ошибка треугольника: {e}")
            return result

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        try:
            a = atr_ind(df["high"].tolist(), df["low"].tolist(), df["close"].tolist(), period)
            return float(a) if a is not None else 0.0
        except Exception:
            return 0.0

    def check_volume_filter(self, df: pd.DataFrame) -> bool:
        try:
            if len(df) < VOLUME_MA_PERIOD + 5:
                return False
            vol_ma = df["volume"].rolling(VOLUME_MA_PERIOD).mean().iloc[-1]
            last_vol = df["volume"].iloc[-1]
            return bool(last_vol >= vol_ma * VOLUME_MULTIPLIER)
        except Exception:
            return False

    def check_atr_filter(self, df: pd.DataFrame) -> bool:
        try:
            a = self.calculate_atr(df)
            price = df["close"].iloc[-1]
            atr_percent = (a / price) * 100 if price else 0
            return atr_percent >= MIN_ATR_PERCENT
        except Exception:
            return False

    def calculate_extension(self, df: pd.DataFrame) -> Dict[str, Any]:
        try:
            closes = df["close"].tolist()
            highs = df["high"].tolist()
            lows = df["low"].tolist()
            e = ema(closes, EMA_FAST)
            a = atr_ind(highs, lows, closes, 14) or atr_ind(highs, lows, closes, 20)
            r = rsi(closes, RSI_PERIOD)
            if e is None or not a:
                return {"atr": a, "ema_fast": e, "ext_atr": None, "rsi": r}
            ext = abs(closes[-1] - e) / a
            return {"atr": a, "ema_fast": e, "ext_atr": ext, "rsi": r}
        except Exception as e:
            logger.error(f"Ошибка extension: {e}")
            return {"atr": None, "ema_fast": None, "ext_atr": None, "rsi": None}

    def analyze(self, df: pd.DataFrame, higher_df: pd.DataFrame = None) -> Dict[str, Any]:
        empty = {
            "trend": {"direction": "neutral", "score": 0.0},
            "candles": {"score_long": 0.0, "score_short": 0.0},
            "squeeze": {"is_squeeze": False, "fired": False, "score": 0.0},
            "triangle": {"is_triangle": False, "score": 0.0},
            "total_long": 0.0, "total_short": 0.0,
            "volume_ok": False, "atr_ok": False,
            "higher_trend": "neutral",
            "max_idx": [], "min_idx": [],
            "extension": {"atr": None, "ema_fast": None, "ext_atr": None, "rsi": None},
        }
        if df is None or len(df) < 50:
            return empty
        try:
            trend = self.calculate_trend(df)
            candles = self.detect_candlestick_patterns(df)
            max_idx, min_idx = self.find_extrema(df)
            squeeze = self.detect_squeeze(df)
            triangle = self.detect_triangle(df, max_idx, min_idx)
            extension = self.calculate_extension(df)

            volume_ok = self.check_volume_filter(df)
            atr_ok = self.check_atr_filter(df)

            higher_trend = "neutral"
            if higher_df is not None and len(higher_df) > 50:
                ht = self.calculate_trend(higher_df)
                higher_trend = ht["direction"]

            total_long = 0.0
            total_short = 0.0
            if trend["direction"] == "long":
                total_long += trend["score"]
            elif trend["direction"] == "short":
                total_short += trend["score"]

            total_long += candles["score_long"]
            total_short += candles["score_short"]

            if squeeze["fired"]:
                if squeeze["direction"] == "long":
                    total_long += squeeze["score"]
                elif squeeze["direction"] == "short":
                    total_short += squeeze["score"]

            if triangle["is_triangle"] and triangle["direction"] != "neutral":
                if triangle["direction"] == "long":
                    total_long += triangle["score"]
                elif triangle["direction"] == "short":
                    total_short += triangle["score"]

            return {
                "trend": trend, "candles": candles, "squeeze": squeeze, "triangle": triangle,
                "total_long": round(total_long, 2), "total_short": round(total_short, 2),
                "volume_ok": volume_ok, "atr_ok": atr_ok, "higher_trend": higher_trend,
                "max_idx": max_idx.tolist(), "min_idx": min_idx.tolist(), "extension": extension,
            }
        except Exception as e:
            logger.error(f"Ошибка analyze: {e}")
            return empty
