# Repository review and overnight plan assessment

Reviewed 2026-09-19 UTC against main commit `583d171` and Claude's supplied
`overnight-plan.md`. This is a code/document review, not a live strategy validation.
The proposed overnight milestones have not been started by this review.

## Verdict

Phase 1 has a useful foundation: Decimal prices/counts, tested RSA-PSS signing,
fresh signatures on retries, separate environments, no order submission methods,
and public-data fixtures. Phase 2 onward consists of placeholders. There is no
measured predictive edge, realistic fill history, or latency comparison yet.

The overnight plan can produce a plumbing smoke test. Its REST-only data mode
cannot establish market-maker-equivalent speed or credible maker execution.
Prioritize complete, timestamped data capture before strategy breadth.

## Bugs fixed in this review

| Priority | Finding | Fix |
|---|---|---|
| P1 | Discovery captured time before HTTP retries; a market that closed during the request could still be selected. Watch also displayed the pre-request time. | Sample the clock after discovery; reject quotes fetched across close in both commands. Explicit replay time remains supported. |
| P2 | Market listing returned only the first 1,000 markets, losing additional history. | Follow cursors, retain filters, fail on repeated cursors. |
| P2 | Missing/malformed market arrays could look like a successful empty result. Invalid book objects could crash watch with AttributeError/TypeError. Invalid levels could create fictitious liquidity. | Validate containers, pairs, price/size ranges and duplicates; ignore zero-size levels; raise ParseError. |
| P2 | `--watch nan` or infinity passed validation, potentially failing or hanging the watcher. | Require a positive finite interval before entering the command. |

The original suite passed 174 tests. New regressions reproduced 19 failures before
the fixes; the repeated-cursor test replaces the old first-page warning test.
Run `.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider` from the repo root.
Tests are offline. No account credentials or orders were used.
Final validation: **198 tests passed**, with `git diff --check` clean. No live
API smoke test or authenticated feed capture was performed in this review.

## Required changes to Claude's plan

1. **Name two data modes honestly.** Unauthenticated REST polling is a coarse
   research fallback. It misses between-poll changes and is unsuitable for a latency
   claim. The target mode should capture authenticated Kalshi book snapshots/deltas,
   public trades, lifecycle messages, BRTI, and direct Coinbase BTC-USD ticks.
   A fresh key must be provisioned securely before authenticated capture. No key
   should be pasted into chat. The key exposure mentioned in Claude's screenshot
   has not been independently checked here; its revocation status is unknown.

2. **Do not infer executions from net depth or volume changes.** Cancellations,
   new orders and trades are confounded between snapshots. Even observed trades
   plus aggregated depth do not identify every cancellation's queue position.
   Cancellation ahead can advance queue position but cannot itself fill an order.
   Keep actual trade records separate from assumptions. If the trade tape is absent,
   flag maker fills as unvalidated; call alternative runs sensitivity scenarios,
   not mathematically guaranteed PnL bounds. Investigate ticker/time filters,
   pagination, historical cutoffs and active-volume markets before declaring the
   documented public trades endpoint unavailable after an empty response.

3. **Record raw ticks before downsampling.** The one-second rolling buffer is a
   derived model input, not the only stored spot data. Persist source timestamps,
   local UTC receive time, monotonic receive time, session ID, subscription ID,
   sequence, parse status and raw payload. Record HTTP request/response times too.
   Compute p50/p95/p99 source-to-receive delay where clock comparability permits,
   event-loop lag, writer backlog, gaps and stale durations. Track clock offset;
   a timestamp difference alone is not pure network latency. Compare spot changes
   to subsequent Kalshi quote changes, without claiming to observe a market
   maker's private feed or decision time.

4. **Capture the settlement reference.** Subscribe to both BRTI channels when
   available. The 5 Hz channel has raw values and source timestamps; averages live
   on the separate per-second channel. Its trailing average and final-minute
   average have different boundary semantics. Preserve timestamps and window counts,
   and reconcile against finalized market values. Do not replace the official
   settlement average with an average of all 5 Hz messages or a Coinbase candle.

5. **Recover books before trusting them.** After reconnect, sequence gap or a
   malformed delta, mark the affected state invalid and obtain a fresh snapshot.
   Preserve an explicit data-gap record rather than silently skipping the bad row.
   Test subscription acknowledgements, errors, duplicate/out-of-order messages,
   rollover and snapshot/delta interleaving. REST and WebSocket use different book
   field names; the existing REST parser is not a WebSocket parser.

6. **Finish fee accounting.** Six-decimal model-fee rounding alone is incomplete.
   Implement account balance precision, rounding fees, and the per-order accumulator
   and rebates across partial maker/taker fills. Confirm the applicable schedule,
   series/event overrides and account precision. Maker multipliers 0 and 0.25 are
   sensitivity assumptions until verified, not an exhaustive range of possible fees.

7. **Fix replay and evaluation assumptions.** Replay in local availability order;
   source timestamps received later must not leak into earlier decisions. Include
   order-arrival and cancel latency, outstanding-order exposure reservations and
   partial fills. A trade-nothing baseline must reconcile to zero. Hold out later
   windows chronologically and keep all ticks from each contract together for
   uncertainty estimates; thousands of ticks from 36 windows are not thousands of
   independent outcomes. Synthetic scenarios test correctness, not profitability.

8. **Tighten recorder operations.** A stop caused by KILL, disk floor, time limit
   or invalid credentials must not be automatically restarted. Restart transient
   failures only with a bounded policy and the original end time. Count SQLite WAL,
   sidecars and logs toward the disk cap, use a single-writer lock and bounded writer
   queue, flush on shutdown, and log overflow as data loss. Finish collecting pending
   settlements or report them unresolved. Budget every request, including retries
   and settlement checks, under the chosen public-API cap.

9. **Correct the testing claims.** Queue quantity ahead should not increase for an
   unchanged resting order with valid continuous data, but replacement/resnapshot
   can reset its estimate. 'Size never grows after a loss' should prohibit
   loss-dependent requested sizing, not increases in realized fills when depth
   improves. YES/NO symmetry is conditional on complementary inputs and symmetric
   fees, tick grids and rounding. Count mutation checks as coverage evidence,
   never proof that all bugs were found.

## Revised priorities for tonight

1. Incorporate these phase 1 fixes and implement phase 2 capture with deterministic
   reconnect, rollover, staleness, parser, disk and restart tests.
2. Run a short capture, inspect it, then begin the bounded overnight recording.
   If only REST is available, label the dataset coarse throughout every report.
3. While capture runs, build replay and fee accounting with synthetic fixtures;
   then the model, calibration and broker/strategy integration as time permits.
   Prioritize those tests over an arbitrary target of 20 injected mutations.
4. Produce a morning data-quality report first. Report actual captured duration,
   complete windows, settlement coverage, gaps and measured latency percentiles.
   About nine hours falls short of phase 2's 24-hour collection requirement.
   The overnight simulation is a smoke test, not completion of phases 2-4 or
   justification for demo/live orders.

The proposed schedule totals roughly 7-7.5 hours of development, with capture
starting after M1. A full nine-hour recording therefore ends later than a nine-hour
work session; morning reporting must distinguish elapsed work from captured data.

## Primary documentation checked

- [Kalshi orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates): snapshot/delta fields and resnapshot support.
- [BRTI 5 Hz](https://docs.kalshi.com/websockets/cfbenchmarks-value-5hz): raw ticks, timestamps, authentication.
- [BRTI per-second averages](https://docs.kalshi.com/websockets/cfbenchmarks-value): averaging windows and availability checks.
- [Public trades](https://docs.kalshi.com/api-reference/market/get-trades): trade records and pagination.
- [Pagination](https://docs.kalshi.com/getting_started/pagination).
- [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding): rounding components and order accumulator.
- [Coinbase Exchange channels](https://docs.cdp.coinbase.com/exchange/websocket-feed/channels).

These sources document available interfaces, not this account's entitlements,
the current fee coefficient, profitability, or a speed advantage. Those remain
measurements/verification work for subsequent phases.
