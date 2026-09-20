# Stop-loss / early exit: handoff for the next session

Goal (owner, 2026-09-19): "it's not cutting the losers fast enough." Today the bot rests a maker bid and holds
every position to settlement. No exit logic exists (`strategy.py` mentions take-profit/stop-loss as not implemented).

## State on main
- Sizing `ramp` (PR #30): 5 contracts, +max(1, 20%) after each settled win, reset to base after loss/breakeven, cap
  `risk.max_contracts_per_trade` (10). Not modeled in `lab`/`backtest` yet (they use `percent`/`EntryFilters`).
- Demo runs (`btcbot demo`) place real orders on Kalshi DEMO only. Write gate: `KalshiClient.create_order/cancel_order`
  refuse anything but `KalshiEnv.DEMO`. Do not loosen it. No Claude session uses a key; the owner runs demo commands.
- Candidate A (frozen, `docs/research/candidate-a.config.yaml`) is negative on the post-freeze forward windows (3 trades, 1 win,
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
   fee via the repo's fee formula in `paper_broker.py`/`btcbot.fees` (exchange `fee_cost` on fills is parsed strictly). Risk: exposure released only by the actual exit fill.
5. Tests offline with fake client (`tests/test_demo_trader.py` pattern); regression: exit never increases size, never
   fires after settlement, partial exit fill leaves remainder tracked.

## Codex's backtest pipeline (in progress; the stop-loss work must plug into it, owner message 2026-09-19)
1. Download ~20,000-30,000 market outcomes plus one-minute candles; add Coinbase 1-minute BTC history.
2. Train only on earlier complete markets; validate on later months, grouped by whole market (no window split).
3. Simulate exits at the recorded bid with taker fees and adverse candle assumptions (worst case inside a candle).
4. Send the strongest few models through the local one-second order-book replay (`lab`).
5. Forward-paper-test survivors without real orders (`btcbot paper`), before any demo order.
So: define the stop as a pure function of (entry, current best bid, tau) so Codex's simulator and the live trader
share it, and treat stop parameters as one more grid axis judged only on the later-month validation.

## Rules that bind this work (repo CLAUDE.md, plus owner workflow: push each commit, review gate, merge not rebase)
- No martingale / no size increase after a loss (ramp already resets to base). No profitability claims without
  out-of-sample results. Lab verdicts never say "profitable". Prices/counts `Decimal`. ASCII console output.
- Commit and push separately (review-gate hook: run a repo review, then `mark-reviewed.mjs repo-reviewer`).
- Merge, do not rebase. Owner merges PRs.

## Commands
- Tests: `.venv/Scripts/python.exe -m pytest` (778 pass at 8974790)
- Lab: `.venv/Scripts/btcbot.exe lab --grid min_edge=0.04 --grid max_price=none,0.6`
- Data: `data/paper-KXBTC15M-prod-20260919T*.sqlite` (recorded prod windows)

## Progress (Claude, 2026-09-20): design steps 1-3 built, 4-5 not started

- **`strategy.should_exit(entry_price, current_bid, tau_sec, held_sec, stop_loss_pct, take_profit_pct,
  stop_min_hold_sec, stop_min_tau_sec) -> "stop_loss" | "take_profit" | None`**: the pure function step 2
  and the Codex-pipeline note both ask for, so a backtest replay and (later) a live trader can share one
  decision. `current_bid` must be the best bid of the side actually HELD (what a sell could get), not the
  mid; `None` (nothing to sell into) or either threshold left `None` means keep holding, i.e. today's
  behavior when nothing is configured. `decide()` calls it when `has_position` and returns a new
  `Action.EXIT` (side, price, `exit_reason`) instead of always `HOLD`; a resting order still takes priority
  over an exit check, unchanged from before.
- **Config**: `BotConfig.exit: ExitRules` (`stop_loss_pct`, `take_profit_pct`, `stop_min_hold_sec`,
  `stop_min_tau_sec`), all off/zero by default -- `config.yaml`'s shipped `exit:` block matches the code
  defaults exactly (a test checks this, same as every other section).
- **Fill simulation**: `PaperBroker.place_exit_order(side, size, book, ts, worst_price=None)` -- a taker SELL
  that crosses into `side`'s OWN bid queue (best price first), the mirror of `place_taker_order` (a taker BUY
  computed from the OPPOSITE side's bids, since there is no separate published ask book). Depth-limited, like
  any other fill.
- **Wired into `btcbot.backtest.replay_prepared`** (so both `btcbot backtest` and `btcbot lab` exercise
  whatever `config.exit` says) -- **not** into `live_paper.py`, so `btcbot paper`/`btcbot demo` are
  byte-for-byte unchanged until someone deliberately wires step 4 in. `TradeRecord` gained `exit_reason` /
  `exit_price` (`None` for a settlement-resolved trade); `build_report`'s resolved/unresolved split now reads
  `pnl_usd is not None` rather than `result is not None`, since an early exit resolves PnL without the market
  ever settling (same one-line fix applied in `webui.py`). A partial exit fill (thin book) splits the
  position: the sold part becomes a closed `stop_loss`/`take_profit` trade, the rest keeps its original entry
  price and rides to settlement like before.
- **Update (2026-09-20, overnight): `lab.py`'s grid now has the axis.** `LabParams.stop_loss_pct` /
  `take_profit_pct` / `stop_min_hold_sec` / `stop_min_tau_sec` wire straight into `config.exit`
  (`_config_for`), independent of sizing -- `LabParams.from_config()` picks up whatever `config.exit` already
  says (off by default, matching `config.yaml`), and all four are sweepable (`--grid stop_loss_pct=none,10,20`).
  A single `btcbot lab` run can now rank a stop against everything else on the train/test split instead of
  comparing separate runs by hand. New tests: `tests/test_lab.py::TestExitRulesGrid` (baseline inherits
  `config.exit`, values parse/range-check, `expand_grid` sweeps them, `_config_for` actually carries them into
  the replayed config, and an end-to-end replay of a seeded price-drop window closes early with a tight stop
  but not without one) -- each assertion verified to fail against a deliberately broken build first, same
  method as steps 1-3. Still no real sweep has been RUN against recorded data (see
  `docs/research/overnight-handoff.md` item 3); this only makes that sweep possible in one `--grid` call.
- **Not done**: steps 4-5 (execution.py demo backend, live_paper.py wiring, Codex pipeline hookup) are
  untouched; `btcbot demo`'s real order path cannot exit early yet. No real recorded data was used -- all of
  the tests below are synthetic fixtures, same caveat as every other phase before a real run.
- Tests: `tests/test_strategy.py::TestShouldExit`/`TestExit`, `tests/test_paper_broker.py::TestExitOrders`,
  `tests/test_backtest.py::TestStopLoss` (closes early without settlement, off-by-default holds to
  settlement as before, a partial fill leaves the remainder tracked to settlement), `tests/test_config.py`'s
  `exit:` cases, `tests/test_lab.py::TestExitRulesGrid` (added overnight, see above). Full suite: 828 passed,
  offline only, no key used.
