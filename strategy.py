"""
Модуль принятия решений (v3) — двухфазный вход вместо мгновенного.
"""
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass, field

from config import (
    SCORE_THRESHOLD, MIN_RR_RATIO, WEIGHTS,
    EMA_FAST, RSI_PERIOD, RSI_LONG_MIN, RSI_LONG_MAX, RSI_SHORT_MIN, RSI_SHORT_MAX,
    EXT_ATR_MAX_FIRE, SL_ATR_MULT, SL_BUFFER_PCT, SL_CAP_PCT, TP_R_MULTIPLE,
    PULLBACK_MIN_BARS, PULLBACK_MAX_BARS, PULLBACK_CONFIRM_VOL_MIN,
    ALLOW_SHORT, REQUIRE_HTF,
)
from indicators import ema, atr as atr_ind, rsi as rsi_ind

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    symbol: str
    side: str
    score: float
    entry_price: float
    stop_loss: float
    take_profit: float
    reason: str
    signal_type: str = "PULLBACK"
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def sl_pct(self) -> float:
        return abs(self.entry_price - self.stop_loss) / self.entry_price * 100 if self.entry_price else 0.0


class Strategy:
    def __init__(self, threshold: float = SCORE_THRESHOLD):
        self.threshold = threshold
        self.weights = WEIGHTS

    def detect_fire(self, symbol: str, df, analysis: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            if df is None or len(df) < 50:
                return None
            if not analysis.get("volume_ok", False):
                return None
            if not analysis.get("atr_ok", False):
                return None

            squeeze = analysis.get("squeeze", {})
            if not squeeze.get("fired"):
                return None

            total_long = analysis.get("total_long", 0.0)
            total_short = analysis.get("total_short", 0.0)
            higher_trend = analysis.get("higher_trend", "neutral")

            side = None
            score = 0.0
            if total_long >= self.threshold and total_long > total_short and squeeze["direction"] == "long":
                side, score = "long", total_long
            elif (ALLOW_SHORT and total_short >= self.threshold and total_short > total_long
                  and squeeze["direction"] == "short"):
                side, score = "short", total_short
            if side is None:
                return None

            if REQUIRE_HTF:
                if side == "long" and higher_trend == "short":
                    return None
                if side == "short" and higher_trend == "long":
                    return None

            ext = analysis.get("extension", {})
            ext_atr = ext.get("ext_atr")
            r = ext.get("rsi")
            if ext_atr is not None and ext_atr > EXT_ATR_MAX_FIRE:
                logger.debug(f"{symbol}: цена перерастянута ({ext_atr:.2f}xATR) -- отклонено")
                return None
            if r is not None:
                if side == "long" and not (RSI_LONG_MIN <= r <= RSI_LONG_MAX):
                    return None
                if side == "short" and not (RSI_SHORT_MIN <= r <= RSI_SHORT_MAX):
                    return None

            reason = self._build_reason(analysis, side)
            armed = {
                "side": side, "score": score, "reason": reason,
                "fired_ts": int(df.index[-1].value // 10**6),
                "zone_low": squeeze.get("zone_low") or float(df["low"].iloc[-6:].min()),
                "zone_high": squeeze.get("zone_high") or float(df["high"].iloc[-6:].max()),
                "meta": {
                    "squeeze_bars": squeeze.get("squeeze_bars"), "momentum": squeeze.get("momentum"),
                    "bandwidth": squeeze.get("bandwidth"), "score": score,
                },
            }
            logger.info(f"FIRE {side.upper()} {symbol} | score={score:.2f} | {reason} -- на ожидание отката")
            return armed
        except Exception as e:
            logger.error(f"Ошибка detect_fire {symbol}: {e}")
            return None

    def check_pullback(self, symbol: str, df, armed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            if df is None or len(df) < EMA_FAST + 5:
                return None
            side = armed["side"]
            fired_ts = armed["fired_ts"]
            zone_low = armed["zone_low"]
            zone_high = armed["zone_high"]

            ts_ms = (df.index.astype("int64") // 10**6).tolist()
            idxs = [i for i, t in enumerate(ts_ms) if t <= fired_ts]
            if not idxs:
                return {"invalid": True}
            fire_idx = idxs[-1]

            bars_since = (len(df) - 1) - fire_idx
            if bars_since < PULLBACK_MIN_BARS:
                return None
            if bars_since > PULLBACK_MAX_BARS:
                return {"expired": True}
            if fire_idx + 1 >= len(df):
                return None

            closes = df["close"].tolist()
            opens = df["open"].tolist()
            highs = df["high"].tolist()
            lows = df["low"].tolist()
            volumes = df["volume"].tolist()

            since_lows = lows[fire_idx + 1:]
            since_highs = highs[fire_idx + 1:]
            if not since_lows or not since_highs:
                return None

            e_fast = ema(closes, EMA_FAST)
            atr_v = atr_ind(highs, lows, closes, 14) or atr_ind(highs, lows, closes, 20)
            rsi_v = rsi_ind(closes, RSI_PERIOD)
            if e_fast is None or atr_v is None or rsi_v is None:
                return None

            close, open_ = closes[-1], opens[-1]
            if len(volumes) < 21:
                return None
            avg_v = sum(volumes[-21:-1]) / 20.0
            vol_ok = avg_v > 0 and volumes[-1] >= avg_v * PULLBACK_CONFIRM_VOL_MIN

            if side == "long":
                if close < zone_low * (1 - SL_BUFFER_PCT / 100):
                    return {"invalid": True}
                pulled_back = min(since_lows) <= e_fast * 1.01
                confirm = (pulled_back and close > open_ and close > closes[-2] and close > e_fast
                           and RSI_LONG_MIN <= rsi_v <= RSI_LONG_MAX and vol_ok)
                if not confirm:
                    return None
                sl_zone = min(min(since_lows), zone_low) * (1 - SL_BUFFER_PCT / 100)
                sl_atr = close - SL_ATR_MULT * atr_v
                sl = min(sl_zone, sl_atr)
                max_sl = close * (1 - SL_CAP_PCT / 100)
                if sl < max_sl:
                    sl = max_sl
                if sl >= close:
                    sl = close * (1 - 0.5 / 100)
                risk = close - sl
                tp = close + TP_R_MULTIPLE * risk
            else:
                if close > zone_high * (1 + SL_BUFFER_PCT / 100):
                    return {"invalid": True}
                pulled_back = max(since_highs) >= e_fast * 0.99
                confirm = (pulled_back and close < open_ and close < closes[-2] and close < e_fast
                           and RSI_SHORT_MIN <= rsi_v <= RSI_SHORT_MAX and vol_ok)
                if not confirm:
                    return None
                sl_zone = max(max(since_highs), zone_high) * (1 + SL_BUFFER_PCT / 100)
                sl_atr = close + SL_ATR_MULT * atr_v
                sl = max(sl_zone, sl_atr)
                max_sl = close * (1 + SL_CAP_PCT / 100)
                if sl > max_sl:
                    sl = max_sl
                if sl <= close:
                    sl = close * (1 + 0.5 / 100)
                risk = sl - close
                tp = close - TP_R_MULTIPLE * risk

            if risk <= 0:
                return None
            rr = abs(tp - close) / risk if risk else 0
            if rr < MIN_RR_RATIO - 1e-6:
                return None

            meta = armed.get("meta", {})
            signal = TradeSignal(
                symbol=symbol, side=side, score=meta.get("score", armed.get("score", 0.0)),
                entry_price=round(close, 8), stop_loss=round(sl, 8), take_profit=round(tp, 8),
                reason=armed.get("reason", "Pullback") + " + Reclaim",
                signal_type="PULLBACK_LONG" if side == "long" else "PULLBACK_SHORT",
                details={**meta, "bars_since_fire": bars_since, "rsi": round(rsi_v, 2), "atr": atr_v},
            )
            logger.info(f"CONFIRM {side.upper()} {symbol} | entry={close:.6g} sl={sl:.6g} tp={tp:.6g} | bars_since_fire={bars_since}")
            return signal
        except Exception as e:
            logger.error(f"Ошибка check_pullback {symbol}: {e}")
            return None

    def build_breakout_signal(self, symbol: str, df, armed: Dict[str, Any]) -> Optional[TradeSignal]:
        try:
            closes = df["close"].tolist()
            highs = df["high"].tolist()
            lows = df["low"].tolist()
            close = closes[-1]
            side = armed["side"]
            zone_low, zone_high = armed["zone_low"], armed["zone_high"]
            atr_v = atr_ind(highs, lows, closes, 14) or atr_ind(highs, lows, closes, 20)

            if side == "long":
                sl = min(zone_low, close - SL_ATR_MULT * atr_v if atr_v else zone_low) * (1 - SL_BUFFER_PCT / 100)
                sl = max(sl, close * (1 - SL_CAP_PCT / 100))
                risk = close - sl
                if risk <= 0:
                    return None
                tp = close + TP_R_MULTIPLE * risk
            else:
                sl = max(zone_high, close + SL_ATR_MULT * atr_v if atr_v else zone_high) * (1 + SL_BUFFER_PCT / 100)
                sl = min(sl, close * (1 + SL_CAP_PCT / 100))
                risk = sl - close
                if risk <= 0:
                    return None
                tp = close - TP_R_MULTIPLE * risk

            meta = armed.get("meta", {})
            return TradeSignal(
                symbol=symbol, side=side, score=meta.get("score", 0.0),
                entry_price=round(close, 8), stop_loss=round(sl, 8), take_profit=round(tp, 8),
                reason=armed.get("reason", "Breakout"), signal_type="BREAKOUT_" + side.upper(), details=meta,
            )
        except Exception as e:
            logger.error(f"Ошибка build_breakout_signal {symbol}: {e}")
            return None

    def _build_reason(self, analysis, side: str) -> str:
        reasons = []
        if analysis.get("trend", {}).get("direction") == side:
            reasons.append("Тренд")
        candles = analysis.get("candles", {})
        if side == "long":
            if candles.get("hammer"):
                reasons.append("Молот")
            if candles.get("bullish_engulfing"):
                reasons.append("Поглощение")
        else:
            if candles.get("shooting_star"):
                reasons.append("Падающая звезда")
            if candles.get("bearish_engulfing"):
                reasons.append("Поглощение")
        if analysis.get("squeeze", {}).get("fired"):
            reasons.append(f"TTM Squeeze ({analysis['squeeze'].get('squeeze_bars')} bars)")
        if analysis.get("triangle", {}).get("is_triangle") and analysis["triangle"].get("direction") == side:
            reasons.append(f"Треугольник ({analysis['triangle'].get('type')})")
        return " + ".join(reasons) if reasons else "Комплексный сигнал"
