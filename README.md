# Super Trader Bot (v3) — заменяет предыдущего бота полностью

Главный файл называется `inflow_scanner_v2_render.py`.

## Если видите ошибку от Bybit при старте

**retCode 10003 "API key is invalid"** — домен не совпадает с типом ключа
(demo/live/testnet). Задайте `BYBIT_BASE_URL=https://api-demo.bybit.com` для
demo-ключей (переменную можно не переименовывать — читается как раньше).

**retCode 10032 "Demo trading are not supported"** — Bybit Demo Trading не
поддерживает часть эндпоинтов (spot/inverse/option), которые ccxt пытается
подгрузить по умолчанию. В этой версии используется официальный ccxt-метод
`exchange.enable_demo_trading(True)`, который решает это сам. Если ошибка
всё же повторяется — значит версия пакета `ccxt` в requirements слишком
старая и не содержит этот метод; обновите ccxt (`pip install -U ccxt`,
нужна версия 4.3.x или новее) либо переключитесь на настоящий testnet:
`EXCHANGE_SANDBOX=true` (и используйте testnet-ключи вместо demo-ключей).

## Запуск
```
pip install -r requirements.txt
python inflow_scanner_v2_render.py
```

## Переменные окружения (основные)
- `BYBIT_API_KEY`, `BYBIT_API_SECRET`
- `BYBIT_BASE_URL` (или `BYBIT_DEMO_URL`) = `https://api-demo.bybit.com` для demo-ключей
- `EXCHANGE_SANDBOX=true` — testnet вместо demo trading
- `DRY_RUN=true|false`
- `SYMBOLS` — список пар через запятую
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — опционально

## Что нового в v3
- Двухфазный вход FIRE -> PULLBACK CONFIRM (не покупаем хай, ждём откат к EMA50)
- Только закрытые бары (исправлен repainting-баг)
- HTF-кэш обновляется по TTL
- Персистентные риск-контуры: MAX_POSITIONS, дневной лимит убытка, блок после серии убытков
- Верификация SL на бирже после входа
- ATR-трейлинг, безубыток, частичное закрытие, тайм-стоп, фейд по моментуму
- Корректный расчёт размера позиции через exchange.amount_to_precision()
- Правильная поддержка Bybit Demo Trading через exchange.enable_demo_trading(True)
