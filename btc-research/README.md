# btc-research

Exploratory research, separate from the phase-gated bot in `src/btcbot`. Nothing here places
orders or is wired into the CLI; it exists to test whether the "watch the 15m/30m/1h/24h trend
and follow the leader" approach has real signal, and to compare exit rules on a losing trade.

## What other bots in this space actually do (from public writeups, not this repo's own results)

- Nearly all public retail strategies for short-window crypto binaries are one of: (a) a
  volatility-clock fair-value model like the one in `docs/btc15m-bot-spec.md` section 4
  (price vs. strike, scaled by realized vol and time left, mapped through a normal CDF), or
  (b) pure momentum/order-flow following. Momentum-only strategies are the ones people report
  losing money on after fees once they stop cherry-picking; the vol-adjusted fair-value model is
  the one with a real edge story (it prices in *how much* the underlying needs to move given the
  time left, not just *which way* it's currently moving).
- "Follow the trend across multiple timeframes" is a real, well-known heuristic (multi-timeframe
  momentum alignment), but on a 15-minute binary it is easy to be right about direction and still
  lose, because price can be trending correctly and still not move enough by close, or it reverses
  in the last 2-3 minutes when the settlement window (the 60s BRTI average) locks in. That's the
  scenario in the prompt: 9 minutes left, "no" trending, contracts at 60 cents held to 80-99 --
  survivorship bias in what gets remembered, since the losing tail of that same bet is a hold to
  zero.
- The fix people who don't blow up eventually converge on is exactly what phase 4 of this repo's
  spec already calls for: size for the tail case, and make exit decisions off a real edge estimate
  (fair probability vs. price), not off "it's been trending this way."

## Files

- `fetch_trend_data.py` -- pulls 1-minute BTC-USD candles from Coinbase's public REST API
  (same source the spec already names for the Phase 2 spot feed) and writes a CSV. Used for the
  trend signal and the modeled volatility, not for settlement truth.
- `fetch_kalshi_settlements.py` -- pulls **real** KXBTC15M settlement history straight from
  Kalshi's public API: `floor_strike`, `expiration_value` and `result` for every settled window.
  No credentials needed (same as `btcbot discover`); it reuses `btcbot.kalshi_client.KalshiClient`
  so pagination, retries and Decimal-safe parsing come for free. This is the "live Kalshi data"
  piece: it replaces the Coinbase-close approximation of who won each window with Kalshi's own
  recorded outcome.
- `record_live_orderbook.py` -- polls the currently *open* market's orderbook every few seconds
  and appends top-of-book prices to a CSV, going forward only. Kalshi has no historical
  order-book endpoint, so a real (not modeled) in-window contract-price path can only be captured
  live; run this continuously for a while to build one up. Read-only, no credentials, places no
  orders -- a much smaller, research-only cousin of the Phase 2 recorder in the main spec.
- `trend_backtest.py` -- given a Coinbase CSV (and optionally a Kalshi settlements CSV), checks
  15/30/1h/24h trend agreement at a configurable entry point in each 15-minute window, and
  compares two exits on the resulting trade:
  - **hold**: hold to settlement no matter what (the spec's own default exit)
  - **flip**: cut the losing side and take the other one if the model-implied probability of your
    side drops to a threshold and there's still enough time left, priced with the same v1
    fair-probability model from the spec (modeled, not an observed Kalshi price, until
    `record_live_orderbook.py` has accumulated enough real in-window price history to backtest
    against instead)
- `test_trend_backtest.py`, `test_fetch_kalshi_settlements.py` -- offline unit tests (synthetic
  price series / `httpx.MockTransport` against a real settled-market payload shape, no network).

## Usage

```
# Trend signal + modeled exit pricing, from Coinbase
python btc-research/fetch_trend_data.py --hours 48 --out btc-research/data/btc_1m.csv

# Real Kalshi settlement ground truth (who actually won each window)
python btc-research/fetch_kalshi_settlements.py --out btc-research/data/kalshi_settlements.csv

# Backtest against real settlements where available, Coinbase-derived elsewhere
python btc-research/trend_backtest.py \
    --csv btc-research/data/btc_1m.csv \
    --kalshi-csv btc-research/data/kalshi_settlements.csv

# Start building a real in-window price history for future exit backtests (run for a while)
python btc-research/record_live_orderbook.py --out btc-research/data/orderbook_live.csv

pytest btc-research/
```

## Reading the output

The script prints an in-sample ("train") and held-out ("test") split so a strategy that only
looks good on the data it was tuned against doesn't get reported as if it worked. Per CLAUDE.md,
none of this is a profitability claim -- it needs Kalshi's own recorded order-book history and
fees (Phase 2 and Phase 4 of the main bot) before any number here means real money. What it can
tell you now: whether multi-timeframe trend agreement predicts the settlement direction better
than a coin flip on Kalshi's own recorded outcomes, and whether flipping sides on an adverse move
would have beaten holding, on the entry price and thresholds you pass in.
