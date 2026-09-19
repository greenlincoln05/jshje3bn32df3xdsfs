# btc15m-bot

A Python bot for Kalshi's rolling 15-minute Bitcoin up/down contracts (series `KXBTC15M`).

- **Paper trading is the default. Live trading is not implemented yet and cannot be enabled by accident.**
- No martingale, doubling, or loss-chasing sizing, ever.
- No secrets in the repo: keys come from environment variables or a gitignored `.env`.
- This is an experiment. Most retail bots lose money after fees. The goal is to measure whether an edge exists,
  not to assume one, so nothing here claims profitability.

## Status

| Phase | What | State |
|------:|------|-------|
| 1 | Skeleton, Kalshi client (RSA-PSS signing), market discovery | **done** |
| 2 | Spot feed + recorder (24h+ of order books, spot ticks, settlements) | **built and tested; no real capture run yet** |
| 3 | Fair-probability model + calibration report | **built and tested; no real calibration report yet (no captured data)** |
| 4 | Backtest + queue-aware paper broker | **built and tested; no real backtest result yet (no captured data)** |
| 5 | Live paper run, several days | **built and tested; no real live run yet (needs real network access)** |
| 6 | Demo-environment order/cancel/fill validation (`demo-check`) and demo trading (`demo`) | **built and tested offline; no real demo-check or demo run yet (needs a demo key on the owner's machine)** |
| 7 | Live, optional, only if phases 4-6 show positive edge after fees | not started |

Phase 1 shipped a read-only client with no order-placing methods at all; Phase 6 added
`create_order`/`cancel_order`, hard-gated to Kalshi's demo environment only (see the Phase 6 write-up below
and CLAUDE.md) -- there is still no live order-placing code anywhere in this repo.

The [repository review and overnight-plan assessment](docs/review-and-overnight-plan.md)
records phase 1 bug fixes and phase 2 requirements for measuring latency and data quality.
Market listing follows pagination; discovery re-checks time after network calls and refuses
to display a quote fetched across market close.

**Phase 2, as built:** `spot_feed.py` streams Coinbase's public `ticker` channel for BTC-USD into a rolling
buffer with staleness detection and a REST fallback; `recorder.py` polls Kalshi's public REST endpoints
(order book, market list/state, settlement follow-up) into SQLite, with a KILL-file switch, a free-disk
floor, a database size cap, and a repeated-failure stop, none of which restart themselves. `btcbot record`
wires the two together. All 46 new tests are offline (fakes for the WebSocket and the Kalshi client; no
network). Two things are still open, both flagged in the review above rather than worked around:

- **REST-only, not authenticated capture.** Kalshi's order-book-delta WebSocket needs an API key even for
  public data, and no key has been provisioned through a secure channel (see "A note on API keys" below), so
  this is the "coarse" mode the review describes, not full order-flow. Anything computed from this data
  should say so.
- **No real capture run at all yet, not even a short one.** This session runs in an ephemeral remote
  container that can be reclaimed between turns, so it is not a place to leave a 9- or 24-hour background
  recorder running unattended. It also turned out the container's network policy denies outbound access to
  `external-api.kalshi.com` and Coinbase directly (a 403 policy denial, confirmed via the proxy status
  endpoint, not a transient failure), so not even a short live smoke test was possible from here. Validation
  for `record` is the offline test suite only; an actual capture needs to run somewhere with real network
  access, most likely the owner's own machine.

**Phase 3, as built:** `model.py` implements the v1 fair-probability model from spec section 4 (a driftless
lognormal approximation, `Phi(ln(S/K) / (sigma * sqrt(tau_eff)))`, clamped to [0.02, 0.98], with a
continuous, separately-derived treatment for the last 60s once part of the settlement average is already
observed), an EWMA per-second volatility estimator, a market-mid blend, and prediction logging into the
recorder's own SQLite database (a `predictions` table, joinable to `settlements` by ticker). `btcbot
calibrate` computes the Brier score and a reliability table (model vs. market mid vs. blend) from a
database's logged predictions. 51 new offline tests, including `hypothesis` property tests (boundedness,
monotonicity in spot/strike) that caught and fixed a real floating-point overflow bug in the near-zero-tau
edge case. As with Phase 2: **no real calibration report exists yet**, because no real recorded data exists
yet — this is tooling, verified against synthetic fixtures, not a result. The settlement-history study some
earlier planning docs mention is not part of the spec's own Phase 3 definition (section 8.3) and was left
out of this phase's scope; it can be added later if wanted.

**Phase 4, as built:** `paper_broker.py` (Kalshi's quadratic fee formula, the tapered tick grid, and
queue-position fill simulation, bracketed optimistic/pessimistic since Phase 2 records order books, not a
trade tape), `risk.py` (every limit in spec section 5: per-trade and exposure caps, trades/hour, a UTC daily
loss limit, a consecutive-loss pause needing manual `resume()`, the KILL-file check, and an active,
enforced "size cannot increase after a loss" gate, not just an absence of a feature that would do that),
`strategy.py` (edge = p_side - price - expected_fee for YES/NO, resting orders only, hold to settlement),
and `execution.py` (the interface spec section 3 asks be shared by paper and live -- only the paper backend
exists yet; a demo backend arrived in Phase 6, see below, and live still does not exist). `backtest.py` replays a
recorder database through all four end to end, on one simulated clock where volatility carries continuously
across window boundaries the way it would live, and reports PnL, win rate, max drawdown, trades/day, and an
explicit "beats trade-nothing after fees?" line. `btcbot backtest` runs all four
queue-assumption x maker-fee-multiplier combinations by default. 128 new offline tests (fixtures built with
`recorder.py`'s own schema, plus `hypothesis` invariants for the fee formula and the queue's
never-increases/never-negative properties).

**No real backtest result exists yet**, the same root cause as Phases 2 and 3: no real recorded data exists
to replay. Every number this phase can currently produce comes from hand-built synthetic fixtures, not real
market data -- see [docs/running-live.md](docs/running-live.md) for how to actually produce and analyze a
real capture once you're on a machine with real network access.

**Risk-adjusted entry and sizing (added after real demo trading exposed the gap):** a flat `min_edge` is not
a risk-adjusted bar -- the same raw edge is roughly even stakes at a price near 0.5, but a small, capped win
against a much larger loss (or the reverse) near a price of 0 or 1, exactly where the model has never been
calibration-checked (`btcbot calibrate`). `strategy.decide()` now takes an optional `min_price`/`max_price`
band (`BotConfig` defaults to `[0.15, 0.85]` -- a reasoned starting guardrail, not a backtested-optimal
cutoff; tune it with `btcbot lab`) and reports each `Decision`'s Kelly-optimal bankroll fraction
(`kelly_fraction()`: `(p - price) / (1 - price)`, the standard formula for a $1-payout binary bet). Kelly's
own math is most aggressive exactly where a probability estimate is least trustworthy -- for a fixed edge,
the fraction grows without bound as price approaches 1 -- so `sizing.mode: kelly` in `config.yaml` stakes
only a fraction of it (`kelly_fraction_multiplier`, default 0.2) against `risk.max_open_exposure_usd`,
instead of a flat `contracts_per_trade`. Off by default (`sizing.mode: fixed`) for `btcbot backtest`/`btcbot
lab` (which already has its own independent `risk_pct`-based sizing for research); wired into
`live_paper.py`'s decision loop, so `btcbot paper` and `btcbot demo` (which reuses the same loop unchanged)
both pick it up.

**Phase 5, as built:** `live_paper.py`'s `LivePaperTrader` runs the same model/strategy/risk/paper-broker
stack backtest.py replays offline, but driven by data as it arrives rather than from a finished database.
It does not poll a second time: `recorder.py` gained two hook attributes, `on_orderbook` and `on_settlement`
(assigned after construction, not passed to `__init__`, since the trader needs a second connection to the
same database file the recorder just created), and `btcbot paper` wires the trader's
`on_orderbook_snapshot`/`on_settlement` methods into a `Recorder` running against the same public endpoints
`btcbot record` uses -- so a paper run's database has the exact same order-book/spot/settlement tables a
recording does, plus a `predictions` table (`btcbot.model.log_prediction`, now actually populated
continuously instead of only during a backtest replay) and the trades the strategy made. Because settlement
for a window is often still unknown when the next window's first snapshot arrives (per the README's verified
timings, a market finalizes a few seconds after close), a filled position moves to a pending-settlement map
at rollover and is only turned into a resolved trade once `on_settlement` actually fires for that ticker;
anything still pending when the run stops (`shutdown()`, on the kill file or the hour limit) is reported
unresolved rather than guessed at as a win or loss. `backtest.py`'s report builder (`build_report`) was
made public and reused as-is, so `btcbot paper`'s own end-of-run summary and `btcbot backtest --db
<that run's database>` are directly comparable -- spec section 8.5's "compare live paper results to the
backtest." 14 new offline tests (a fake spot buffer and hand-built order-book snapshots driving the trader
directly; no recorder or network involved).

**No real live paper run exists yet**, the same root cause as every prior phase: this session's container
cannot reach Kalshi or Coinbase (confirmed network-policy denial, not a transient failure) and is not a place
to leave a multi-hour or multi-day process running unattended anyway. Running it for real, for the "several
days" the spec asks for, has to happen on the owner's own machine -- see
[docs/running-live.md](docs/running-live.md).

**Phase 6, as built:** its own prerequisite (real `record`/`paper` data collected and reviewed) was
explicitly waived by the owner for writing and offline-testing this phase's code, so this is unreviewed
against real trading results the same way Phases 2-5 were when first built -- see the "not started until
you say so" framing below and in `docs/btc15m-bot-spec.md` section 8.

- **6a** (`kalshi_client.py`): `create_order`, `cancel_order`, `get_order`, `list_orders`, `list_fills`,
  `get_positions`. Every order carries a client-generated `client_order_id` (a fresh UUID unless one is
  passed in) so an application-level retry can't double-place. `create_order`/`cancel_order` -- the only two
  calls that can change real-world state -- carry a hard assertion refusing to sign against anything but
  `KalshiEnv.DEMO` (`KalshiWriteNotAllowedError`); the other four are read-only and, like `get_balance`,
  allowed against either environment. **Writes use Kalshi's V2 endpoints** (`POST`/`DELETE
  /portfolio/events/orders`; the legacy `/portfolio/orders` writes are deprecated), whose side vocabulary is
  YES-only: buying NO at `p` is an `ask` on YES at `1 - p`. Order, fill and position field names (`outcome_side`,
  `count_fp`, `fee_cost`, `position_fp`, ...) were read from docs.kalshi.com on 2026-09-19, **not yet proven
  against a real response**, and required fields raise a `ParseError` instead of defaulting (an earlier version
  defaulted a missing fee to 0, which would have made the fidelity report claim makers pay nothing). The
  owner's first real `demo-check` run is what confirms it, and it reads a NO order back specifically to prove
  the YES/NO mapping.
- **6b** (`execution.py`): `DemoExecutionBackend` implements the existing `ExecutionBackend` protocol
  unchanged (place_resting_order/cancel_order/place_taker_order), so strategy code cannot tell it isn't
  paper. Fills come from polling `list_fills`, deduplicated by fill id, and anything the account already held
  before the backend started is learned first and never re-reported. `reconcile()` cancels every open
  order the account holds (a freshly started process has no legitimate ones yet, so any it finds are by
  definition leftovers from a previous crash) and reports open positions without touching them (this bot
  doesn't exit early; spec: hold to settlement by default).
- **6c** (`btcbot demo-check`, new `demo_check.py`): runs the plan's exact checklist -- auth-check + balance,
  market discovery, a deliberately unfillable resting order then a cancel, a tiny market order expected to
  fill then a position check, an optional settlement check (skipped unless the current window has already
  closed), three rejection tests (an off-grid price, an order costing far more than the account balance, an
  order against an already-settled market -- each one **failing** this check if Kalshi does *not* reject it,
  since an unrejected dangerous order is the actual failure mode being tested for), a small burst of balance
  calls (the 429 backoff math itself is exercised offline in `test_client.py`, not re-tested against a real
  account), and a crash-and-restart reconciliation test (places an order, deliberately doesn't cancel it,
  then builds a fresh `DemoExecutionBackend` and confirms `reconcile()` finds and cancels it). One report row
  per check; exit code is nonzero if anything failed (a skip does not fail the run).
- **6d** (fidelity report, also in `demo_check.py`): compares real fills captured during a `demo-check` run
  against what `paper_broker.py`'s fee formula would have charged for the same trade -- this is what would
  settle whether makers pay a fee under plain `quadratic` (see "Verified Kalshi API facts"). Narrower than
  the full plan (latency and fill-rate need order-book history captured alongside the real fills, which this
  one-shot script doesn't do): fee only, from whatever fill(s) the run's own checklist happens to produce.
- 47 new offline tests (`test_client.py`, `test_execution.py`, `test_demo_check.py`, `test_cli.py`), all
  against `httpx.MockTransport`/hand-written fakes -- no network, no real key, matching every prior phase.

**No real demo-check run exists yet.** Beyond this session's usual network-policy denial, `btcbot demo-check`
specifically needs the owner's own demo API key, which by CLAUDE.md's own rule no Claude Code session may
ever use or store -- this is not a limitation of this particular sandbox, it is permanent. Running it for
real, on the owner's own machine with their own key, is what `docs/running-live.md` now walks through.

### A note on API keys

No Kalshi API key has been used by any Claude session working on this repo, demo or production. A key that
has been pasted into a chat (this one or another assistant's) should be treated as exposed and revoked/rotated,
regardless of whether it targets demo or prod — the practice that keeps this safe is a fresh key that goes
straight into a local `.env` (directly, or via `btcbot dashboard`'s Settings tab, which only ever writes to
that same local file) and is never pasted into a conversation. This applies with extra force to
`btcbot demo-check` (Phase 6): unlike `auth-check`'s single read-only balance call, it places and cancels
real orders with whatever key it's given, so that key has to be the owner's own, entered locally, and run
by the owner -- never a Claude Code session, on this or any future phase.

## Setup

Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Testing against the real network (Kalshi, Coinbase) has to happen on your own machine, not in a Claude Code
remote session -- see [docs/running-live.md](docs/running-live.md) for the checklist.

## Commands

```powershell
btcbot discover --env prod            # strike, close time and top of book for the open window
btcbot discover --env prod --watch 5  # one line every 5 s; the ticker rolls over every 15 minutes
btcbot discover                       # same, against KALSHI_ENV (default: demo)
btcbot auth-check                     # one signed GET /portfolio/balance: proves your key and signing work
btcbot record --env prod --hours 9    # poll public market data + Coinbase spot ticks into ./data/*.sqlite
btcbot paper --env prod --hours 9     # trade the paper strategy against live public data; no real orders
btcbot calibrate --db data/recorder-....sqlite   # Brier score + reliability table from logged predictions
btcbot backtest --db data/recorder-....sqlite    # replay through strategy + risk + paper broker; PnL etc.
btcbot demo-check                     # place/cancel real (fake-money) demo orders; validates order handling
btcbot dashboard                      # local web UI: monitor backtests, live paper PnL/trades, edit .env
```

`discover` needs no credentials because Kalshi's market data is public. `auth-check` needs `KALSHI_KEY_ID` and
`KALSHI_PRIVATE_KEY_PATH` (copy `.env.example` to `.env`; on Windows write the key path unquoted or with forward
slashes, because a quoted `"C:\temp\..."` turns `\t` into a tab). Demo and production keys are separate and only work
in the environment they were created in. `discover` exits 1 when no market is open (between windows).

`record` also needs no credentials: it polls the same public endpoints plus Coinbase's public WebSocket ticker.
Create a file named `KILL` (or pass `--kill-file`) to stop it early; it also stops itself on a time limit, low
free disk space, a database size cap, or too many consecutive request failures, and none of those restart on
their own. It writes one timestamped SQLite file per run under `--data-dir` (default `./data`, gitignored).

`paper` (Phase 5) also needs no credentials: it runs the same recorder polling as `record`, plus the model,
strategy, risk and paper-broker stack from Phase 4, driven live instead of replayed -- so a `paper` run's
database has everything a `record` run's has, plus logged predictions and any simulated trades. It never
places a real order; `--kill-file` (default `./KILL`) cancels any open order and stops the run. Prints its
own end-of-run trading report, in the same shape `backtest` prints, so `btcbot backtest --db <that file>`
gives a directly comparable report from replaying the exact same data (spec section 8.5).

`calibrate` reads predictions logged (via `btcbot.model.log_prediction`) into a database, joins them to
`record`'s `settlements` table by ticker, and reports a Brier score and a reliability table for the model,
the market mid, and their blend. `backtest.py` recomputes predictions on the fly during replay without
persisting them, so a backtest alone still won't give `calibrate` anything new; a `paper` run logs them
continuously as it goes, so `calibrate` has real predictions to score once a `paper` run has settled some
windows. Until then, `calibrate` only has something to report on synthetic test data.

`backtest` also needs no credentials: it only reads a local recorder database. By default it runs all four
combinations of queue assumption (`optimistic`/`pessimistic`) and maker-fee multiplier (`0`/`0.25`) and
prints one report per combination, since neither is confirmed (see "Verified Kalshi API facts" and
`paper_broker.py`'s module docstring) -- `--queue` and `--maker-fee-multiplier` narrow it to one.

`demo-check` (Phase 6) is different from every command above: it needs your own demo `KALSHI_KEY_ID` /
`KALSHI_PRIVATE_KEY_PATH` (same setup as `auth-check`) and it places and cancels real orders -- fake money
only, always against the demo environment, never `--env prod` (there is no such flag for this command).
It runs the checklist from `docs/btc15m-bot-spec.md` section 8 end to end and prints one PASS/FAIL/SKIP row
per check, plus a fidelity comparison of the one real fill's fee against `paper_broker.py`'s fee formula.
Exit code is nonzero if anything failed. **No Claude Code session has ever run this for real** -- see
CLAUDE.md and [docs/running-live.md](docs/running-live.md) for why, and for how to run it yourself.

Example (prices vary):

```text
Environment : prod (read-only market data, no credentials used)
Series      : KXBTC15M - Bitcoin price up down (fifteen_min; fees: quadratic x1)
Market      : KXBTC15M-26SEP182145-45 [active]
Strike      : 81,238.12  (floor_strike, strike_type=greater_or_equal)
Closes      : 2026-09-19 01:45:00 UTC
Remaining   : 3m 39.1s  (219.1 s)
Top of book (dollars per contract; asks are implied from opposite-side bids):
  YES  bid  0.054 x       638.13   ask  0.055 x       972.37   spread 0.001   mid 0.0545
  NO   bid  0.945 x       972.37   ask  0.946 x       638.13   spread 0.001   mid 0.9455
```

## Dashboard (local web UI)

`btcbot dashboard` is a monitoring tool, not one of the numbered build phases: a small local web server
(stdlib `http.server`, no new dependency) that reads what `record`/`paper`/`backtest` already produce.

- **Binds to `127.0.0.1` only** (`--port`, default 8765) -- never reachable from another machine.
- **Live / Paper monitor** tab: picks a `--data-dir` (default `./data`) database and shows trade count, win
  rate, total PnL, unresolved count, a cumulative-PnL chart, and a trades table, refreshing every few
  seconds. This reads a new `trades` table `btcbot paper` now writes to as trades resolve (in addition to
  keeping them in memory for its own end-of-run report), so a `paper` run can be watched live from a second
  process while it's still going, the same way `calibrate` already reads its `predictions` table mid-run.
- **Backtest** tab: runs `btcbot backtest`'s replay against a chosen database and queue/fee combination on
  demand and renders the report, instead of using the CLI.
- **Settings** tab: reads and writes the same local `--env-file` (default `./.env`) every other command
  already reads via `KALSHI_ENV` / `KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH` -- a nicer editor for the file
  `docs/running-live.md` already tells you to edit by hand, nothing more. It never sends a key anywhere
  except from your own browser to this localhost server, which only ever writes it to that file, and it
  always masks the key id on read. **This cannot place, cancel, or modify a Kalshi order, in demo or in
  prod:** this dashboard has no path to `kalshi_client.py`'s order endpoints at all -- entering a key here
  only lets you run `auth-check` or `demo-check` yourself with it, exactly as if you'd edited `.env`
  directly and typed the command. (Phase 6 did add real, demo-only order-placing code elsewhere in this
  repo -- see below -- but this dashboard was never wired to it and stays that way.)

As always: a key pasted into a chat with any assistant is exposed and should be reissued, never reused --
type it into the dashboard's Settings tab (or `.env` directly) instead, on your own machine.

## Safety model

| Mode | Target | Money | Status |
|------|--------|-------|--------|
| `record` | prod market data, read-only | none | Phase 2: **built** (REST polling; no key) |
| `paper` (default) | prod data, simulated fills | none | Phase 4/5: **built** (`backtest` offline, `paper` live); no real live run yet |
| `demo` | Kalshi demo environment | fake | Phase 6: **built** (`demo-check`); no real demo-check run yet -- needs the owner's own demo key |
| `live` | Kalshi prod | real | Phase 7: needs `mode: live`, `--i-understand-real-money`, `KALSHI_ENV=prod` and a typed confirmation |

- The client defaults to the **demo** environment. Production is opt-in per command (`--env prod`) or `KALSHI_ENV=prod`.
- `.env` (and `.env.*` except `.env.example`), `*.pem`, `*.key`, key-like `.txt` files and a `secrets/` folder are gitignored. The API key id is a `SecretStr` and `KalshiAuth` masks itself in
  `repr`. Request headers are never logged.
- Prices and contract counts are `Decimal` end to end. Kalshi sends fixed-point strings, and bare JSON numbers such
  as `floor_strike` are decoded straight to `Decimal`, never through `float`.
- `config.yaml` is validated strictly: unknown keys (typos) and out-of-range values are errors, and a missing file
  is an error rather than a silent fall-back to defaults.

## Layout

```text
config.yaml                 all tunables (spec section 6); credentials and environment are NOT in here
.env.example                KALSHI_ENV, KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH
CLAUDE.md                   working rules and conventions for Claude Code sessions in this repo
docs/
  btc15m-bot-spec.md        the original build spec: source of truth for scope and phases
src/btcbot/
  config.py                 BotConfig (config.yaml) and KalshiSettings (env vars / .env)
  models.py                 Market, Series, OrderBook, Balance, KalshiOrder/KalshiFill/Position (Phase 6): Decimal-only views of API payloads
  kalshi_client.py          RSA-PSS signing (KalshiAuth) + async REST client with retry/backoff; Phase 6 write endpoints (demo-only)
  market_discovery.py       find the open KXBTC15M market: series -> open markets (see the discovery note below)
  cli.py                    discover, auth-check, record, calibrate, backtest, paper, demo-check, dashboard
  spot_feed.py              Phase 2: Coinbase public WebSocket ticker -> rolling buffer, staleness, REST fallback
  recorder.py               Phase 2: order books, market state, spot ticks and settlements -> SQLite
  model.py                  Phase 3: v1 fair-probability model, EWMA volatility, prediction logging, calibration
  strategy.py               Phase 4: edge = p_side - price - fee for YES/NO; resting orders only
  risk.py                   Phase 4: every limit in spec section 5, kill switch, size-no-increase-after-a-loss
  execution.py              Phase 4: paper/demo order interface -- PaperExecutionBackend + DemoExecutionBackend (Phase 6)
  paper_broker.py           Phase 4: fees, tick grid, queue-position fill simulation
  backtest.py               Phase 4: replay a recorder database through all of the above; PnL/win-rate/etc.
  live_paper.py             Phase 5: drives the same stack live via recorder.py's hooks; reuses backtest's report
  demo_check.py             Phase 6: `btcbot demo-check`'s checklist + the paper-vs-demo-fill fidelity report
  webui.py                  not a phase: local dashboard (backtests, live paper PnL/trades, .env settings)
tests/
  fixtures/                 real public API payloads and an OpenSSL signing vector
  test_signing.py, test_client.py, test_config.py, test_models.py, test_market_discovery.py, test_cli.py
  test_spot_feed.py, test_recorder.py                    Phase 2, offline (fake WebSocket + fake Kalshi client)
  test_model.py                                          Phase 3, offline (includes hypothesis property tests)
  test_risk.py, test_paper_broker.py, test_strategy.py,
  test_execution.py, test_backtest.py                    Phase 4, offline (synthetic fixtures + hypothesis)
  test_live_paper.py                                     Phase 5, offline (fake spot buffer + hand-built snapshots)
  test_webui.py                                           dashboard, offline (real HTTP calls to 127.0.0.1 only)
  test_demo_check.py                                     Phase 6, offline (fake KalshiClient; test_client.py covers the new write endpoints directly)
```

`config.py`, `models.py` and `demo_check.py` are additions to the layout in the build spec. Kalshi's own
WebSocket (order-book deltas) is not implemented: it needs an API key even for public channels (see
"Verified Kalshi API facts" below), and no key has been provisioned through a secure channel, so
`recorder.py` polls REST instead; `spot_feed.py`'s WebSocket client is Coinbase's separate, unauthenticated
public ticker feed. **No live order-placing code against Kalshi exists anywhere in this repo.** Phase 6 did
add real order-placing code (`kalshi_client.py`'s `create_order`/`cancel_order`, driven by
`execution.py`'s `DemoExecutionBackend`), but it is hard-gated to Kalshi's demo environment only -- see
CLAUDE.md and the Phase 6 write-up above. `paper_broker.py` itself still never talks to Kalshi at all.

## Verified Kalshi API facts

Checked on 2026-09-18/19 against docs.kalshi.com and the live API. Re-verify before relying on any of it.

| Topic | Finding |
|-------|---------|
| Base URLs | Prod `https://external-api.kalshi.com/trade-api/v2`, demo `https://external-api.demo.kalshi.co/trade-api/v2`. The older `api.elections.kalshi.com` and `demo-api.kalshi.co` hosts still work. |
| Auth | Headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`. Signature is base64 RSA-PSS with SHA-256, MGF1(SHA-256), salt length = digest length, over `timestamp + METHOD + path`. The path starts at the host root (`/trade-api/v2/...`) and has no query string. |
| Public data | Series, events, markets and orderbook work **without credentials** on REST (confirmed live on prod and demo). |
| WebSocket | Prod `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. The handshake **requires authentication even for public channels**, so recording order-book deltas over WebSocket needs an API key. Order books use the `orderbook_delta` channel: an `orderbook_snapshot` first, then deltas whose `seq` numbers let a client detect gaps. |
| Discovery | `GET /markets?series_ticker=KXBTC15M&status=open`; each market carries its `event_ticker`. In a ticker like `KXBTC15M-26SEP182145-45` the time part is the window close in US Eastern time. Markets are pre-created about a day ahead as `initialized` (no strike) and flip to `active`, with `floor_strike` filled in, about 0.7 s after `open_time`. **Do not use `GET /events?status=open`**: at the 02:15Z rollover on 2026-09-19 it first listed the new market 60.0 s after it opened (at 02:00Z it was still stale at 60.7 s), so a watcher saw no market for a minute. Measured at that one boundary: `/markets?status=open` 3.1 s, `/markets` by close-time window (`min_close_ts`/`max_close_ts`, no status) 0.7 s, single-market lookup 0.8 s. `status=open` is also not fully trustworthy on demo (`GET /markets?status=open` returned a long-closed market), so discovery re-checks status and times. |
| Market fields | `floor_strike` (bare JSON number, the opening 60 s average, present from open), `strike_type: greater_or_equal`, `status: active`, UTC `open_time`/`close_time`, prices as `*_dollars` strings, counts as `*_fp` strings with 2 decimals. Counts can be fractional. |
| Tick size | `price_level_structure: tapered_deci_cent`: 0.001 below $0.10 and above $0.90, 0.01 in between. `price_ranges` on each market is the source of truth for valid prices. |
| Orderbook | `GET /markets/{ticker}/orderbook` returns `orderbook_fp.yes_dollars` / `no_dollars`: bids only, `[price, count]`, ascending, best bid last. A NO bid at q is a YES ask at 1-q. **Take prices from the orderbook, never from the market object:** its `yes_bid_dollars` / `yes_ask_dollars` / `*_size_fp` fields lagged the live orderbook by several seconds (frozen for about 8 s while the book moved every second). |
| Rate limits | Token bucket: 10 tokens per request by default; Basic tier 200 read and 100 write tokens/s. A 429 body is `{"error": "too many requests"}` with **no** `Retry-After`; back off exponentially. Limits for unauthenticated calls are not documented. |
| Fees | Series metadata says `fee_type: quadratic`, `fee_multiplier: 1`, and `/series/fee_changes` lists no scheduled changes. Taker fee = 0.07 x contracts x P x (1-P), rounded up to $0.000001 and then to balance precision with per-order rebates; the Fee Rounding doc's worked example (model fee $0.00363825 on $0.055 of revenue) matches 0.07 x 0.055 x 0.945 exactly, i.e. one contract at $0.055; the coefficient is inferred from that example, not read from the fee-schedule PDF, which could not be fetched. **Whether makers pay a fee under plain `quadratic` is not confirmed**; `quadratic_with_maker_fees` is a separate fee type (maker multiplier 0.25). |
| Settlement | Yes if the average of the 60 s of BRTI before close is at least the average of the 60 s before open. $1 per winning contract. Source: CF Benchmarks. A market goes `finalized` about 4.5 s after close, with `result` and `expiration_value` (the settled BRTI average) filled in. |
| Settlement history | Across the 1000 most recent settled windows (from 2026-09-08, via unauthenticated `GET /markets?series_ticker=KXBTC15M&status=settled`, paginated): `expiration_value` of one window equals `floor_strike` of the next in 997 of 997 consecutive pairs, and `result` is `yes` exactly when `expiration_value >= floor_strike` in 1000 of 1000. So the exact BRTI averages are on record for past windows. |
| CF Benchmarks | Kalshi offers BRTI itself through the authenticated WebSocket channel `cfbenchmarks_value` (about 1 Hz, with trailing 60 s and final-minute averages) and a 5 Hz variant. The REST passthrough needs an account entitlement. |
| Lifecycle | `initialized` -> `active` at `open_time` -> `closed` at `close_time` -> `determined` -> `finalized`. Orders, including cancels, are rejected after `close_time`. |
| Demo | Lists `KXBTC15M` with the same tickers and strikes as prod, but thin: in one sampled window demo had 854 contracts traded against 1.6M on prod, and a top of book of bid 0.01 x 1 / ask 0.10 x 17. Good for order-handling tests, not for fill realism. |
