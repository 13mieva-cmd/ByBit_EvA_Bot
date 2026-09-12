"""
TTM Squeeze Bot — literature-aligned (John Carter + crypto futures risk).

Entry: BB20/2 inside KC20/1.5ATR >=5 bars -> fire + mom>0 + volume
Risk: 1% deposit, size = risk_usd / stop_distance, MarkPrice SL
Exit: partial at 1R, BE, trail, momentum fade
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
    MAX_POSITIONS, MAX_ENTRIES_PER_SCAN, MAX_ENTRIES_PER_DAY,
    SIZE_MODE, POSITION_SIZE_USD,
    DAILY_LOSS_USD, CONSEC_LOSS_BLOCK,
    TRAIL_PCT, BE_TRIGGER_R, PARTIAL_PCT, RECONCILE_SEC, COOLDOWN_HOURS,
    BTC_15M_MIN, MIN_SQUEEZE_BARS, MOMENTUM_FADE_BARS,
)
from strategy import detect_ttm
from storage import State, append_csv
from trader import BybitTrader
from indicators import momentum_hist, ema

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ttm")

dp = Dispatcher()
state = State(STATE_FILE)
trader: BybitTrader | None = None
last_alert: dict[str, float] = {}
entries_this_scan = 0


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
            "category": "linear", "symbol": "BTCUSDT", "interval": "15", "limit": 2,
        })
        rows = sorted(d.get("result", {}).get("list", []), key=lambda x: int(x[0]))
        if len(rows) < 2:
            return True
        c0, c1 = float(rows[-2][4]), float(rows[-1][4])
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


async def klines(session, symbol, interval="15", limit=80):
    d = await fetch(session, "/v5/market/kline", {
        "category": "linear", "symbol": symbol, "interval": interval, "limit": limit,
    })
    rows = list(reversed(d.get("result", {}).get("list", [])))
    if not rows:
        return None
    return {
        "o": [float(r[1]) for r in rows],
        "h": [float(r[2]) for r in rows],
        "l": [float(r[3]) for r in rows],
        "c": [float(r[4]) for r in rows],
        "v": [float(r[5]) for r in rows],
    }


def size_usd(sl_pct: float) -> float:
    """Сумма позиции: FIXED = POSITION_SIZE_USD; RISK = от ширины стопа."""
    if SIZE_MODE == "RISK":
        if sl_pct <= 0:
            return SIZE_MIN_USD
        s = RISK_USD / (sl_pct / 100.0)
        return round(max(SIZE_MIN_USD, min(SIZE_MAX_USD, s)), 2)
    # FIXED
    return round(max(SIZE_MIN_USD, min(SIZE_MAX_USD, POSITION_SIZE_USD)), 2)


async def try_enter(bot, symbol, sig):
    global entries_this_scan
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state.reset_day(today)
    if state.blocked():
        return
    if len(state.positions) >= MAX_POSITIONS:
        log.info("max positions %s", MAX_POSITIONS)
        return
    if entries_this_scan >= MAX_ENTRIES_PER_SCAN:
        log.info("max entries this scan %s", MAX_ENTRIES_PER_SCAN)
        return
    if state.entries_today() >= MAX_ENTRIES_PER_DAY:
        log.info("max entries today %s", MAX_ENTRIES_PER_DAY)
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
    state.incr_entry()
    entries_this_scan += 1
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
    global entries_this_scan
    entries_this_scan = 0
    async with aiohttp.ClientSession() as session:
        if not await btc_ok(session):
            log.info("BTC filter skip")
            return
        coins = await universe(session)
        log.info("Scan %s symbols", len(coins))
        n = 0
        for c in coins:
            sym = c["symbol"]
            if time.time() - last_alert.get(sym, 0) < 4 * 3600:
                continue
            kl = await klines(session, sym)
            if not kl:
                continue
            ema50_1h = None
            kl1h = await klines(session, sym, "60", 60)
            if kl1h and len(kl1h["c"]) >= 50:
                ema50_1h = ema(kl1h["c"], 50)
            sig = detect_ttm(kl["o"], kl["h"], kl["l"], kl["c"], kl["v"], ema50_1h=ema50_1h)
            if not sig:
                continue
            n += 1
            last_alert[sym] = time.time()
            base = sym.replace("USDT", "")
            side = "LONG" if sig["side"] == "Buy" else "SHORT"
            text = (
                f"<b>{side} TTM</b> {'*' * sig['stars']} — <b>{base}</b>\n\n"
                f"KC x{sig['squeeze_bars']} | vol x{sig['vol_spike']} | BW {sig['bb_bandwidth']}%\n"
                f"Price <code>${sig['entry']:.6g}</code>\n"
                f"SL <code>${sig['sl']:.6g}</code> (-{sig['sl_pct']:.2f}%)\n"
                f"TP 2R <code>${sig['tp']:.6g}</code> (+{sig['tp_pct']:.2f}%)\n"
                f"Mom {sig['momentum']:.6g}"
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
        # Carter: 2 bars momentum turning against after profit
        if gain_r >= 0.6 and not tr.get("trail"):
            async with aiohttp.ClientSession() as session:
                kl = await klines(session, sym, "15", 24)
            if kl and len(kl["c"]) >= 10:
                fade = False
                if side == "Buy":
                    # 2 consecutive lower closes after green impulse
                    c = kl["c"]
                    if c[-1] < c[-2] < c[-3] and c[-1] < entry:
                        fade = True
                    mom = momentum_hist(c, 12)
                    mom1 = momentum_hist(c[:-1], 12)
                    mom2 = momentum_hist(c[:-2], 12)
                    if mom is not None and mom1 is not None and mom2 is not None:
                        if mom2 > 0 and mom1 < mom2 and mom < mom1:
                            fade = True
                else:
                    c = kl["c"]
                    if c[-1] > c[-2] > c[-3] and c[-1] > entry:
                        fade = True
                    mom = momentum_hist(c, 12)
                    mom1 = momentum_hist(c[:-1], 12)
                    mom2 = momentum_hist(c[:-2], 12)
                    if mom is not None and mom1 is not None and mom2 is not None:
                        if mom2 < 0 and mom1 > mom2 and mom > mom1:
                            fade = True
                if fade:
                    await trader.close(sym, side)
                    tr["exit_reason"] = "MOMENTUM"
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
        f"<b>TTM Squeeze Bot</b>\n"
        f"Carter BB/KC | squeeze>={MIN_SQUEEZE_BARS} | EMA50 1h | vol 1.3x\n"
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
        f"Entries today: {state.entries_today()}/{MAX_ENTRIES_PER_DAY}\n"
        f"Size mode: {SIZE_MODE} | ${POSITION_SIZE_USD if SIZE_MODE=='FIXED' else RISK_USD}\n"
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
    global trader
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    if BYBIT_API_KEY and BYBIT_API_SECRET:
        trader = BybitTrader(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_BASE_URL)
        log.info("Bybit balance $%s", await trader.balance())
    asyncio.create_task(loop_scan(bot))
    if trader:
        asyncio.create_task(loop_recon(bot))
    try:
        await bot.send_message(
            TELEGRAM_CHAT_ID,
            f"<b>TTM Squeeze Bot started</b>\nRisk ${RISK_USD} | {'trading' if trader else 'signals only'}",
        )
    except Exception as e:
        log.error(e)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
