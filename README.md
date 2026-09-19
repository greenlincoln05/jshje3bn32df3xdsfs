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
| 5 | Live paper run, several days | not started |
| 6 | Demo-environment order/cancel/fill validation | not started |
| 7 | Live, optional, only if phases 4-6 show positive edge after fees | not started |

Phase 1 is read-only: the client has no order-placing methods at all.

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
exists; no order-placing code against Kalshi itself exists anywhere in this repo). `backtest.py` replays a
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

### A note on API keys

No Kalshi API key has been used by any Claude session working on this repo, demo or production. A key that
has been pasted into a chat (this one or another assistant's) should be treated as exposed and revoked/rotated,
regardless of whether it targets demo or prod — the practice that keeps this safe is a fresh key that goes
straight into a local `.env` and is never pasted into a conversation.

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
btcbot calibrate --db data/recorder-....sqlite   # Brier score + reliability table from logged predictions
btcbot backtest --db data/recorder-....sqlite    # replay through strategy + risk + paper broker; PnL etc.
```

`discover` needs no credentials because Kalshi's market data is public. `auth-check` needs `KALSHI_KEY_ID` and
`KALSHI_PRIVATE_KEY_PATH` (copy `.env.example` to `.env`; on Windows write the key path unquoted or with forward
slashes, because a quoted `"C:\temp\..."` turns `\t` into a tab). Demo and production keys are separate and only work
in the environment they were created in. `discover` exits 1 when no market is open (between windows).

`record` also needs no credentials: it polls the same public endpoints plus Coinbase's public WebSocket ticker.
Create a file named `KILL` (or pass `--kill-file`) to stop it early; it also stops itself on a time limit, low
free disk space, a database size cap, or too many consecutive request failures, and none of those restart on
their own. It writes one timestamped SQLite file per run under `--data-dir` (default `./data`, gitignored).

`calibrate` reads predictions a running strategy has logged (via `btcbot.model.log_prediction`) into that same
database, joins them to `record`'s `settlements` table by ticker, and reports a Brier score and a reliability
table for the model, the market mid, and their blend. Nothing currently logs live predictions into a real
database -- `backtest.py` recomputes predictions on the fly during replay without persisting them, and a
loop that would log them continuously is Phase 5 -- so today `calibrate` only has something to report on
synthetic test data.

`backtest` also needs no credentials: it only reads a local recorder database. By default it runs all four
combinations of queue assumption (`optimistic`/`pessimistic`) and maker-fee multiplier (`0`/`0.25`) and
prints one report per combination, since neither is confirmed (see "Verified Kalshi API facts" and
`paper_broker.py`'s module docstring) -- `--queue` and `--maker-fee-multiplier` narrow it to one.

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

## Safety model

| Mode | Target | Money | Status |
|------|--------|-------|--------|
| `record` | prod market data, read-only | none | Phase 2: **built** (REST polling; no key) |
| `paper` (default) | prod data, simulated fills | none | Phase 4: **built** (`backtest`, offline); live-loop pending Phase 5 |
| `demo` | Kalshi demo environment | fake | Phase 6 |
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
  models.py                 Market, Series, OrderBook, Balance: Decimal-only views of API payloads
  kalshi_client.py          RSA-PSS signing (KalshiAuth) + async REST client with retry/backoff
  market_discovery.py       find the open KXBTC15M market: series -> open markets (see the discovery note below)
  cli.py                    discover, auth-check, record, calibrate, backtest
  spot_feed.py              Phase 2: Coinbase public WebSocket ticker -> rolling buffer, staleness, REST fallback
  recorder.py               Phase 2: order books, market state, spot ticks and settlements -> SQLite
  model.py                  Phase 3: v1 fair-probability model, EWMA volatility, prediction logging, calibration
  strategy.py               Phase 4: edge = p_side - price - fee for YES/NO; resting orders only
  risk.py                   Phase 4: every limit in spec section 5, kill switch, size-no-increase-after-a-loss
  execution.py              Phase 4: the paper/live-shared order interface -- only the paper backend exists
  paper_broker.py           Phase 4: fees, tick grid, queue-position fill simulation
  backtest.py               Phase 4: replay a recorder database through all of the above; PnL/win-rate/etc.
tests/
  fixtures/                 real public API payloads and an OpenSSL signing vector
  test_signing.py, test_client.py, test_config.py, test_models.py, test_market_discovery.py, test_cli.py
  test_spot_feed.py, test_recorder.py                    Phase 2, offline (fake WebSocket + fake Kalshi client)
  test_model.py                                          Phase 3, offline (includes hypothesis property tests)
  test_risk.py, test_paper_broker.py, test_strategy.py,
  test_execution.py, test_backtest.py                    Phase 4, offline (synthetic fixtures + hypothesis)
```

`config.py` and `models.py` are additions to the layout in the build spec. Kalshi's own WebSocket (order-book
deltas) is not implemented: it needs an API key even for public channels (see "Verified Kalshi API facts"
below), and no key has been provisioned through a secure channel, so `recorder.py` polls REST instead;
`spot_feed.py`'s WebSocket client is Coinbase's separate, unauthenticated public ticker feed. No order-placing
code against Kalshi exists anywhere in this repo -- `execution.py`'s paper backend only ever calls
`paper_broker.py`.

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
