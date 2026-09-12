"""TTM indicators: BB, KC, ATR, momentum, RSI."""
from __future__ import annotations
import math
from typing import Optional


def ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def atr(highs, lows, closes, period: int = 20) -> Optional[float]:
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return None
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def bollinger(closes: list[float], period: int = 20, mult: float = 2.0) -> Optional[dict]:
    if len(closes) < period:
        return None
    w = closes[-period:]
    mid = sum(w) / period
    var = sum((c - mid) ** 2 for c in w) / period
    std = math.sqrt(var)
    up, lo = mid + mult * std, mid - mult * std
    bw = (up - lo) / mid * 100 if mid else 0
    return {"upper": up, "middle": mid, "lower": lo, "bandwidth": bw}


def keltner(highs, lows, closes, ema_p=20, atr_p=20, atr_mult=1.5) -> Optional[dict]:
    if len(closes) < max(ema_p, atr_p) + 1:
        return None
    mid = ema(closes, ema_p)
    a = atr(highs, lows, closes, atr_p)
    if mid is None or a is None:
        return None
    return {"upper": mid + atr_mult * a, "middle": mid, "lower": mid - atr_mult * a, "atr": a}


def bb_inside_kc(bb: dict, kc: dict) -> bool:
    if not bb or not kc:
        return False
    return bb["upper"] <= kc["upper"] and bb["lower"] >= kc["lower"]


def momentum_hist(closes: list[float], length: int = 12) -> Optional[float]:
    """Carter-style momentum proxy: close - midline of HH/LL and SMA over length.
    Positive = bullish bias for long on fire.
    """
    if len(closes) < length + 1:
        return None
    window = closes[-(length + 1) : -1] if len(closes) > length else closes[:-1]
    if len(window) < length:
        window = closes[-length:]
    hh = max(window)
    ll = min(window)
    sma = sum(window) / len(window)
    midline = ((hh + ll) / 2 + sma) / 2
    return closes[-1] - midline
