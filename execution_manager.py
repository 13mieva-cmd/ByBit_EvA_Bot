"""
Исполнение сделок (v3): корректный расчёт размера позиции (exchange.amount_to_precision),
кап на объём, верификация защитного стопа, персистентные риск-контуры, сопровождение
позиции (БУ, ATR-трейлинг, partial, time-stop, фейд по моментуму).

Инициализация биржи вынесена в data_manager.build_exchange() -- единая точка,
которая поддерживает demo/testnet ключи (BYBIT_BASE_URL/BYBIT_DEMO_URL/EXCHANGE_SANDBOX)
и даёт понятную диагностику при retCode=10003/10032.
"""
import logging
import time
from typing import Optional, Dict, Any

from ccxt.base.errors import NetworkError, ExchangeError, InsufficientFunds, InvalidOrder

from config import (
    EXCHANGE_ID, API_KEY, API_SECRET,
    RISK_PER_TRADE, DRY_RUN, MAX_POSITION_PCT_OF_BALANCE, MAX_POSITIONS,
    DAILY_LOSS_PCT, CONSEC_LOSS_BLOCK, CONSEC_LOSS_BLOCK_HOURS, DAILY_LOSS_BLOCK_HOURS,
    LEVERAGE, MARGIN_MODE, BE_TRIGGER_R, TRAIL_ATR_MULT, PARTIAL_PCT,
    MAX_HOLD_BARS, MOM_FADE_BARS, TIMEFRAME, MOM_LENGTH, COOLDOWN_MINUTES,
    TRADES_CSV,
)
from strategy import TradeSignal
from storage import append_csv
from indicators import atr as atr_ind, momentum_hist
from data_manager import build_exchange

logger = logging.getLogger(__name__)

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1d": 86400}


class ExecutionManager:
    def __init__(self, state):
        self.state = state
        self.exchange = self._init_exchange()
        self.dry_run = DRY_RUN
        self._leverage_set = set()

    def _init_exchange(self):
        try:
            exchange = build_exchange(EXCHANGE_ID, API_KEY, API_SECRET)
            logger.info(f"ExecutionManager: биржа {EXCHANGE_ID} готова")
            return exchange
        except Exception as e:
            logger.error(f"Ошибка инициализации биржи в ExecutionManager: {e}")
            raise

    def _ensure_leverage(self, symbol: str):
        if symbol in self._leverage_set or self.dry_run:
            return
        try:
            if hasattr(self.exchange, "set_margin_mode"):
                self.exchange.set_margin_mode(MARGIN_MODE, symbol)
        except Exception as e:
            logger.debug(f"set_margin_mode {symbol}: {e}")
        try:
            if hasattr(self.exchange, "set_leverage"):
                self.exchange.set_leverage(int(LEVERAGE), symbol)
        except Exception as e:
            logger.debug(f"set_leverage {symbol}: {e}")
        self._leverage_set.add(symbol)

    def get_usdt_balance(self) -> float:
        try:
            balance = self.exchange.fetch_balance()
            usdt = balance.get("USDT", {})
            free = float(usdt.get("free", 0.0) or 0.0)
            total = float(usdt.get("total", free) or free)
            return total if total > 0 else free
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            raise

    def risk_gate_ok(self) -> bool:
        if not self.state.enabled():
            return False
        if self.state.blocked():
            return False
        if len(self.state.positions) >= MAX_POSITIONS:
            return False
        return True

    def register_pnl_and_check_breakers(self, pnl: float, balance: float):
        self.state.add_pnl(pnl)
        daily_limit = -abs(DAILY_LOSS_PCT) * max(balance, 1e-9)
        if self.state.data.get("daily_pnl", 0) <= daily_limit:
            self.state.block("daily_loss", DAILY_LOSS_BLOCK_HOURS)
            logger.warning(f"Дневной лимит убытка достигнут ({self.state.data['daily_pnl']:.2f}) -- блокировка входов")
        elif self.state.data.get("consec_losses", 0) >= CONSEC_LOSS_BLOCK:
            self.state.block("consec_losses", CONSEC_LOSS_BLOCK_HOURS)
            logger.warning(f"{CONSEC_LOSS_BLOCK} убытков подряд -- блокировка входов")

    def calculate_position_size(self, signal: TradeSignal, balance: float) -> Optional[float]:
        try:
            entry = signal.entry_price
            sl = signal.stop_loss
            risk_per_unit = abs(entry - sl)
            if risk_per_unit <= 0:
                logger.error("Нулевое расстояние до стопа")
                return None

            risk_amount = balance * RISK_PER_TRADE
            position_size = risk_amount / risk_per_unit
            notional = position_size * entry

            max_notional = balance * MAX_POSITION_PCT_OF_BALANCE * LEVERAGE
            if notional > max_notional and entry > 0:
                position_size = max_notional / entry
                logger.info(f"{signal.symbol}: размер урезан капом ({MAX_POSITION_PCT_OF_BALANCE*100:.0f}% баланса)")

            try:
                position_size = float(self.exchange.amount_to_precision(signal.symbol, position_size))
            except Exception:
                position_size = round(position_size, 6)

            market = self.exchange.markets.get(signal.symbol, {})
            min_amount = (market.get("limits", {}) or {}).get("amount", {}).get("min")
            if min_amount and position_size < min_amount:
                logger.warning(f"{signal.symbol}: размер {position_size} меньше минимума биржи {min_amount}")
                return None
            if position_size <= 0:
                return None

            logger.info(f"{signal.symbol}: размер={position_size} | риск={risk_amount:.2f} USDT "
                        f"({RISK_PER_TRADE*100:.1f}%) | SL-дистанция={risk_per_unit:.6f}")
            return position_size
        except Exception as e:
            logger.error(f"Ошибка расчёта размера позиции: {e}")
            return None

    def execute_signal(self, signal: TradeSignal) -> Dict[str, Any]:
        result = {"success": False, "order_id": None, "message": "", "signal": signal}

        if not self.risk_gate_ok():
            result["message"] = "Риск-гейт закрыт (блокировка / выключен / лимит позиций)"
            return result
        if signal.symbol in self.state.positions:
            result["message"] = "Уже есть открытая позиция по символу"
            return result
        if self.state.is_cool(signal.symbol):
            result["message"] = "Символ на кулдауне"
            return result

        try:
            balance = self.get_usdt_balance()
            if balance < 10:
                result["message"] = "Недостаточно средств (баланс < 10 USDT)"
                logger.warning(result["message"])
                return result

            amount = self.calculate_position_size(signal, balance)
            if amount is None or amount <= 0:
                result["message"] = "Не удалось рассчитать размер позиции"
                return result

            side = "buy" if signal.side == "long" else "sell"
            symbol = signal.symbol
            self._ensure_leverage(symbol)

            if self.dry_run:
                logger.info(f"[DRY_RUN] {signal.side.upper()} {symbol} | Entry={signal.entry_price} "
                            f"SL={signal.stop_loss} TP={signal.take_profit} Size={amount} "
                            f"Score={signal.score} | {signal.reason}")
                self.state.add_pos(symbol, side=signal.side, entry=signal.entry_price, sl=signal.stop_loss,
                                    tp=signal.take_profit, size=amount, signal_type=signal.signal_type,
                                    be=False, trail=False, partial=False)
                append_csv(TRADES_CSV, {"event": "entry", "ts": time.time(), "symbol": symbol, "side": signal.side,
                                         "entry": signal.entry_price, "sl": signal.stop_loss, "tp": signal.take_profit,
                                         "size": amount, "dry_run": True})
                result.update(success=True, message="DRY_RUN: ордер не отправлен", order_id="dry-run")
                return result

            params = {
                "stopLoss": {"triggerPrice": signal.stop_loss, "type": "market"},
                "takeProfit": {"triggerPrice": signal.take_profit, "type": "market"},
            }
            order = self.exchange.create_order(symbol=symbol, type="market", side=side, amount=amount, params=params)
            order_id = order.get("id")
            logger.info(f"Ордер выставлен | ID={order_id} | {side.upper()} {amount} {symbol}")

            time.sleep(2)
            if not self._verify_protective_stop(symbol):
                logger.error(f"{symbol}: SL не обнаружен на бирже после входа -- закрываю позицию для безопасности")
                self._market_close(symbol, signal.side, amount)
                result["message"] = "SL не подтверждён биржей -- позиция закрыта для безопасности"
                return result

            self.state.add_pos(symbol, side=signal.side, entry=signal.entry_price, sl=signal.stop_loss,
                                tp=signal.take_profit, size=amount, signal_type=signal.signal_type,
                                be=False, trail=False, partial=False)
            append_csv(TRADES_CSV, {"event": "entry", "ts": time.time(), "symbol": symbol, "side": signal.side,
                                     "entry": signal.entry_price, "sl": signal.stop_loss, "tp": signal.take_profit,
                                     "size": amount, "dry_run": False})
            result.update(success=True, order_id=order_id, message=f"Ордер исполнен (ID={order_id})", raw_order=order)
            return result

        except InsufficientFunds as e:
            result["message"] = f"Недостаточно средств: {e}"
        except InvalidOrder as e:
            result["message"] = f"Некорректный ордер: {e}"
        except (NetworkError, ExchangeError) as e:
            result["message"] = f"Ошибка сети/биржи: {e}"
        except Exception as e:
            result["message"] = f"Неожиданная ошибка исполнения: {e}"
        logger.error(result["message"])
        return result

    def _verify_protective_stop(self, symbol: str) -> bool:
        try:
            positions = self.exchange.fetch_positions([symbol])
            for p in positions:
                sz = float(p.get("contracts") or p.get("info", {}).get("size") or 0)
                if sz > 0:
                    sl = (p.get("info", {}) or {}).get("stopLoss")
                    return bool(sl and str(sl) not in ("", "0"))
            return False
        except Exception as e:
            logger.warning(f"Не удалось проверить SL для {symbol}: {e}")
            return False

    def _market_close(self, symbol: str, side: str, amount: Optional[float] = None) -> bool:
        try:
            if self.dry_run:
                return True
            if amount is None:
                positions = self.exchange.fetch_positions([symbol])
                amount = sum(float(p.get("contracts") or 0) for p in positions)
            if not amount:
                return True
            close_side = "sell" if side == "long" else "buy"
            self.exchange.create_order(symbol=symbol, type="market", side=close_side,
                                        amount=amount, params={"reduceOnly": True})
            return True
        except Exception as e:
            logger.error(f"Ошибка закрытия {symbol}: {e}")
            return False

    def cancel_all_orders(self, symbol: str) -> bool:
        try:
            if self.dry_run:
                return True
            self.exchange.cancel_all_orders(symbol)
            return True
        except Exception as e:
            logger.error(f"Ошибка отмены ордеров: {e}")
            return False

    def reconcile(self, data_manager):
        if not self.state.positions:
            return
        tf_sec = _TF_SECONDS.get(TIMEFRAME, 900)

        for symbol in list(self.state.positions.keys()):
            tr = self.state.positions[symbol]
            side = tr["side"]
            entry = float(tr["entry"])

            kl = self._closed_only(symbol, data_manager)
            if kl is None or len(kl["c"]) < MOM_LENGTH + 3:
                continue
            mark = data_manager.get_live_price(symbol) or kl["c"][-1]

            risk = abs(entry - float(tr["sl"]))
            if risk <= 0:
                continue
            gain_r = ((mark - entry) if side == "long" else (entry - mark)) / risk

            self._maybe_manage_stops(symbol, tr, kl, gain_r, dry=self.dry_run)
            self._check_exit_conditions(symbol, tr, kl, mark, gain_r, tf_sec)

    def _closed_only(self, symbol: str, data_manager) -> Optional[Dict[str, list]]:
        try:
            df = data_manager.update_latest_candle(symbol)
            if df is None or len(df) < 5:
                return None
            return {"o": df["open"].tolist(), "h": df["high"].tolist(),
                    "l": df["low"].tolist(), "c": df["close"].tolist(), "v": df["volume"].tolist()}
        except Exception as e:
            logger.warning(f"Не удалось получить данные для reconcile {symbol}: {e}")
            return None

    def _maybe_manage_stops(self, symbol: str, tr: dict, kl: dict, gain_r: float, dry: bool):
        side = tr["side"]
        entry = float(tr["entry"])

        if not tr.get("be") and gain_r >= BE_TRIGGER_R:
            be_sl = entry * (1.001 if side == "long" else 0.999)
            if dry or self._update_sl(symbol, be_sl):
                tr["be"] = True
                tr["sl"] = be_sl
                self.state.save()
                logger.info(f"{symbol}: стоп переведён в БУ")

        if not tr.get("trail") and gain_r >= 1.0:
            atr_v = atr_ind(kl["h"], kl["l"], kl["c"], 14) or atr_ind(kl["h"], kl["l"], kl["c"], 20)
            if atr_v:
                trail_dist = TRAIL_ATR_MULT * atr_v
                new_sl = (kl["c"][-1] - trail_dist) if side == "long" else (kl["c"][-1] + trail_dist)
                improved = (side == "long" and new_sl > float(tr["sl"])) or (side == "short" and new_sl < float(tr["sl"]))
                if improved and (dry or self._update_sl(symbol, new_sl)):
                    tr["sl"] = new_sl
                    tr["trail"] = True
                    self.state.save()
                    logger.info(f"{symbol}: ATR-трейлинг обновлён -> {new_sl:.6g}")
            if not tr.get("partial") and (dry or self._partial_close(symbol, side)):
                tr["partial"] = True
                self.state.save()

    def _check_exit_conditions(self, symbol: str, tr: dict, kl: dict, mark: float, gain_r: float, tf_sec: int) -> bool:
        side = tr["side"]
        c = kl["c"]

        mom_1 = momentum_hist(c, MOM_LENGTH)
        mom_2 = momentum_hist(c[:-1], MOM_LENGTH)
        mom_3 = momentum_hist(c[:-2], MOM_LENGTH) if MOM_FADE_BARS >= 2 else None
        fade = False
        if side == "long" and mom_1 is not None and mom_2 is not None:
            if MOM_FADE_BARS >= 2 and mom_3 is not None:
                fade = mom_1 > 0 and mom_2 > 0 and mom_3 > 0 and mom_1 < mom_2 < mom_3
            else:
                fade = mom_1 > 0 and mom_2 > 0 and mom_1 < mom_2
        elif side == "short" and mom_1 is not None and mom_2 is not None:
            if MOM_FADE_BARS >= 2 and mom_3 is not None:
                fade = mom_1 < 0 and mom_2 < 0 and mom_3 < 0 and mom_1 > mom_2 > mom_3
            else:
                fade = mom_1 < 0 and mom_2 < 0 and mom_1 > mom_2

        opened_at = float(tr.get("opened_at") or 0)
        bars_held = int((time.time() - opened_at) / tf_sec) if opened_at else 0

        exit_reason = None
        if fade:
            exit_reason = "MOMENTUM_FADE"
        elif bars_held >= MAX_HOLD_BARS and gain_r < 1.5:
            exit_reason = "TIME_STOP"
        elif side == "long" and mark <= float(tr["sl"]):
            exit_reason = "SL_HIT"
        elif side == "short" and mark >= float(tr["sl"]):
            exit_reason = "SL_HIT"
        elif side == "long" and mark >= float(tr["tp"]):
            exit_reason = "TP_HIT"
        elif side == "short" and mark <= float(tr["tp"]):
            exit_reason = "TP_HIT"

        if not exit_reason:
            return False

        pnl_per_unit = (mark - float(tr["entry"])) if side == "long" else (float(tr["entry"]) - mark)
        pnl = pnl_per_unit * float(tr.get("size") or 0)

        if not self.dry_run:
            self._market_close(symbol, side)
        balance = 0.0
        try:
            balance = self.get_usdt_balance()
        except Exception:
            pass

        self.state.remove_pos(symbol)
        self.state.cool(symbol, COOLDOWN_MINUTES)
        self.register_pnl_and_check_breakers(pnl, balance or 1.0)
        append_csv(TRADES_CSV, {"event": "exit", "ts": time.time(), "symbol": symbol, "side": side,
                                 "pnl": round(pnl, 4), "reason": exit_reason})
        logger.info(f"{symbol}: закрыта ({exit_reason}) | PnL={pnl:.4f}")
        return True

    def _update_sl(self, symbol: str, new_sl: float) -> bool:
        try:
            positions = self.exchange.fetch_positions([symbol])
            for p in positions:
                side = "buy" if p.get("side") == "long" else "sell"
                self.exchange.create_order(symbol=symbol, type="market", side=side, amount=0,
                                            params={"stopLoss": {"triggerPrice": new_sl, "type": "market"}})
                return True
            return False
        except Exception as e:
            logger.warning(f"Не удалось обновить SL {symbol}: {e}")
            return False

    def _partial_close(self, symbol: str, side: str) -> bool:
        try:
            positions = self.exchange.fetch_positions([symbol])
            for p in positions:
                sz = float(p.get("contracts") or 0)
                if sz > 0:
                    qty = float(self.exchange.amount_to_precision(symbol, sz * PARTIAL_PCT / 100))
                    if qty <= 0:
                        return False
                    close_side = "sell" if side == "long" else "buy"
                    self.exchange.create_order(symbol=symbol, type="market", side=close_side,
                                                amount=qty, params={"reduceOnly": True})
                    return True
            return False
        except Exception as e:
            logger.warning(f"Не удалось сделать partial close {symbol}: {e}")
            return False
