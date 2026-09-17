"""
inflow_scanner_v2_render.py -- Super Trader Bot (v3), главный модуль.
Имя файла сохранено таким же, как у бота-предшественника, чтобы не менять
команду запуска в деплое (Railway/Render/Procfile/systemd и т.п.).
"""
import time
import logging
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from config import (
    SYMBOLS, LOOP_INTERVAL, RECONCILE_INTERVAL, DRY_RUN, LOG_LEVEL,
    ENTRY_MODE, REQUIRE_HTF, STATE_FILE, SIGNALS_CSV,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SCORE_THRESHOLD,
)
from data_manager import DataManager
from analyzer import Analyzer
from strategy import Strategy
from execution_manager import ExecutionManager
from storage import State, append_csv

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("bot.log", encoding="utf-8")],
)
logger = logging.getLogger("SuperTraderBot")


def notify(text: str):
    logger.info(f"[notify] {text}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)
    except Exception as e:
        logger.warning(f"Telegram notify failed: {e}")


def log_signal_to_csv(signal):
    append_csv(SIGNALS_CSV, {
        "datetime": datetime.now().isoformat(timespec="seconds"),
        "symbol": signal.symbol, "side": signal.side, "score": signal.score,
        "entry": signal.entry_price, "sl": signal.stop_loss, "tp": signal.take_profit,
        "reason": signal.reason, "signal_type": signal.signal_type,
    })


def run_bot():
    logger.info("=" * 70)
    logger.info("Запуск SUPER TRADER BOT (v3) -- полная замена предыдущей версии")
    logger.info(f"Символы: {SYMBOLS}")
    logger.info(f"Порог голосования: {SCORE_THRESHOLD} | Режим входа: {ENTRY_MODE.upper()} | HTF-фильтр: {REQUIRE_HTF}")
    logger.info(f"Режим: {'DRY_RUN (без реальных ордеров)' if DRY_RUN else 'РЕАЛЬНАЯ ТОРГОВЛЯ'}")
    logger.info("=" * 70)

    try:
        state = State(STATE_FILE)
        data_manager = DataManager()
        analyzer = Analyzer(order=5)
        strategy = Strategy()
        execution = ExecutionManager(state)
    except Exception as e:
        logger.critical(f"Критическая ошибка инициализации: {e}")
        sys.exit(1)

    for symbol in SYMBOLS:
        try:
            data_manager.get_historical_data(symbol)
            data_manager.get_higher_tf_data(symbol)
            logger.info(f"Данные {symbol} загружены")
        except Exception as e:
            logger.error(f"Ошибка загрузки {symbol}: {e}")

    notify(
        f"🚀 Super Trader Bot запущен\n"
        f"Режим: {'DRY_RUN' if DRY_RUN else 'LIVE'} | Вход: {ENTRY_MODE}\n"
        f"Символы: {', '.join(SYMBOLS)}"
    )

    logger.info("Бот переходит в рабочий цикл...")
    last_reconcile = 0.0

    while True:
        cycle_start = time.time()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        state.reset_day(today)
        logger.info(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Новый цикл анализа")

        for symbol in SYMBOLS:
            try:
                df = data_manager.update_latest_candle(symbol)
                higher_df = data_manager.get_higher_tf_data(symbol) if REQUIRE_HTF else None
                analysis = analyzer.analyze(df, higher_df)

                if symbol in state.armed:
                    armed = state.armed[symbol]
                    res = strategy.check_pullback(symbol, df, armed)
                    if res is None:
                        continue
                    if isinstance(res, dict) and (res.get("expired") or res.get("invalid")):
                        why = "истёк срок ожидания" if res.get("expired") else "структура сломана (неудачный ретест)"
                        logger.info(f"{symbol}: сетап снят с ожидания -- {why}")
                        state.disarm(symbol)
                        continue
                    state.disarm(symbol)
                    signal = res
                    log_signal_to_csv(signal)
                    result = execution.execute_signal(signal)
                    notify(
                        f"{'✅' if result['success'] else '❌'} {signal.side.upper()} {symbol}\n"
                        f"Entry={signal.entry_price} SL={signal.stop_loss} TP={signal.take_profit}\n"
                        f"{result['message']}"
                    )
                    continue

                if symbol in state.positions:
                    continue
                if state.is_cool(symbol):
                    continue

                if ENTRY_MODE == "breakout":
                    armed = strategy.detect_fire(symbol, df, analysis)
                    if armed:
                        signal = strategy.build_breakout_signal(symbol, df, armed)
                        if signal:
                            log_signal_to_csv(signal)
                            result = execution.execute_signal(signal)
                            notify(f"{'✅' if result['success'] else '❌'} {signal.side.upper()} {symbol} (breakout) | {result['message']}")
                else:
                    armed = strategy.detect_fire(symbol, df, analysis)
                    if armed:
                        state.arm(symbol, **armed)
                        notify(
                            f"🔭 Watching {armed['side'].upper()} {symbol}\n"
                            f"Score={armed['score']:.2f} | {armed['reason']}\n"
                            f"Ждём откат к EMA перед входом (не покупаем хай)."
                        )

            except Exception as e:
                logger.error(f"Ошибка при обработке {symbol}: {e}", exc_info=True)
                continue

        if time.time() - last_reconcile >= RECONCILE_INTERVAL:
            try:
                execution.reconcile(data_manager)
            except Exception as e:
                logger.error(f"Ошибка reconcile: {e}", exc_info=True)
            last_reconcile = time.time()

        elapsed = time.time() - cycle_start
        sleep_time = max(1.0, LOOP_INTERVAL - elapsed)
        logger.info(
            f"Цикл завершён за {elapsed:.1f} сек | Позиций: {len(state.positions)} | "
            f"Ожидают отката: {len(state.armed)} | Ожидание {sleep_time:.1f} сек..."
        )
        time.sleep(sleep_time)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        logger.info("\nБот остановлен пользователем (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
