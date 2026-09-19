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
  (same source the spec already names for the Phase 2 spot feed) and writes a CSV.
- `trend_backtest.py` -- given that CSV, checks 15/30/1h/24h trend agreement at a configurable
  entry point in each 15-minute window, and compares two exits on the resulting trade:
  - **hold**: hold to settlement no matter what (the spec's own default exit)
  - **flip**: cut the losing side and take the other one if the model-implied probability of your
    side drops to a threshold and there's still enough time left, priced with the same v1
    fair-probability model from the spec (not a real observed Kalshi price -- there is no recorded
    Kalshi order-book history to backtest against yet, so this is a modeled approximation of the
    contract price path, not a measured one)
- `test_trend_backtest.py` -- offline unit tests (synthetic price series, no network).

## Usage

```
python btc-research/fetch_trend_data.py --hours 48 --out btc-research/data/btc_1m.csv
python btc-research/trend_backtest.py --csv btc-research/data/btc_1m.csv
pytest btc-research/test_trend_backtest.py
```

## Reading the output

The script prints an in-sample ("train") and held-out ("test") split so a strategy that only
looks good on the data it was tuned against doesn't get reported as if it worked. Per CLAUDE.md,
none of this is a profitability claim -- it needs Kalshi's own recorded order-book history and
fees (Phase 2 and Phase 4 of the main bot) before any number here means real money. What it can
tell you now: whether multi-timeframe trend agreement predicts the settlement direction better
than a coin flip on real BTC data, and whether flipping sides on an adverse move would have beaten
holding, on the entry price and thresholds you pass in.
