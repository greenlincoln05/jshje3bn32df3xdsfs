# Overnight handoff (2026-09-20 ~00:10 local)

## Running now (owner's PC)
- `btcbot paper --env prod --hours 24 --kill-file KILL_PAPER` (PID 17996, db `data/paper-KXBTC15M-prod-20260920T040250Z.sqlite`).
  A duplicate recorder (`...T040329Z`, killed ~00:07) also holds a few minutes of data: pass ONLY ONE of the two
  overlapping dbs to `lab-suite` or windows are double counted. `...T034921Z` and `...T234337Z` are earlier
  post-freeze recordings; they do not overlap the 04:02Z ones except the tail of `T034921Z` (stopped 00:05 local).
- `btcbot demo --hours 24` (started 00:07, sizing `ramp`: 5 contracts, +max(1, 20%) after each settled win, reset after
  loss/breakeven, cap 10). Demo DB `data/demo-KXBTC15M-demo-20260920T040724Z.sqlite`. Fake money, demo key.
- `btcbot dashboard` (127.0.0.1).

## Tasks safe to run overnight (offline, no key, no orders)
1. Candidate suite forward check (PR #28), after enough windows exist (>=30 resolved post-freeze trades per strategy):
   `.venv\Scripts\btcbot.exe lab-suite --db <one non-overlapping db per period> --after 2026-09-20T03:00:31Z`
   Report only P&L, Wilson win-rate interval and trade counts. Never call anything profitable.
2. Codex's data pipeline (20-30k market outcomes, Coinbase 1-minute history, earlier-train / later-month
   validation, exits at recorded bid with taker fees and adverse-candle assumption). Public data only.
3. Stop-loss lab replay per `docs/research/stop-loss-handoff.md` (pure function of entry, best bid, time left;
   parameters as a grid axis judged only on later-month validation). Lab/backtest ONLY tonight.
4. Ramp parity in `lab`/`backtest`: they still use `percent`/`EntryFilters`, not `ramp`. Add so results reflect live sizing.

## Do NOT do tonight
- No live/prod order code; the demo-only write gate (`KalshiEnv.DEMO`) stays. Stop-loss on the demo exchange waits
  for lab results and the owner's go-ahead.
- No Claude/Codex session uses an API key; the owner runs `demo`, `demo-check`, `stream`.
- Do not restart or kill the paper recorder or demo; do not create `KILL_PAPER`.
- No size increase after a loss; no profitability claims without out-of-sample results.

## Morning checks for the owner
- Demo ramp working? Orders in `demo_orders` should read 5, then 6, 7... after consecutive settled wins.
- Recorder still writing (db mtime recent); demo process alive; note partial fills (a 5-lot order can fill only
  2 contracts while the paper twin assumes all 5; the twin is optimistic).
- Workflow: commit, then push as a separate call; review gate (`mark-reviewed.mjs`) needs a real review first.

## Progress (Claude, 2026-09-20 overnight)

**Ramp reset-then-ramp-again, verified (owner's specific ask):** traced `ramp_next_size()` and its
`LivePaperTrader._resolve()` wiring -- a loss already resets straight to `contracts_per_trade` (base)
unconditionally, and the next settled win resumes growing from there, exactly as intended. No production bug;
added `TestLiveTraderRampMode` (`tests/test_ramp_sizing.py`) driving the actual live-trader decision loop
through win/LOSS/win/win (order sizes `[5, 6, 5, 6]`) and two-losses-in-a-row (never below base), since only
the pure function had coverage before. Confirmed each assertion catches a regression by temporarily breaking
the reset branch and watching 4 tests fail, then restoring. See PR #35.

**Item 4, ramp parity in lab/backtest -- done for the lab's `EntryFilters` path, not `run_backtest`'s bare
path:** `EntryFilters.ramp_growth_pct` / `LabParams.ramp_growth_pct` (a new sweepable grid axis) now drive
the exact same `ramp_next_size()` used live, independent of the `account_usd`/`risk_pct_per_trade`
(percent-mode) machinery. `LabParams.from_config()` picks it up automatically when `config.sizing.mode` is
`ramp` (the shipped default) -- so a plain `btcbot lab` run (no `--grid`) now reflects live sizing without
asking for it explicitly, the same way percent/kelly never did (those stay opt-in only, unchanged).
Deliberately did **not** wire this into `run_backtest()`'s own no-filters call: it would have broken
`test_no_filters_is_identical_to_the_plain_backtest`'s invariant (that a bare backtest call is byte-for-byte
what a raw `filters=None` replay produces) by silently injecting filters behind the caller's back. Whoever
picks up `btcbot backtest`'s own CLI parity next should decide deliberately whether that invariant should
change, not have it changed for them.

New tests: `tests/test_backtest.py::TestRampSizingInBacktest` (win/LOSS/win/win via a full-filling order-book
sequence -- `FULL_FILL_SIZES`, since the default `seed_fillable_window` sequence only ever fills 4 contracts
regardless of order size, which would hide any sizing-mode bug), `tests/test_lab.py::TestRampSizing` (same
end-to-end check, plus `from_config()`'s auto-detection and the grid-axis wiring). All new assertions verified
to fail against a deliberately broken build first. Full suite: 823 passed, offline only, no key used.

**Still open from this list:** item 1 (candidate suite forward check) and item 2 (Codex's data pipeline) need
real recorded data this sandbox does not have; item 3 (stop-loss lab replay/sweep) has its mechanism built
(PR #32) but running an actual sweep also needs real recorded data. `btcbot backtest`'s own ramp parity
(above) is a real, separate follow-up if wanted.
