# BTC perpetuals with idle cash: paper backtest (2026-09-24)

Owner's ask: "Can we work on parlaying some of our latent cash that's sitting there into perpetuals for Bitcoin,
trading that with either no margin or some margin? Before we try on the Kalshi demo, let's try and copy it on paper
trading and see if it's even worth a dime."

## What Kalshi actually offers

Kalshi lists a CFTC-approved Bitcoin perpetual, `BTCPERP`, live since 2026-06-03
([CFTC](https://www.cftc.gov/PressRoom/PressReleases/9240-26), [Kalshi](https://news.kalshi.com/p/kalshi-launches-perpetual-futures-america)).
It also runs a perps **demo** environment with synthetic trading activity, available by request, through a separate
`/margin` API ([docs](https://docs.kalshi.com/margin)).

The rules this paper engine copies come from Kalshi's help-center pages on
[contract specs](https://help.kalshi.com/en/articles/15357587-btc-perpetual-futures-contract-specifications),
[funding](https://help.kalshi.com/en/articles/15357613-how-funding-works) and
[fees](https://help.kalshi.com/en/articles/16071417-perps-fees-explained). They were read through search snippets,
because this sandbox cannot load the pages; **verify each one before trusting a number**.

| Rule | Value used |
|---|---|
| Contract | 0.0001 BTC, fractional allowed; reference index BRTI (the same index the 15-minute bot uses) |
| Fees | charged on **notional**: taker 12 bps at the lowest 30-day-volume tier (down to 2.6 bps), maker 5 to 0.6 bps |
| Funding | every 8 h at 12 AM / 8 AM / 4 PM US Eastern; the premium TWAP over BRTI; clamped to +/-2%; zeroed below 0.01% |
| Leverage | up to about 5.7x on BTC; initial margin = notional / chosen leverage |
| Maintenance | about 90% of initial margin |

**Two consequences before any strategy:**
- **"No margin" is not "no liquidation".** At 1x the posted margin equals the notional. Maintenance at 90% of that
  means a roughly 10% adverse move liquidates the position. At 2x it's about 5%, at 3x about 3.3%.
- **Holding the perp is BTC price exposure, not yield on cash.** Idle cash in a 1x long perp earns exactly what BTC
  does, minus fees, minus funding when the perp trades above spot.

## What was built (paper only)

- `perp_paper.py`: an isolated-margin paper account following the rules above.
  - Taker fills, with slippage against a spot proxy.
  - Funding on the Eastern-time schedule. Daylight saving is hand-coded, so Windows needs no `tzdata`.
  - Maintenance-margin liquidation, checked against each bar's adverse extreme.
  - Loss per position capped at its posted margin.
  - `Decimal` money throughout.
- `perp_backtest.py` + `btcbot perp-backtest`: runs pre-registered strategies on 1-minute BTC bars from a local
  database, either `download-history`'s Coinbase candles or `download-polymarket-history`'s Binance 1 s klines.
  Details:
  - Orders fill at the next bar's open.
  - Results are split in time.
  - The held-out test is judged on daily returns, with a t-statistic against staying in cash and against
    buy-and-hold.
  - No verdict is given under 30 test days, and the wording never says "profitable".
- **No perps order code exists anywhere.** A test parses both modules and asserts they import no network or Kalshi
  client and define no order function. Trading on the demo would need a new `/margin` client. That is new write
  code, and it needs the owner's explicit go-ahead, the same gate Phase 6 went through.

### Strategies, fixed before seeing data (nothing to tune, so nothing to overfit)

| Name | What it does | Why it's here |
|---|---|---|
| `flat` | stays in cash | the "is it worth a dime" bar: covering fees and funding at all |
| `hold` | long once, sits | "just put idle cash into BTC via the perp" |
| `trend_24h` | hourly: long if BTC > 0.5% above its 24 h average, short if > 0.5% below, else keep | slow, so fees stay small |
| `window_15m` | the Kalshi bot's idea copied to the perp: 5 minutes into each 15-minute window, go with the window's direction, exit at its end | the literal "copy it" |

## Synthetic harness check (NOT market data)

The check ran 60 days of random-walk bars (about 2.3% daily volatility), which have no edge to find. $500, 12 bps
taker, 1 bp slippage:

- **`window_15m`:** -99% in both halves. About 3,500 round trips at roughly 24 bps each cost over $460 on $500.
  Per-15-minute trading cannot survive Kalshi's lowest fee tier unless its edge beats ~24 bps per trade.
- **`trend_24h`:** whipsawed, -38% in the train half and +19% in the test half. That is noise, and the harness
  correctly refused a verdict because the test half had only 19 days.
- **`hold`:** followed the walk.

## What could not be done here

No real BTC data was run. This session's network policy still refused the data hosts at the proxy (HTTP 403 on
CONNECT) after they were opened, so the policy probably takes effect only in a new session. To run it:

```
btcbot download-history --start 2026-03-01T00:00:00Z --end 2026-09-20T00:00:00Z       # Coinbase 1m candles (+ Kalshi outcomes)
btcbot perp-backtest --db data/history-KXBTC15M-prod-....sqlite                       # 1x and 2x, funding 0 and 0.01%/8h
btcbot perp-backtest --db ... --leverage 1 --funding-8h 0.0003 --fee-bps 2.6          # sensitivity: costly funding, top fee tier
```

## Next steps, each gated on the one before

1. A real backtest over at least 6 months, read verdict-first.
2. If anything survives: forward paper trading on live public data (no orders), like `btcbot paper`.
3. Only then, and only with the owner's go-ahead: a demo-gated `/margin` client for Kalshi's perps demo.

Prior art worth knowing: at least one public attempt at a Kalshi perps desk reported "NO-GO for alpha" after a
strategy tournament ([vinilpolepalli/quantfirm#74](https://github.com/vinilpolepalli/quantfirm/pull/74)).
