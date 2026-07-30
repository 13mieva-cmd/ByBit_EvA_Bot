Bot entry point (per Procfile): scanner.py — this is the file that actually runs.

Live files: config.py, scanner.py, trader.py, auto_trade.py, storage.py, indicators.py, visuals.py
Offline tool: backtest.py (python backtest.py --symbol XUSDT --days 30 | --top 15 --days 14)

Signal types: STANDARD, SURGE, PULLBACK, BB_SQUEEZE
  BB_SQUEEZE (15m): squeeze (relative percentile OR absolute bandwidth cap, fresh within
  N bars) -> breakout above upper band + volume confirmation -> anti-parabolic guard ->
  small pullback that holds above mid-band -> entry. Confirmed by OI 24h AND OI 4h.

Mode: 4-signal auto-trading (STANDARD/SURGE/PULLBACK/BB_SQUEEZE — each toggleable
independently via /sig_on /sig_off), long-only trend filter, trailing stop enabled
after TP1 (per-signal-type trigger/distance, including BB_SQUEEZE).

Fixes applied in this pass (see audit for details):
  - scanner.py: BB_SQUEEZE squeeze-detection now genuinely uses percentile-OR-absolute-cap
    (previously the percentile branch was mathematically redundant with the cap check and
    had zero effect on any outcome — now fixed and verified to change real outcomes).
  - scanner.py: removed a dead no-op line in the star-rating bonus block.
  - backtest.py: fixed a look-ahead bias where the EMA50(1h) filter could use a 1h candle's
    final close before that candle had actually finished forming (up to ~59 min of future
    information leaking into the backtest).
  - backtest.py: fixed warm-up window sizing — it only accounted for the 15m BB lookback,
    leaving the first ~20 hours of every backtest window with zero possible signals
    because the 1h EMA50 filter didn't have enough history yet, regardless of market
    conditions.

Known caveat (not fixed, needs a decision): backtest.py simulates a static TP/SL bracket
only. The live bot's trailing stop (activates at AUTO_TP1_TRIGGER_PCT_BB, trails at
AUTO_TRAIL_DISTANCE_PCT_BB, removes the fixed TP) is NOT modeled in the backtest, and
backtest.py imposes a 12h (MAX_HOLD_BARS=48) timeout that doesn't exist in live auto-trading.
Backtest results are therefore a lower bound / conservative estimate of what the live bot's
trailing-stop exits could actually capture on trades that keep running past +1.0%.

NOTE: inflow_scanner_v4_full.py (if still present in the repo) is a stale, un-patched
duplicate of an earlier version of scanner.py — it is not referenced by the Procfile and
is not imported by anything. Safe to delete; kept out of this delivery to avoid two
diverging copies of the same bot.
