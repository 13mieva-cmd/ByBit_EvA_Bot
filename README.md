# Super Trader Bot (v3) — заменяет предыдущего бота полностью

Главный файл называется `inflow_scanner_v2_render.py` — так же, как у бота-предшественника.

## Если видите ошибку "API key is invalid" (retCode 10003)

Бот падает на этапе подключения к бирже, ДО запуска стратегии. Обычно причина одна из:

1. У вас demo/testnet ключи Bybit, а бот бьёт в LIVE API. **В этой версии это уже
   исправлено** -- бот распознаёт вашу существующую переменную `BYBIT_BASE_URL`
   (например, `https://api-demo.bybit.com`) точно так же, как это делал бот-предшественник.
   Ничего переименовывать в Railway не нужно.
2. Опечатка/лишний пробел в `BYBIT_API_KEY` / `BYBIT_API_SECRET`.
3. На ключе включён IP-whitelist, не пропускающий IP хостинга.
4. У ключа не включены права Contract Trade / Unified Trading.

## Запуск
```
pip install -r requirements.txt
python inflow_scanner_v2_render.py
```

## Переменные окружения (основные)
- `BYBIT_API_KEY`, `BYBIT_API_SECRET` — ключи Bybit
- `BYBIT_BASE_URL` (или `BYBIT_DEMO_URL`) — домен API; задайте `https://api-demo.bybit.com`
  для demo-ключей, оставьте пустым для live
- `EXCHANGE_SANDBOX=true` — альтернативный способ включить ccxt testnet-режим
- `DRY_RUN=true|false` — без реальных ордеров / реальная торговля
- `SYMBOLS` — список пар через запятую
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — опциональные уведомления

## Что нового в v3
- Двухфазный вход FIRE -> PULLBACK CONFIRM (не покупаем хай, ждём откат к EMA50)
- Только закрытые бары (исправлен repainting-баг)
- HTF-кэш обновляется по TTL (раньше замерзал навсегда)
- Персистентные риск-контуры: MAX_POSITIONS, дневной лимит убытка, блок после серии убытков
- Верификация SL на бирже после входа
- ATR-трейлинг, безубыток, частичное закрытие, тайм-стоп, фейд по моментуму
- Корректный расчёт размера позиции через exchange.amount_to_precision()
- Поддержка demo/testnet ключей Bybit (BYBIT_BASE_URL/BYBIT_DEMO_URL/EXCHANGE_SANDBOX)

## Структура
- `inflow_scanner_v2_render.py` — главный цикл
- `config.py`, `storage.py`, `indicators.py`, `data_manager.py`, `analyzer.py`,
  `strategy.py`, `execution_manager.py`
