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
| 2 | Spot feed + recorder (24h+ of order books, spot ticks, settlements) | not started |
| 3 | Fair-probability model + calibration report | not started |
| 4 | Backtest + queue-aware paper broker | not started |
| 5 | Live paper run, several days | not started |
| 6 | Demo-environment order/cancel/fill validation | not started |
| 7 | Live, optional, only if phases 4-6 show positive edge after fees | not started |

Phase 1 is read-only: the client has no order-placing methods at all.

## Setup

Python 3.11 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Commands

```powershell
btcbot discover --env prod            # strike, close time and top of book for the open window
btcbot discover --env prod --watch 5  # one line every 5 s; the ticker rolls over every 15 minutes
btcbot discover                       # same, against KALSHI_ENV (default: demo)
btcbot auth-check                     # one signed GET /portfolio/balance: proves your key and signing work
```

`discover` needs no credentials because Kalshi's market data is public. `auth-check` needs `KALSHI_KEY_ID` and
`KALSHI_PRIVATE_KEY_PATH` (copy `.env.example` to `.env`; on Windows write the key path unquoted or with forward
slashes, because a quoted `"C:\temp\..."` turns `\t` into a tab). Demo and production keys are separate and only work
in the environment they were created in. `discover` exits 1 when no market is open (between windows).

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
| `record` | prod market data, read-only | none | Phase 2 |
| `paper` (default) | prod data, simulated fills | none | Phase 4-5 |
| `demo` | Kalshi demo environment | fake | Phase 6 |
| `live` | Kalshi prod | real | Phase 7: needs `mode: live`, `--i-understand-real-money`, `KALSHI_ENV=prod` and a typed confirmation |

- The client defaults to the **demo** environment. Production is opt-in per command (`--env prod`) or `KALSHI_ENV=prod`.
- `.env`, `*.pem` and `*.key` are gitignored. The API key id is a `SecretStr` and `KalshiAuth` masks itself in
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
  cli.py                    discover, auth-check
  spot_feed.py              placeholder, Phase 2: Coinbase spot feed
  recorder.py               placeholder, Phase 2: order books, spot ticks and settlements to disk
  model.py                  placeholder, Phase 3: fair-probability model
  strategy.py               placeholder, Phase 4: edge and entry/exit decisions
  risk.py                   placeholder, Phase 4: limits, kill switch, PnL tracking
  execution.py              placeholder, Phase 4: one order interface for paper and live
  paper_broker.py           placeholder, Phase 4: queue-aware simulated fills
  backtest.py               placeholder, Phase 4: replay recorded data
tests/
  fixtures/                 real public API payloads and an OpenSSL signing vector
  test_signing.py, test_client.py, test_config.py, test_models.py, test_market_discovery.py, test_cli.py
  test_model.py, test_risk.py, test_paper_broker.py     placeholders for Phases 3 and 4
```

Placeholder modules hold only a docstring saying what the build spec asks of them; they contain no behaviour and are
filled in by their phase. `config.py` and `models.py` are additions to the layout in the build spec, and the WebSocket
client will live in `kalshi_client.py` when Phase 2 adds it.

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
