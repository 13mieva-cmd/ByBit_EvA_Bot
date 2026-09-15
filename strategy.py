"""
TTM Squeeze — Carter + crypto, CLOSED-BAR only (no intrabar / no HTF lookahead).

Signal bar = last CLOSED 15m candle (forming candle stripped by caller).
HTF series must already exclude the incomplete HTF bar.
VOL_SPIKE and BW_EXPAND evaluated strictly on that signal bar.
"""
from __future__ import annotations
from typing import Optional

from config import (
    BB_PERIOD, BB_MULT, KC_EMA, KC_ATR, KC_MULT,
    MIN_SQUEEZE_BARS, MAX_SQUEEZE_BARS, MOM_LENGTH, VOL_SPIKE_MIN,
    BW_EXPAND_MIN, MOM_MIN_PCT,
    EMA_FAST, EMA_SLOW, REQUIRE_EMA_STACK,
    REQUIRE_HTF, HTF_EMA, REQUIRE_BTC_TREND,
    SL_ATR_MULT, SL_BUFFER_PCT, SL_CAP_PCT, TP_R_MULTIPLE, ALLOW_SHORT,
    EXT_ATR_MAX_FIRE, RSI_PERIOD, RSI_LONG_MIN, RSI_LONG_MAX,
    RSI_SHORT_MIN, RSI_SHORT_MAX, PULLBACK_MIN_BARS, PULLBACK_MAX_BARS,
    PULLBACK_CONFIRM_VOL_MIN,
)
from indicators import (
    bollinger, keltner, bb_inside_kc, momentum_hist, momentum_series, ema, atr, rsi,
)


def detect_ttm(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    htf_closes: Optional[list[float]] = None,
    btc_htf_closes: Optional[list[float]] = None,
) -> Optional[dict]:
    need = max(BB_PERIOD, KC_EMA, KC_ATR, EMA_SLOW, MOM_LENGTH) + MIN_SQUEEZE_BARS + 8
    if len(closes) < need:
        return None

    n = len(closes)
    start = max(BB_PERIOD + 1, n - (MIN_SQUEEZE_BARS + 14))

    squeeze_flags: list[bool] = []
    for i in range(start, n):
        bb = bollinger(closes[: i + 1], BB_PERIOD, BB_MULT)
        kc = keltner(highs[: i + 1], lows[: i + 1], closes[: i + 1], KC_EMA, KC_ATR, KC_MULT)
        squeeze_flags.append(bb_inside_kc(bb, kc) if bb and kc else False)

    if len(squeeze_flags) < MIN_SQUEEZE_BARS + 2:
        return None

    bb_sig = bollinger(closes, BB_PERIOD, BB_MULT)
    kc_sig = keltner(highs, lows, closes, KC_EMA, KC_ATR, KC_MULT)
    bb_prev = bollinger(closes[:-1], BB_PERIOD, BB_MULT)
    kc_prev = keltner(highs[:-1], lows[:-1], closes[:-1], KC_EMA, KC_ATR, KC_MULT)
    if not all((bb_sig, kc_sig, bb_prev, kc_prev)):
        return None

    in_sq_sig = bb_inside_kc(bb_sig, kc_sig)
    in_sq_prev = bb_inside_kc(bb_prev, kc_prev)
    # Fire = first green after reds on CLOSED bars only
    fired = in_sq_prev and not in_sq_sig
    if not fired:
        return None

    # Length of the squeeze run that JUST ENDED (must end at prev bar)
    # walk back from second-to-last flag (prev = signal-1)
    run = 0
    j = len(squeeze_flags) - 2  # prev bar in flags
    while j >= 0 and squeeze_flags[j]:
        run += 1
        j -= 1
    max_run = run
    if max_run < MIN_SQUEEZE_BARS or max_run > MAX_SQUEEZE_BARS:
        return None

    bw_sig = bb_sig["bandwidth"]
    bw_prev = bb_prev["bandwidth"]
    if bw_prev <= 0 or bw_sig < bw_prev * BW_EXPAND_MIN:
        if not (closes[-1] > bb_sig["upper"] or closes[-1] < bb_sig["lower"]):
            return None

    close = closes[-1]
    open_ = opens[-1]
    mid = bb_sig["middle"]

    moms = momentum_series(closes, MOM_LENGTH, lookback=3)
    mom = moms[-1]
    mom_1 = moms[-2] if len(moms) >= 2 else None
    if mom is None:
        return None
    mom_rising = mom_1 is not None and mom > mom_1
    mom_falling = mom_1 is not None and mom < mom_1
    mom_strong = abs(mom) / close * 100 >= MOM_MIN_PCT if close else False

    if len(volumes) < 21:
        return None
    avg_v = sum(volumes[-21:-1]) / 20.0
    if avg_v <= 0:
        return None
    vol_x = volumes[-1] / avg_v
    if vol_x < VOL_SPIKE_MIN:
        return None

    # Zone extremes from the SAME squeeze run that just ended (prev bar = flags[-2])
    zone_lows, zone_highs = [], []
    i = len(squeeze_flags) - 2
    while i >= 0 and squeeze_flags[i]:
        pi = start + i
        if 0 <= pi < n:
            zone_lows.append(lows[pi])
            zone_highs.append(highs[pi])
        i -= 1

    e50 = ema(closes, EMA_FAST)
    e200 = ema(closes, EMA_SLOW)
    stack_long = stack_short = False
    if e50 is not None:
        if REQUIRE_EMA_STACK and e200 is not None:
            stack_long = close > e50 > e200
            stack_short = close < e50 < e200
        else:
            stack_long = close > e50
            stack_short = close < e50

    htf_long = htf_short = True
    if REQUIRE_HTF:
        if htf_closes is None or len(htf_closes) < HTF_EMA + 5:
            return None
        htf_e = ema(htf_closes, HTF_EMA)
        if htf_e is None:
            return None
        htf_long = htf_closes[-1] > htf_e
        htf_short = htf_closes[-1] < htf_e

    btc_ok_long = btc_ok_short = True
    if REQUIRE_BTC_TREND:
        if btc_htf_closes is None or len(btc_htf_closes) < HTF_EMA + 5:
            return None
        btc_e = ema(btc_htf_closes, HTF_EMA)
        if btc_e is None:
            return None
        btc_ok_long = btc_htf_closes[-1] > btc_e
        btc_ok_short = btc_htf_closes[-1] < btc_e

    # --- Anti-chasing filters (avoid "buying the top / selling the bottom") ---
    atr_v = atr(highs, lows, closes, 14) or atr(highs, lows, closes, 20)
    not_overextended = True
    if e50 is not None and atr_v:
        ext_atr = abs(close - e50) / atr_v
        not_overextended = ext_atr <= EXT_ATR_MAX_FIRE

    rsi_v = rsi(closes, RSI_PERIOD)
    rsi_ok_long = rsi_v is not None and RSI_LONG_MIN <= rsi_v <= RSI_LONG_MAX
    rsi_ok_short = rsi_v is not None and RSI_SHORT_MIN <= rsi_v <= RSI_SHORT_MAX

    long_ok = (
        fired and close > mid and close > open_
        and mom > 0 and mom_rising and mom_strong
        and stack_long and htf_long and btc_ok_long
        and not_overextended and rsi_ok_long
    )
    short_ok = (
        ALLOW_SHORT and fired and close < mid and close < open_
        and mom < 0 and mom_falling and mom_strong
        and stack_short and htf_short and btc_ok_short
        and not_overextended and rsi_ok_short
    )

    side = "Buy" if long_ok else ("Sell" if short_ok else None)
    if side is None:
        return None

    entry = close
    if side == "Buy":
        sl_zone = (min(zone_lows) if zone_lows else min(lows[-MIN_SQUEEZE_BARS:])) * (1 - SL_BUFFER_PCT / 100)
        sl_atr = entry - SL_ATR_MULT * atr_v if atr_v else sl_zone
        sl = min(sl_zone, sl_atr)
        max_sl = entry * (1 - SL_CAP_PCT / 100)
        if sl < max_sl:
            sl = max_sl
        if sl >= entry:
            sl = entry * (1 - 0.5 / 100)
        risk = entry - sl
        tp = entry + TP_R_MULTIPLE * risk
    else:
        sl_zone = (max(zone_highs) if zone_highs else max(highs[-MIN_SQUEEZE_BARS:])) * (1 + SL_BUFFER_PCT / 100)
        sl_atr = entry + SL_ATR_MULT * atr_v if atr_v else sl_zone
        sl = max(sl_zone, sl_atr)
        max_sl = entry * (1 + SL_CAP_PCT / 100)
        if sl > max_sl:
            sl = max_sl
        if sl <= entry:
            sl = entry * (1 + 0.5 / 100)
        risk = sl - entry
        tp = entry - TP_R_MULTIPLE * risk

    r_pct = abs(entry - sl) / entry * 100 if entry else 0
    stars = 1
    if max_run >= MIN_SQUEEZE_BARS + 2 and vol_x >= 2.0 and (mom_rising if side == "Buy" else mom_falling):
        stars = 3
    elif max_run >= MIN_SQUEEZE_BARS + 1 and vol_x >= 1.8:
        stars = 2

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
        "bb_bandwidth": round(bw_sig, 2),
        "stars": stars,
        "fired": True,
        "trend_ok": stack_long if side == "Buy" else stack_short,
        "htf_ok": htf_long if side == "Buy" else htf_short,
        "btc_ok": btc_ok_long if side == "Buy" else btc_ok_short,
        "atr": atr_v,
        "rsi": round(rsi_v, 2) if rsi_v is not None else None,
        "zone_low": min(zone_lows) if zone_lows else min(lows[-MIN_SQUEEZE_BARS:]),
        "zone_high": max(zone_highs) if zone_highs else max(highs[-MIN_SQUEEZE_BARS:]),
    }


def check_pullback_entry(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    ts: list[int],
    armed: dict,
) -> Optional[dict]:
    """
    Second stage of the v2 entry engine. Called on every scan for symbols that already
    had a squeeze FIRE (see detect_ttm) but have not been entered yet.

    Returns:
      - None                  -> still waiting, keep the setup armed
      - {"expired": True}     -> caller should disarm, no entry
      - {"invalid": True}     -> caller should disarm, no entry (structure broken)
      - full signal dict      -> caller should disarm AND treat this as an entry signal
    """
    side = armed["side"]
    fired_ts = armed["fired_ts"]
    zone_low = armed["zone_low"]
    zone_high = armed["zone_high"]

    idxs = [i for i, t in enumerate(ts) if t <= fired_ts]
    if not idxs:
        return {"invalid": True}
    fire_idx = idxs[-1]

    bars_since = (len(closes) - 1) - fire_idx
    if bars_since < PULLBACK_MIN_BARS:
        return None
    if bars_since > PULLBACK_MAX_BARS:
        return {"expired": True}
    if fire_idx + 1 >= len(closes):
        return None

    since_lows = lows[fire_idx + 1:]
    since_highs = highs[fire_idx + 1:]
    if not since_lows or not since_highs:
        return None

    e_fast = ema(closes, EMA_FAST)
    atr_v = atr(highs, lows, closes, 14) or atr(highs, lows, closes, 20)
    rsi_v = rsi(closes, RSI_PERIOD)
    if e_fast is None or atr_v is None or rsi_v is None:
        return None

    close, open_ = closes[-1], opens[-1]
    if len(volumes) < 21:
        return None
    avg_v = sum(volumes[-21:-1]) / 20.0
    vol_ok = avg_v > 0 and volumes[-1] >= avg_v * PULLBACK_CONFIRM_VOL_MIN

    if side == "Buy":
        if close < zone_low * (1 - SL_BUFFER_PCT / 100):
            return {"invalid": True}
        pulled_back = min(since_lows) <= e_fast * 1.01
        confirm = (
            pulled_back
            and close > open_
            and close > closes[-2]
            and close > e_fast
            and RSI_LONG_MIN <= rsi_v <= RSI_LONG_MAX
            and vol_ok
        )
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
        confirm = (
            pulled_back
            and close < open_
            and close < closes[-2]
            and close < e_fast
            and RSI_SHORT_MIN <= rsi_v <= RSI_SHORT_MAX
            and vol_ok
        )
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

    r_pct = abs(close - sl) / close * 100 if close else 0
    meta = armed.get("meta", {})
    return {
        "side": side,
        "signal_type": "TTM_LONG_PULLBACK" if side == "Buy" else "TTM_SHORT_PULLBACK",
        "entry": close,
        "sl": sl,
        "tp": tp,
        "sl_pct": r_pct,
        "tp_pct": abs(tp - close) / close * 100,
        "squeeze_bars": meta.get("squeeze_bars"),
        "momentum": meta.get("momentum"),
        "vol_spike": meta.get("vol_spike"),
        "bb_bandwidth": meta.get("bb_bandwidth"),
        "stars": meta.get("stars", 1),
        "fired": True,
        "trend_ok": True,
        "htf_ok": True,
        "btc_ok": True,
        "atr": atr_v,
        "rsi": round(rsi_v, 2),
        "bars_since_fire": bars_since,
    }
