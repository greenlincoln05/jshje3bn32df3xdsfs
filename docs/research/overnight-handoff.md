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
