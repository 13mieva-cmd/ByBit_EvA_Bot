"""Auto-trading orchestration: signal -> entry, reconciliation, safety rails."""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from aiogram import Bot

from config import (
    POSITION_SIZE_USD, AUTO_TP_PCT, AUTO_HARD_SL_PCT,
    AUTO_PULLBACK_TP_PCT, AUTO_PULLBACK_SL_PCT,
    MAX_AUTO_POSITIONS, DAILY_LOSS_LIMIT_USD, CONSECUTIVE_LOSS_BLOCK,
    AUTO_TRADE_SIGNAL_TYPES, RECONCILE_INTERVAL_SEC,
    POST_TRADE_COOLDOWN_HOURS,
    BTC_FILTER_ENABLED, BTC_FILTER_15M_DROP_MAX,
    BTC_FILTER_15M_PUMP_MAX, BTC_FILTER_1H_VOLATILITY_MAX,
    TELEGRAM_CHAT_ID,
    LEVERAGE, DEPOSIT_USD,
    AUTO_TRAIL_ENABLED, AUTO_TP1_TRIGGER_PCT, AUTO_TRAIL_DISTANCE_PCT,
    AUTO_TP1_TRIGGER_PCT_PB, AUTO_TRAIL_DISTANCE_PCT_PB,
    EMA_PERIOD,
    AUTO_WAIT_FOR_PULLBACK, AUTO_PULLBACK_WAIT_TYPES,
    PULLBACK_WATCH_INTERVAL_SEC, PULLBACK_WATCH_TIMEOUT_HOURS,
    PULLBACK_MIN_RETRACE_PCT, PULLBACK_MAX_RETRACE_PCT, PULLBACK_ENTRY_RSI_MAX,
)
from trader import BybitTrader
from indicators import calculate_rsi, calculate_ema

log = logging.getLogger("auto")


BYBIT_PUBLIC = "https://api-demo.bybit.com"


async def check_btc_health() -> dict:
    """
    Returns dict with BTC stats and is_ok flag.
    Fetches 15m and 1h klines for BTC.
    """
    result = {
        "is_ok": True,
        "reason": "",
        "change_15m": 0.0,
        "volatility_1h": 0.0,
    }
    try:
        async with aiohttp.ClientSession() as session:
            # Last 15m candle change
            async with session.get(
                f"{BYBIT_PUBLIC}/v5/market/kline",
                params={"category": "linear", "symbol": "BTCUSDT", "interval": "15", "limit": 1},
                timeout=10
            ) as r:
                data = await r.json()
            kl = data.get("result", {}).get("list", [])
            if not kl:
                return result
            op_15m = float(kl[0][1])
            cl_15m = float(kl[0][4])
            change_15m = (cl_15m - op_15m) / op_15m * 100 if op_15m > 0 else 0
            result["change_15m"] = change_15m

            # Last 1h candles for volatility (std dev of closes over 4×15m candles)
            async with session.get(
                f"{BYBIT_PUBLIC}/v5/market/kline",
                params={"category": "linear", "symbol": "BTCUSDT", "interval": "15", "limit": 4},
                timeout=10
            ) as r:
                data1h = await r.json()
            kl1h = data1h.get("result", {}).get("list", [])
            if len(kl1h) >= 4:
                closes = [float(k[4]) for k in kl1h]
                mean = sum(closes) / len(closes)
                if mean > 0:
                    variance = sum((c - mean) ** 2 for c in closes) / len(closes)
                    std_dev = math.sqrt(variance)
                    vol_pct = (std_dev / mean) * 100
                    result["volatility_1h"] = vol_pct
    except Exception as e:
        log.warning(f"check_btc_health: {e}")
        return result

    # Apply thresholds
    if change_15m <= -BTC_FILTER_15M_DROP_MAX:
        result["is_ok"] = False
        result["reason"] = f"BTC падает быстро ({change_15m:+.2f}% за 15м)"
    elif change_15m >= BTC_FILTER_15M_PUMP_MAX:
        result["is_ok"] = False
        result["reason"] = f"BTC резко растёт ({change_15m:+.2f}% за 15м) — FOMO ралли"
    elif result["volatility_1h"] >= BTC_FILTER_1H_VOLATILITY_MAX:
        result["is_ok"] = False
        result["reason"] = f"BTC волатилен ({result['volatility_1h']:.2f}% std за 1ч)"

    return result


async def _fetch_klines_1h(session, symbol: str, limit: int) -> list:
    """Самодостаточныйفetch 1h-свечей — не импортируется из scanner.py,
    т.к. scanner.py сам импортирует AutoTrader (circular import).
    Тот же паттерн, что уже используется в check_btc_health()."""
    try:
        async with session.get(
            f"{BYBIT_PUBLIC}/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": "60", "limit": limit},
            timeout=10,
        ) as r:
            data = await r.json()
        return list(reversed(data.get("result", {}).get("list", [])))
    except Exception as e:
        log.warning(f"pullback-watch kline {symbol}: {e}")
        return []


async def evaluate_pullback_entry(session, pending: dict) -> dict:
    """
    Проверяет, готов ли отложенный сигнал ко входу на откате.

    Возвращает dict с "action":
      "enter"  — цена откатила, RSI остыл, отскок пошёл -> можно входить
      "wait"   — ещё рано, продолжаем ждать (peak_price обновлён)
      "cancel" — тренд сломан ИЛИ истёк таймаут -> снимаем с наблюдения
    """
    symbol = pending["symbol"]
    if time.time() > pending["expires_at"]:
        return {"action": "cancel", "reason": f"таймаут ожидания ({PULLBACK_WATCH_TIMEOUT_HOURS:.0f}ч)"}

    need = EMA_PERIOD + 5
    klines_1h = await _fetch_klines_1h(session, symbol, max(need, 30))
    if len(klines_1h) < need:
        return {"action": "wait", "reason": "недостаточно данных"}

    try:
        closes_1h = [float(k[4]) for k in klines_1h]
    except (ValueError, IndexError, TypeError):
        return {"action": "wait", "reason": "bad kline data"}

    current_price = closes_1h[-1]
    ema50 = calculate_ema(closes_1h, EMA_PERIOD)
    rsi_1h = calculate_rsi(closes_1h, 14)
    if ema50 is None or rsi_1h is None:
        return {"action": "wait", "reason": "индикаторы недоступны"}

    peak_price = max(pending.get("peak_price", pending["detected_price"]), current_price)

    # Тренд сломан — цена ушла ниже EMA50 (та же опора, что держит PULLBACK-сигнал)
    if current_price < ema50:
        return {"action": "cancel", "reason": "цена ушла ниже EMA50 — тренд сломан"}

    retrace_pct = (peak_price - current_price) / peak_price * 100 if peak_price > 0 else 0

    # Откатило слишком глубоко — это уже похоже на разворот, а не на откат
    if retrace_pct > PULLBACK_MAX_RETRACE_PCT:
        return {"action": "cancel", "reason": f"откат {retrace_pct:.1f}% — похоже на разворот, а не на откат"}

    bounced = len(closes_1h) >= 2 and closes_1h[-1] > closes_1h[-2]
    ready = (
        retrace_pct >= PULLBACK_MIN_RETRACE_PCT
        and rsi_1h <= PULLBACK_ENTRY_RSI_MAX
        and bounced
    )

    return {
        "action": "enter" if ready else "wait",
        "price": current_price,
        "peak_price": peak_price,
        "rsi_1h": rsi_1h,
        "retrace_pct": retrace_pct,
    }


class AutoTrader:
    def __init__(self, bot: Bot, trader: BybitTrader, state_store):
        self.bot = bot
        self.trader = trader
        self.state = state_store
        self.allowed_types = {t.strip() for t in AUTO_TRADE_SIGNAL_TYPES.split(",")}
        self._signal_lock = asyncio.Lock()

    async def notify(self, text: str):
        try:
            await self.bot.send_message(TELEGRAM_CHAT_ID, text)
        except Exception as e:
            log.warning(f"notify: {e}")

    async def handle_signal(self, signal: dict):
        """Called by scanner when alert is generated.
        Решает: войти сразу, поставить в лист ожидания отката, или пропустить."""
        async with self._signal_lock:
            if not self.state.is_enabled():
                return
            sig_type = signal["signal_type"]
            if sig_type not in self.allowed_types:
                return
            # Per-signal-type toggle check
            if not self.state.get_signal_toggle(sig_type):
                log.info(f"Signal type {sig_type} disabled, skip {signal['symbol']}")
                return

            symbol = signal["symbol"]

            # Post-trade cooldown check
            if self.state.is_in_post_trade_cooldown(symbol):
                log.info(f"{symbol} in post-trade cooldown, skip auto-entry")
                return
            if symbol in self.state.active_positions:
                return

            # Какие типы сигналов должны сначала дождаться отката, а не
            # входить сразу по цене сигнала (см. AUTO_WAIT_FOR_PULLBACK в config.py)
            wait_types = (
                {t.strip() for t in AUTO_PULLBACK_WAIT_TYPES.split(",")}
                if AUTO_WAIT_FOR_PULLBACK else set()
            )

            if self.state.is_pending(symbol):
                if sig_type in wait_types:
                    return  # уже отслеживаем этот символ — не сбрасываем таймер/пик
                # Пришёл более прямой сигнал (например, сам PULLBACK) —
                # он сам по себе уже подтверждение отката, используем его
                # вместо дальнейшего ожидания.
                self.state.remove_pending_entry(symbol)
            elif sig_type in wait_types:
                await self._queue_pullback_watch(signal)
                return

            await self._try_enter(signal, sig_type)

    async def _queue_pullback_watch(self, signal: dict):
        """STANDARD/SURGE по конструкции ловят монету уже ПОСЛЕ импульса —
        рыночный вход сразу по сигналу означает вход на хае. Вместо этого
        ставим символ в лист ожидания; реальный вход случится только когда
        pullback_watch_loop() увидит настоящий откат (см. evaluate_pullback_entry)."""
        symbol = signal["symbol"]
        sig_type = signal["signal_type"]
        now = time.time()
        self.state.add_pending_entry(
            symbol=symbol,
            signal_type=sig_type,
            stars=signal["stars"],
            detected_price=signal["price"],
            peak_price=signal["price"],
            detected_at=now,
            expires_at=now + PULLBACK_WATCH_TIMEOUT_HOURS * 3600,
        )
        base = symbol.replace("USDT", "")
        await self.notify(
            f"👀 <b>{base}</b> — сигнал {sig_type} {'⭐' * signal['stars']}, вход отложен\n\n"
            f"Цена сигнала: <code>${signal['price']:.6g}</code>\n"
            f"Жду откат ≥{PULLBACK_MIN_RETRACE_PCT}% от пика и RSI(1ч) ≤{PULLBACK_ENTRY_RSI_MAX}, "
            f"пока цена держится выше EMA50.\n"
            f"Отменю, если тренд сломается или пройдёт {PULLBACK_WATCH_TIMEOUT_HOURS:.0f}ч без отката."
        )

    async def _try_enter(self, signal: dict, sig_type: str):
        """Финальные проверки готовности + реальный вход маркет-ордером.
        Используется и для немедленных сигналов (PULLBACK, либо когда
        AUTO_WAIT_FOR_PULLBACK выключен), и для отложенных сигналов,
        дождавшихся отката в pullback_watch_loop()."""
        symbol = signal["symbol"]

        # BTC market filter check
        if BTC_FILTER_ENABLED and self.state.is_btc_filter_enabled():
            btc_health = await check_btc_health()
            if not btc_health["is_ok"]:
                log.info(f"BTC filter blocked {symbol}: {btc_health['reason']}")
                base = symbol.replace("USDT", "")
                await self.notify(
                    f"⛔ <b>{base}</b> — авто-вход пропущен\n\n"
                    f"Сигнал: {sig_type} {'⭐' * signal['stars']}\n"
                    f"Причина: <b>{btc_health['reason']}</b>\n\n"
                    f"<i>Алерт пришёл, но рынок BTC нестабилен — "
                    f"бот не вошёл для безопасности. Можешь зайти вручную, если уверен.</i>"
                )
                return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.state.maybe_reset_day(today)

        if self.state.is_blocked():
            log.info(f"Auto blocked ({self.state.blocked_reason}), skip {symbol}")
            return
        if len(self.state.active_positions) >= MAX_AUTO_POSITIONS:
            log.info(f"Max {MAX_AUTO_POSITIONS} positions, skip {symbol}")
            return
        if symbol in self.state.active_positions:
            return

        # Also check Bybit-side: is there really no position? (sync safety)
        bybit_positions = await self.trader.get_open_positions(symbol)
        if bybit_positions:
            log.info(f"Bybit already has position in {symbol}, skip")
            return

        # Select TP/SL based on signal type
        if sig_type == "PULLBACK":
            tp_pct = AUTO_PULLBACK_TP_PCT
            sl_pct = AUTO_PULLBACK_SL_PCT
        else:
            tp_pct = AUTO_TP_PCT
            sl_pct = AUTO_HARD_SL_PCT

        base = symbol.replace("USDT", "")
        stars_str = "⭐" * signal["stars"]
        await self.notify(
            f"🤖 <b>AUTO-ENTRY</b> — {base}\n"
            f"Сигнал: {sig_type} {stars_str}\n"
            f"Размер: ${POSITION_SIZE_USD}\n"
            f"TP +{tp_pct}% / SL −{sl_pct}%\n"
            f"Открываю позицию..."
        )

        if sig_type == "PULLBACK":
            trig_pct, dist_pct = AUTO_TP1_TRIGGER_PCT_PB, AUTO_TRAIL_DISTANCE_PCT_PB
        else:
            trig_pct, dist_pct = AUTO_TP1_TRIGGER_PCT, AUTO_TRAIL_DISTANCE_PCT
        result = await self.trader.open_long_with_tpsl(
            symbol, POSITION_SIZE_USD, tp_pct, sl_pct, leverage=LEVERAGE,
            trail_trigger_pct=(trig_pct if AUTO_TRAIL_ENABLED else None),
            trail_distance_pct=(dist_pct if AUTO_TRAIL_ENABLED else None),
        )
        if not result["ok"]:
            err = result.get("error", "unknown")
            code = result.get("code", "")
            await self.notify(
                f"❌ <b>{base}</b>: ошибка входа\n"
                f"<code>{err}</code> (код {code})"
            )
            return

        await asyncio.sleep(2)
        positions = await self.trader.get_open_positions(symbol)
        if not positions:
            await self.notify(
                f"⚠️ <b>{base}</b>: ордер отправлен, "
                f"но позиция не подтверждена. Проверь Bybit."
            )
            return
        pos = positions[0]

        # === Настройка защиты от ФАКТИЧЕСКОЙ цены входа (avgPrice) ===
        # Ордер маркетный — реальная цена входа отличается от расчётной.
        if AUTO_TRAIL_ENABLED:
            # Трейлинг с activePrice на уровне первого профита. Пока цена не дошла —
            # держит обычный стоп. Дошла -> Bybit САМ включает трейлинг. Фикс. TP нет,
            # значит нечему закрыть сделку раньше трейлинга.
            arm = await self.trader.arm_trailing_from_fill(
                symbol, sl_pct, trig_pct, dist_pct)
            if arm.get("ok"):
                result["sl_price"] = arm["sl_price"]
                result["active_price"] = arm["active_price"]
            else:
                log.warning(f"arm_trailing {symbol}: {arm.get('error')}")
        else:
            adjust_result = await self.trader.set_tpsl_from_fill(symbol, tp_pct, sl_pct)
            if adjust_result.get("ok"):
                result["tp_price"] = adjust_result["tp_price"]
                result["sl_price"] = adjust_result["sl_price"]
            else:
                log.warning(
                    f"Failed to adjust TP/SL for {symbol}: {adjust_result.get('error')}"
                )

        # === Проверка, что стоп РЕАЛЬНО стоит на бирже ===
        protected = await self.trader.verify_position_protected(symbol)
        if protected.get("has_stop") is False:
            await self.notify(
                f"🚨 <b>{base}</b>: стоп не установлен на бирже! "
                f"Закрываю позицию немедленно."
            )
            await self.trader.close_position_market(symbol)
            return

        self.state.add_position(
            symbol=symbol,
            entry_price=pos["entry_price"],
            qty=pos["size"],
            tp_price=result["tp_price"],
            sl_price=result["sl_price"],
            leverage=result["leverage"],
            signal_type=sig_type,
            stars=signal["stars"],
        )
        await self.notify(
            f"✅ <b>{base}</b> позиция открыта ({sig_type})\n\n"
            f"Вход: <code>${pos['entry_price']:.6g}</code>\n"
            f"Размер: ${POSITION_SIZE_USD} (qty {pos['size']})\n"
            f"Плечо: {result['leverage']:.0f}x\n"
            + (f"📈 Трейлинг: включится на +{trig_pct}%, дистанция {dist_pct}%\n"
               if AUTO_TRAIL_ENABLED
               else f"🎯 TP: <code>${result['tp_price']:.6g}</code> (+{tp_pct}%)\n")
            + f"🛑 SL: <code>${result['sl_price']:.6g}</code> (−{sl_pct}%) "
            f"≈ −${POSITION_SIZE_USD * sl_pct / 100:.2f} "
            f"({POSITION_SIZE_USD * sl_pct / 100 / DEPOSIT_USD * 100:.1f}% депозита)\n\n"
            f"Активных позиций: {len(self.state.active_positions)}/{MAX_AUTO_POSITIONS}"
        )

    async def pullback_watch_loop(self):
        """Фоновый цикл: раз в PULLBACK_WATCH_INTERVAL_SEC проверяет лист
        ожидания (символы, где STANDARD/SURGE уже поймали импульс, но вход
        отложен до отката) и входит, как только откат подтверждён."""
        while True:
            try:
                await self.check_pending_entries()
            except Exception as e:
                log.exception(f"pullback_watch: {e}")
            await asyncio.sleep(PULLBACK_WATCH_INTERVAL_SEC)

    async def check_pending_entries(self):
        pending = dict(self.state.pending_entries)
        if not pending:
            return
        async with aiohttp.ClientSession() as session:
            for symbol, p in pending.items():
                try:
                    await self._process_pending(session, symbol, p)
                except Exception as e:
                    log.exception(f"pending {symbol}: {e}")

    async def _process_pending(self, session, symbol: str, pending: dict):
        result = await evaluate_pullback_entry(session, pending)
        action = result.get("action")
        base = symbol.replace("USDT", "")

        async with self._signal_lock:
            # Символ мог уже уйти из листа ожидания (гонка с handle_signal,
            # например прямой PULLBACK-сигнал успел его забрать) — перепроверяем
            # прямо перед изменением состояния.
            if not self.state.is_pending(symbol):
                return

            if action == "cancel":
                self.state.remove_pending_entry(symbol)
                await self.notify(
                    f"🚫 <b>{base}</b> — вход на откате отменён: {result.get('reason', '')}"
                )
                return

            if action == "wait":
                if "peak_price" in result:
                    self.state.update_pending_entry(symbol, peak_price=result["peak_price"])
                return

            if action == "enter":
                self.state.remove_pending_entry(symbol)
                signal = {
                    "symbol": symbol,
                    "signal_type": pending["signal_type"],
                    "stars": pending["stars"],
                    "price": result["price"],
                }
                retrace = result.get("retrace_pct", 0)
                await self.notify(
                    f"↩️ <b>{base}</b> — откат подтверждён, вхожу\n\n"
                    f"Сигнал был по <code>${pending['detected_price']:.6g}</code>, "
                    f"сейчас <code>${result['price']:.6g}</code> "
                    f"(откат {retrace:.1f}% от пика, RSI {result['rsi_1h']:.0f})"
                )
                await self._try_enter(signal, pending["signal_type"])

    async def reconcile_loop(self):
        while True:
            try:
                await self.reconcile_once()
            except Exception as e:
                log.exception(f"reconcile: {e}")
            await asyncio.sleep(RECONCILE_INTERVAL_SEC)

    async def reconcile_once(self):
        if not self.state.active_positions:
            return
        bybit_positions = await self.trader.get_open_positions()
        bybit_syms = {p["symbol"] for p in bybit_positions}
        for symbol in list(self.state.active_positions.keys()):
            if symbol in bybit_syms:
                continue
            await self.handle_closed_position(symbol)


    async def handle_closed_position(self, symbol: str):
        tracked = self.state.active_positions.get(symbol)
        if not tracked:
            return

        # Lookup closed PnL
        closed = await self.trader.get_closed_pnl(symbol, 5)
        pnl_usd = None
        close_reason = "UNKNOWN"
        exit_price = None
        for cp in closed:
            try:
                updated_ts = int(cp.get("updatedTime", 0)) / 1000
                if updated_ts <= tracked["opened_at"] - 5:
                    continue
                pnl_usd = float(cp.get("closedPnl", 0))
                exit_price = float(cp.get("avgExitPrice", 0))
            except (KeyError, ValueError, TypeError):
                continue
            # Определение причины закрытия.
            # ВАЖНО: в режиме трейлинга фиксированного TP НЕТ (tp_price = None).
            # Раньше здесь был abs(exit_price - None) -> TypeError -> запись
            # пропускалась, причина оставалась UNKNOWN, а PnL мог быть взят
            # от ДРУГОЙ (более старой) сделки. Это ломало дневной лимит убытка.
            try:
                entry_p = tracked.get("entry_price") or 0
                tp_p = tracked.get("tp_price")
                sl_p = tracked.get("sl_price")
                if exit_price > 0:
                    if sl_p and abs(exit_price - sl_p) / sl_p < 0.01:
                        close_reason = "SL"
                    elif tp_p and abs(exit_price - tp_p) / tp_p < 0.005:
                        close_reason = "TP"
                    elif tp_p is None and entry_p and exit_price > entry_p:
                        # трейлинг-режим: вышли выше входа -> сработал трейлинг-стоп
                        close_reason = "TRAIL"
                    else:
                        close_reason = "MANUAL"
            except (TypeError, ValueError, ZeroDivisionError):
                close_reason = "UNKNOWN"
            break

        if pnl_usd is not None:
            self.state.add_pnl(pnl_usd)
            if pnl_usd < 0:
                self.state.incr_consecutive_loss()
            elif pnl_usd > 0:
                self.state.reset_consecutive_loss()

        self.state.remove_position(symbol)

        # Set post-trade cooldown: don't auto-trade this symbol again for N hours
        self.state.add_post_trade_cooldown(symbol, POST_TRADE_COOLDOWN_HOURS)

        base = symbol.replace("USDT", "")
        emoji = {"TP": "✅", "SL": "🛑", "TRAIL": "📈",
                 "MANUAL": "✋", "UNKNOWN": "❓"}.get(close_reason, "❓")
        pnl_str = f"${pnl_usd:+.2f}" if pnl_usd is not None else "?"
        exit_str = f"<code>${exit_price:.6g}</code>" if exit_price else "?"

        msg = (
            f"{emoji} <b>{base}</b> закрыта\n\n"
            f"Причина: <b>{close_reason}</b>\n"
            f"Выход: {exit_str}\n"
            f"P&L: <b>{pnl_str}</b>\n"
            f"Дневной P&L: <b>${self.state.daily_pnl:+.2f}</b>\n"
            f"Подряд убытков: {self.state.consecutive_losses}\n"
            f"Активных позиций: {len(self.state.active_positions)}/{MAX_AUTO_POSITIONS}\n\n"
            f"🔒 <b>{base}</b> заблокирован для авто-входа на {POST_TRADE_COOLDOWN_HOURS}ч.\n"
            f"<i>(Алерты в Telegram продолжат приходить, но бот не будет автоматически входить.)</i>"
        )

        if self.state.daily_pnl <= -DAILY_LOSS_LIMIT_USD:
            self.state.block_daily()
            msg += (
                f"\n\n🚫 <b>Daily loss limit</b> (−${DAILY_LOSS_LIMIT_USD}) достигнут.\n"
                f"Авто-торговля заблокирована до завтра (UTC)."
            )
        elif self.state.consecutive_losses >= CONSECUTIVE_LOSS_BLOCK:
            self.state.block_consecutive()
            msg += (
                f"\n\n🚫 <b>{CONSECUTIVE_LOSS_BLOCK} убытков подряд</b>.\n"
                f"Авто-торговля заблокирована. <code>/resume</code> чтобы разблокировать."
            )

        await self.notify(msg)

    async def panic_close_all(self) -> tuple[int, int]:
        """Close all auto positions by market. Returns (closed_ok, closed_fail)."""
        ok, fail = 0, 0
        for symbol in list(self.state.active_positions.keys()):
            try:
                await self.trader.cancel_all_orders(symbol)
                result = await self.trader.close_position_market(symbol)
                if result.get("ok"):
                    self.state.remove_position(symbol)
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                log.warning(f"panic {symbol}: {e}")
                fail += 1
        self.state.block_panic()
        return ok, fail
