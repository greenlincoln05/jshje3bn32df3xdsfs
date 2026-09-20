# ML entry/exit layers: handoff (2026-09-20)

Owner's ask (two messages): "Start building out ML layers on top" as four independently-testable
configurations --

1. Current strategy with settlement holding.
2. ML entry filter with settlement holding.
3. Current entries with ML exits.
4. ML entry filter and ML exits together.

-- plus Codex's data pipeline steps (the same list `stop-loss-handoff.md` already described as "in
progress"): download ~20,000-30,000 market outcomes plus one-minute candles, add Coinbase 1-minute BTC
history, train only on earlier complete markets, validate on later months grouped by whole market, simulate
exits at the recorded bid with taker fees and adverse-candle assumptions, send the strongest few models
through the local one-second order-book replay, forward-paper-test survivors without real orders.

## What this session built

Everything below is offline, no key, no network from this sandbox (see "What could not be done here"), and
nothing is wired into `live_paper.py`/`btcbot paper`/`btcbot demo` yet -- the same "lab first, live later"
order every other phase in this repo has followed.

### The model itself (`ml_model.py`)
A hand-rolled logistic regression: standardized features, batch gradient descent, L2 weight decay. No numpy
or scikit-learn -- consistent with the project's existing minimal-dependency policy (`model.py`'s own v1
formula uses only `math.erf`) and with keeping a saved model as inspectable JSON (feature names, weights,
bias, per-feature mean/scale), never a pickle that could execute arbitrary code on load.

### Feature vectors (`ml_features.py`)
`entry_features()` / `exit_features()`: fixed-order float dicts built only from information a live trader
would actually have at that instant (edge, p_side, price, tau, spread, depth, sigma, 60s momentum for entry;
held time, tau, unrealized %, sigma, momentum for exit). Both models only ever see a candidate/tick the base
strategy already reached -- an entry model never scores a side `strategy.decide()` would have skipped (there
is no recorded outcome for a side never taken), and the exit model only runs while a position from one of
those entries is held.

### The four layers, wired into `btcbot.lab` (`backtest.py`, `lab.py`)
- `backtest.EntryFilters` gained `ml_entry_model` / `ml_entry_min_edge` / `ml_exit_model` /
  `ml_exit_prob_threshold`. Inside `replay_prepared`'s `screen()`, an ML entry model re-scores a candidate
  `strategy.decide()` already proposed (same veto-only precedent price-band/trend/persistence filters
  already set) -- it can reject a trade, never invent one. A separate check after `decide()` can override a
  `HOLD` into an `EXIT` when the exit model's predicted probability clears a threshold, mirroring
  `strategy.should_exit()`'s early-exit mechanism without touching that pure function at all. **Bug caught by
  the tests and fixed during this work:** the first version of the exit override did not check
  `resting_order_id is None`, so it could fire while the entry order was still partially resting -- violating
  "a resting order always takes priority over an exit check" (`strategy.py`'s own documented precedence) and
  splitting one position into two spurious exits. Caught immediately by
  `tests/test_backtest.py::TestMLExitLayer`, fixed with one added guard.
- `lab.LabParams` gained `ml_entry_model_path` / `ml_entry_min_edge` / `ml_exit_model_path` /
  `ml_exit_prob_threshold` (paths, not loaded models, so `LabParams` stays the same plain JSON-able dataclass
  every other field here is; `_filters_for` loads and caches the file).
- `lab.run_ml_ablation()` (CLI: `btcbot ml-ablation`) builds exactly the four requested configurations and
  evaluates each on the SAME time-ordered train/test split (`split_windows`), so the four rows are directly
  comparable. `render_ml_ablation_report()` prints them side by side, train and test.

### Training (`ml_pipeline.py`)
`build_training_examples()` walks a prepared replay's steps directly (NOT `replay_prepared` -- that
function's job is a realistic, risk/broker-aware replay and stays untouched) with much simpler bookkeeping:
one open candidate at a time, no risk manager, no broker, just enough to ask "what would the strategy have
proposed here, and did it win" for the entry model, and "would selling now, taker fee included, have beaten
holding to settlement" for the exit model. `train_and_validate()` (CLI: `btcbot ml-train`) reuses
`lab.split_windows` -- the SAME time-ordered, embargoed, whole-market grouping the lab's own train/test sweep
already uses -- so a model is only ever validated on markets later than every one it trained on, exactly
"train only on earlier complete markets, validate on later months, grouped by whole market." Reports a Brier
score on both sides; a much higher validate score than train means the model fit noise in training, the same
overfitting signal `btcbot lab`'s own train/test gap already warns about. Not a PnL or profitability number.

### The larger, coarser dataset (`history_pipeline.py`, `coinbase_history.py`, `market_level_pipeline.py`)
Kalshi's public API does not retain a tick-level order book for markets from months ago -- only
`recorder.py`'s own live poll does, and that has only ever run for a few days at a time in this project. So
the "20,000-30,000 market outcomes" dataset is necessarily coarser: settled markets (via
`kalshi_client.py`'s already-public, already-unauthenticated `list_markets(status="settled")` -- no new
endpoint, no key) joined with Coinbase 1-minute candles (`coinbase_history.py`, the same public,
unauthenticated `api.exchange.coinbase.com` host `spot_feed.py` already uses for its REST fallback).
`market_level_pipeline.py` adds:
- `split_markets_by_time()`: the same train-earlier/validate-later, embargoed, grouped-by-market split as
  `lab.split_windows`, over this dataset's rows instead of recorded snapshots.
- `market_level_examples()`: one example per settled market, features are the v1 model's OWN `p_yes`
  (reconstructed from the last candle before close plus a candle-derived realized-vol proxy) and that sigma.
  This is a calibration correction over the v1 formula (the same thing `btcbot calibrate`'s reliability table
  already measures), not a price/edge model -- there is no recorded book price this far back to compute an
  edge against.
- `adverse_exit_price()` / `adverse_exit_pnl()`: the "adverse candle assumption (worst case inside a
  candle)" the owner asked for -- re-prices the v1 model at the candle's least favorable print for a held
  side (LOW for a YES holder, HIGH for a NO holder), taker fee included. A standalone sanity estimate of what
  an exit might have cost on this dataset, not a full replay (there is no book to replay against this far
  back) and it is not fed into `market_level_examples()`.
- `btcbot download-history`: backfills both tables into one SQLite database.

## What could not be done here

This session's own environment cannot reach Kalshi or Coinbase (CLAUDE.md, `docs/running-live.md`) -- every
network-touching piece above (`download-history`, and by extension any real training or ablation run) is
built and offline-tested via `httpx.MockTransport` only, exactly like `record`/`stream`/`demo-check` before
it, and is the owner's to actually run. Concretely, still to do, in order:

1. Run `btcbot download-history` for a real date range (the owner, on their own machine).
2. Run `btcbot ml-train --which entry` and `--which exit` against that data and a real recorder database,
   and read the validate Brier score honestly -- a model that is not better calibrated than the v1 formula
   on held-out markets is not worth carrying forward.
3. Run `btcbot ml-ablation` with the resulting model(s) against real recorded order-book data (`btcbot
   record`/`btcbot paper`'s own databases) -- this is "send the strongest few models through the local
   one-second order-book replay."
4. Only after a layer shows a real, out-of-sample edge: wire it into `live_paper.py` so `btcbot paper` can
   forward-paper-test it without real orders -- the deliberate next step this handoff does NOT take, same as
   how ramp sizing and stop-loss both landed in the lab before `live_paper.py`.
5. Only after (4): revisit whether `btcbot demo` should ever use an ML-driven decision -- still gated by the
   same demo-only write assertion in `kalshi_client.py`, untouched by any of this.

No profitability claim is made anywhere in this work; every new report explicitly labels itself a
calibration measure or a sensitivity comparison, per CLAUDE.md.

## Tests

`tests/test_ml_model.py`, `tests/test_ml_features.py`, `tests/test_ml_pipeline.py`,
`tests/test_coinbase_history.py`, `tests/test_history_pipeline.py`, `tests/test_market_level_pipeline.py`,
plus new classes in `tests/test_backtest.py` (`TestMLEntryLayer`, `TestMLExitLayer`), `tests/test_lab.py`
(`TestMLAblation`), `tests/test_models.py` (`Market.result`), and `tests/test_cli.py`
(`TestDownloadHistoryEndToEnd`, `TestMlTrainEndToEnd`, `TestMlAblationEndToEnd`). Every new assertion was
verified to fail against a deliberately broken build first (including the resting-order precedence bug
above, and the adverse-candle YES/NO side selection). Full suite: 904 passed, offline only, no key used, no
real network reached.

## Commands

- Train: `.venv/Scripts/btcbot.exe ml-train --db data/recorder-....sqlite --which entry --out models/entry.json`
- Compare the four layers: `.venv/Scripts/btcbot.exe ml-ablation --db data/recorder-....sqlite --entry-model models/entry.json --exit-model models/exit.json`
- Backfill (owner, needs network): `.venv/Scripts/btcbot.exe download-history --start 2026-01-01T00:00:00Z --end 2026-09-01T00:00:00Z`
