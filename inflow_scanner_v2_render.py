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
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_ALLOWED_IDS,
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_BASE_URL,
    STATE_FILE, METRICS_CSV,
    MIN_TURNOVER_USD, MAX_SYMBOLS, MIN_AGE_DAYS, SCAN_INTERVAL_SEC, BLACKLIST,
    LEVERAGE, RISK_USD, SIZE_MIN_USD, SIZE_MAX_USD,
    MAX_POSITIONS, DAILY_LOSS_USD, CONSEC_LOSS_BLOCK,
    TRAIL_PCT, BE_TRIGGER_R, PARTIAL_PCT, RECONCILE_SEC, COOLDOWN_HOURS,
    BTC_15M_MIN, MIN_SQUEEZE_BARS, MAX_SQUEEZE_BARS, HTF_INTERVAL, REQUIRE_HTF,
    REQUIRE_BTC_TREND, BTC_HTF_INTERVAL, MOM_LENGTH, MAX_HOLD_BARS, MOM_FADE_BARS,
    USE_CLOSED_BARS_ONLY, VOL_SPIKE_MIN, BW_EXPAND_MIN, EMA_FAST, EMA_SLOW,
    REQUIRE_EMA_STACK, SL_ATR_MULT, SL_CAP_PCT, TP_R_MULTIPLE, ALLOW_SHORT,
    DEPOSIT_USD, RISK_PCT,
    ENTRY_MODE, PULLBACK_MAX_BARS, PULLBACK_MIN_BARS, EXT_ATR_MAX_FIRE,
    RSI_LONG_MIN, RSI_LONG_MAX, RSI_SHORT_MIN, RSI_SHORT_MAX, TRAIL_ATR_MULT,
)
from strategy import detect_ttm, check_pullback_entry
from storage import State, append_csv
from trader import BybitTrader
from indicators import momentum_hist, momentum_series, atr as atr_ind

# Bars required by detect_ttm(): max(BB_PERIOD, KC_EMA, KC_ATR, EMA_SLOW, MOM_LENGTH) + MIN_SQUEEZE_BARS + 8.
# Scan-fetch limit MUST always be >= that, with margin, or detect_ttm() silently returns None every time.
SCAN_KLINE_LIMIT = max(220, EMA_SLOW + MIN_SQUEEZE_BARS + 40)
# Klines needed for the pullback-watch loop: only needs to cover fire bar + PULLBACK_MAX_BARS + indicator warmup
ARMED_KLINE_LIMIT = max(SCAN_KLINE_LIMIT, EMA_SLOW + PULLBACK_MAX_BARS + 40)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ttm")

dp = Dispatcher()
state = State(STATE_FILE)
trader: BybitTrader | None = None
http: aiohttp.ClientSession | None = None  # single shared session
last_alert: dict[str, float] = {}
# last signal cache for manual enter button: symbol -> sig dict
last_signals: dict[str, dict] = {}


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_main() -> InlineKeyboardMarkup:
    auto = "🟢 AUTO ON" if state.enabled() else "⚪ AUTO OFF"
    return _kb([
        [
            InlineKeyboardButton(text="📡 Scan", callback_data="cmd:scan"),
            InlineKeyboardButton(text="📊 Status", callback_data="cmd:status"),
        ],
        [
            InlineKeyboardButton(text=auto, callback_data="cmd:auto_toggle"),
            InlineKeyboardButton(text="📂 Positions", callback_data="cmd:positions"),
        ],
        [
            InlineKeyboardButton(text="🔭 Watching", callback_data="cmd:armed"),
            InlineKeyboardButton(text="⚙️ Settings", callback_data="cmd:settings"),
        ],
        [
            InlineKeyboardButton(text="📖 Help", callback_data="cmd:help"),
            InlineKeyboardButton(text="▶️ Resume", callback_data="cmd:resume"),
        ],
        [
            InlineKeyboardButton(text="🛑 PANIC", callback_data="cmd:panic"),
        ],
    ])


def kb_signal(symbol: str, side: str) -> InlineKeyboardMarkup:
    base = symbol.replace("USDT", "")
    rows = [
        [InlineKeyboardButton(
            text=f"{'🟢 Long' if side == 'Buy' else '🔴 Short'} · Enter now",
            callback_data=f"enter:{symbol}",
        )],
        [
            InlineKeyboardButton(text="📊 Status", callback_data="cmd:status"),
            InlineKeyboardButton(text="📂 Positions", callback_data="cmd:positions"),
        ],
    ]
    return _kb(rows)


def kb_position(symbol: str) -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text="✖️ Close position", callback_data=f"close:{symbol}")],
        [
            InlineKeyboardButton(text="📂 All positions", callback_data="cmd:positions"),
            InlineKeyboardButton(text="📊 Status", callback_data="cmd:status"),
        ],
    ])


def kb_back() -> InlineKeyboardMarkup:
    return _kb([[InlineKeyboardButton(text="◀️ Menu", callback_data="cmd:menu")]])


def htf_label() -> str:
    m = {"15": "15m", "60": "1H", "240": "4H", "D": "1D"}
    return m.get(str(HTF_INTERVAL), f"{HTF_INTERVAL}")


def card_help() -> str:
    mode = "🟢 trading" if trader else "🟡 signals only"
    return (
        f"<b>TTM Squeeze Bot</b> · crypto futures\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Mode:</b> {mode}\n"
        f"<b>Work TF:</b> 15m (closed bars only)\n"
        f"<b>Trend TF:</b> {htf_label()} EMA{HTF_EMA}\n"
        f"<b>Stack:</b> EMA{EMA_FAST}/{EMA_SLOW} "
        f"{'ON' if REQUIRE_EMA_STACK else 'OFF'}\n"
        f"<b>BTC filter:</b> {'ON' if REQUIRE_BTC_TREND else 'OFF'} "
        f"({BTC_HTF_INTERVAL}m)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Entry rules</b>\n"
        f"• Squeeze {MIN_SQUEEZE_BARS}–{MAX_SQUEEZE_BARS} bars → FIRE\n"
        f"• BW expand ≥{BW_EXPAND_MIN}x · Vol ≥{VOL_SPIKE_MIN}x\n"
        f"• Momentum rising · bullish close\n"
        f"• Not overextended: ≤{EXT_ATR_MAX_FIRE}×ATR from EMA{EMA_FAST} at fire\n"
        f"• RSI sanity: long {RSI_LONG_MIN}-{RSI_LONG_MAX} · short {RSI_SHORT_MIN}-{RSI_SHORT_MAX}\n"
        f"• Long only: {'yes' if not ALLOW_SHORT else 'long+short'}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Entry mode:</b> <code>{ENTRY_MODE.upper()}</code>\n"
        + (
            f"• Fire = watch-only. Bot waits up to {PULLBACK_MAX_BARS} bars for price to "
            f"pull back to EMA{EMA_FAST} and reclaim with a confirming candle — "
            f"never buys the breakout bar itself.\n"
            if ENTRY_MODE != "breakout" else
            f"• Legacy: enters immediately on the fire bar (chases the breakout).\n"
        ) +
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Risk</b>\n"
        f"• Risk ${RISK_USD:.0f}/trade ({RISK_PCT}% of ${DEPOSIT_USD:.0f})\n"
        f"• Leverage {LEVERAGE:.0f}x · size ${SIZE_MIN_USD:.0f}–{SIZE_MAX_USD:.0f}\n"
        f"• Max positions {MAX_POSITIONS}\n"
        f"• SL: zone / ATR×{SL_ATR_MULT} · cap {SL_CAP_PCT}%\n"
        f"• TP {TP_R_MULTIPLE:.0f}R · BE @{BE_TRIGGER_R}R · "
        f"partial {PARTIAL_PCT:.0f}% @1R\n"
        f"• Fade {MOM_FADE_BARS} bars · time-stop {MAX_HOLD_BARS} bars\n"
        f"• Daily loss −${DAILY_LOSS_USD:.0f} · "
        f"{CONSEC_LOSS_BLOCK} losses → block\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Use buttons below or commands."
    )


def card_settings() -> str:
    return (
        f"<b>⚙️ Live settings</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Scan every <code>{SCAN_INTERVAL_SEC}s</code> · "
        f"recon <code>{RECONCILE_SEC}s</code>\n"
        f"Universe top <code>{MAX_SYMBOLS}</code> · "
        f"min turnover <code>${MIN_TURNOVER_USD/1e6:.0f}M</code>\n"
        f"Min age <code>{MIN_AGE_DAYS}d</code> · "
        f"cooldown <code>{COOLDOWN_HOURS}h</code>\n"
        f"Closed bars: <code>{USE_CLOSED_BARS_ONLY}</code>\n"
        f"API: <code>{BYBIT_BASE_URL.replace('https://','')}</code>\n"
        f"Trail <code>{TRAIL_ATR_MULT}xATR</code> · "
        f"BTC 15m gate <code>{BTC_15M_MIN}%</code>\n"
        f"Entry mode <code>{ENTRY_MODE}</code> · "
        f"pullback window <code>{PULLBACK_MAX_BARS} bars</code>\n"
        f"Ext filter <code>{EXT_ATR_MAX_FIRE}xATR</code> · "
        f"RSI long <code>{RSI_LONG_MIN}-{RSI_LONG_MAX}</code> short <code>{RSI_SHORT_MIN}-{RSI_SHORT_MAX}</code>"
    )


async def card_status() -> str:
    bal = None
    if trader:
        try:
            bal = await trader.balance()
        except Exception:
            pass
    auto = "🟢 ON" if state.enabled() else "⚪ OFF"
    blk = state.blocked()
    reason = state.data.get("blocked_reason") or "—"
    npos = len(state.positions)
    pnl = float(state.data.get("daily_pnl") or 0)
    pnl_s = f"{pnl:+.2f}"
    lines = [
        f"<b>📊 Status</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Auto: <b>{auto}</b>",
        f"Blocked: <b>{'🔒 ' + reason if blk else '🔓 no'}</b>",
        f"Positions: <b>{npos}/{MAX_POSITIONS}</b>",
        f"Watching (pullback): <b>{len(state.armed)}</b>",
        f"Day PnL: <b>${pnl_s}</b>",
    ]
    if bal is not None:
        lines.append(f"Balance: <b>${bal:,.2f}</b>")
    else:
        lines.append("Balance: <i>no API / n/a</i>")
    lines.append(f"Risk/trade: <b>${RISK_USD:.0f}</b> · lev <b>{LEVERAGE:.0f}x</b>")
    if npos == 0:
        lines.append("")
        lines.append("<i>No open trades. Waiting for TTM fire…</i>")
    else:
        lines.append("")
        lines.append("<b>Open:</b>")
        for sym, tr in state.positions.items():
            base = sym.replace("USDT", "")
            side = "LONG" if tr.get("side") == "Buy" else "SHORT"
            flags = []
            if tr.get("be"):
                flags.append("BE")
            if tr.get("partial"):
                flags.append("partial")
            if tr.get("trail"):
                flags.append("trail")
            fl = (" · " + " ".join(flags)) if flags else ""
            lines.append(
                f"• <b>{base}</b> {side} @ <code>{float(tr.get('entry', 0)):.6g}</code>{fl}"
            )
    return "\n".join(lines)


def card_signal(sig: dict, symbol: str) -> str:
    base = symbol.replace("USDT", "")
    side = "LONG 🟢" if sig["side"] == "Buy" else "SHORT 🔴"
    stars = "⭐" * int(sig.get("stars") or 1)
    filters = []
    if sig.get("fired"):
        filters.append("FIRE")
    if REQUIRE_EMA_STACK and sig.get("trend_ok"):
        filters.append(f"EMA{EMA_FAST}/{EMA_SLOW}")
    if REQUIRE_HTF and sig.get("htf_ok"):
        filters.append(htf_label())
    if REQUIRE_BTC_TREND and sig.get("btc_ok"):
        filters.append("BTC")
    filt = " · ".join(filters) if filters else "—"
    usd = size_usd(sig["sl_pct"])
    return (
        f"<b>{side}</b> TTM {stars} — <b>{base}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Setup</b>\n"
        f"Squeeze: <code>{sig['squeeze_bars']}</code> bars · "
        f"Vol <code>x{sig['vol_spike']}</code>\n"
        f"BW: <code>{sig['bb_bandwidth']}%</code> · "
        f"Mom <code>{sig['momentum']:.6g}</code>\n"
        f"Filters: <code>{filt}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Levels</b>\n"
        f"Entry ≈ <code>${sig['entry']:.6g}</code>\n"
        f"SL <code>${sig['sl']:.6g}</code> "
        f"(−{sig['sl_pct']:.2f}%)\n"
        f"TP {TP_R_MULTIPLE:.0f}R <code>${sig['tp']:.6g}</code> "
        f"(+{sig['tp_pct']:.2f}%)\n"
        f"Size ≈ <code>${usd:.0f}</code> · lev {LEVERAGE:.0f}x\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>{'Pullback-confirmed entry' if 'PULLBACK' in sig.get('signal_type','') else 'Closed-bar signal'} · 15m</i>"
    )


def card_watch(sig: dict, symbol: str) -> str:
    base = symbol.replace("USDT", "")
    side = "LONG 🟢" if sig["side"] == "Buy" else "SHORT 🔴"
    level = sig["zone_low"] if sig["side"] == "Buy" else sig["zone_high"]
    return (
        f"<b>🔭 Watching · {side}</b> — <b>{base}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Squeeze fired (<code>{sig['squeeze_bars']}</code> bars · vol <code>x{sig['vol_spike']}</code>) — "
        f"NOT entering on the breakout bar.\n"
        f"Waiting up to <code>{PULLBACK_MAX_BARS}</code> bars for a pullback to EMA{EMA_FAST} "
        f"with a reclaim candle.\n"
        f"RSI at fire: <code>{sig.get('rsi')}</code>\n"
        f"Setup invalidates if price closes back through <code>${level:.6g}</code>.\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Watch-only — no entry yet.</i>"
    )


def card_opened(base: str, entry: float, sig: dict, usd: float) -> str:
    side = "LONG 🟢" if sig["side"] == "Buy" else "SHORT 🔴"
    return (
        f"<b>✅ Position opened</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{base}</b> {side}\n"
        f"Entry <code>${entry:.6g}</code>\n"
        f"SL <code>${sig['sl']:.6g}</code> (−{sig['sl_pct']:.2f}%)\n"
        f"TP {TP_R_MULTIPLE:.0f}R <code>${sig['tp']:.6g}</code>\n"
        f"Size <code>${usd:.0f}</code> · risk ${RISK_USD:.0f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"BE @{BE_TRIGGER_R}R · partial {PARTIAL_PCT:.0f}% @1R · "
        f"fade {MOM_FADE_BARS} / time {MAX_HOLD_BARS}"
    )


def card_closed(symbol: str, pnl, reason: str | None) -> str:
    base = symbol.replace("USDT", "")
    day = float(state.data.get("daily_pnl") or 0)
    pnl_s = f"{pnl:+.2f}" if pnl is not None else "n/a"
    why = reason or "exchange / SL / TP"
    return (
        f"<b>📤 Position closed</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>{base}</b>\n"
        f"PnL: <b>${pnl_s}</b>\n"
        f"Reason: <code>{why}</code>\n"
        f"Day PnL: <b>${day:+.2f}</b>"
    )


def card_positions_empty() -> str:
    armed_n = len(state.armed)
    watch_line = (
        f"\n🔭 <b>{armed_n}</b> setup(s) watching for a pullback right now."
        if armed_n else ""
    )
    return (
        f"<b>📂 Positions</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Open: <b>0/{MAX_POSITIONS}</b>\n\n"
        f"<i>Нет открытых сделок.</i>\n"
        f"Бот ждёт TTM fire на 15m с фильтрами "
        f"{htf_label()} + EMA{EMA_FAST}/{EMA_SLOW}"
        f"{' + BTC' if REQUIRE_BTC_TREND else ''}."
        f"{watch_line}"
    )


async def card_positions() -> tuple[str, InlineKeyboardMarkup | None]:
    if not state.positions:
        return card_positions_empty(), kb_back()
    lines = [
        f"<b>📂 Positions</b> · {len(state.positions)}/{MAX_POSITIONS}",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]
    rows = []
    live = {}
    if trader:
        try:
            live = {p["symbol"]: p for p in await trader.positions()}
        except Exception:
            pass
    for sym, tr in state.positions.items():
        base = sym.replace("USDT", "")
        side = "LONG" if tr.get("side") == "Buy" else "SHORT"
        entry = float(tr.get("entry") or 0)
        sl = float(tr.get("sl") or 0)
        mark = None
        if sym in live:
            mark = float(live[sym].get("mark_price") or 0)
        u = ""
        if mark and entry:
            if tr.get("side") == "Buy":
                u = f" · mark {mark:.6g} ({(mark/entry-1)*100:+.2f}%)"
            else:
                u = f" · mark {mark:.6g} ({(entry/mark-1)*100:+.2f}%)"
        flags = []
        if tr.get("be"):
            flags.append("BE")
        if tr.get("partial"):
            flags.append("½")
        if tr.get("trail"):
            flags.append("trail")
        fl = (" [" + " ".join(flags) + "]") if flags else ""
        lines.append(
            f"<b>{base}</b> {side}{fl}\n"
            f"  in <code>{entry:.6g}</code> SL <code>{sl:.6g}</code>{u}"
        )
        rows.append([InlineKeyboardButton(
            text=f"✖️ Close {base}", callback_data=f"close:{sym}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Menu", callback_data="cmd:menu")])
    return "\n".join(lines), _kb(rows)



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


async def klines(session, symbol, interval="15", limit=250, closed_only: bool | None = None):
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


async def try_enter(bot, symbol, sig, notify: bool = True) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state.reset_day(today)
    if not trader:
        if notify:
            await bot.send_message(TELEGRAM_CHAT_ID, "⚠️ No Bybit API — signals only", reply_markup=kb_back())
        return False
    if state.blocked() or len(state.positions) >= MAX_POSITIONS:
        if notify:
            await bot.send_message(
                TELEGRAM_CHAT_ID,
                f"⚠️ Skip entry: blocked or max positions ({len(state.positions)}/{MAX_POSITIONS})",
                reply_markup=kb_back(),
            )
        return False
    if symbol in state.positions or state.is_cool(symbol):
        return False
    if await trader.positions(symbol):
        return False
    usd = size_usd(sig["sl_pct"])
    base = symbol.replace("USDT", "")
    if notify:
        await bot.send_message(
            TELEGRAM_CHAT_ID,
            f"⏳ Opening <b>{base}</b> · size ~${usd:.0f} · lev {LEVERAGE:.0f}x…",
        )
    res = await trader.open_market(symbol, sig["side"], usd, sig["sl"], LEVERAGE, None)
    if not res.get("ok"):
        await bot.send_message(
            TELEGRAM_CHAT_ID,
            f"❌ Entry failed <b>{base}</b>\n<code>{res.get('error')}</code>",
            reply_markup=kb_back(),
        )
        return False
    await asyncio.sleep(2)
    pos = await trader.positions(symbol)
    if not pos:
        await bot.send_message(TELEGRAM_CHAT_ID, f"⚠️ {base}: order sent, position not found", reply_markup=kb_back())
        return False
    if not await trader.has_sl(symbol):
        await trader.close(symbol, sig["side"])
        await bot.send_message(TELEGRAM_CHAT_ID, f"❌ No SL on {base} — closed for safety", reply_markup=kb_back())
        return False
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
        card_opened(base, entry, sig, usd),
        reply_markup=kb_position(symbol),
    )
    return True


async def process_armed(bot: Bot, session):
    """
    Stage 2 of the entry engine: for every symbol with a pending (armed) squeeze-fire,
    check whether a valid pullback + reclaim has happened yet. Runs BEFORE the universe
    scan and independently of the top-N turnover ranking, so a setup is never lost just
    because the coin temporarily drops out of the top MAX_SYMBOLS by volume.
    """
    for sym in list(state.armed.keys()):
        armed = state.armed[sym]
        kl = await klines(session, sym, limit=ARMED_KLINE_LIMIT, closed_only=True)
        if not kl:
            continue
        res = check_pullback_entry(kl["o"], kl["h"], kl["l"], kl["c"], kl["v"], kl["ts"], armed)
        if res is None:
            continue
        if res.get("expired") or res.get("invalid"):
            state.disarm(sym)
            why = "expired (no pullback in time)" if res.get("expired") else "invalidated (structure broken)"
            await bot.send_message(
                TELEGRAM_CHAT_ID,
                f"⌛ <b>{sym.replace('USDT', '')}</b> pullback setup {why} — no entry.",
                reply_markup=kb_back(),
            )
            continue
        state.disarm(sym)
        last_alert[sym] = time.time()
        last_signals[sym] = res
        await bot.send_message(
            TELEGRAM_CHAT_ID, card_signal(res, sym), reply_markup=kb_signal(sym, res["side"]),
        )
        if trader and state.enabled() and not state.blocked():
            await try_enter(bot, sym, res, notify=True)
        await asyncio.sleep(0.08)


async def scan_once(bot: Bot):
    session = http
    if session is None or session.closed:
        log.error("HTTP session not ready")
        return
    if not await btc_ok(session):
        log.info("BTC filter skip")
        return

    await process_armed(bot, session)

    btc_htf = None
    if REQUIRE_BTC_TREND:
        btc_htf = await htf_closes(session, "BTCUSDT", interval=BTC_HTF_INTERVAL)
    coins = await universe(session)
    log.info("Scan %s symbols", len(coins))
    n = 0
    for c in coins:
        sym = c["symbol"]
        if sym in state.armed:
            continue
        if time.time() - last_alert.get(sym, 0) < 4 * 3600:
            continue
        kl = await klines(session, sym, limit=SCAN_KLINE_LIMIT, closed_only=True)
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

        if ENTRY_MODE == "breakout":
            last_alert[sym] = time.time()
            last_signals[sym] = sig
            await bot.send_message(
                TELEGRAM_CHAT_ID, card_signal(sig, sym), reply_markup=kb_signal(sym, sig["side"]),
            )
            if trader and state.enabled() and not state.blocked():
                await try_enter(bot, sym, sig, notify=True)
        else:
            state.arm(
                sym,
                side=sig["side"],
                fired_ts=kl["ts"][-1],
                zone_low=sig["zone_low"],
                zone_high=sig["zone_high"],
                meta={
                    "squeeze_bars": sig["squeeze_bars"], "vol_spike": sig["vol_spike"],
                    "bb_bandwidth": sig["bb_bandwidth"], "momentum": sig["momentum"],
                    "stars": sig["stars"],
                },
            )
            await bot.send_message(TELEGRAM_CHAT_ID, card_watch(sig, sym), reply_markup=kb_back())

        await asyncio.sleep(0.08)
    log.info("Scan done alerts=%s armed=%s", n, len(state.armed))



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
    reason = (tr or {}).get("exit_reason")
    await bot.send_message(
        TELEGRAM_CHAT_ID,
        card_closed(symbol, pnl, reason),
        reply_markup=kb_main(),
    )
    if state.data.get("daily_pnl", 0) <= -DAILY_LOSS_USD:
        state.block("daily_loss", 20)
        await bot.send_message(
            TELEGRAM_CHAT_ID,
            f"🔒 <b>Daily loss limit</b> (−${DAILY_LOSS_USD:.0f}) — trading blocked.\n/resume to unlock",
            reply_markup=kb_main(),
        )
    elif state.data.get("consec_losses", 0) >= CONSEC_LOSS_BLOCK:
        state.block("consec", 48)
        await bot.send_message(
            TELEGRAM_CHAT_ID,
            f"🔒 <b>{CONSEC_LOSS_BLOCK} consecutive losses</b> — blocked.\n/resume to unlock",
            reply_markup=kb_main(),
        )


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

        if http is None or http.closed:
            continue
        kl = await klines(http, sym, "15", 40, closed_only=True)
        if not kl or len(kl["c"]) < MOM_LENGTH + 3:
            continue
        c = kl["c"]

        if not tr.get("be") and gain_r >= BE_TRIGGER_R:
            be_sl = entry * (1.001 if side == "Buy" else 0.999)
            if (await trader.set_sl(sym, be_sl, side)).get("ok"):
                tr["be"] = True
                state.save()
                await bot.send_message(TELEGRAM_CHAT_ID, f"🛡️ BE · <b>{sym.replace('USDT', '')}</b> — stop to breakeven")

        if not tr.get("trail") and gain_r >= 1.0:
            if not tr.get("partial"):
                if (await trader.partial(sym, PARTIAL_PCT)).get("ok"):
                    tr["partial"] = True
                    state.save()
            trail_pct = TRAIL_PCT
            if TRAIL_ATR_MULT > 0:
                atr_v = atr_ind(kl["h"], kl["l"], kl["c"], 14) or atr_ind(kl["h"], kl["l"], kl["c"], 20)
                if atr_v and mark:
                    trail_pct = max(0.1, (TRAIL_ATR_MULT * atr_v / mark) * 100)
            if (await trader.set_trail(sym, trail_pct, side)).get("ok"):
                tr["trail"] = True
                state.save()
                await bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"📉 Trail ({trail_pct:.2f}%) + partial · <b>{sym.replace('USDT', '')}</b>",
                )

        opened_at = float(tr.get("opened_at") or 0)
        bars_held = int((time.time() - opened_at) / 900) if opened_at else 0

        mom_1 = momentum_hist(c, MOM_LENGTH)
        mom_2 = momentum_hist(c[:-1], MOM_LENGTH)
        mom_3 = momentum_hist(c[:-2], MOM_LENGTH) if MOM_FADE_BARS >= 2 else None
        fade = False
        if side == "Buy" and mom_1 is not None and mom_2 is not None:
            if MOM_FADE_BARS >= 2 and mom_3 is not None:
                fade = (mom_1 > 0 and mom_2 > 0 and mom_3 > 0
                        and mom_1 < mom_2 and mom_2 < mom_3)
            else:
                fade = mom_1 > 0 and mom_2 > 0 and mom_1 < mom_2
        elif side == "Sell" and mom_1 is not None and mom_2 is not None:
            if MOM_FADE_BARS >= 2 and mom_3 is not None:
                fade = (mom_1 < 0 and mom_2 < 0 and mom_3 < 0
                        and mom_1 > mom_2 and mom_2 > mom_3)
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


@dp.message(Command("start", "help", "menu"))
async def cmd_start(m: types.Message):
    await m.answer(card_help(), reply_markup=kb_main())


@dp.message(Command("scan"))
async def cmd_scan(m: types.Message):
    await m.answer("📡 <b>Scanning universe…</b>\n15m closed bars · filters active")
    await scan_once(m.bot)
    await m.answer(
        f"✅ Scan done\nPositions: <b>{len(state.positions)}/{MAX_POSITIONS}</b>",
        reply_markup=kb_main(),
    )


@dp.message(Command("auto_on"))
async def cmd_on(m: types.Message):
    if not trader:
        await m.answer("⚠️ No Bybit API keys — signals only", reply_markup=kb_main())
        return
    if state.blocked():
        await m.answer(
            f"🔒 Blocked: <code>{state.data.get('blocked_reason')}</code>\nUse /resume",
            reply_markup=kb_main(),
        )
        return
    bal = await trader.balance()
    state.set_enabled(True)
    await m.answer(
        f"🟢 <b>AUTO ON</b>\n"
        f"Balance <b>${(bal or 0):,.2f}</b>\n"
        f"Risk ${RISK_USD:.0f}/trade · max {MAX_POSITIONS} pos · lev {LEVERAGE:.0f}x",
        reply_markup=kb_main(),
    )


@dp.message(Command("auto_off"))
async def cmd_off(m: types.Message):
    state.set_enabled(False)
    await m.answer(
        "⚪ <b>AUTO OFF</b>\nOnly signals — no new entries",
        reply_markup=kb_main(),
    )


@dp.message(Command("status"))
async def cmd_status(m: types.Message):
    await m.answer(await card_status(), reply_markup=kb_main())


@dp.message(Command("positions", "pos"))
async def cmd_positions(m: types.Message):
    text, kb = await card_positions()
    await m.answer(text, reply_markup=kb)


@dp.message(Command("armed", "watching"))
async def cmd_armed(m: types.Message):
    if not state.armed:
        await m.answer("🔭 No setups currently watching for a pullback.", reply_markup=kb_main())
        return
    lines = ["<b>🔭 Watching for pullback</b>", "━━━━━━━━━━━━━━━━━━━━"]
    now = time.time()
    for sym, a in state.armed.items():
        base = sym.replace("USDT", "")
        side = "LONG" if a.get("side") == "Buy" else "SHORT"
        age_bars = int((now - float(a.get("armed_at") or now)) / 900)
        lines.append(f"• <b>{base}</b> {side} · {age_bars}/{PULLBACK_MAX_BARS} bars")
    await m.answer("\n".join(lines), reply_markup=kb_main())


@dp.message(Command("settings", "config"))
async def cmd_settings(m: types.Message):
    await m.answer(card_settings(), reply_markup=kb_back())


@dp.message(Command("resume"))
async def cmd_resume(m: types.Message):
    state.unblock()
    await m.answer("🔓 <b>Unblocked</b> — trading allowed again", reply_markup=kb_main())


@dp.message(Command("panic"))
async def cmd_panic(m: types.Message):
    closed = 0
    if trader:
        for sym in list(state.positions.keys()):
            await trader.close(sym, state.positions[sym].get("side", "Buy"))
            state.remove_pos(sym)
            closed += 1
    state.block("panic", 72)
    state.set_enabled(False)
    await m.answer(
        f"🛑 <b>PANIC</b>\nClosed: <b>{closed}</b>\nAuto OFF · blocked 72h\n/resume to unlock",
        reply_markup=kb_main(),
    )


@dp.callback_query(F.data.startswith("cmd:"))
async def cb_cmd(q: CallbackQuery):
    action = (q.data or "").split(":", 1)[-1]
    await q.answer()
    bot = q.bot
    chat = q.message.chat.id if q.message else TELEGRAM_CHAT_ID

    if action == "menu" or action == "help":
        await bot.send_message(chat, card_help(), reply_markup=kb_main())
    elif action == "scan":
        await bot.send_message(chat, "📡 <b>Scanning…</b>")
        await scan_once(bot)
        await bot.send_message(
            chat,
            f"✅ Scan done · pos {len(state.positions)}/{MAX_POSITIONS}",
            reply_markup=kb_main(),
        )
    elif action == "status":
        await bot.send_message(chat, await card_status(), reply_markup=kb_main())
    elif action == "settings":
        await bot.send_message(chat, card_settings(), reply_markup=kb_back())
    elif action == "positions":
        text, kb = await card_positions()
        await bot.send_message(chat, text, reply_markup=kb)
    elif action == "armed":
        if not state.armed:
            await bot.send_message(chat, "🔭 No setups currently watching for a pullback.", reply_markup=kb_main())
        else:
            lines = ["<b>🔭 Watching for pullback</b>", "━━━━━━━━━━━━━━━━━━━━"]
            now = time.time()
            for sym, a in state.armed.items():
                base = sym.replace("USDT", "")
                side = "LONG" if a.get("side") == "Buy" else "SHORT"
                age_bars = int((now - float(a.get("armed_at") or now)) / 900)
                lines.append(f"• <b>{base}</b> {side} · {age_bars}/{PULLBACK_MAX_BARS} bars")
            await bot.send_message(chat, "\n".join(lines), reply_markup=kb_main())
    elif action == "auto_toggle":
        if not trader:
            await bot.send_message(chat, "⚠️ No API keys", reply_markup=kb_main())
        elif state.blocked():
            await bot.send_message(
                chat, f"🔒 Blocked: {state.data.get('blocked_reason')} — /resume",
                reply_markup=kb_main(),
            )
        elif state.enabled():
            state.set_enabled(False)
            await bot.send_message(chat, "⚪ AUTO OFF", reply_markup=kb_main())
        else:
            bal = await trader.balance()
            state.set_enabled(True)
            await bot.send_message(
                chat,
                f"🟢 AUTO ON · bal ${(bal or 0):,.2f}",
                reply_markup=kb_main(),
            )
    elif action == "resume":
        state.unblock()
        await bot.send_message(chat, "🔓 Unblocked", reply_markup=kb_main())
    elif action == "panic":
        closed = 0
        if trader:
            for sym in list(state.positions.keys()):
                await trader.close(sym, state.positions[sym].get("side", "Buy"))
                state.remove_pos(sym)
                closed += 1
        state.block("panic", 72)
        state.set_enabled(False)
        await bot.send_message(
            chat, f"🛑 PANIC · closed {closed} · blocked 72h", reply_markup=kb_main(),
        )


@dp.callback_query(F.data.startswith("enter:"))
async def cb_enter(q: CallbackQuery):
    symbol = (q.data or "").split(":", 1)[-1]
    await q.answer("Opening…")
    sig = last_signals.get(symbol)
    chat_id = q.message.chat.id if q.message else TELEGRAM_CHAT_ID
    if not sig:
        await q.bot.send_message(
            chat_id,
            "⚠️ Signal expired — run /scan for a fresh setup",
            reply_markup=kb_main(),
        )
        return
    ok = await try_enter(q.bot, symbol, sig, notify=True)
    if not ok and trader and not state.blocked():
        await q.bot.send_message(
            chat_id,
            "Could not open (already in position, cooldown, or limit).",
            reply_markup=kb_main(),
        )


@dp.callback_query(F.data.startswith("close:"))
async def cb_close(q: CallbackQuery):
    symbol = (q.data or "").split(":", 1)[-1]
    await q.answer("Closing…")
    base = symbol.replace("USDT", "")
    if not trader:
        await q.message.answer("⚠️ No API", reply_markup=kb_main())
        return
    tr = state.positions.get(symbol)
    side = (tr or {}).get("side", "Buy")
    if tr:
        tr["exit_reason"] = "MANUAL"
        state.save()
    res = await trader.close(symbol, side)
    if res.get("ok"):
        await on_closed(q.bot, symbol)
    else:
        # keep local position bookkeeping intact (position is likely still open on exchange);
        # reconcile() will pick it up on the next pass instead of us silently losing track of it
        await q.message.answer(
            f"⚠️ Close failed for <b>{base}</b> — position kept in tracking, will retry via reconcile",
            reply_markup=kb_main(),
        )




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
            mode = "trading" if trader else "signals only"
            await bot.send_message(
                TELEGRAM_CHAT_ID,
                f"<b>🚀 TTM Squeeze online</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Mode: <b>{mode}</b>\n"
                f"15m + {htf_label()} · EMA{EMA_FAST}/{EMA_SLOW}\n"
                f"Risk ${RISK_USD:.0f} · max {MAX_POSITIONS} pos · lev {LEVERAGE:.0f}x\n"
                f"Closed-bar · BTC filter {'ON' if REQUIRE_BTC_TREND else 'OFF'}",
                reply_markup=kb_main(),
            )
        except Exception as e:
            log.error(e)
        await dp.start_polling(bot)
    finally:
        if http and not http.closed:
            await http.close()


if __name__ == "__main__":
    asyncio.run(main())
