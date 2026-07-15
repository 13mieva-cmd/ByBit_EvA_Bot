#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EVA v3 — ИМПУЛЬСНЫЙ БОТ (полная переделка по спеке)
=====================================================
ДАННЫЕ:  Binance Futures (объёмы больше, есть taker buy volume -> честный CVD)
ЦЕНЫ/ТОРГОВЛЯ: Bybit (сверка цены, символ должен существовать на Bybit)
ИСПОЛНЕНИЕ v1: PAPER-режим — бот ведёт виртуальные сделки с учётом комиссий
               и пишет каждую в журнал. Реальное исполнение (Bybit demo API) — v2,
               только после проверки логики на данных. Реальные деньги — только
               после положительной статистики. Это красная линия.

СТРАТЕГИЯ (чек-лист из спеки, LONG):
  1. Затишье: объёмы ровные относительно Volume MA20
  2. Триггер: всплеск объёма на M15 >= 2.5x MA20 (1-я свеча импульса)
  3. Структура: 3 зелёные свечи подряд, без длинной верхней тени у 3-й (<=30%)
  4. Размер 3-й свечи ограничен ATR (не "паранормальный бар")
  5. Деньги: OI растёт устойчиво + CVD (дельта) растёт на всех 3 свечах
  6. Тренд: close > EMA21 > EMA50 (M15)
  7. Логика: пробит локальный уровень (max high за сутки до импульса)
  8. Безопасность: RSI14(M15) < 75
  9. ВХОД: лимитка на ретесте (фибо 0.382 от импульса), НЕ на хаях
 10. SL: под Low 1-й свечи с отступом 0.1%
 11. TP1 (1:1): закрыть 50%, включить ТРЕЙЛИНГ (откат 0.4%) на остаток
 12. ЛИМИТЫ: до 2 позиций ОДНОВРЕМЕННО; закрылась — слот сразу освобождается;
     дневной лимит опционален (ENV MAX_DAILY_TRADES, 0 = выключен)

ДЕПЛОЙ: Railway, переменные окружения:
  TG_TOKEN     — токен телеграм-бота (НОВЫЙ токен, не старого бота!)
  DATA_DIR     — /data (volume), по умолчанию /data
  DEPOSIT_USD  — 500
  MARGIN_USD   — 50
  LEVERAGE     — 10
Start Command:  python eva_v3_impulse.py
"""

import os, time, json, csv, math, threading
import datetime as dt
import urllib.request, urllib.parse

# ============================== КОНФИГ ==============================
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
DATA_DIR   = os.environ.get("DATA_DIR", "/data")
DEPOSIT    = float(os.environ.get("DEPOSIT_USD", 500))
MARGIN     = float(os.environ.get("MARGIN_USD", 50))
LEVERAGE   = float(os.environ.get("LEVERAGE", 10))
NOTIONAL   = MARGIN * LEVERAGE                 # объём позиции, $ (по спеке 50*10=500)

MAX_CONCURRENT     = int(os.environ.get("MAX_CONCURRENT", 2))   # позиций ОДНОВРЕМЕННО (слоты)
MAX_DAILY_TRADES   = int(os.environ.get("MAX_DAILY_TRADES", 0))  # 0 = дневного лимита НЕТ (слоты пополняются)

# --- сигнал (спека) ---
TF               = "15m"
VOL_MA_LEN       = 20        # Volume MA 20
VOL_SPIKE_MIN    = 2.5       # всплеск 1-й свечи >= 2.5x MA20 (спека: 2-3x)
QUIET_BARS       = 12        # затишье до импульса: 12 свечей без всплесков
QUIET_MAX        = 1.8       # ...без объёма > 1.8x MA20
WICK_MAX         = 0.30      # верхняя тень 3-й свечи <= 30% размаха
ATR_LEN          = 14
BAR3_ATR_MAX     = 2.5       # размах 3-й свечи <= 2.5x ATR14 (не параболик)
OI_MIN_GROW      = 0.01      # OI за импульс >= +1%
RSI_LEN          = 14
RSI_MAX          = 75.0
LEVEL_LOOKBACK   = 96        # уровень = max(high) за ~сутки (96 x 15м) ДО импульса
EMA_FAST, EMA_SLOW = 21, 50

# --- вход/выход (спека) ---
FIB_RETRACE      = 0.382     # лимитка: откат 38.2% от импульса (low1 -> high3)
ENTRY_TTL_BARS   = 8         # ждём ретест максимум 8 свечей (2ч), потом отмена
SL_BUFFER        = 0.001     # стоп = low1 * (1 - 0.001)
TP1_RR           = 1.0       # TP1 на 1:1 -> закрыть 50%
TRAIL_CALLBACK   = 0.004     # трейлинг-откат 0.4% (по цене Bybit)
FEE_MAKER        = 0.0002    # вход лимиткой (maker) 0.02%
FEE_TAKER        = 0.00055   # выходы по рынку (taker) 0.055% — честный учёт издержек

# --- вселенная ---
MAX_COINS        = 120       # топ по обороту Binance, пересечённый с Bybit
MIN_QUOTE_VOL24  = 30_000_000  # >= $30M суточного оборота (наш проверенный порог)
SCAN_EVERY_SEC   = 60        # проверять закрытие 15м-свечей раз в минуту
MANAGE_EVERY_SEC = 45        # управление позицией (TP/SL/трейлинг) по цене Bybit

# --- файлы (на volume) ---
def ensure_dirs():
    try: os.makedirs(DATA_DIR, exist_ok=True)
    except Exception: pass
ensure_dirs()
STATE_FILE   = os.path.join(DATA_DIR, "v3_state.json")     # позиция/лимиты/день
TRADES_FILE  = os.path.join(DATA_DIR, "v3_trades.csv")     # закрытые сделки (paper)
SIGNALS_FILE = os.path.join(DATA_DIR, "v3_signals.csv")    # все сигналы (совместимый формат)
CHAT_FILE    = os.path.join(DATA_DIR, "v3_chat.txt")

BINANCE = "https://fapi.binance.com"
BYBIT   = "https://api.bybit.com"

# ============================== HTTP/TG ==============================
def http_json(url, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": "eva-v3"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def tg(method, _timeout=10, **kw):
    """_timeout — локальный сокет-таймаут urlopen. kw может содержать СВОЙ 'timeout' —
    это параметр Telegram для long-polling (getUpdates), другая сущность. Раньше они
    были перепутаны: urlopen обрывал соединение на 10с, пока просили Telegram ждать 25с
    -> гонка и 'read operation timed out'. Теперь _timeout всегда больше, чем kw['timeout']."""
    if not TG_TOKEN: return None
    try:
        data = urllib.parse.urlencode(kw).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=_timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("tg err:", e); return None

def tg_send(chat, text):
    if not chat: return
    tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
       disable_web_page_preview=True)

def load_chat():
    try:
        with open(CHAT_FILE) as f: return f.read().strip()
    except Exception: return None

def save_chat(cid):
    try:
        with open(CHAT_FILE, "w") as f: f.write(str(cid))
    except Exception: pass

# ============================== ДАННЫЕ ==============================
_uni_cache = {"ts": 0, "coins": []}
def universe():
    """Топ Binance USDT-перпов по суточному обороту, существующих и на Bybit."""
    if time.time() - _uni_cache["ts"] < 1800 and _uni_cache["coins"]:
        return _uni_cache["coins"]
    try:
        tick = http_json(f"{BINANCE}/fapi/v1/ticker/24hr")
        binance = {}
        for t in tick:
            s = t.get("symbol", "")
            if not s.endswith("USDT"): continue
            qv = float(t.get("quoteVolume", 0) or 0)
            if qv >= MIN_QUOTE_VOL24: binance[s] = qv
        # пересечение с Bybit
        by = http_json(f"{BYBIT}/v5/market/tickers?category=linear")
        bybit_syms = {x["symbol"] for x in by["result"]["list"]}
        coins = [s for s in binance if s in bybit_syms]
        coins.sort(key=lambda s: -binance[s])
        _uni_cache["coins"] = coins[:MAX_COINS]; _uni_cache["ts"] = time.time()
    except Exception as e:
        print("universe err:", e)
    return _uni_cache["coins"]

def klines15(symbol, limit=200):
    """Binance 15m: возвращает (open,high,low,close,vol,taker_buy,close_time_ms).
    taker_buy (поле 9) -> дельта = 2*taker_buy - vol -> честный CVD-прокси."""
    d = http_json(f"{BINANCE}/fapi/v1/klines?symbol={symbol}&interval={TF}&limit={limit}")
    o = [float(x[1]) for x in d]; h = [float(x[2]) for x in d]
    l = [float(x[3]) for x in d]; c = [float(x[4]) for x in d]
    v = [float(x[5]) for x in d]; tb = [float(x[9]) for x in d]
    ct = [int(x[6]) for x in d]
    return o, h, l, c, v, tb, ct

def oi_hist(symbol, limit=12):
    """История OI Binance (period=15m). Возвращает список значений OI (старые->новые)."""
    try:
        d = http_json(f"{BINANCE}/futures/data/openInterestHist?symbol={symbol}&period=15m&limit={limit}")
        return [float(x["sumOpenInterest"]) for x in d]
    except Exception:
        return []

def bybit_price(symbol):
    try:
        d = http_json(f"{BYBIT}/v5/market/tickers?category=linear&symbol={symbol}")
        return float(d["result"]["list"][0]["lastPrice"])
    except Exception:
        return None

# ============================== ИНДИКАТОРЫ ==============================
def ema_series(v, span):
    if not v: return []
    a = 2 / (span + 1); out = [v[0]]
    for x in v[1:]: out.append(a * x + (1 - a) * out[-1])
    return out

def rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    g = l = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0: g += d
        else: l -= d
    ag, al = g / period, l / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)

def atr(h, l, c, period=14):
    n = len(c)
    if n < period + 2: return 0.0
    trs = []
    for i in range(1, n):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    a = sum(trs[:period]) / period
    for x in trs[period:]:
        a = (a * (period - 1) + x) / period
    return a

# ============================== СИГНАЛ (по спеке) ==============================
def detect_signal(o, h, l, c, v, tb, oi):
    """Полный чек-лист спеки на ЗАКРЫТЫХ свечах. Импульс = свечи -3,-2,-1
    (последняя закрытая = 3-я свеча импульса). Возвращает (ok, details|причина)."""
    n = len(c)
    if n < LEVEL_LOOKBACK + 30: return False, "мало истории"
    i1, i2, i3 = n - 3, n - 2, n - 1

    # 1) три зелёные подряд
    green = all(c[i] > o[i] for i in (i1, i2, i3))
    if not green: return False, "нет 3 зелёных"
    # бычий импульс: хотя бы одна из 2-й/3-й закрылась выше high предыдущей
    if not (c[i2] > h[i1] or c[i3] > h[i2]): return False, "нет закрытий выше high"

    # 2) затишье до импульса + всплеск объёма на 1-й свече
    base = v[i1 - VOL_MA_LEN:i1]
    if len(base) < VOL_MA_LEN: return False, "мало объёмной базы"
    vma = sum(base) / len(base)
    if vma <= 0: return False, "нулевая база"
    quiet = all(x <= vma * QUIET_MAX for x in v[i1 - QUIET_BARS:i1])
    if not quiet: return False, "не было затишья"
    spike = v[i1] / vma
    if spike < VOL_SPIKE_MIN: return False, f"слабый всплеск x{spike:.1f}"

    # 3) фитиль 3-й свечи <= 30% размаха
    rng3 = h[i3] - l[i3]
    if rng3 <= 0: return False, "нулевая 3-я свеча"
    upper_wick = (h[i3] - c[i3]) / rng3
    if upper_wick > WICK_MAX: return False, f"фитиль {upper_wick*100:.0f}%>30%"

    # 4) 3-я свеча не параболик (ATR-кап)
    a = atr(h[:i3], l[:i3], c[:i3], ATR_LEN)
    if a > 0 and rng3 > BAR3_ATR_MAX * a: return False, "3-я свеча параболик"

    # 5) CVD: дельта > 0 на всех трёх свечах (агрессивные покупки)
    deltas = [2 * tb[i] - v[i] for i in (i1, i2, i3)]
    if not all(d > 0 for d in deltas): return False, "дельта не растёт"

    # 6) OI: устойчивый рост за импульс (>= +1% и без слома на 3-й)
    oi_ok = False; oi_chg = 0.0
    if len(oi) >= 4:
        oi_chg = oi[-1] / oi[-4] - 1 if oi[-4] > 0 else 0
        oi_ok = oi_chg >= OI_MIN_GROW and oi[-1] >= oi[-2] * 0.998
    if not oi_ok: return False, f"OI не растёт ({oi_chg*100:+.1f}%)"

    # 7) тренд: close > EMA21 > EMA50
    e21 = ema_series(c, EMA_FAST)[-1]; e50 = ema_series(c, EMA_SLOW)[-1]
    if not (c[i3] > e21 > e50): return False, "нет аптренда EMA"

    # 8) RSI < 75
    r = rsi(c[-(RSI_LEN * 6):], RSI_LEN)
    if r > RSI_MAX: return False, f"RSI {r:.0f} перегрет"

    # 9) пробой локального уровня (max high за сутки ДО импульса)
    level = max(h[i1 - LEVEL_LOOKBACK:i1])
    if not (c[i2] > level or c[i3] > level): return False, "уровень не пробит"

    impulse = h[i3] - l[i1]
    if impulse <= 0: return False, "нет импульса"
    entry = h[i3] - FIB_RETRACE * impulse            # лимитка на откате 38.2%
    sl = l[i1] * (1 - SL_BUFFER)                     # под Low 1-й свечи
    if entry <= sl: return False, "вход ниже стопа"
    risk_pct = (entry - sl) / entry
    tp1 = entry + (entry - sl) * TP1_RR              # 1:1

    return True, dict(
        spike=spike, deltas=deltas, oi_chg=oi_chg, rsi=r,
        e21=e21, e50=e50, level=level, low1=l[i1], high3=h[i3],
        entry=entry, sl=sl, tp1=tp1, risk_pct=risk_pct,
        wick=upper_wick, close3=c[i3],
    )

# ============================== СОСТОЯНИЕ/ЛИМИТЫ ==============================
def _default_state():
    return dict(day=str(dt.datetime.now(dt.timezone.utc).date()),
                trades_today=0, paused=False,
                pendings={},    # sym -> лимитка в ожидании ретеста (занимает слот)
                positions={})   # sym -> открытая позиция (занимает слот)

def load_state():
    try:
        with open(STATE_FILE) as f: st=json.load(f)
    except Exception: return _default_state()
    # миграция со старого одно-слотового формата
    if "pendings" not in st:
        st["pendings"]={}
        p=st.pop("pending",None)
        if p: st["pendings"][p["sym"]]=p
    if "positions" not in st:
        st["positions"]={}
        p=st.pop("position",None)
        if p: st["positions"][p["sym"]]=p
    return st

def save_state(st):
    try:
        with open(STATE_FILE, "w") as f: json.dump(st, f)
    except Exception as e: print("state save err:", e)

def utc_day():
    return str(dt.datetime.now(dt.timezone.utc).date())

def roll_day(st, chat=None):
    d = utc_day()
    if d != st.get("day"):
        st["day"] = d; st["trades_today"] = 0
        save_state(st)
        if chat: tg_send(chat, f"\U0001F305 Новый день (UTC) — счётчик сделок обнулён ({daily_txt(st)}).")

def slots_used(st):
    return len(st.get("pendings",{})) + len(st.get("positions",{}))

def engaged_syms(st):
    return set(st.get("pendings",{})) | set(st.get("positions",{}))

def daily_txt(st):
    n = st.get("trades_today", 0)
    return f"{n}/{MAX_DAILY_TRADES}" if MAX_DAILY_TRADES > 0 else f"{n} (дневного лимита нет)"

def open_risk_usd(st):
    """Суммарный структурный риск всех занятых слотов, $ (без комиссий)."""
    total=0.0
    for p in st.get("pendings",{}).values():
        total += (p["entry"]-p["sl"]) * (NOTIONAL/p["entry"])
    for pos in st.get("positions",{}).values():
        if not pos.get("half_closed"):
            total += (pos["entry"]-pos["sl"]) * pos["qty"]
    return total

def trading_allowed(st):
    if st.get("paused"): return False, "пауза"
    if MAX_DAILY_TRADES>0 and st.get("trades_today",0)>=MAX_DAILY_TRADES:
        return False, f"дневной лимит {MAX_DAILY_TRADES} исчерпан"
    if slots_used(st)>=MAX_CONCURRENT:
        return False, f"заняты все слоты ({MAX_CONCURRENT}/{MAX_CONCURRENT})"
    return True, ""

# ============================== ЖУРНАЛЫ ==============================
def log_signal(coin, price):
    try:
        new = not os.path.exists(SIGNALS_FILE)
        with open(SIGNALS_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["ts", "coin", "type", "price", "btc_price"])
            b = bybit_price("BTCUSDT") or ""
            w.writerow([dt.datetime.now().isoformat(timespec="seconds"), coin, "impulse", price, b])
    except Exception as e: print("log_signal err:", e)

def log_trade(row):
    try:
        new = not os.path.exists(TRADES_FILE)
        with open(TRADES_FILE, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["ts_open","ts_close","coin","entry","exit","qty","part","pnl_usd","r_mult","reason"])
            w.writerow(row)
    except Exception as e: print("log_trade err:", e)

# ============================== PAPER-ДВИЖОК ==============================
def _profit_scenarios(entry, sl, tp1, qty):
    """Сценарии итога сделки в $, ЧЕСТНО с комиссиями (вход maker + выходы taker)."""
    fee_in = NOTIONAL * FEE_MAKER
    def leg(exit_px, q, fee_share):
        return (exit_px - entry) * q - exit_px * q * FEE_TAKER - fee_share
    half = qty / 2
    risk = entry - sl
    stop_full   = leg(sl, qty, fee_in)                                    # стоп до TP1
    tp1_half    = leg(tp1, half, fee_in / 2)                              # закрытие 50% на 1:1
    trail_at_t1 = tp1_half + leg(tp1 * (1 - TRAIL_CALLBACK), half, fee_in / 2)  # минимум после TP1
    r2          = tp1_half + leg(entry + 2 * risk, half, fee_in / 2)      # тренд до 2R
    r3          = tp1_half + leg(entry + 3 * risk, half, fee_in / 2)      # тренд до 3R
    return stop_full, tp1_half, trail_at_t1, r2, r3

def open_pending(st, sym, d, chat):
    st["pendings"][sym] = dict(sym=sym, entry=d["entry"], sl=d["sl"], tp1=d["tp1"],
                         low1=d["low1"], high3=d["high3"], ttl=ENTRY_TTL_BARS,
                         last_bar=None, born=time.time())
    save_state(st)
    risk_all = open_risk_usd(st)
    qty = NOTIONAL / d["entry"]
    risk_usd = NOTIONAL * d["risk_pct"]
    stop_full, tp1_half, trail_min, r2, r3 = _profit_scenarios(d["entry"], d["sl"], d["tp1"], qty)
    by = bybit_price(sym)
    impulse_pct = (d["high3"] / d["low1"] - 1) * 100
    dsum = sum(d.get("deltas", [])) or 0
    warn = ""
    if risk_usd > DEPOSIT * 0.02:
        warn = (f"\n\u26A0\uFE0F <b>Риск {risk_usd:.0f}$ = {risk_usd/DEPOSIT*100:.1f}% депозита</b> — "
                f"выше правила 1-2%. Твои параметры (маржа {MARGIN:.0f}$ x{LEVERAGE:.0f}), но на реале это агрессивно.")
    L = [
        f"\U0001F680 <b>{sym} · СИГНАЛ: импульс 3 свечей</b>",
        f"\U0001F4B5 Цена: ${d['close3']:.6g} (Binance)" + (f"  \u00b7  ${by:.6g} (Bybit)" if by else ""),
        "",
        "\U0001F9E0 <b>ПОЧЕМУ ВХОЖУ — весь чек-лист (факты):</b>",
        f"\u2705 Затишье было, затем ВСПЛЕСК объёма \u00d7{d['spike']:.1f} от MA20 (порог \u2265{VOL_SPIKE_MIN}x)",
        f"\u2705 3 зелёные свечи подряд: импульс {d['low1']:.6g} \u2192 {d['high3']:.6g} (+{impulse_pct:.1f}%)",
        f"\u2705 Фитиль 3-й свечи {d['wick']*100:.0f}% (\u226430%) — продавец не гасит",
        f"\u2705 3-я свеча в норме ATR — не параболик, ретест вероятен",
        f"\u2705 CVD растёт: дельта покупок положительна на всех 3 свечах (+{dsum:,.0f})",
        f"\u2705 OI {d['oi_chg']*100:+.1f}% — заходят НОВЫЕ деньги (не шорт-сквиз)",
        f"\u2705 Тренд: цена > EMA21 (${d['e21']:.6g}) > EMA50 (${d['e50']:.6g})",
        f"\u2705 Пробит суточный уровень ${d['level']:.6g}",
        f"\u2705 RSI {d['rsi']:.0f} (<{RSI_MAX:.0f}) — не перегрет",
        "",
        "\U0001F4CB <b>ПЛАН СДЕЛКИ:</b>",
        f"\U0001F4CC Вход ЛИМИТКОЙ на ретесте (фибо 38.2%): <b>${d['entry']:.6g}</b>",
        f"\U0001F4E6 Объём: <b>${NOTIONAL:.0f}</b> = {qty:.4g} {sym.replace('USDT','')} (маржа {MARGIN:.0f}$ \u00d7 плечо {LEVERAGE:.0f})",
        f"\U0001F6D1 Стоп: <b>${d['sl']:.6g}</b> (под Low 1-й свечи, \u2212{d['risk_pct']*100:.2f}%) \u2192 потеря <b>{stop_full:+.2f}$</b>",
        f"\U0001F3AF TP1 (1:1): <b>${d['tp1']:.6g}</b> \u2192 закрываю 50% \u2192 <b>{tp1_half:+.2f}$</b> в карман",
        f"\U0001F513 После TP1: трейлинг {TRAIL_CALLBACK*100:.1f}% на остаток 50%",
        "",
        "\U0001F4B0 <b>СЦЕНАРИИ ИТОГА (с комиссиями):</b>",
        f"\u2022 стоп-лосс: <b>{stop_full:+.2f}$</b>",
        f"\u2022 TP1 + трейлинг сразу: <b>{trail_min:+.2f}$</b> (минимум после TP1 — уже в плюсе)",
        f"\u2022 тренд до 2R: <b>{r2:+.2f}$</b>",
        f"\u2022 тренд до 3R: <b>{r3:+.2f}$</b>",
        f"{warn}",
        "",
        f"\u23F3 Жду ретеста {ENTRY_TTL_BARS} свечей (2ч). Отмена: закрытие ниже стопа или таймаут.",
        (f"\U0001F517 Суммарный риск занятых слотов: \u2248{risk_all:.2f}$ ({risk_all/DEPOSIT*100:.1f}% депозита) — две лонг-позиции = удвоенная ставка на рынок"
         if slots_used(st)>1 else None),
        f"\U0001F9EA PAPER-режим \u00b7 Слоты: {slots_used(st)}/{MAX_CONCURRENT} заняты \u00b7 сделок сегодня: {st.get('trades_today',0)}",
        "Команды: /pos \u00b7 /stats \u00b7 /pause \u00b7 /help",
    ]
    tg_send(chat, "\n".join(x for x in L if x is not None))
    log_signal(sym, d["close3"])

def cancel_pending(st, chat, sym, reason):
    p = st.get("pendings",{}).pop(sym, None)
    if not p: return
    tg_send(chat, f"\u274C {sym}: лимитка отменена — {reason}. Слот свободен ({slots_used(st)}/{MAX_CONCURRENT}).")
    save_state(st)

def fill_pending(st, chat, sym):
    p = st["pendings"].pop(sym)
    qty = NOTIONAL / p["entry"]
    fee_in = NOTIONAL * FEE_MAKER
    st["positions"][sym] = dict(sym=sym, entry=p["entry"], sl=p["sl"], tp1=p["tp1"],
                          qty=qty, qty_init=qty, fee_in=fee_in,
                          half_closed=False, peak=0.0,
                          opened=dt.datetime.now().isoformat(timespec="seconds"))
    st["trades_today"] = st.get("trades_today", 0) + 1
    save_state(st)
    stop_full, tp1_half, trail_min, r2, r3 = _profit_scenarios(p["entry"], p["sl"], p["tp1"], qty)
    tg_send(chat,
        f"\u2705 <b>{p['sym']}: ВХОД ИСПОЛНЕН</b> (ретест сработал)\n"
        f"\U0001F4B5 Цена входа: <b>${p['entry']:.6g}</b>\n"
        f"\U0001F4E6 Куплено: <b>{qty:.4g} {p['sym'].replace('USDT','')}</b> на ${NOTIONAL:.0f} "
        f"(маржа {MARGIN:.0f}$ \u00d7{LEVERAGE:.0f})\n"
        f"\U0001F6D1 Стоп ${p['sl']:.6g} \u2192 {stop_full:+.2f}$  \u00b7  "
        f"\U0001F3AF TP1 ${p['tp1']:.6g} \u2192 {tp1_half:+.2f}$ за 50%\n"
        f"\U0001F513 После TP1 — трейлинг {TRAIL_CALLBACK*100:.1f}%: тренд до 2R даст {r2:+.2f}$, до 3R \u2014 {r3:+.2f}$\n"
        f"\U0001F4C5 Сделка \u2116{st['trades_today']} сегодня \u00b7 Слоты: {slots_used(st)}/{MAX_CONCURRENT} \u00b7 PAPER (комиссии учтены)\n"
        f"Команды: /pos \u00b7 /stats")

def close_part(st, chat, pos, price, part, reason):
    """part: 0.5 или 1.0 от ТЕКУЩЕГО остатка конкретной позиции pos."""
    qty_close = pos["qty"] * part
    gross = (price - pos["entry"]) * qty_close
    fee_exit = price * qty_close * FEE_TAKER
    fee_in_share = pos.get("fee_in", 0.0) * (qty_close / pos.get("qty_init", qty_close))
    pnl = gross - fee_exit - fee_in_share   # ЧЕСТНО: и входная (maker), и выходная (taker) комиссии
    risk_per_unit = pos["entry"] - pos["sl"]
    r_mult = ((price - pos["entry"]) / risk_per_unit) if risk_per_unit > 0 else 0
    log_trade([pos["opened"], dt.datetime.now().isoformat(timespec="seconds"),
               pos["sym"], f"{pos['entry']:.8g}", f"{price:.8g}", f"{qty_close:.8g}",
               f"{part:.2f}", f"{pnl:.2f}", f"{r_mult:.2f}", reason])
    pos["qty"] -= qty_close
    emoji = "\U0001F4B0" if pnl >= 0 else "\U0001F53B"
    tg_send(chat, f"{emoji} <b>{pos['sym']}: {reason}</b> по ${price:.6g}\n"
                  f"PnL части: {pnl:+.2f}$ ({r_mult:+.2f}R, комиссии учтены)")
    if pos["qty"] <= 1e-12 or part >= 0.999:
        st["positions"].pop(pos["sym"], None)
        tg_send(chat, f"\U0001F4CB {pos['sym']} закрыта полностью. <b>Слот освободился</b> "
                      f"({slots_used(st)}/{MAX_CONCURRENT} занято) — могу открывать следующую. "
                      f"Сегодня сделок: {daily_txt(st)}.")
    save_state(st)

def manage_position(st, chat):
    """Управление КАЖДОЙ открытой позицией по живой цене Bybit: SL / TP1 / трейлинг."""
    for sym, pos in list(st.get("positions", {}).items()):
        price = bybit_price(sym); time.sleep(0.05)
        if price is None: continue
        if not pos["half_closed"]:
            if price <= pos["sl"]:
                close_part(st, chat, pos, pos["sl"], 1.0, "СТОП-ЛОСС"); continue
            if price >= pos["tp1"]:
                close_part(st, chat, pos, pos["tp1"], 0.5, "ТЕЙК-ПРОФИТ 1 (1:1)")
                if sym in st.get("positions", {}):
                    pos["half_closed"] = True; pos["peak"] = price; save_state(st)
                    tg_send(chat, f"\U0001F513 {sym}: трейлинг включён (откат {TRAIL_CALLBACK*100:.1f}% от пика).")
                continue
        else:
            if price > pos["peak"]:
                pos["peak"] = price; save_state(st)
            trail_stop = pos["peak"] * (1 - TRAIL_CALLBACK)
            if price <= trail_stop:
                close_part(st, chat, pos, trail_stop, 1.0, "ТРЕЙЛИНГ-СТОП")

def check_pending(st, chat):
    """Проверка ретеста для КАЖДОЙ лимитки по закрытой 15м-свече: филл / отмена / TTL."""
    for sym, p in list(st.get("pendings", {}).items()):
        try:
            o, h, l, c, v, tb, ct = klines15(sym, limit=3); time.sleep(0.08)
        except Exception:
            continue
        lo, cl = l[-2], c[-2]   # последняя ЗАКРЫТАЯ свеча
        if cl < p["sl"]:
            cancel_pending(st, chat, sym, "закрытие ниже стопа до входа (структура сломана)"); continue
        if lo <= p["entry"]:
            fill_pending(st, chat, sym); continue
        p["ttl"] -= 1
        if p["ttl"] <= 0:
            cancel_pending(st, chat, sym, f"ретеста не было за {ENTRY_TTL_BARS} свечей")
        else:
            save_state(st)

# ============================== СТАТИСТИКА ==============================
def pos_text(st):
    pens = st.get("pendings", {}); poss = st.get("positions", {})
    margin_used = MARGIN * slots_used(st)
    head = (f"\U0001F4CA Слоты: {slots_used(st)}/{MAX_CONCURRENT} \u00b7 "
            f"сделок сегодня {daily_txt(st)} \u00b7 "
            f"маржа занята {margin_used:.0f}$/{DEPOSIT:.0f}$")
    if not pens and not poss:
        return head + "\nВсе слоты свободны — сканирую рынок."
    L = [head]
    for sym, pos in poss.items():
        pr = bybit_price(sym) or pos["entry"]
        upnl = (pr - pos["entry"]) * pos["qty"]
        stage = "трейлинг (стоп в плюсе)" if pos["half_closed"] else "жду TP1/SL"
        L.append(f"\U0001F4CC {sym}: вход ${pos['entry']:.6g} \u2192 сейчас ${pr:.6g} "
                 f"({upnl:+.2f}$) \u00b7 {stage}")
    for sym, p in pens.items():
        L.append(f"\u23F3 {sym}: жду ретеста ${p['entry']:.6g} (осталось {p['ttl']} свечей)")
    return "\n".join(L)

def stats_text():
    if not os.path.exists(TRADES_FILE):
        return "\U0001F4CA Сделок ещё нет. PAPER-движок копит статистику."
    rows = list(csv.DictReader(open(TRADES_FILE)))
    if not rows: return "\U0001F4CA Сделок ещё нет."
    n = len(rows)
    pnls = [float(r["pnl_usd"]) for r in rows]
    rs = [float(r["r_mult"]) for r in rows]
    wins = sum(1 for x in pnls if x > 0)
    total = sum(pnls)
    return ("\U0001F4CA <b>PAPER-статистика (честная, с комиссиями)</b>\n"
            f"Закрытий: {n} \u00b7 в плюсе: {wins} ({wins/n*100:.0f}%)\n"
            f"Средний R: {sum(rs)/n:+.2f} \u00b7 Сумма PnL: {total:+.2f}$\n"
            f"Депозит {DEPOSIT:.0f}$ \u2192 {'\u2705' if total>=0 else '\u274C'} {total/DEPOSIT*100:+.1f}%\n\n"
            "<i>Правда о стратегии = эти цифры на дистанции, а не красота сигналов. "
            "Реальные деньги — только если тут устойчивый плюс.</i>")

# ============================== ОСНОВНОЙ ЦИКЛ ==============================
_last_bar_scanned = {}

def scan_once(st, chat):
    """Проверка закрытых 15м-свечей по вселенной: ищем сетапы, заполняем свободные слоты."""
    ok_allowed, why = trading_allowed(st)
    if not ok_allowed:
        return
    busy = engaged_syms(st)
    for sym in universe():
        if sym in busy: continue    # по одной монете — только одна сделка
        try:
            o, h, l, c, v, tb, ct = klines15(sym, limit=LEVEL_LOOKBACK + 40)
            time.sleep(0.08)
        except Exception:
            continue
        if len(c) < LEVEL_LOOKBACK + 30: continue
        # работаем только по ЗАКРЫТЫМ свечам: отбрасываем последнюю (текущую)
        o, h, l, c, v, tb, ct = o[:-1], h[:-1], l[:-1], c[:-1], v[:-1], tb[:-1], ct[:-1]
        bar_id = ct[-1]
        if _last_bar_scanned.get(sym) == bar_id: continue
        _last_bar_scanned[sym] = bar_id
        oi = oi_hist(sym, limit=8); time.sleep(0.05)
        ok, d = detect_signal(o, h, l, c, v, tb, oi)
        if not ok: continue
        # финальные проверки перед выставлением
        allowed, why = trading_allowed(st)
        if not allowed: return
        open_pending(st, sym, d, chat)
        busy.add(sym)
        if slots_used(st) >= MAX_CONCURRENT:
            return  # оба слота заняты — до освобождения

def tg_loop(st):
    offset = 0
    while True:
        try:
            r = tg("getUpdates", _timeout=35, timeout=25, offset=offset)
            if not r or not r.get("ok"):
                time.sleep(2); continue
            for u in r["result"]:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                text = (msg.get("text") or "").strip()
                cid = msg.get("chat", {}).get("id")
                if not cid: continue
                save_chat(cid)
                if text.startswith("/help"):
                    tg_send(cid,
                        "\U0001F4D6 <b>Команды EVA v3:</b>\n"
                        "/start — запуск и краткая сводка\n"
                        "/pos — текущая позиция/лимитка: цена, PnL, стадия\n"
                        "/stats — PAPER-статистика: win rate, средний R, PnL $ и %\n"
                        "/pause — пауза (новые сигналы не ищутся, позиция ведётся)\n"
                        "/resume — возобновить сканирование\n"
                        "/help — эта справка")
                elif text.startswith("/start"):
                    st["paused"] = False; save_state(st)
                    tg_send(cid, "\U0001F916 <b>EVA v3 — импульсный бот (PAPER)</b>\n"
                        "Данные: Binance \u00b7 Цены: Bybit \u00b7 Исполнение: виртуальное с честным учётом\n"
                        f"Лимиты: до {MAX_CONCURRENT} позиций одновременно (скользящие слоты) \u00b7 "
                        f"{'дневной предохранитель ' + str(MAX_DAILY_TRADES) if MAX_DAILY_TRADES>0 else 'без дневного лимита'} \u00b7 "
                        f"Объём ${NOTIONAL:.0f} (маржа {MARGIN:.0f}$ x{LEVERAGE:.0f})\n"
                        "Команды: /pos \u00b7 /stats \u00b7 /pause \u00b7 /resume \u00b7 /help")
                elif text.startswith("/pause"):
                    st["paused"] = True; save_state(st)
                    tg_send(cid, "\u23F8 Пауза: новые сигналы не ищу (открытая позиция ведётся).")
                elif text.startswith("/resume"):
                    st["paused"] = False; save_state(st)
                    tg_send(cid, "\u25B6\uFE0F Сканирование возобновлено.")
                elif text.startswith("/pos"):
                    tg_send(cid, pos_text(st))
                elif text.startswith("/stats"):
                    tg_send(cid, stats_text())
        except Exception as e:
            print("tg_loop err:", e); time.sleep(3)

def main():
    st = load_state()
    chat = load_chat()
    print("EVA v3 запущен (PAPER). chat:", "есть" if chat else "нет")
    threading.Thread(target=tg_loop, args=(st,), daemon=True).start()
    last_scan = last_manage = 0
    while True:
        try:
            chat = load_chat()
            roll_day(st, chat)
            now = time.time()
            if now - last_manage >= MANAGE_EVERY_SEC:
                last_manage = now
                check_pending(st, chat)
                manage_position(st, chat)
            if now - last_scan >= SCAN_EVERY_SEC:
                last_scan = now
                scan_once(st, chat)
        except Exception as e:
            print("main err:", e)
        time.sleep(2)

if __name__ == "__main__":
    main()
