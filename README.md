# Super Trader Bot (v3) — заменяет предыдущего бота полностью

Главный файл называется `inflow_scanner_v2_render.py` — так же, как у бота-предшественника,
чтобы деплой (Render/Procfile/systemd) не пришлось перенастраивать.

## Запуск
```bash
pip install -r requirements.txt
export BYBIT_API_KEY="..."
export BYBIT_API_SECRET="..."
python inflow_scanner_v2_render.py
```

## Что нового
- Двухфазный вход FIRE -> PULLBACK CONFIRM (не покупаем хай, ждём откат к EMA50)
- Только закрытые бары (исправлен repainting-баг)
- HTF-кэш обновляется по TTL (раньше замерзал навсегда)
- Персистентные риск-контуры: MAX_POSITIONS, дневной лимит убытка, блок после серии убытков
- Верификация SL на бирже после входа
- ATR-трейлинг, безубыток, частичное закрытие, тайм-стоп, фейд по моментуму
- Корректный расчёт размера позиции через exchange.amount_to_precision()

## Структура
- `inflow_scanner_v2_render.py` — главный цикл
- `config.py`, `storage.py`, `indicators.py`, `data_manager.py`, `analyzer.py`,
  `strategy.py`, `execution_manager.py`
