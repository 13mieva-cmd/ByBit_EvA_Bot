"""
TTM Squeeze Bot — Carter + crypto (15m + 4H).

Anti-lookahead:
  - 15m: drop forming candle (USE_CLOSED_BARS_ONLY)
  - HTF/BTC: drop incomplete senior bar
  - VOL/BW only on closed signal bar
"""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_IDS,
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_BASE_URL,
    STATE_FILE, METRICS_CSV,
    MIN_TURNOVER_USD, MAX_SYMBOLS, MIN_AGE_DAYS, SCAN_INTERVAL_SEC, BLACKLIST,
    LEVERAGE, RISK_USD, SIZE_MIN_USD, SIZE_MAX_USD,
    MAX_POSITIONS, DAILY_LOSS_USD, CONSEC_LOSS_BLOCK,
    TRAIL_PCT, BE_TRIGGER_R, PARTIAL_PCT, RECONCILE_SEC, COOLDOWN_HOURS,
    BTC_15M_MIN, MIN_SQUEEZE_BARS, HTF_INTERVAL, REQUIRE_HTF,
    REQUIRE_BTC_TREND, BTC_HTF_INTERVAL, MOM_LENGTH, MAX_HOLD_BARS, MOM_FADE_BARS,
    USE_CLOSED_BARS_ONLY,
)
from strategy import detect_ttm
from storage import State, append_csv
from trader import BybitTrader
from indicators import momentum_hist, momentum_series

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ttm")

dp = Dispatcher()
state = State(STATE_FILE)
trader: BybitTrader | None = None
http: aiohttp.ClientSession | None = None  # single shared session
last_alert: dict[str, float] = {}


def allowed(msg) -> bool:
    ids = set()
    if TELEGRAM_CHAT_ID:
        ids.add(int(TELEGRAM_CHAT_ID))
    for p in (TELEGRAM_ALLOWED_IDS or "").split(","):
        p = p.strip()
        if p.lstrip("-").isdigit():
            ids.add(int(p))
    if not ids:
        return True
    uid = getattr(getattr(msg, "from_user", None), "id", None)
    cid = getattr(getattr(msg, "chat", None), "id", None)
    return uid in ids or cid in ids


@dp.message.middleware()
async def auth_mw(handler, event, data):
    if isinstance(event, types.Message) and not allowed(event):
        try:
            await event.answer("No access")
        except Exception:
            pass
        return
    return await handler(event, data)


async def fetch(session, path, params=None):
    async with session.get(f"{BYBIT_BASE_URL}{path}", params=params or {}, timeout=20) as r:
        return await r.json(content_type=None)


async def btc_ok(session) -> bool:
    try:
        d = await fetch(session, "/v5/market/kline", {
            "category": "linear", "symbol": "BTCUSDT", "interval": "15", "limit": 3,
        })
        rows = sorted(d.get("result", {}).get("list", []), key=lambda x: int(x[0]))
        if len(rows) < 3:
            return True
        c0, c1 = float(rows[-3][4]), float(rows[-2][4])
        return ((c1 - c0) / c0 * 100 if c0 else 0) >= BTC_15M_MIN
    except Exception:
        return True


async def universe(session):
    inst = await fetch(session, "/v5/market/instruments-info", {"category": "linear", "limit": 1000})
    tick = await fetch(session, "/v5/market/tickers", {"category": "linear"})
    tickers = {t["symbol"]: t for t in tick.get("result", {}).get("list", [])}
    now = time.time() * 1000
    out = []
    for i in inst.get("result", {}).get("list", []):
        sym = i.get("symbol", "")
        if not sym.endswith("USDT") or i.get("status") != "Trading":
            continue
        if i.get("contractType") != "LinearPerpetual":
            continue
        if sym.replace("USDT", "") in BLACKLIST:
            continue
        launch = int(i.get("launchTime") or 0)
        if launch and (now - launch) < MIN_AGE_DAYS * 86400000:
            continue
        t = tickers.get(sym)
        if not t:
            continue
        try:
            turn = float(t.get("turnover24h") or 0)
        except Exception:
            continue
        if turn < MIN_TURNOVER_USD:
            continue
        out.append({"symbol": sym, "turnover": turn})
    out.sort(key=lambda x: -x["turnover"])
    return out[:MAX_SYMBOLS]


def _strip_forming(kl: dict) -> dict:
    if not kl or len(kl["c"]) < 3:
        return kl
    return {k: v[:-1] for k, v in kl.items()}


async def klines(session, symbol, interval="15", limit=120, closed_only: bool | None = None):
    if closed_only is None:
        closed_only = USE_CLOSED_BARS_ONLY
    d = await fetch(session, "/v5/market/kline", {
        "category": "linear", "symbol": symbol, "interval": interval, "limit": limit,
    })
    rows = list(reversed(d.get("result", {}).get("list", [])))
    if not rows:
        return None
    kl = {
        "o": [float(r[1]) for r in rows],
        "h": [float(r[2]) for r in rows],
        "l": [float(r[3]) for r in rows],
        "c": [float(r[4]) for r in rows],
        "v": [float(r[5]) for r in rows],
        "ts": [int(r[0]) for r in rows],
    }
    if closed_only and len(kl["c"]) >= 3:
        kl = _strip_forming(kl)
    return kl


async def htf_closes(session, symbol, interval=None, limit=100):
    interval = interval or HTF_INTERVAL
    kl = await klines(session, symbol, interval=interval, limit=limit, closed_only=True)
    return kl["c"] if kl else None


def size_usd(sl_pct: float) -> float:
    if sl_pct <= 0:
        return SIZE_MIN_USD
    s = RISK_USD / (sl_pct / 100.0)
    return round(max(SIZE_MIN_USD, min(SIZE_MAX_USD, s)), 2)


async def try_enter(bot, symbol, sig):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state.reset_day(today)
    if state.blocked() or len(state.positions) >= MAX_POSITIONS:
        return
    if symbol in state.positions or state.is_cool(symbol):
        return
    if await trader.positions(symbol):
        return
    usd = size_usd(sig["sl_pct"])
    base = symbol.replace("USDT", "")
    await bot.send_message(TELEGRAM_CHAT_ID, f"AUTO entry {base} ${usd:.0f}...")
    res = await trader.open_market(symbol, sig["side"], usd, sig["sl"], LEVERAGE, None)
    if not res.get("ok"):
        await bot.send_message(TELEGRAM_CHAT_ID, f"Fail {base}: {res.get('error')}")
        return
    await asyncio.sleep(2)
    pos = await trader.positions(symbol)
    if not pos:
        await bot.send_message(TELEGRAM_CHAT_ID, f"Warn {base}: no position")
        return
    if not await trader.has_sl(symbol):
        await trader.close(symbol, sig["side"])
        await bot.send_message(TELEGRAM_CHAT_ID, f"No SL on {base} — closed")
        return
    entry = pos[0]["entry_price"]
    state.add_pos(
        symbol, side=sig["side"], entry=entry, sl=sig["sl"], tp=sig["tp"],
        sl_pct=sig["sl_pct"], size_usd=usd, signal=sig["signal_type"],
        be=False, trail=False, partial=False,
    )
    append_csv(METRICS_CSV, {
        "event": "entry", "ts": time.time(), "symbol": symbol,
        "side": sig["side"], "sl_pct": sig["sl_pct"], "squeeze": sig["squeeze_bars"],
    })
    await bot.send_message(
        TELEGRAM_CHAT_ID,
        f"Opened <b>{base}</b> @ <code>${entry:.6g}</code>\n"
        f"SL <code>${sig['sl']:.6g}</code> size ${usd:.0f}",
    )


async def scan_once(bot: Bot):
    session = http
    if session is None or session.closed:
        log.error("HTTP session not ready")
        return
    if not await btc_ok(session):
        log.info("BTC filter skip")
        return
    btc_htf = None
    if REQUIRE_BTC_TREND:
        btc_htf = await htf_closes(session, "BTCUSDT", interval=BTC_HTF_INTERVAL)
    coins = await universe(session)
    log.info("Scan %s symbols", len(coins))
    n = 0
    for c in coins:
        sym = c["symbol"]
        if time.time() - last_alert.get(sym, 0) < 4 * 3600:
            continue
        kl = await klines(session, sym, limit=120, closed_only=True)
        if not kl:
            continue
        htf = await htf_closes(session, sym) if REQUIRE_HTF else None
        sig = detect_ttm(
            kl["o"], kl["h"], kl["l"], kl["c"], kl["v"],
            htf_closes=htf, btc_htf_closes=btc_htf,
        )
        if not sig:
            continue
        n += 1
        last_alert[sym] = time.time()
        base = sym.replace("USDT", "")
        side = "LONG" if sig["side"] == "Buy" else "SHORT"
        filters = []
        if sig.get("fired"):
            filters.append("FIRE")
        if sig.get("trend_ok"):
            filters.append("EMA50/200")
        if sig.get("htf_ok"):
            filters.append("4H")
        if sig.get("btc_ok"):
            filters.append("BTC")
        filt = " · ".join(filters) if filters else "—"
        text = (
            f"<b>{side} TTM</b> {'*' * sig['stars']} — <b>{base}</b>\n\n"
            f"KC x{sig['squeeze_bars']} | vol x{sig['vol_spike']} | BW {sig['bb_bandwidth']}%\n"
            f"Price <code>${sig['entry']:.6g}</code>\n"
            f"SL <code>${sig['sl']:.6g}</code> (-{sig['sl_pct']:.2f}%)\n"
            f"TP 2R <code>${sig['tp']:.6g}</code> (+{sig['tp_pct']:.2f}%)\n"
            f"Mom {sig['momentum']:.6g}\n"
            f"Filters: {filt}\n"
            f"<i>closed-bar signal</i>"
        )
        await bot.send_message(TELEGRAM_CHAT_ID, text)
        if trader and state.enabled() and not state.blocked():
            await try_enter(bot, sym, sig)
        await asyncio.sleep(0.08)
    log.info("Scan done alerts=%s", n)


async def on_closed(bot, symbol):
    tr = state.positions.get(symbol)
    if not tr:
        return
    pnl = None
    if trader:
        closed = await trader.closed_pnl(symbol, 5)
        if closed:
            try:
                pnl = float(closed[0].get("closedPnl") or 0)
            except Exception:
                pass
    if pnl is not None:
        state.add_pnl(pnl)
    state.remove_pos(symbol)
    state.cool(symbol, COOLDOWN_HOURS)
    append_csv(METRICS_CSV, {
        "event": "exit", "ts": time.time(), "symbol": symbol, "pnl": pnl,
        "reason": (tr or {}).get("exit_reason"),
    })
    await bot.send_message(
        TELEGRAM_CHAT_ID,
        f"Closed {symbol.replace('USDT', '')} PnL {pnl}\n"
        f"Day ${state.data.get('daily_pnl', 0):+.2f}",
    )
    if state.data.get("daily_pnl", 0) <= -DAILY_LOSS_USD:
        state.block("daily_loss", 20)
        await bot.send_message(TELEGRAM_CHAT_ID, "Daily loss limit — blocked")
    elif state.data.get("consec_losses", 0) >= CONSEC_LOSS_BLOCK:
        state.block("consec", 48)
        await bot.send_message(TELEGRAM_CHAT_ID, "Consec losses — /resume")


async def reconcile(bot: Bot):
    if not trader or not state.positions:
        return
    live = {p["symbol"]: p for p in await trader.positions()}
    for sym in list(state.positions.keys()):
        tr = state.positions[sym]
        if sym not in live:
            await on_closed(bot, sym)
            continue
        mark = live[sym]["mark_price"]
        entry = float(tr["entry"])
        side = tr["side"]
        risk = abs(entry - float(tr["sl"]))
        if risk <= 0:
            continue
        gain_r = ((mark - entry) if side == "Buy" else (entry - mark)) / risk

        if not tr.get("be") and gain_r >= BE_TRIGGER_R:
            be_sl = entry * (1.001 if side == "Buy" else 0.999)
            if (await trader.set_sl(sym, be_sl, side)).get("ok"):
                tr["be"] = True
                state.save()
                await bot.send_message(TELEGRAM_CHAT_ID, f"BE {sym.replace('USDT', '')}")

        if not tr.get("trail") and gain_r >= 1.0:
            if not tr.get("partial"):
                if (await trader.partial(sym, PARTIAL_PCT)).get("ok"):
                    tr["partial"] = True
                    state.save()
            if (await trader.set_trail(sym, TRAIL_PCT, side)).get("ok"):
                tr["trail"] = True
                state.save()
                await bot.send_message(TELEGRAM_CHAT_ID, f"Trail {sym.replace('USDT', '')}")

        opened_at = float(tr.get("opened_at") or 0)
        bars_held = int((time.time() - opened_at) / 900) if opened_at else 0

        if http is None or http.closed:
            continue
        kl = await klines(http, sym, "15", 40, closed_only=True)
        if not kl or len(kl["c"]) < MOM_LENGTH + 3:
            continue

        # Fade via reverse indices on closed series: [-1]=now, [-2]=prev, [-3]=prev2
        c = kl["c"]
        mom_1 = momentum_hist(c, MOM_LENGTH)            # bar [-1]
        mom_2 = momentum_hist(c[:-1], MOM_LENGTH)        # bar [-2]
        mom_3 = momentum_hist(c[:-2], MOM_LENGTH) if MOM_FADE_BARS >= 2 else None
        fade = False
        if side == "Buy" and mom_1 is not None and mom_2 is not None:
            # consecutive fade: still >0 but decreasing toward zero
            if MOM_FADE_BARS >= 2 and mom_3 is not None:
                fade = (mom_1 > 0 and mom_2 > 0 and mom_3 > 0
                        and mom_1 < mom_2 and mom_2 < mom_3)
            else:
                fade = mom_1 > 0 and mom_2 > 0 and mom_1 < mom_2
        elif side == "Sell" and mom_1 is not None and mom_2 is not None:
            if MOM_FADE_BARS >= 2 and mom_3 is not None:
                fade = (mom_1 < 0 and mom_2 < 0 and mom_3 < 0
                        and mom_1 > mom_2 and mom_2 > mom_3)  # less negative = fading
            else:
                fade = mom_1 < 0 and mom_2 < 0 and mom_1 > mom_2

        if fade:
            await trader.close(sym, side)
            tr["exit_reason"] = "MOMENTUM_FADE"
            state.save()
            await on_closed(bot, sym)
            continue

        if bars_held >= MAX_HOLD_BARS and gain_r < 1.5:
            await trader.close(sym, side)
            tr["exit_reason"] = "TIME_STOP"
            state.save()
            await on_closed(bot, sym)


async def loop_scan(bot):
    while True:
        try:
            await scan_once(bot)
        except Exception:
            log.exception("scan")
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def loop_recon(bot):
    while True:
        try:
            await reconcile(bot)
        except Exception:
            log.exception("recon")
        await asyncio.sleep(RECONCILE_SEC)


@dp.message(Command("start", "help"))
async def cmd_start(m: types.Message):
    await m.answer(
        f"<b>TTM Squeeze Bot</b> (crypto 15m+4H, closed-bar)\n"
        f"squeeze≥{MIN_SQUEEZE_BARS} | vol≥1.7 | mom rising\n"
        f"EMA50/200 + 4H + BTC 1H | SL ATR×1.6\n"
        f"Exit: fade {MOM_FADE_BARS} / time {MAX_HOLD_BARS} bars\n"
        f"Risk ${RISK_USD} | lev {LEVERAGE}x | max {MAX_POSITIONS}\n\n"
        f"/scan /auto_on /auto_off /status /resume /panic"
    )


@dp.message(Command("scan"))
async def cmd_scan(m: types.Message):
    await m.answer("Scanning...")
    await scan_once(m.bot)
    await m.answer("Done")


@dp.message(Command("auto_on"))
async def cmd_on(m: types.Message):
    if not trader:
        await m.answer("No API keys")
        return
    if state.blocked():
        await m.answer(f"Blocked: {state.data.get('blocked_reason')} — /resume")
        return
    bal = await trader.balance()
    state.set_enabled(True)
    await m.answer(f"AUTO ON | balance ${bal or 0:.2f}")


@dp.message(Command("auto_off"))
async def cmd_off(m: types.Message):
    state.set_enabled(False)
    await m.answer("AUTO OFF")


@dp.message(Command("status"))
async def cmd_status(m: types.Message):
    await m.answer(
        f"Auto: {'ON' if state.enabled() else 'OFF'}\n"
        f"Blocked: {state.blocked()} {state.data.get('blocked_reason','')}\n"
        f"Positions: {len(state.positions)}/{MAX_POSITIONS}\n"
        f"Day PnL: ${state.data.get('daily_pnl', 0):+.2f}"
    )


@dp.message(Command("resume"))
async def cmd_resume(m: types.Message):
    state.unblock()
    await m.answer("Unblocked")


@dp.message(Command("panic"))
async def cmd_panic(m: types.Message):
    if trader:
        for sym in list(state.positions.keys()):
            await trader.close(sym, state.positions[sym].get("side", "Buy"))
            state.remove_pos(sym)
    state.block("panic", 72)
    state.set_enabled(False)
    await m.answer("PANIC done")


async def main():
    global trader, http
    http = aiohttp.ClientSession()
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        if BYBIT_API_KEY and BYBIT_API_SECRET:
            trader = BybitTrader(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_BASE_URL, session=http)
            log.info("Bybit balance $%s", await trader.balance())
        asyncio.create_task(loop_scan(bot))
        if trader:
            asyncio.create_task(loop_recon(bot))
        try:
            await bot.send_message(
                TELEGRAM_CHAT_ID,
                f"<b>TTM Squeeze Bot started</b>\n"
                f"closed-bar only | 4H filter | Risk ${RISK_USD} | "
                f"{'trading' if trader else 'signals only'}",
            )
        except Exception as e:
            log.error(e)
        await dp.start_polling(bot)
    finally:
        if http and not http.closed:
            await http.close()


if __name__ == "__main__":
    asyncio.run(main())
