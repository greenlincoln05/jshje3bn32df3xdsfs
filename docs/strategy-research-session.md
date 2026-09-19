# BTC strategy research session — September 19, 2026

## Active work and stop time

The user authorized checking the latest repository and continuing historical/public-endpoint testing for the next few hours. No real orders or account credentials are authorized or needed. Profitability is a research objective, not an assumed outcome.

- Latest fetched upstream main: `d0b710d`, includes phases 2–5 (recorder/model/backtest/live paper).
- Isolated worktree: `C:/Users/Lincoln/Documents/ChatGPT/New project/btc-strategy-lab`, branch `codex/strategy-research`.
- Original checkout `../jshje3bn32df3xdsfs` contains uncommitted, different recorder work. Preserve it. Do not pull/reset that checkout.
- Public recorder started at 16:04:45 UTC for three hours; expected finish around 19:04:45 UTC (3:04 PM Eastern). PID is in `data/live-research/capture.pid` (initially 39736). It runs the upstream recorder loaded at process startup, not subsequent source edits. No duplicate recorder is needed.
- A local, read-only replay watcher runs until 19:10 UTC. PID in `data/live-research/replay.pid` (initially 64776). It replays every five minutes into `latest-replay.json` and appends `replay-history.jsonl`. Source SQLite is backed up into memory for a consistent analysis snapshot. The watcher loads code at startup; restart ONLY the watcher if later changes require a new version.
- Heartbeat automation `btc-strategy-research-follow-up` returns to this task every 30 minutes. At/after **19:30 UTC**, write the final findings and disable the heartbeat. Stop only these task-owned processes if still running. Do not extend this session indefinitely.
- The user need only keep the machine awake and the app running for local follow-ups. Process survival and automation timing must be checked rather than assumed.

## User's actual discretionary strategy

Look at 15-minute, 30-minute, one-hour and 24-hour BTC trends; follow the leading YES/NO side around nine minutes before close, paying about 60 cents. Sell around 80 or 99 cents. When price reverses, sometimes hold if much time remains; otherwise switch sides depending on time and trend. This is a trade-management strategy, not simply predicting the final outcome.

## Research already completed

`../btc-research` contains public Kalshi candles/settlements for 3,000 markets from August 18 to September 19, 47,116 Coinbase minute candles, 210 fixed-rule variants, raw trade ledgers, and six regression tests for the candle replay. Seven signal filters × three decision times × two price bands × five exits were screened. One-minute decision/execution lag, ask buys/bid sells, five contracts, fees per leg, and extra one-cent slippage per fill were modeled. See `docs/trend-research-findings.md`.

The chronological periods were 1,000 development windows, 1,000 previously uninspected validation windows, and the 1,000 already examined in the earlier baseline study. ALL these data are now inspected. Do not tune on them and describe the result as a new holdout. The development-only winner failed validation. A different variant was positive in each period after slippage, but was discovered by examining all results; it is exploratory and needs fresh data.

## Frozen fresh-data candidates

All entries require 55–65 cent asks both at signal and at simulated execution, spread <=6 cents, one position of five contracts. Minute numbers are minutes after market open; execution follows one minute later. The data are coarse REST snapshots converted to minute-close quotes, not guaranteed fills.

1. 15-minute trend agrees with leader, decide minute 3, hold to settlement.
2. Leader alone, decide minute 6, target 80 cents.
3. 15-minute trend agrees, decide minute 6, target 80 cents.
4. All four trends agree, decide minute 6, target 80 cents.
5. All four trends agree, decide minute 6, target 99 cents.
6. 15-minute trend agrees, decide minute 6, target 80 cents, at most one late switch: within final five minutes, opposite side bid >=55 cents and five-minute spot direction supports it. The switch executes at the next minute quote, rejects new-side asks >85 cents, and charges sale plus purchase fees.

The watcher also runs the repository's model/maker strategy under optimistic/pessimistic queues and maker fee multipliers 0/0.25. Unsettled windows are excluded from realized trend PnL; missing quote minutes are reported and excluded. Prior-day Coinbase warmup ends before the live capture begins. No depth/queue guarantees are made for candle-based trend results. Model and trend strategies have different execution assumptions and should not be compared as equally executable.

## Fixes currently in this worktree

- Per-trade returns were incorrectly supplied to a per-second EWMA. Added completed receive-time second buckets, elapsed-time variance normalization, 60-second warmup, and warmup reset after >3-second feed gaps, shared by live and replay code.
- Backtest hard-coded `spot_is_stale=False`; now checks last received spot age and volatility readiness.
- New entries in the final minute are blocked until a real observed settlement-window average is supplied. The prior spot fallback was not such an average.
- Pricing predictions during warmup/staleness/final-minute fallback are not logged as calibrated forecasts in live paper.
- Resting-order cancellation takes precedence over holding an existing partial position, so unfilled remainder can be cancelled near close or when pricing inputs are unavailable.
- Recorder code discards an order book received at/after market close (running original capture does not yet load this change; replay independently excludes post-close entries).

Validation: upstream 437 tests passed before changes; **445 tests passed** after changes, including new timing/unit/staleness/partial-order/final-minute regressions. Older synthetic fixtures now include sufficient warmup and ongoing fresh ticks. A Windows temporary-directory permission failure was bypassed with a fresh workspace-local pytest base directory, not by weakening tests.

## Useful commands from this worktree

```powershell
.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp=data/test-run-N --tb=short
.venv/Scripts/python.exe btc-research/watch_capture.py --until 2026-09-19T19:10:00+00:00 --once
Get-Content data/live-research/capture.stderr.log -Tail 20
Get-Content data/live-research/replay.stderr.log -Tail 20
Get-Content data/live-research/latest-replay.json
```

Choose a NEW `test-run-N` directory for each pytest run. Pytest removes an existing base directory, so avoid reusing computed/destructive paths. Do not read or print original checkout credential files.

## Follow-up priorities and limits

1. Check capture/replay health and settled-window counts. At the first watcher check there were 207 books, 2,375 ticks and no finalized windows; optimistic model replay had one unresolved hypothetical position, pessimistic had no fills. These are not profits.
2. Inspect any later differing live/replay behavior. Remaining concerns include tick source timestamp age versus receive-time freshness, time at which settlement/strike information becomes available in offline replay, optimistic queue assumptions from cancellations, and uncertainty from Coinbase/BRTI basis. Do not assume these have been solved.
3. Add unit/regression checks for any substantive new fix. Preserve frozen candidates and retain prior reports rather than silently replacing failed variants.
4. At completion, report actual PnL, fees, drawdown, entries/exits and unresolved trades, plus data quality. Twelve or so fresh windows cannot establish profitability. A no-trade or negative result is valid evidence.

The user explicitly requested milestone commits. Commit each completed, validated milestone on codex/strategy-research, keeping raw capture data and unfinished experiments out of commits. Pricing fixes and research tools are separate milestones. No push or PR has been made; commits are local.
