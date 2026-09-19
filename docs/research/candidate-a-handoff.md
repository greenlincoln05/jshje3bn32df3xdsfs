# Candidate A: handoff for Strategy Lab testing

**For:** Codex (further testing in the Strategy Lab). **From:** the owner and Claude Code, 2026-09-19.
**Repo state this was written against:** `main` at `0f7d721` (PR #22 merged). **Frozen config:** [`candidate-a.config.yaml`](candidate-a.config.yaml).

## 0. The ask, in one paragraph

Candidate A is the Strategy Lab's baseline row: the repo's default `config.yaml` settings, run through `btcbot lab` on the two production paper recordings from 2026-09-19. On the training slice it made **9 trades, 7 wins (78%), +$114.54, t = +1.6**; on the held-out slice **4 trades, 3 wins (75%), +$35.20, t = +0.6**. The owner wants to *refine this exact candidate* and test beyond it. **Your job is to stress-test and improve Candidate A without fooling ourselves**: it is 13 trades in one direction on one afternoon, so the honest starting point is "an interesting hypothesis", not "a strategy that works". Follow the protocol in section 8. Do not change `config.yaml` defaults, and do not make any profitability claim.

## 1. What Candidate A is (exactly)

Candidate A = **the values in `candidate-a.config.yaml`** (a byte-for-byte frozen copy of `config.yaml` at `0f7d721`) **plus the lab's own defaults**, which are part of the definition because they change the result:

| Setting | Value | Where it lives |
|---|---|---|
| Model / blend | `model_blend 0.5` (50% model, 50% market mid), EWMA vol over 900 s | config |
| Edge | `min_edge 0.04` (model P(win) minus bid price minus fee must be at least 4 cents) | config |
| Book quality | `min_depth 10` contracts at the bid, `max_spread 0.06` | config |
| Entry window | between `min_tau_sec 30` and `max_tau_sec 780` seconds left; stale spot or last 60 s: no entry | config |
| Order type | resting maker bid at the best bid, hold to settlement; cancel 20 s before close | `strategy.decide` |
| Sizing in the lab | **minimum order premium = 5% of the starting account** (`LabParams.min_stake_pct = 5`, so $25 on $500), whole contracts rounded up, **this replaces the 5-contract default**; result: 32 to 70 contracts per order | `lab.py` (PR #22) |
| Account and risk (lab defaults) | account **$500**; max at risk at once **25%**; **daily loss stop 10% ($50) per UTC day**; 5 consecutive losses pause; 12 trades/hour; no size increase after a loss | `lab.AccountSettings` |
| Fills | **optimistic** queue model (every drop in a level's size counts as a trade that reached us); maker fee multiplier 0 | lab defaults |
| Split | first 70% of windows train, 1 window skipped, rest test (`--split 0.7`) | lab defaults |
| Price band | **none in the lab.** `config.yaml` has `min_price 0.15 / max_price 0.85`, but `LabParams.from_config` does not read them. No trade in this sample was outside the band, so the numbers are unaffected, but it is a real difference between "the lab's Candidate A" and "what `btcbot paper` runs" | `lab.py` |

### Reproduce the 78% row (expected: `n=9 win 78% pnl $114.54 t +1.6 dd 5.0%` train, `n=4 win 75% pnl $35.20 t +0.6 dd 5.0%` test)

```powershell
cd C:\Users\Lincoln\jshje3bn32df3xdsfs
.\.venv\Scripts\btcbot.exe --config docs\research\candidate-a.config.yaml lab `
  --db data\paper-KXBTC15M-prod-20260919T170425Z.sqlite `
  --db data\paper-KXBTC15M-prod-20260919T175817Z.sqlite `
  --min-train-trades 4 --grid min_edge=0.04
```

A single-value grid means "one combination: the baseline". If you do not get those numbers, stop and tell the owner before testing anything else.

## 2. What the 78% means, and does not

| Slice | Trades | Wins | Win rate | 95% interval on the true win rate | PnL | t |
|---|--:|--:|--:|---|--:|--:|
| Train (14 windows) | 9 | 7 | 78% | **45% to 94%** | +$114.54 | +1.6 |
| Test (5 windows) | 4 | 3 | 75% | **30% to 95%** | +$35.20 | +0.6 |
| Both | 13 | 10 | 77% | **50% to 92%** | +$149.74 | n/a |

- The mean entry price was **0.56**, so the break-even win rate is about **56%**. The intervals above **include** that, so "no edge at all" is not ruled out.
- The lab's own verdict is `insufficient` (an exploratory ranking, because the training minimum is below 20 trades; it also needs 30+ test trades before it will say anything stronger).
- **All 13 trades were NO.** Of the 19 settled windows in the two files, 12 settled NO and 7 YES; BTC fell about $400 over the run. The three losses were all windows that settled YES (the market rose and the candidate had bought NO). A strategy that bets on falling prices does well in a falling market; this sample cannot separate skill from regime.
- Wins are small and losses large at high entry prices: the four entries at 0.70 to 0.85 won 3 of 4 (75%) and netted about zero.

## 3. Data

Production paper recordings, public Kalshi order books polled at 1 Hz plus a Coinbase BTC-USD stand-in for BRTI. **Use only production files.** The lab now rejects any file with `demo` in its name (PR #22), because the demo book is synthetic.

| File | Span (UTC) | Windows | Settled | Bytes | sha256 (first 16) |
|---|---|--:|---|--:|---|
| `data\paper-KXBTC15M-prod-20260919T170425Z.sqlite` | 17:04 to 17:57 | 4 | 3 NO | 20,041,728 | `054f4a9ff72b55ed` |
| `data\paper-KXBTC15M-prod-20260919T175817Z.sqlite` | 17:58 to 21:48 | 17 | 9 NO, 7 YES | 84,299,776 | `79bec26c67e016f8` |

Merged: 20 windows (tickers `KXBTC15M-26SEP191315-15` through `...191800-00`; the ET clock time is in the ticker), 19 settled. **A new recording is running** (`data\paper-KXBTC15M-prod-20260919T234337Z.sqlite`, started 2026-09-19 19:43 local for 24 h, more files will follow). **That data did not exist when Candidate A was defined: treat it as the forward test** (section 8).

## 4. Window by window (so nothing is hidden)

`split` is the lab's time-ordered 70/30 split. "no trade" in windows 11 to 14 is **not** the strategy declining: after the second loss in the training slice (window 10) the **daily loss stop ($50) halted all entries for the rest of that UTC day** (314 refusals in the training slice; `risk_blocked`). This truncates the sample, so lab numbers are dominated by the earliest windows. The test slice starts with a fresh risk state.

| # | window | split | settled | candidate did | size | entry | model P(win) | min left | result | PnL |
|--:|---|---|---|---|--:|--:|--:|--:|---|--:|
| 1 | 91315-15 | train | NO | bought NO | 37 | 0.68 | 0.83 | 9.5 | no | +11.84 |
| 2 | 91330-30 | train | NO | bought NO | 35 | 0.73 | 0.75 | 12.8 | no | +9.45 |
| 3 | 91345-45 | train | NO | bought NO | 66 | 0.38 | 0.41 | 12.7 | no | +40.92 |
| 4 | 91400-00 | train | NO | bought NO | 59 | 0.43 | 0.46 | 10.3 | no | +33.63 |
| 5 | 91415-15 | train | NO | bought NO | 53 | 0.48 | 0.57 | 7.0 | no | +27.56 |
| 6 | 91430-30 | train | YES | no trade | - | - | - | - | - | - |
| 7 | 91445-45 | train | YES | bought NO | 56 | 0.45 | 0.64 | 12.4 | yes | **-25.20** |
| 8 | 91500-00 | train | NO | bought NO | 38 | 0.66 | 0.70 | 11.3 | no | +12.92 |
| 9 | 91515-15 | train | NO | bought NO | 54 | 0.47 | 0.49 | 12.6 | no | +28.62 |
| 10 | 91530-30 | train | YES | bought NO | 60 | 0.42 | 0.42 | 12.2 | yes | **-25.20** |
| 11 | 91545-45 | train | NO | no trade (daily loss stop) | - | - | - | - | - | - |
| 12 | 91600-00 | train | YES | no trade (daily loss stop) | - | - | - | - | - | - |
| 13 | 91615-15 | train | YES | no trade (daily loss stop) | - | - | - | - | - | - |
| 14 | 91630-30 | train | NO | no trade (daily loss stop) | - | - | - | - | - | - |
| 15 | 91645-45 | skipped | YES | (embargo window) | - | - | - | - | - | - |
| 16 | 91700-00 | test | NO | bought NO | 34 | 0.74 | 0.79 | 12.9 | no | +8.84 |
| 17 | 91715-15 | test | NO | bought NO | 70 | 0.36 | 0.64 | 6.5 | no | +44.80 |
| 18 | 91730-30 | test | YES | bought NO | 34 | 0.74 | 0.84 | 8.8 | yes | **-25.16** |
| 19 | 91745-45 | test | NO | bought NO | 32 | 0.79 | 0.84 | 8.6 | no | +6.72 |
| 20 | 91800-00 | test | unsettled | no trade | - | - | - | - | - | - |

Notes: the loss in window 18 came at **84% model confidence**; the model's confidence has not separated winners from losers in this sample.

## 5. What has already been tried (do not repeat without a new reason)

All on the same 20 windows, Codex-version lab, optimistic fills, `--min-train-trades 4` (exploratory). "Train / Test" are `trades, win rate, PnL`. Every test slice has 1 to 4 trades, so **none of these is evidence**.

| Idea | Setting | Train | Test | Read |
|---|---|---|---|---|
| **Candidate A (baseline)** | defaults | 9, 78%, +$114.54 | 4, 75%, +$35.20 | the row to refine |
| Price cap | `max_price=0.65` | 9, 78%, +$125.26 | 3, 67%, +$39.44 | about the same |
| Stricter edge | `min_edge=0.06` | 8, 75%, +$47.58 | 3, 67%, -$4.67 | fewer trades, worse test |
| Looser edge | `min_edge=0.02` | 7, 71%, +$60.51 | 4, 75%, +$56.92 | no clear change |
| Timing | `min_tau_sec=300, max_tau_sec=480` | 8, 75%, +$73.91 | 2, 100%, +$52.39 | 2 test trades |
| Timing | `max_tau_sec=600` | 8, 75%, +$85.80 | 4, 75%, +$31.16 | about the same |
| Trend (fade) | `trend_mode=against` | 9, 78%, +$113.52 | 2, 100%, +$19.32 | 2 test trades |
| Trend (follow) | `trend_mode=with` | 7, 71%, +$73.31 | 4, 75%, +$35.20 | no better |
| Multi-timeframe | `trend_mode=aligned4` | not ranked (under 4 train trades) | - | needs 24 h of prior spot; the files span about 7 h |
| Min chance of winning | `min_p_side=0.6` / `0.7` | **10, 100%**, +$140.67 / +$103.96 | 4, 75%, **+$1.20** | **overfit signature**: perfect in training, gone in test |
| Persistence | `persist_steps=5..20` | mixed | 2 to 3 trades | no clear change |
| Sizing | `risk_pct=1/2/5`, `max_growth_pct=10/25` | same trades, scaled | scaled | a risk dial: profit and drawdown scale together, t unchanged |
| Model weight (older lab: 5 fixed contracts, before PR #22) | `model_blend=0.75` / `1.0` | 14, 71% / 64% | 4, 75% | more trades, lower win rate; not yet rerun under the current lab |

Also measured: over 11,000 logged model probabilities the model was **not more accurate than the market mid** (Brier 0.1517 vs 0.1468, lower is better; `btcbot calibrate --db ...`), so "raise the required edge" does not buy confidence by itself.

## 6. Measurement pitfalls found so far (read before trusting any number)

1. **The daily loss stop truncates the sample.** With the $25 minimum premium, two losses hit the $50 stop and the rest of the UTC day is blocked. Use `--daily-loss-pct 100` *for research runs only* to see the whole sample, and report both. Do not weaken it in `config.yaml`.
2. **The minimum-premium floor changes contract counts with price** (35 contracts at 0.73, 66 at 0.38), which can collide with the "no size increase after a loss" rule. `risk_blocked` counts show it.
3. **Optimistic fills are a best case.** The pessimistic setting never fills, so it bounds nothing. A recorded trade tape would fix this; we do not have one.
4. **Adverse selection is invisible** in a replay: with `model_blend < 1` part of the edge is "mid minus your bid", which is only real if getting filled does not signal the price is about to move against you.
5. REST-polled 1 Hz books and a Coinbase price standing in for BRTI (Kalshi settles on BRTI).
6. The test slice replays with a fresh risk state, so summing train and test is not the same as one continuous run.
7. All trades were NO in a falling market. Anything that looks like skill may be regime.

## 7. Hypotheses to test next (pre-registered, in priority order)

Write each result down as support, no support, or inconclusive. A hypothesis only counts as supported if it holds on data **not used to choose it** with at least **30 resolved test trades**.

| # | Hypothesis | Why | How | Counts as support |
|---|---|---|---|---|
| H1 | A price cap near 0.70 lowers loss size without losing win rate | high-priced entries have small wins and a large loss | `--grid max_price=none,0.85,0.75,0.70,0.65` and report average win vs average loss | lower average loss, not lower total edge, on new data |
| H2 | Entering later (5 to 8 min left) is more accurate | the model has less time to be wrong | `--grid min_tau_sec=120,300 --grid max_tau_sec=480,600` | higher win rate on new data, not just fewer trades |
| H3 | The strategy loses money when the market rises | all three losses were YES-settling windows | segment results by settled direction once YES-heavy days are recorded | a symmetric result, or a filter that avoids rallies |
| H4 | A multi-timeframe trend filter (`aligned4`) helps | Codex's design | needs 24 h of history: run on the new recording | more trades than not, better win rate, on new data |
| H5 | Recalibrating the model (it under-predicts mid-range) beats the 50/50 blend | reliability table shows the model is off in the middle | fit on old windows, test on new (`btcbot calibrate`) | lower Brier than the market mid on new data |
| H6 | Percent sizing with a growth ramp (`risk_pct`, `max_growth_pct`) lowers drawdown | earlier lab result: 10% ramp roughly halved drawdown for ~15% less profit | account-relative limits (`max_open_exposure_pct`, `daily_loss_limit_pct`) | lower drawdown at similar t; it will not raise t |
| H7 | `min_p_side` / `persist_steps` add confidence | earlier lab results | already shows the overfit signature; only worth retesting on new data | must hold on new data with 30+ trades |

## 8. Protocol and rules

**Repo rules (from `CLAUDE.md`; these are hard limits):**
- No API key, demo or production, is ever used or written to a repo file by a Codex or Claude session. The Strategy Lab is offline and needs none.
- No martingale, doubling or any size increase after a loss. Percent sizing only grows after settled wins.
- No profitability claims without recorded out-of-sample results; the lab's verdict text must not call a configuration profitable.
- Demo files are excluded from lab research. Live trading is not implemented and stays that way.
- Every change goes through a PR with tests; do not edit `candidate-a.config.yaml` or the `config.yaml` defaults as part of experiments (copy the file).

**Statistical discipline:**
1. **Freeze Candidate A.** Any variant is a new named candidate (B, C, ...). Keep a running log of every configuration tried (the count matters: the more you try, the more "best" results are luck).
2. **Train on old data, confirm on new data.** The 20 windows above are the *design* set. The new recordings are the *confirmation* set. Do not tune on the confirmation set.
3. Report for every result: trades, wins, win rate **with a 95% interval**, PnL, t, max drawdown, average win, average loss, **and the `risk_blocked` / `too_small` counts**. Always show train, test and the number of combinations tried.
4. Ranking needs `--min-train-trades 20` and a verdict stronger than `insufficient` needs 30+ test trades. Anything lower is exploratory and must be labeled so.
5. Segment by settled direction (NO-settling vs YES-settling windows) and by entry price band before drawing any conclusion.

**Promotion gates (in order, none skipped):** lab result on confirmation data, then forward `btcbot paper` on prod data for several hundred windows, then (only with the owner's approval) `btcbot demo`. No gate implies profitability.

## 9. Forward-test log (fill this in as data arrives)

Re-run the frozen candidate on each new day of recording, with no changes, and append a row.

| Date (ET) | Files | Windows | Trades | Wins | Win rate (95% interval) | PnL | t | `risk_blocked` | NO-settled / YES-settled |
|---|---|--:|--:|--:|---|--:|--:|--:|---|
| 2026-09-19 (design set) | 170425Z, 175817Z | 20 | 13 | 10 | 77% (50% to 92%) | +$149.74 | n/a | 484 | 12 / 7 |

## 10. Code map and commands

| Thing | Where |
|---|---|
| Lab sweep, splits, verdict, filters keys | `src/btcbot/lab.py` (`run_lab`, `LabParams`, `_verdict`) |
| Replay engine, entry filters, sizing in the replay | `src/btcbot/backtest.py` (`prepare_replay`, `replay_prepared`, `EntryFilters`) |
| Entry decision, price band, Kelly, percent sizing | `src/btcbot/strategy.py` (`decide`, `kelly_fraction`, `percent_size`) |
| Risk limits (daily loss stop, no increase after a loss) | `src/btcbot/risk.py`, `config.py` (`RiskLimits`) |
| Calibration report | `btcbot calibrate --db ...` (`src/btcbot/model.py`) |
| Dashboard Strategy Lab tab | `src/btcbot/webui.py` |
| Tests to extend | `tests/test_lab.py`, `tests/test_percent_sizing.py`, `tests/test_backtest.py` |

```powershell
# the whole suite (about 35 s, no network, no key)
.\.venv\Scripts\python.exe -m pytest -q

# a sweep on the design set, full sample (research only; report the default risk stop too)
.\.venv\Scripts\btcbot.exe --config docs\research\candidate-a.config.yaml lab --db <file A> --db <file B> `
  --min-train-trades 4 --daily-loss-pct 100 --grid max_price=none,0.85,0.75,0.70,0.65

# calibration of the model vs the market on one file
.\.venv\Scripts\btcbot.exe calibrate --db data\paper-KXBTC15M-prod-20260919T175817Z.sqlite --bins 5
```

## 11. Open questions for the owner

1. Should the confirmation set be "everything recorded after 2026-09-19 19:43 local", or fixed calendar days?
2. Is a NO-only sample acceptable for promotion, or should we wait for a stretch where BTC rises?
3. Which lab risk defaults should count as "the" Candidate A risk model when the daily loss stop truncates a run: keep 10% of the account, or report both every time (recommended)?
