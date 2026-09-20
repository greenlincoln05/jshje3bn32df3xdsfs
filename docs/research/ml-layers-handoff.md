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

## Review (2026-09-20, later): reconciling parallel ML/validation work, and closing three gaps

The owner asked for a review of "the latest PRs for ML infrastructure" and how to make sure ML actually helps
entry, exit, and profit. In between this handoff's own PR merging and this review, three other sessions
landed real, related work, motivated by an actual event: `docs/research/demo-loss-streak-2026-09-20.md`
records a real `btcbot demo` run losing five trades in a row because a resting order sat for 5m25s while its
edge decayed (fixed upstream by `strategy.decide()`'s new `resting_side`/`resting_price` cancel check). That
incident is also what motivated:

- **`features.py`** (`btcbot features`): flattens recorder databases into one flat, richer feature-store CSV
  per snapshot -- tau, spot-minus-strike, three spot-momentum lookbacks (60/300/900s), book imbalance,
  3-level depth, `p_model`/`p_blend`/`sigma`, and `model_minus_market` (the exact model-vs-market
  disagreement signal the loss streak came from).
- **`validation.py`** (`btcbot validate`): a genuinely rigorous time-split harness this handoff's own
  `run_ml_ablation()` did not have -- whole-market time split with an embargo, Wilson confidence interval, a
  t-statistic, and a hard refusal below `min_test_trades` (default 30), with wording that only ever says
  "evidence consistent with an edge" or "no evidence," never "profitable." Critically, it ships
  `blend_predictor` -- the bot's OWN currently-deployed model, read back from recorded predictions -- as a
  ready-made baseline any other predictor should be compared against.
- **`calibration_report.py`** (`btcbot disagree`): reliability and model-vs-market-disagreement buckets on
  the same feature-store CSV, built specifically to show what happens when the model disagrees with the
  market or the price is cheap -- exactly the loss streak's shape.
- **`watch.py`** (`btcbot watch`): a read-only health check of the newest paper/demo databases.

None of that referenced this handoff's `ml_model.py`/`ml_pipeline.py`/`lab.run_ml_ablation()` work, and vice
versa -- two independently-built pieces of ML-adjacent infrastructure landed side by side. Reviewing both
together surfaced three real gaps, all fixed in this pass:

### 1. Three incompatible feature schemas, with no guard against mixing them up

This project now has three distinct feature vocabularies a `ml_model.LogisticModel` can be trained on:

| Schema | Names | Used by |
|---|---|---|
| `ml_features.ENTRY_FEATURES` / `EXIT_FEATURES` | `edge`, `p_side`, `price`, `tau_sec`, `spread`, `depth`, `sigma`, `momentum_60s` (entry); `held_sec`, `tau_sec`, `unrealized_pct`, `sigma`, `momentum_60s` (exit) | `backtest.py`'s live ML entry/exit gate, `lab.run_ml_ablation()`, `ml_pipeline.build_training_examples()`/`train_and_validate()` |
| `ml_pipeline.FEATURE_STORE_ENTRY_FEATURES` (new, see below) | a subset of `features.py`'s `COLUMNS`: `tau_sec`, `spot_minus_strike`, `spot_move_60s/300s/900s`, `yes_spread`, `yes_bid_size`, `no_bid_size`, `yes_depth3`, `no_depth3`, `book_imbalance`, `p_model`, `sigma`, `model_minus_market` | `ml_pipeline.train_from_feature_rows()`/`train_and_validate_from_features()`, `btcbot validate --model` |
| `market_level_pipeline`'s coarse schema | `p_model`, `sigma` | `market_level_pipeline.market_level_examples()` only |

Because `LogisticModel.predict_proba()` treats any name it does not recognize as that feature's training
mean, loading a model trained on one schema into a pipeline that only ever supplies another would not error
-- it would just quietly degrade to a near-constant prediction, which could pass every other check and still
be worthless. Fixed with `ml_model.check_feature_coverage()`, called once when `lab._load_ml_model()` loads a
path (an `ml_entry_model_path`/`ml_exit_model_path` must actually overlap `ENTRY_FEATURES`/`EXIT_FEATURES`,
or the ablation refuses to run with a clear error naming the mismatch). `LogisticModel.predict_proba()` was
also fixed to treat a feature present with value `None` the same as one missing entirely (`features.py`'s
rows always carry every column, with `None` where a value could not be computed yet, e.g. not enough spot
history for a 900s lookback) -- it previously only handled a missing KEY, and would have raised a `TypeError`
the first time it saw a real feature-store row with any `None` in it.

### 2. `run_ml_ablation()` had no insufficient-data or significance discipline

`lab.run_lab()`'s own grid sweep already refuses a verdict below `ENOUGH_TEST_TRADES` (30) and requires a
test t-stat >= 2 before saying "weak_signal" (never "profitable"). `run_ml_ablation()` -- the direct answer
to the owner's four-layer ask -- had none of that: it just printed four rows of raw PnL/win-rate/t-stat with
nothing stopping someone from reading "layer 2 made more money than layer 1" as a real result off a handful
of test trades. Fixed: each `AblationLayer` now carries its own `verdict_level`/`verdict`
(`_ablation_layer_verdict()`, the same refuse-below-threshold discipline, checked per layer against its OWN
test trades since a fully rigorous paired test across layers with different trade counts is a harder problem
this does not claim to solve) and the report shows each layer's PnL delta against layer 1 explicitly labeled
"not a tested difference," plus a warning that four layers were compared (a lone `weak_signal` could be the
one false positive among four tries).

### 3. No way to ask "does the ML model actually beat what's already running"

Before this pass, `ml_pipeline.train_and_validate()` reported a Brier score with nothing to compare it
against -- a number in isolation cannot say whether an ML model helps. Meanwhile `validation.py` already had
exactly the machinery to answer that (a `Predictor` type, `blend_predictor` as a ready baseline) but no
second predictor to compare it with. Connected the two:

- `ml_pipeline.FEATURE_STORE_ENTRY_FEATURES` + `train_from_feature_rows()` / `train_and_validate_from_features()`:
  trains directly on `features.py`'s row schema (not `ml_features`' tick-level one), using the SAME column
  names those rows already carry -- a features-store row can be fed straight into `LogisticModel.predict_proba()`
  with no adapter, and `predict_proba` is itself a valid `validation.Predictor`. `train_and_validate_from_features()`
  reuses `validation.split_windows` (not a second copy of the same algorithm) and, critically, ALSO scores
  the bot's own `p_blend` on the exact same held-out rows, reporting `beats_baseline` explicitly.
- `btcbot ml-train --features <csv>` (alternative to `--db`): trains this way and prints the baseline
  comparison plainly.
- `btcbot validate --features <csv> --model <path>`: runs the FULL rigorous harness (time-split, Wilson CI,
  t-stat, hard refusal below 30 test trades) for both the current model and a trained ML model, side by
  side, on the same data.

This is the concrete answer to "make sure ML gets us better entry/exit/profit": once real data exists,
`btcbot features` -> `btcbot ml-train --features` -> `btcbot validate --features ... --model ...` gives an
honest, baseline-compared, statistically-disciplined verdict -- not a claim, a falsifiable check. Still
subject to the same limits `validation.py`'s own docstring states (an optimistic maker-fill assumption,
correlated per-window intervals, one policy shape tried) and CLAUDE.md's "no profitability claims without
recorded out-of-sample results."

### What is still NOT connected (deliberately, and correctly so)

- `ml_pipeline.train_and_validate()` (the tick-level `ml_features.ENTRY_FEATURES` path, feeding
  `lab.run_ml_ablation()`) and `train_and_validate_from_features()` (the `features.py`-schema path, feeding
  `btcbot validate --model`) remain two separate training paths on purpose: the former is needed for the
  ablation's queue-aware `PaperBroker` replay (which reads `EntryFilters.ml_entry_model` directly), the
  latter for the richer, already-shared validation/calibration tooling. A model trained one way is not valid
  input for the other pipeline -- `check_feature_coverage()` now catches that mix-up if attempted.
- `market_level_pipeline.py`'s coarse (`p_model`, `sigma`) schema is still a third, separate thing, by
  design: it is the only one of the three that can ever be trained from the "20,000-30,000 markets" dataset
  (no tick-level book exists for it), and it answers a narrower question (is the v1 formula itself
  miscalibrated) than either of the other two.
- None of this is wired into `live_paper.py`/`btcbot paper`/`btcbot demo`. That is still the deliberate next
  step, gated on a real `validate --model` run actually showing `beats_baseline: True` with a `weak_signal`
  (never higher) verdict on real recorded data -- not on anything built in this sandbox.

### Tests added in this pass

`tests/test_ml_model.py::TestFeatureCoverage` (+ the `None`-handling fix), `tests/test_lab.py`'s
`test_rejects_a_model_trained_for_a_different_feature_schema` and `TestAblationLayerVerdict`,
`tests/test_ml_pipeline.py`'s `TestFeatureStoreExamples`/`TestTrainFromFeatureRows`/`TestTrainAndValidateFromFeatures`,
and CLI tests in `tests/test_cli.py` (`ml-train --features`, `--db`/`--features` mutual exclusion) and
`tests/test_validation.py` (`validate --model`). Every new assertion verified to fail against a deliberately
broken build first (including the resting-order-style precedence bugs' cousins: the coverage-threshold
direction, the t-stat significance boundary, the `beats_baseline` comparison direction, and the `--db`/
`--features` XOR check). Full suite: 953 passed, offline only, no key used, no real network reached.
