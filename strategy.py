"""
TTM Squeeze strategy — John Carter canon for 15m crypto futures.

Entry long:
  1. ≥ MIN_SQUEEZE_BARS consecutive bars BB inside KC
  2. Fire: BB expands (no longer fully inside KC) OR close > BB upper
  3. Momentum > 0 (hist proxy)
  4. Volume spike ≥ VOL_SPIKE_MIN
  5. Close of fire bar bullish

Stop: min low of squeeze zone − buffer (thesis invalid)
Target: 2R soft TP, then trail; momentum fade exit
"""
from __future__ import annotations
from typing import Optional

from config import (
    BB_PERIOD, BB_MULT, KC_EMA, KC_ATR, KC_MULT,
    MIN_SQUEEZE_BARS, MOM_LENGTH, VOL_SPIKE_MIN,
    SL_BUFFER_PCT, SL_CAP_PCT, TP_R_MULTIPLE, ALLOW_SHORT,
)
from indicators import bollinger, keltner, bb_inside_kc, momentum_hist, rsi


def detect_ttm(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
) -> Optional[dict]:
    need = max(BB_PERIOD, KC_EMA, KC_ATR) + MIN_SQUEEZE_BARS + 5
    if len(closes) < need:
        return None

    # Build squeeze history (newest last in local lists; we walk oldest→newest)
    n = len(closes)
    squeeze_flags = []  # True = BB inside KC at bar i
    for i in range(need - 5, n):
        bb = bollinger(closes[: i + 1], BB_PERIOD, BB_MULT)
        kc = keltner(highs[: i + 1], lows[: i + 1], closes[: i + 1], KC_EMA, KC_ATR, KC_MULT)
        squeeze_flags.append(bb_inside_kc(bb, kc) if bb and kc else False)

    if len(squeeze_flags) < MIN_SQUEEZE_BARS + 2:
        return None

    # Consecutive squeeze ending recently (must have been in squeeze)
    # Look for run of True ending 1–3 bars before last, then fire on last bars
    max_run = 0
    cur = 0
    for f in squeeze_flags:
        if f:
            cur += 1
            max_run = max(max_run, cur)
        else:
            cur = 0

    if max_run < MIN_SQUEEZE_BARS:
        return None

    # Current bar must be FIRE (not still in full squeeze) OR close > upper
    bb_now = bollinger(closes, BB_PERIOD, BB_MULT)
    kc_now = keltner(highs, lows, closes, KC_EMA, KC_ATR, KC_MULT)
    if not bb_now or not kc_now:
        return None

    in_squeeze_now = bb_inside_kc(bb_now, kc_now)
    close = closes[-1]
    open_ = opens[-1]
    upper = bb_now["upper"]
    lower = bb_now["lower"]
    mid = bb_now["middle"]

    # Need recent squeeze then expansion/fire
    recent_sq = any(squeeze_flags[-(MIN_SQUEEZE_BARS + 3) : -1])
    if not recent_sq:
        return None

    long_fire = (not in_squeeze_now and close > mid) or (close > upper)
    short_fire = (not in_squeeze_now and close < mid) or (close < lower)

    mom = momentum_hist(closes, MOM_LENGTH)
    if mom is None:
        return None

    # Volume
    if len(volumes) < 21:
        return None
    avg_v = sum(volumes[-21:-1]) / 20
    if avg_v <= 0:
        return None
    vol_x = volumes[-1] / avg_v

    # Zone lows/highs during last squeeze run
    zone_lows, zone_highs = [], []
    # walk back from end of flags
    i = len(squeeze_flags) - 1
    while i >= 0 and not squeeze_flags[i]:
        i -= 1
    while i >= 0 and squeeze_flags[i]:
        # map flag index to price index
        pi = (need - 5) + i
        if 0 <= pi < n:
            zone_lows.append(lows[pi])
            zone_highs.append(highs[pi])
        i -= 1

    side = None
    if long_fire and mom > 0 and vol_x >= VOL_SPIKE_MIN and close > open_:
        side = "Buy"
    elif ALLOW_SHORT and short_fire and mom < 0 and vol_x >= VOL_SPIKE_MIN and close < open_:
        side = "Sell"
    else:
        return None

    entry = close
    if side == "Buy":
        sl_raw = min(zone_lows) if zone_lows else min(lows[-MIN_SQUEEZE_BARS:])
        sl = sl_raw * (1 - SL_BUFFER_PCT / 100)
        max_sl = entry * (1 - SL_CAP_PCT / 100)
        if sl < max_sl:
            sl = max_sl
        if sl >= entry:
            sl = entry * (1 - 0.5 / 100)
        risk = entry - sl
        tp = entry + TP_R_MULTIPLE * risk
    else:
        sl_raw = max(zone_highs) if zone_highs else max(highs[-MIN_SQUEEZE_BARS:])
        sl = sl_raw * (1 + SL_BUFFER_PCT / 100)
        max_sl = entry * (1 + SL_CAP_PCT / 100)
        if sl > max_sl:
            sl = max_sl
        if sl <= entry:
            sl = entry * (1 + 0.5 / 100)
        risk = sl - entry
        tp = entry - TP_R_MULTIPLE * risk

    r_pct = abs(entry - sl) / entry * 100 if entry else 0
    return {
        "side": side,
        "signal_type": "TTM_LONG" if side == "Buy" else "TTM_SHORT",
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "sl_pct": r_pct,
        "tp_pct": abs(tp - entry) / entry * 100,
        "squeeze_bars": max_run,
        "momentum": round(mom, 8),
        "vol_spike": round(vol_x, 2),
        "bb_bandwidth": round(bb_now["bandwidth"], 2),
        "stars": 2 if max_run >= MIN_SQUEEZE_BARS + 2 and vol_x >= 1.4 else 1,
    }
