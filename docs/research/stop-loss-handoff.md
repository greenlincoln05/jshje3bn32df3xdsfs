# Stop-loss / early exit: handoff for the next session

Goal (owner, 2026-09-19): "it's not cutting the losers fast enough." Today the bot rests a maker bid and holds
every position to settlement. No exit logic exists (`strategy.py` mentions take-profit/stop-loss as not implemented).

## State on main
- Sizing `ramp` (PR #30): 5 contracts, +max(1, 20%) after each settled win, reset to base after loss/breakeven, cap
  `risk.max_contracts_per_trade` (10). Not modeled in `lab`/`backtest` yet (they use `percent`/`EntryFilters`).
- Demo runs (`btcbot demo`) place real orders on Kalshi DEMO only. Write gate: `KalshiClient.create_order/cancel_order`
  refuse anything but `KalshiEnv.DEMO`. Do not loosen it. No Claude session uses a key; the owner runs demo commands.
- Candidate A (frozen, `candidate-a.config.yaml`) is negative on the post-freeze forward windows (3 trades, 1 win,
  -$21.58). Nothing here is a profitability claim.

## Design to build (proposal, owner approved "build stop loss next")
1. Config `exit:` block (off by default until lab-tested): `stop_loss_pct` (exit if mark falls this % below entry),
   `stop_min_hold_sec`, `stop_min_tau_sec` (do not exit in the last N s; hold to settlement), optional `take_profit`.
2. Mark = best bid of the side held (what a sell could hit), not the mid. Exits are SELLS of the held side; in the
   V2 YES-only vocabulary selling YES = `ask`, selling NO = `bid` on YES at 1-p. Use IOC taker at the bid.
3. Lab/backtest first: replay recorded books, simulate the exit fill at best bid (depth-limited, taker fee), sweep
   `stop_loss_pct` with the train/test split. Expect: thin books make exits fill badly; a stop may lose more than
   holding on binary 15-min markets. Report that honestly.
4. Then `execution.py` demo backend: `place_exit_order`, position reconcile, `demo_events` rows, paper-twin parity,
   fee via the existing formula (`fee_cost` strict). Risk: exposure released only by the actual exit fill.
5. Tests offline with fake client (`tests/test_demo_trader.py` pattern); regression: exit never increases size, never
   fires after settlement, partial exit fill leaves remainder tracked.

## Repo rules that bind this work
- No martingale / no size increase after a loss (ramp already resets to base). No profitability claims without
  out-of-sample results. Lab verdicts never say "profitable". Prices/counts `Decimal`. ASCII console output.
- Commit and push separately (review-gate hook: run a repo review, then `mark-reviewed.mjs repo-reviewer`).
- Merge, do not rebase. Owner merges PRs.

## Commands
- Tests: `.venv/Scripts/python.exe -m pytest` (778 pass at 8974790)
- Lab: `.venv/Scripts/btcbot.exe lab --grid min_edge=0.04 --grid max_price=none,0.6`
- Data: `data/paper-KXBTC15M-prod-20260919T*.sqlite` (recorded prod windows)
