Bot entry point (per Procfile): scanner.py — this is the file that actually runs.

Live files: config.py, scanner.py, trader.py, auto_trade.py, storage.py, indicators.py, visuals.py

Signal types: STANDARD, SURGE, PULLBACK, BB_SQUEEZE
  BB_SQUEEZE: Bollinger Bands squeeze -> breakout above upper band -> small pullback -> entry.

Mode: 4-signal auto-trading (STANDARD/SURGE/PULLBACK/BB_SQUEEZE — each toggleable
independently via /sig_on /sig_off), long-only trend filter, trailing stop enabled
after TP1 (per-signal-type trigger/distance, including BB_SQUEEZE).

NOTE: inflow_scanner_v4_full.py (if still present in the repo) is a stale,
un-patched duplicate of an earlier version of scanner.py — it is not referenced
by the Procfile and is not imported by anything. Safe to delete; kept it out of
this delivery to avoid two diverging copies of the same bot.
