# Kalshi historical trade tape + candlestick backfill: handoff (Part A)

Owner handoff, 2026-09-23. Part A only -- Part B (below) is optional and owner-gated; stop after Part A and
wait for the owner's go-ahead before touching it.

## Rules
- Public, unauthenticated GETs only. No key, no `KalshiAuth`, never touch `create_order`/`cancel_order`/the
  demo gate.
- No placeholder modules, no profitability claims.
- Build against `httpx.MockTransport`/fakes for tests. (This session found the "container can't reach
  Kalshi" claim elsewhere in the repo to be false for at least this session -- `external-api.kalshi.com` and
  `api.elections.kalshi.com` were reachable directly and repeatedly during this task. That doesn't change the
  test strategy: tests stay offline regardless.)
- Parse strictly: `ParseError` on a missing/renamed field, never a silent zero/None.

## A1. Client methods (`kalshi_client.py`)
`get_historical_cutoff`, `list_historical_markets`, `get_historical_trades`, `get_market_candlesticks`
(live), `get_historical_candlesticks`. New `HistoricalCutoff`/`MarketCandle` dataclasses in `models.py`,
strict `from_api`. No auth headers ever. `Trade.from_api`: prefer `taker_outcome_side` over the deprecated
`taker_side`; error if both are present and disagree; error if both are missing.

## A2. Routing helper (`history_pipeline.py`)
`fetch_market_trades`/`fetch_market_candles(client, market, cutoff)` route live vs `/historical/*` by
whether the window is fully before/after/straddling the cutoff; dedupe and log straddles.
`fetch_all_settled_markets` unions live + historical listings (historical wins on conflict) -- flagged as a
probable existing gap, confirmed live: the historical listing alone returns far more settled markets than
the live listing can still see.

## A3. Storage
Reuse the existing `trade_tape` schema/aggregation exactly as `recorder.py` writes it; factor the
aggregation out into a shared module (`btcbot/trade_tape.py`) so `Recorder` and the backfill both use it
instead of copying SQL. The backfill must be idempotent -- dedupe by `trade_id` first, then delete-then-
insert per ticker in one transaction (not the live recorder's additive `ON CONFLICT` upsert, which isn't
idempotent on re-runs). New `market_candles` table and a `backfill_progress(ticker PRIMARY KEY, trades_done,
candles_done, trade_count, fetched_at)` table for resumability. `load_market_candles`/`load_trade_tape`
readers.

## A4. CLI
New `btcbot download-market-history` subcommand (not overloading `download-history`) with flags `--env`,
`--series`, `--db`, `--since`, `--until`, `--limit-markets`, `--no-trades`, `--no-candles`, `--concurrency`,
`--sleep-ms`. `--db` defaults to the newest `data/history-*.sqlite` or creates a fresh one and backfills
`market_outcomes` first. Politeness rate-limiting, progress printing (done/total, trades, candles, ETA),
resume from `backfill_progress`, Ctrl-C safe (a market is either fully committed or not at all), a final
summary (processed/skipped/failed, total prints/tape rows/candles, date span, zero-trade-window count,
straddle count).

## A5. Wiring (small, no new strategy logic)
`fillcheck` needs no change if the schema is reused (add a test proving it against a backfilled DB fixture).
`market_level_pipeline.market_level_examples` gets an optional `market_mid_at_decision` feature from the
latest `market_candles` bar at/before decision time, default off. `calibration_report` gets an optional
"market candles" source so the reliability table can compare model vs. market mid on historical windows.

## A6. Tests (all offline)
Fixtures/inline payloads for cutoff, trades pagination, candles (including a null-price minute), live
candles (`volume_fp`), historical markets. Parsing strict-failure tests, all 4 taker_side/taker_outcome_side
cases, nullable price fields. Routing tests (before/after/straddle cutoff). Pagination tests (cursor
follow/repeat/max_pages). Tape idempotency tests plus a `hypothesis` property (sum of `contracts` equals the
sum of deduped trade `count`s). Resume test (kill mid-run, re-run, no duplicates). No-auth test. CLI
argument/summary tests. The full existing suite must still pass.

## A7. Docs
README status-table row plus a "Historical backfill" section and verified-facts table additions (dated,
marked "docs only, not yet confirmed" until the owner's first real run). CLAUDE.md: one bullet.
`docs/running-live.md`: exact owner commands, with a suggested `--limit-markets 20` smoke test first.

## A8. Report back
Stop and report the passing test count, new/changed files, commands, and open questions -- explicitly flag
ambiguities: volume field-name differences, whether historical trades need `ticker` or `series_ticker`, max
candles per request.

## Part B (optional, owner-gated -- do not start without an explicit go-ahead)
HuggingFace dataset importers (`polymarket-crypto-5m-15m`, `binance-btcusdt-spot-trades`, a Kalshi-orderbook
dataset requiring a license check first): isolated `pm_*`/new-prefixed tables, never mixed with Kalshi
tables, pinned revisions, optional pip extra.
