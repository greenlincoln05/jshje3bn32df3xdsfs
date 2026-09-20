# btc15m-bot: notes for Claude Code

Paper-first bot for Kalshi's rolling 15-minute BTC markets (`KXBTC15M`). `docs/btc15m-bot-spec.md` is the source of
truth for scope and phases; the README has the phase status and a dated table of verified Kalshi API facts.

## Working rules
- Build one phase at a time. When a phase is done, stop and report (passing tests, a short summary, the commands to run
  it, open questions) and wait for the owner's explicit go-ahead before starting the next one.
- Paper trading is the default, and **live** trading must be impossible to enable by accident (all four gates in spec
  section 7). Phase 6 added real order-placing code, but it only ever runs against Kalshi's **demo**
  environment: `kalshi_client.py`'s `create_order`/`cancel_order` carry a hard assertion refusing to sign
  against anything but `KalshiEnv.DEMO`, and nothing points a live backend at prod, because no live backend
  exists at all. Do not add one, or loosen that assertion, before the owner approves Phase 7.
- No martingale, doubling or any size increase after a loss. No secrets in the repo (`.env*`, `*.pem`, `*.key`, key-like `.txt` files and `secrets/` are
  gitignored). No profitability claims without recorded out-of-sample results. No Robinhood scraping and no unofficial
  data sources.
- No placeholder modules remain: `spot_feed`/`recorder` (Phase 2), `model` (Phase 3),
  `strategy`/`risk`/`execution`/`paper_broker`/`backtest` (Phase 4), `live_paper` (Phase 5), and
  `demo_check` (Phase 6) are all implemented. The next new module a phase adds should hold only a docstring
  until that phase actually replaces it, same as these did.
- No API key, demo or production, gets used or written to a repo file by a Claude Code session. A key pasted
  into any chat is treated as exposed; the fix is to revoke/reissue it, never to use it. Phase 2's recorder
  uses public, unauthenticated endpoints only for this reason (Kalshi's order-book WebSocket needs a key even
  for public data). `btcbot paper` (Phase 5) is the same: public data only, no key. `btcbot demo-check`
  (Phase 6c) is different -- it needs a demo key to place real (fake-money) test orders -- so the same rule
  means no Claude Code session has ever run it against real credentials, or ever will: Claude wrote the
  script and its offline tests (a fake client standing in for `KalshiClient`, per `tests/test_demo_check.py`),
  and running it for real, with a real demo key that goes straight into the owner's own `.env` and never into
  a chat, is entirely the owner's to do on their own machine. Phase 6's build itself skipped its own
  prerequisite (real `record`/`paper` data collected and reviewed) at the owner's explicit direction --
  writing and offline-testing the code did not need it -- but that data, plus an actual `demo-check` run,
  still has to happen before trusting any of this or considering Phase 7.
- `execution.py` has a paper backend and a demo backend (Phase 6, `DemoExecutionBackend`). `paper_broker.py`
  never talks to Kalshi; `DemoExecutionBackend` only ever calls `kalshi_client.py`'s demo-gated write
  methods. There is still no *live* order-placing code and no live backend anywhere in this repo; do not add
  one before the owner approves Phase 7 (all four gates, spec section 7).
- Phase 6 (`btcbot demo-check`, `btcbot demo`): order placement exists but ONLY against Kalshi's DEMO
  environment. `KalshiClient.create_order`/`cancel_order` refuse to sign against prod
  (`KalshiWriteNotAllowedError`), `DemoTrader` refuses a non-demo client, and both use the V2 endpoints
  (`POST/DELETE /portfolio/events/orders`; the legacy `/portfolio/orders` writes are deprecated). Kalshi's V2
  side is YES-only: buying NO at p is an `ask` on YES at 1 - p, and `demo-check` reads a NO order back to
  prove it. Order/fill/position field names come from docs.kalshi.com (read 2026-09-19) and are strict: a
  missing `fee_cost` is a ParseError, never a silent zero. A session never runs these; the owner does, with
  their own demo key.
- Sizing modes (`sizing.mode`): `fixed` (default), `kelly`, and `percent`. `percent` stakes a small percent of the
  CURRENT account per order; the account is the starting `account_usd` plus SETTLED profit and loss only, so bets
  grow after wins and shrink after losses (fixed-fractional, the opposite of a martingale). After a settled win the
  next order may grow by at most `max_growth_per_win_pct`; after a loss it can never exceed the order that lost
  (`strategy.percent_size` builds that in, `risk.RiskManager` still vetoes it as a backstop, and the "no size
  increase after a loss" rule is unchanged). `risk.max_open_exposure_pct` / `risk.daily_loss_limit_pct` make the
  two dollar caps follow the account, otherwise a fixed cap stops bets from growing. `risk_pct_per_trade` is capped
  at 10 in the config on purpose. Percent sizing does not create an edge; do not enable it for real money on the
  strength of a paper or demo result, and never raise a size to recover a loss.
- Ramp sizing (`sizing.mode: ramp`, the shipped default): `contracts_per_trade` (5), +max(1, `ramp_growth_pct`%) after
  each SETTLED win, back to base after any loss or breakeven, capped by `risk.max_contracts_per_trade`. Ramp risk
  rules (`live_paper.py`): each win-level lowers the highest entry price by `ramp_max_price_step` (floor
  `ramp_max_price_floor`), and `ramp_idle_reset_windows` (2) consecutive windows without opening a position reset
  the ramp to base. `strategy.ramp_stop_loss_pct` tightens the stop per level; that stop is wired into the
  lab/backtest only until paper/demo exits land.
- `btcbot demo --plumbing`: thin-demo-book TEST mode (max_spread 1.0, min_depth 1, `bid_improve_ticks: 5`, i.e. bid up to 5
  cents inside the spread, never onto the ask). The demo book's median spread is ~14 cents and depth ~100 contracts, so the
  normal filters reject most of it. Plumbing mode exercises placement, fills, settlement and sizing; it says nothing about the
  strategy, which is judged on prod paper. It is a `demo`-only flag and `bid_improve_ticks` defaults to 0 everywhere else.
- `webui.py` (`btcbot dashboard`) is a monitoring tool, not a phase -- it only reads local SQLite databases
  and rewrites three lines of a local `.env`. It binds to `127.0.0.1` only (never `0.0.0.0`) and must stay
  that way. Its Settings tab may write a key the owner enters into their own local `.env`, same as editing
  the file by hand; that is not a Claude Code session using a key (nobody here ever sees the value, since it
  goes from the owner's browser straight to their own disk), and it still doesn't add anywhere for that key
  to place an order -- the same Phase 6 gate applies to any future addition here.

- `stream_recorder.py` (`btcbot stream`) is a READ-ONLY authenticated WebSocket capture of BRTI
  (`cfbenchmarks_value`, `cfbenchmarks_value_5hz`) and order-book deltas. It sends only `subscribe` /
  `unsubscribe` on those market-data channels (a test enforces this) and has no order, cancel or portfolio
  command, so the Phase 6 gate is untouched. Its handshake needs the owner's own key, so Claude writes and
  tests it offline against fake sockets and the owner runs it; a session never uses the key. Message shapes
  come from docs.kalshi.com and are unverified against the live server until the owner's first run.

- `lab.py` (`btcbot lab`, dashboard "Strategy Lab" tab) sweeps entry timing, price bands, trend filters,
  account size and risk sizing over recorded data. It is offline research: no network, no key, no order code.
  Every combination is ranked on a training slice and judged on held-out windows; its verdict must never call
  a configuration profitable (a test checks the wording), because CLAUDE.md's "no profitability claims
  without recorded out-of-sample results" applies to its output. Percent-of-account sizing only ever shrinks
  after a loss, and `risk.py` still enforces "no size increase after a loss" on the ORDERED size.
- `strategy.decide()` takes an optional `min_price`/`max_price` band and reports each `Decision`'s Kelly
  fraction (`kelly_fraction()`, `(p - price) / (1 - price)`) -- added after real `btcbot demo` trading took a
  thin-edge trade on a badly asymmetric payout, which a flat `min_edge` never screens for. `BotConfig`
  defaults the band to `[0.15, 0.85]`: a reasoned guardrail, not a backtested-optimal cutoff, since the
  model has no calibration check at the extremes yet (`btcbot calibrate`) -- tune it with `btcbot lab`.
  `sizing.mode: kelly` sizes by that fraction (times `kelly_fraction_multiplier`, default 0.2 -- full Kelly
  is most aggressive exactly where the model is least trustworthy, near a price of 0 or 1) against
  `risk.max_open_exposure_usd`, instead of a flat `contracts_per_trade`; default is `fixed` (unchanged
  behavior). Wired into `live_paper.py` only, so `btcbot paper` and `btcbot demo` (same decision loop) both
  get it; `btcbot backtest`/`btcbot lab` are untouched and keep their own separate `EntryFilters`/`risk_pct`
  mechanism.
- ML entry/exit layers (`ml_model.py`, `ml_features.py`, `ml_pipeline.py`, `market_level_pipeline.py`,
  `history_pipeline.py`, `coinbase_history.py`, `docs/research/ml-layers-handoff.md`), owner-driven, not a
  numbered phase: a hand-rolled, dependency-free `LogisticModel` (no numpy/scikit-learn) re-scores entries
  `strategy.decide()` already proposed and can force an early exit, wired into `btcbot.lab`'s
  `EntryFilters`/`LabParams` as `ml_entry_model_path`/`ml_exit_model_path` -- it can only veto or exit a
  trade the base strategy already took, never invent one it would have skipped. `lab.run_ml_ablation()`
  (`btcbot ml-ablation`) runs the four layers the owner asked for (current entry+hold, ML entry+hold,
  current entry+ML exit, ML entry+ML exit) on the SAME train/test split. `btcbot ml-train` fits a model from
  a recorder database via `ml_pipeline.train_and_validate()`, reusing `lab.split_windows`'s exact
  train-on-earlier/validate-on-later, grouped-by-market discipline. Not wired into `live_paper.py`/`btcbot
  paper`/`btcbot demo` yet -- that is the deliberate next step once a real model has been trained and
  validated on real data, same as every other phase's "lab first, live later" order. `btcbot download-history`
  backfills settled markets (via `kalshi_client.py`'s already-public `list_markets(status="settled")`) and
  Coinbase 1-minute candles for the separate, coarser market-level pipeline (`market_level_pipeline.py`);
  like `record`/`stream` it is public data needing no key, but this session's own environment cannot reach
  Kalshi/Coinbase (see `docs/running-live.md`), so it is the owner's to run, built and tested here only via
  `httpx.MockTransport`. No real data has been downloaded or trained on from this sandbox; every reported
  Brier score or ablation number in this work is on synthetic fixtures, same caveat as every phase before a
  real run.
- Three feature schemas now exist for a `ml_model.LogisticModel` (`ml_features.ENTRY_FEATURES`/`EXIT_FEATURES`
  for the tick-level lab/ablation path, `ml_pipeline.FEATURE_STORE_ENTRY_FEATURES` for the `btcbot features`
  CSV path, `market_level_pipeline.MARKET_LEVEL_FEATURES` = `{p_model, sigma}` for the coarse historical
  dataset) and they are NOT interchangeable -- `LogisticModel.predict_proba()` silently treats an unrecognized
  feature as its training mean rather than erroring, so a model trained for one schema plugged into a pipeline
  expecting another would just degrade to a near-constant prediction. `ml_model.check_feature_coverage()`
  refuses to load a model whose features do not overlap the pipeline's schema (called by both
  `lab._load_ml_model()` and `cli._cmd_validate`'s `--model` path); see `docs/research/ml-layers-handoff.md`'s
  "Review" section for the full table. It is a name-overlap heuristic, not a semantic check: because
  `market_level_pipeline`'s two features are literally named `p_model`/`sigma`, a model trained on that coarse
  schema passes the coverage check against `FEATURE_STORE_ENTRY_FEATURES` too (which also has `p_model`/`sigma`
  columns, computed differently, at the real tau rather than a fixed 1 second) -- coverage catches a
  wrong-schema model with no overlap at all, not one whose overlapping names mean something different.
  `train_and_validate_from_features()` (`btcbot ml-train --features`) trains directly on the richer `btcbot
  features` schema and scores the bot's own `p_blend` on the same held-out rows so `beats_baseline` answers
  "does this actually help," and `btcbot validate --model` runs the full time-split harness for a trained model
  side by side with the current one. `train_and_validate_market_level()` (`btcbot ml-train --history`, on a
  `btcbot download-history` database) does the same `beats_baseline` comparison for the third, candle-only
  schema, baselined against the v1 model's own unmodified `p_model` on the same held-out markets -- a
  calibration measure only, since there is no recorded book price this far back to simulate a trade against.

## Commands
- Tests (offline): `.venv/Scripts/python.exe -m pytest`
- Read-only live check, no credentials needed: `.venv/Scripts/btcbot.exe discover --env prod`
- Record public data (no credentials needed): `.venv/Scripts/btcbot.exe record --env prod --hours 9`
- Live paper trading, no credentials, no real orders (Phase 5): `.venv/Scripts/btcbot.exe paper --env prod --hours 9`
- Calibration report from a recorder database: `.venv/Scripts/btcbot.exe calibrate --db data/recorder-....sqlite`
- Backtest a recorder database: `.venv/Scripts/btcbot.exe backtest --db data/recorder-....sqlite`
- BRTI + order-book stream, READ-ONLY, needs YOUR key in `.env` (run by the owner): `.venv/Scripts/btcbot.exe stream --env prod --hours 9`
- Strategy lab on recorded data (offline): `.venv/Scripts/btcbot.exe lab --grid min_edge=0.02,0.04 --grid max_price=none,0.6`
- Demo-only order validation, needs a demo key (Phase 6, owner runs this, never a Claude Code session): `.venv/Scripts/btcbot.exe demo-check`
- One-time demo setup, needs YOUR demo key (run by the owner; puts collateral on the shard BTC trades on): `.venv/Scripts/btcbot.exe demo-allocate`
- Demo-environment order validation, needs YOUR demo key (run by the owner): `.venv/Scripts/btcbot.exe demo-check`
- Strategy placing REAL orders on the DEMO exchange (fake money), with a paper twin of every order, needs YOUR demo key: `.venv/Scripts/btcbot.exe demo --hours 2`
- Local monitoring dashboard (binds to 127.0.0.1 only): `.venv/Scripts/btcbot.exe dashboard`
- Health check of the newest paper/demo databases, read-only (offline): `.venv/Scripts/btcbot.exe watch`
- Flatten recorder databases into one model-ready feature CSV (offline): `.venv/Scripts/btcbot.exe features --data-dir data --output data/research/features.csv`
- Calibration + model-vs-market disagreement report on a features CSV (offline): `.venv/Scripts/btcbot.exe disagree --features data/research/features.csv`
- Rigorous time-split validation (Wilson CI, t-stat, refuses under 30 test trades) of the current model, or --model against it, on a features CSV (offline): `.venv/Scripts/btcbot.exe validate --features data/research/features.csv --model models/entry.json`
- Train an ML entry/exit model from a recorder database (offline): `.venv/Scripts/btcbot.exe ml-train --db data/recorder-....sqlite --which entry --out models/entry.json`
- Train an ML entry model from a features CSV instead, with a baseline comparison (offline): `.venv/Scripts/btcbot.exe ml-train --features data/research/features.csv --out models/entry.json`
- Train the coarse market-level calibration model from a download-history database instead, with a baseline comparison (offline): `.venv/Scripts/btcbot.exe ml-train --history data/history-....sqlite --out models/market_level.json`
- Compare the four ML entry/exit layers on recorded data (offline): `.venv/Scripts/btcbot.exe ml-ablation --db data/recorder-....sqlite --entry-model models/entry.json --exit-model models/exit.json`
- ONE-TIME backfill of settled markets + Coinbase candles, needs network (owner runs this, never a Claude Code session): `.venv/Scripts/btcbot.exe download-history --start 2026-01-01T00:00:00Z --end 2026-09-01T00:00:00Z`
- Testing any of the above against the real network: see `docs/running-live.md` (this session's own
  environment cannot reach Kalshi/Coinbase; that has to happen on the owner's machine).

## Conventions
- Prices and contract counts are `Decimal`, never `float`. The client decodes JSON numbers straight to `Decimal`.
- Take prices from the orderbook endpoint, not from the market object: its bid/ask fields lag by several seconds.
- Find the current market with `GET /markets?status=open` and re-check status and times client-side. Do not use
  `GET /events?status=open`: it hides a new window for a full minute after each rollover.
- Kalshi demo and production credentials are separate. The client defaults to demo.
- API tests use `httpx.MockTransport` and the real public payloads in `tests/fixtures/`; they never touch the network.
- Keep console output ASCII so Windows consoles do not raise encoding errors.
