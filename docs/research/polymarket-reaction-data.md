# Polymarket 15m reaction data: source evaluation, pipeline, and ML layer (2026-09-24)

Owner's ask: "Find the best available data to train the ML layer on. Train it on the way the polymarket 15m
market reacts to certain events and the btc price reactions. You have full auth to download any data sets
you want to explore for their validity."

## Bottom line

- **Best data for this question is sub-second, and it already exists publicly**: two Hugging Face datasets
  pair Polymarket's BTC Up/Down order books with Binance BTC/USDT at millisecond or 100 ms resolution (table
  below). Polymarket's own history endpoints are much weaker than they look: `prices-history` is empty below
  12 hours for resolved markets, the Goldsky fill subgraph went incomplete at the 2026-04-28 exchange
  migration, and the Data API trade tape is 1-second, prints-only, and capped at an offset of 10,000.
- **Best data we can collect ourselves, for any date range and refreshable**: the Data API trade tape plus
  Binance's checksum-verified 1 s kline archive. This session built that downloader, a validity report,
  lead-lag and event-study analysis, and two ML models with honest baselines (`btcbot
  download-polymarket-history`, `btcbot pm-reaction`).
- **No real data was downloaded or trained on in this session.** This container's network policy blocks every
  market-data host (Polymarket, Binance, Hugging Face, Kalshi, Coinbase, arXiv); only GitHub and PyPI are
  reachable. Everything is tested offline against mock transports and synthetic data with a planted lag.
  The model numbers below are harness checks on synthetic data, not results.
- **Prior evidence sets expectations**: OpenMarket (CU Boulder, arXiv 2607.26245) ran a walk-forward logistic
  regression over 43 microstructure features on exactly these markets. It *slightly underperformed* the
  Polymarket mid out-of-sample, and its simulated trading netted -0.116 per attempted trade after fees and
  slippage. Polymarket quotes followed large Binance moves with a median lag of 347 ms. Polymarket also
  added taker fees (about `0.07 * p * (1 - p)` per share, ~1.75 cents at 50 cents) to these markets in
  January 2026, specifically to kill latency arbitrage. Anything our models find has to clear that bar.

## Data sources, ranked

| # | Source | What it has | Resolution / range | Validity notes | Verdict |
|---|---|---|---|---|---|
| 1 | [whodisidk/polymarket-btc-updown-exchange-data](https://huggingface.co/datasets/whodisidk/polymarket-btc-updown-exchange-data) (HF) | Polymarket BTC Up/Down books (5m/15m/1h) + Hyperliquid books, Binance BTCUSDT trades and reference price, captured trades, lifecycle, resolutions | 100 ms grid, 2026-05-25 to 2026-08-29 (post exchange migration) | Ships `COVERAGE.jsonl` window-level quality flags. Snapshots can repeat a stale state, some windows are missing, and there is no Binance order book | **Best for the reaction question on today's market structure.** Needs an importer once its `SCHEMA.md` can be read |
| 2 | [OpenMarket](https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket) (HF, [paper](https://arxiv.org/abs/2607.26245), [code](https://github.com/gregyoung14/openmarket)) | Polymarket BTC 15m binary order-book events + Binance BTC/USDT, 2.9M explicit lead-lag pairs, 727M rows / 8.69 GiB | Millisecond, 54 Polymarket days 2026-02-12 to 2026-05-15 | Collector clocks drift (bounded to +/-99 ms per the paper); WebSocket reconnect gaps. Spans the 2026-04-28 V2 migration, so split pre/post | **Best documented, with a published negative baseline to reproduce.** Needs an importer |
| 3 | Polymarket Data API trade tape + Binance 1 s archive (**built here**) | Every taker print (time, token, side, price, size) per window + resolution; Binance 1 s OHLCV with taker-buy volume | 1 s; any range since the series launched | Prints, not quotes: stale and bouncing, which the analysis controls for (below). Offset cap of 10,000 truncates the oldest prints of a very busy window (flagged per window). Binance is not the settlement source (measured, below) | **Best self-collected source.** Unlimited range, refreshable, but 1 s cannot resolve a ~350 ms reaction |
| 4 | Our own `btcbot record-polymarket` recordings | Full two-sided books every 2 s + settlements | 2 s, forward-only | True quotes, so no print staleness. `pm-reaction --recorder-db` reads them | Good going forward. A CLOB market WebSocket recorder would make it sub-second |
| 5 | [aliplayer1/polymarket-crypto-updown](https://huggingface.co/datasets/aliplayer1/polymarket-crypto-updown) (HF) | Markets, live WebSocket ticks, BBO, spot prices; 5m/15m/1h/4h; updated every 3 h | Live-captured ticks | Its `prices` config comes from `prices-history`, which is empty below 12 h for resolved markets (see below); the `ticks`/`orderbook` configs are the useful part | Worth a look once reachable |
| -- | [BrockMisner/polymarket-crypto-5m-15m](https://huggingface.co/datasets/BrockMisner/polymarket-crypto-5m-15m) | Data API trades, resolutions, 1-minute CLOB prices, Binance **1-minute** candles | Last updated 2026-03-24 | Too coarse on the BTC side, and stale | Superseded by #3 |
| -- | [kachoio/polymarket-5-minute-crypto-up-down-markets](https://huggingface.co/datasets/kachoio/polymarket-5-minute-crypto-up-down-markets) | Per-second top of book, ~89k markets | 1 s, 2026-03-24 to 2026-05-18 | **5-minute markets only** | Cross-check only |
| -- | Kaggle hugobde "Polymarket Bitcoin 15min Up or Down" | Up price at 1-minute fidelity | Oct-Dec 2025 | Needs a Kaggle account | Too coarse |
| x | CLOB `/prices-history` | Up-token price series | -- | **Returns nothing below 12 h granularity once a market resolves** ([py-clob-client#216](https://github.com/Polymarket/py-clob-client/issues/216)) | Useless for closed 15m windows |
| x | Goldsky order-fill subgraph | On-chain fills | -- | **Incomplete since the 2026-04-28 CTF Exchange V2 migration** ([poly_data](https://github.com/warproxxx/poly_data) moved to Envio HyperSync) | Do not use for post-April data |
| x | `api.binance.com` | Klines | -- | Refuses US IP addresses (HTTP 451) | Use `data.binance.vision` / `data-api.binance.vision` (done) |
| x | Chainlink BTC/USD Data Streams (the actual resolution source) | -- | -- | No free second-level history | Binance stands in; the basis is measured, not assumed |

## What was built

All read-only and public: no key, no wallet. `PolymarketClient` still has no order method (its exact-method
guard test was updated to add the one new read, `get_market_trades`). Nothing is wired into `strategy.py`,
`btcbot lab`, `paper`/`demo`, or any Kalshi decision.

- `polymarket_client.py`: `PmTrade` + `get_market_trades()` (Data API, `takerOnly=true`, newest-first pages
  re-sorted oldest-first, `truncated` flag at the offset cap). Wallet/name/profile fields are parsed away and
  never stored.
- `binance_history.py`: daily 1 s archive with **SHA-256 checksum verification**, per-value ms/us timestamp
  detection (Binance switched spot archives to microseconds on 2025-01-01), REST fallback for days not yet
  archived.
- `pm_history.py` + `btcbot download-polymarket-history`: a resumable backfill into one `pm_`/`btc_`-prefixed
  SQLite file. Windows come from the slug epoch, which is the window **start**: `btc-updown-15m-1765548000` is
  "December 12, 9:00AM-9:15AM ET". Gamma's `endDate` must equal start + 900, or the window is recorded as
  `window_mismatch` and left out.
- `pm_reaction.py` + `btcbot pm-reaction`: the validity report, lead-lag, event study and both models, plus a
  JSON report. Models are saved to `models/polymarket/` (gitignored) in a schema `ml-ablation` /
  `validate --model` cannot load.

## Method, and the traps it avoids

**Timing, no lookahead.** "At instant t" means what traded in the second ending at t: Binance's bar opening
at t-1 and Polymarket prints stamped t-1, forward-filled. A real Binance bar must exist within 5 s of a window's
start and end, and more than 60 forward-filled seconds drops the window. A test caught the first version
forward-filling a stale pre-window price across a window with no Binance data at all.

**Validity report.**
- Coverage, truncation and thin windows.
- Binance gaps.
- **Binance-vs-resolution agreement**: how often Binance's open-to-close direction matches Polymarket's
  Chainlink-based resolution. Below 90% is a warning, and it also reports the median move size where they
  disagree, which should be tiny.
- Same-second Up vs Down-implied price consistency.
- **Lead-lag sanity**: a peak where Polymarket *leads* Binance is flagged as a clock problem, not a finding.

**Event study.** Two event types:
- **BTC shocks**: a move of 3 sigma or more within 5 s, with sigma taken from *before* the move.
- **Strike crossings**: BTC crossing the window's opening price.

For each event, the direction-signed Polymarket Up-price change at lags 0-120 s is shown next to the change a
driftless-lognormal fair value says it should have made. It reports the half-life and the share priced in at 60 s.

**Outcome model**: P(Up | now), on `logit_pm`, `logit_fair`, tau, sigma, 30 s BTC return, 30 s Polymarket
move and flow. The baseline is **Polymarket's own price**.

**Reaction model**: P(Polymarket's Up price is higher in 15 s | it moved at least 1 cent). The baseline is a
**control** model:
- Polymarket's own history.
- The side of its last print.
- BTC's move *since* that print.

That control exists because of a false positive found while building this. With **zero** planted lag, a
Polymarket-only baseline still lost to "BTC information" at t < -6. Two artifacts cause that: bid-ask bounce,
and within-second timing at 1 s resolution. The control removes both, so "YES" now means BTC information from
*before* the last print still predicts the next one, which is a genuinely slow reaction. There is a
regression test for this.

**Honest comparisons.**
- Time-ordered split by whole window, with a 1-window embargo.
- "Beats" requires a **window-grouped paired t <= -2**. Rows inside a window share one outcome, so the window
  is the independent unit.
- Rows whose last Polymarket print is more than 5 s old are dropped, because a stale print "reacts" to
  everything that happened since.

## Synthetic harness check (NOT market data)

`tests/pm_synthetic.py` simulates a random-walk BTC and a Polymarket that prices it with a planted lag. 120
windows, default settings:

| planted lag | lead-lag peak | reaction: full vs control Brier | paired t | "slow reaction" |
|---|---|---|---|---|
| 0 s (3 seeds) | +1 s | 0.2157 vs 0.2172 (seed 7) | -1.44, -0.45, -0.05 | no |
| 2 s | +3 s | 0.1945 vs 0.2161 | -7.05 | YES |
| 3 s | +4 s | 0.1805 vs 0.2142 | -8.58 | YES |
| 5 s | +6 s | 0.1576 vs 0.2124 | -12.27 | YES |

The outcome model never beat the market on these (paired t between +0.7 and +2.4), which is correct: the
simulated market is already fair. The +1 s on the lead-lag peak comes from 1 s bucketing. That bucketing is
also why this dataset cannot measure a sub-second reaction, and why sources #1 and #2 matter.

## What could not be done here, and next steps

1. **Allow the data hosts, then run it.** In the environment's network settings, allow
   `gamma-api.polymarket.com`, `data-api.polymarket.com`, `data.binance.vision`, `data-api.binance.vision` (and
   `huggingface.co` for sources #1/#2). Then:
   ```
   btcbot download-polymarket-history --since 2026-09-20T00:00:00Z --limit-markets 20   # smoke test
   btcbot download-polymarket-history --since 2026-05-01T00:00:00Z                        # resumable
   btcbot pm-reaction --db data/pm-history-15m-....sqlite
   ```
   Read the validity section first. If Binance agreement with the resolution is below ~90%, or the lead-lag
   peak is negative, stop and fix the data before reading any model line.
2. **Importers for #1 and #2** once their schemas can be read (`SCHEMA.md` / the OpenMarket repo), feeding
   `pm_reaction.build_series` a quote-mid series instead of prints. That is the only way to measure the
   ~350 ms reaction itself. Reproduce OpenMarket's negative result first as a calibration of our own harness.
3. **Sub-second forward capture**: a CLOB market WebSocket recorder alongside `record-polymarket`.
4. **Cross-venue (needs the owner's go-ahead)**: use Polymarket's 15m price as a feature for the Kalshi bot.
   The windows align, but Kalshi settles on a 60 s BRTI average and Polymarket on one Chainlink print, so the
   strikes differ. CLAUDE.md currently keeps Polymarket separate from everything Kalshi, deliberately.

No profitability claim is made anywhere. A reaction model saying YES predicts a one-to-two-cent move in a
market charging about 1.75 cents in taker fees at 50 cents, before the spread.
