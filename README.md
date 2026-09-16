# Super Trader Bot (v3) — заменяет предыдущего бота полностью

Главный файл называется `inflow_scanner_v2_render.py` — так же, как у бота-предшественника,
чтобы деплой (Render/Railway/Procfile/systemd) не пришлось перенастраивать.

## Запуск
```
pip install -r requirements.txt
export BYBIT_API_KEY="..."
export BYBIT_API_SECRET="..."
python inflow_scanner_v2_render.py
```

## Если видите ошибку "API key is invalid" (retCode 10003)
Это означает, что Bybit отклонил ключ/секрет ещё на этапе подключения (до запуска
стратегии). Проверьте по порядку:

1. В переменных окружения (Railway/Render → Variables) нет случайных пробелов,
   переносов строк или кавычек вокруг `BYBIT_API_KEY` / `BYBIT_API_SECRET`.
2. Ключ создан для того же типа аккаунта, к которому стучится бот:
   - Если у вас ключи **demo-аккаунта** Bybit (Demo Trading, домен `api-demo.bybit.com`,
     как было в самой первой версии бота) — задайте `BYBIT_DEMO_URL=https://api-demo.bybit.com`.
   - Если у вас ключи **testnet** — задайте `EXCHANGE_SANDBOX=true`.
   - Если у вас обычные **live**-ключи — ничего из этого включать не нужно (по умолчанию
     оба параметра выключены, бот бьёт в боевой Bybit API).
3. На ключе не включён IP-whitelist, не пропускающий IP хостинга (либо добавьте
   IP в whitelist, либо отключите его).
4. У ключа включены права **Contract Trade / Unified Trading**, а не только Read-only.

## Что нового в v3
- Двухфазный вход FIRE -> PULLBACK CONFIRM (не покупаем хай, ждём откат к EMA50)
- Только закрытые бары (исправлен repainting-баг)
- HTF-кэш обновляется по TTL (раньше замерзал навсегда)
- Персистентные риск-контуры: MAX_POSITIONS, дневной лимит убытка, блок после серии убытков
- Верификация SL на бирже после входа
- ATR-трейлинг, безубыток, частичное закрытие, тайм-стоп, фейд по моментуму
- Корректный расчёт размера позиции через exchange.amount_to_precision()
- Поддержка demo/testnet ключей Bybit (EXCHANGE_SANDBOX / BYBIT_DEMO_URL)

## Структура
- `inflow_scanner_v2_render.py` — главный цикл
- `config.py`, `storage.py`, `indicators.py`, `data_manager.py`, `analyzer.py`,
  `strategy.py`, `execution_manager.py`
