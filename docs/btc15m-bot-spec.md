# BTC 15-Minute Prediction Market Bot — Build Spec

Hand this file to Claude Code and ask it to build the project described below.
Start with: "Read btc15m-bot-spec.md and implement it phase by phase. Stop after each phase and show me the results."

---

## 1. Goal

Build a Python bot that trades Kalshi's rolling 15-minute Bitcoin up/down binary contracts
(series ticker `KXBTC15M`). These are the same style of contracts shown in Robinhood's
prediction markets hub. I could not confirm a public Robinhood API for placing
prediction-market orders, so this bot talks to **Kalshi's API directly**.

**Non-negotiable defaults**
- Default mode is **paper trading**. Live trading must be impossible to enable by accident.
- No martingale or loss-chasing position sizing. Ever.
- No secrets in the repo. API keys and private keys come from environment variables or a gitignored file.
- This is an experiment. Most retail bots lose money after fees. Build for measuring edge honestly, not for assuming it exists.

## 2. Contract mechanics (verify against current Kalshi docs before coding)

- A new market opens every 15 minutes, 24/7. Tickers look like `KXBTC15M-26SEP181545-45`.
- Resolves **Yes** if the simple average of the 60 seconds of CF Benchmarks' BRTI before the window **close** is >= the simple average of the 60 seconds of BRTI before the window **open**.
- The market exposes a reference price (`floor_strike`) equal to the opening average.
- Settlement pays $1.00 per winning contract, $0 otherwise.
- BRTI is a licensed index. Use a public spot feed (Coinbase, and optionally Kraken/Bitstamp for a median) as a proxy and treat the gap as model error.
- Query by **series -> events -> markets**, never hard-code an event ticker, since events roll every 15 minutes.
- Prices and counts may come back as fixed-point strings (fields like `*_dollars`, `*_fp`). Parse with `Decimal`, not float.

**Claude Code: before writing the client, fetch the current Kalshi API docs and confirm base URLs, endpoints, auth headers, field names, rate limits, and the fee schedule. Do not rely on memory.** Kalshi has both a production and a demo environment; the demo environment must be the default target.

## 3. Architecture

```
btc15m-bot/
  README.md
  pyproject.toml
  .env.example              # KALSHI_KEY_ID, KALSHI_PRIVATE_KEY_PATH, KALSHI_ENV=demo
  .gitignore                # .env, *.pem, data/, logs/
  config.yaml               # all tunables (see section 6)
  src/btcbot/
    kalshi_client.py        # REST + WebSocket, RSA-PSS signing, retries/backoff on 429/5xx
    spot_feed.py            # Coinbase WebSocket ticker, rolling 1s price buffer, staleness detection
    market_discovery.py     # find current open KXBTC15M market, strike, close time, seconds remaining
    model.py                # fair-probability estimator (section 4)
    strategy.py             # edge calc, entry/exit decisions
    risk.py                 # limits, kill switch, position/PnL tracking
    execution.py            # order placement, cancel/replace, fill tracking (paper + live share one interface)
    paper_broker.py         # simulated fills against recorded/live order book
    recorder.py             # write order books + spot ticks + settlements to Parquet/SQLite
    backtest.py             # replay recorded data through strategy + paper broker
    cli.py                  # commands: record, paper, demo, live, backtest, report
  tests/
    test_model.py
    test_risk.py
    test_signing.py
    test_paper_broker.py
```

Use Python 3.11+, `asyncio`, `httpx`, `websockets`, `cryptography` (RSA-PSS), `pydantic` for config, `pandas`/`pyarrow` for data, `pytest`.

## 4. Fair-probability model (v1)

Let:
- `K` = market strike (opening 60s average, from `floor_strike`)
- `S` = current proxy spot (median of available feeds, or last 5s average)
- `tau` = seconds until close
- `sigma` = realized per-second volatility from the last N minutes of 1s log returns (EWMA)

The settlement value is a 60-second average, so it behaves roughly like the price ~30 seconds before close. v1 approximation:

```
tau_eff = max(tau - 30, 1)
d       = ln(S / K) / (sigma * sqrt(tau_eff))
p_yes   = Phi(d)          # standard normal CDF, zero drift
```

Requirements:
- Clamp `p_yes` to [0.02, 0.98].
- When `tau <= 60`, the average is partially locked in. Compute using the seconds of the closing window already observed plus a simulated remainder, instead of the approximation above.
- Add a `blend` parameter: `p = blend * p_model + (1 - blend) * p_market_mid`. Default 0.5. Report calibration for both.
- Log every prediction with inputs so calibration (Brier score, reliability curve) can be computed after settlement.
- Model is a plug-in interface (`predict(state) -> float`) so alternatives (Markov momentum, ML) can be swapped in later.

## 5. Strategy and risk rules

**Entry**
- Compute `edge = p_side - price_paid - expected_fee` for both YES and NO (NO probability = 1 - p_yes).
- Trade only if `edge >= min_edge` (default 0.04) and the book has at least `min_depth` contracts at or better than the intended price.
- Prefer resting limit orders (maker) over crossing the spread. Cancel unfilled orders at `cancel_before_close_sec`.
- Skip a window if: spot feed is stale (> 3s), no market is open, spread is wider than `max_spread`, or `tau` is outside `[min_tau_sec, max_tau_sec]`.

**Exit**
- Default is hold to settlement. Optional take-profit/stop-loss exits are configurable but off by default.

**Risk (all enforced in `risk.py`, checked before every order)**
- `max_contracts_per_trade`, `max_open_exposure_usd`, `max_trades_per_hour`
- `daily_loss_limit_usd`: when hit, bot stops trading until the next UTC day
- `max_consecutive_losses`: pause and require manual restart
- Kill switch: if a file named `KILL` exists in the working directory, cancel all open orders and exit
- Position size is a fixed fraction or fixed contract count. Never increases after a loss.
- On any unhandled exception or auth failure: cancel open orders, log, exit non-zero

## 6. Config (`config.yaml`)

```yaml
mode: paper                 # paper | demo | live
series_ticker: KXBTC15M
spot_feeds: [coinbase]
vol_window_sec: 900
vol_method: ewma
model_blend: 0.5
min_edge: 0.04
min_depth: 10
max_spread: 0.06
min_tau_sec: 30
max_tau_sec: 780
cancel_before_close_sec: 20
sizing:
  contracts_per_trade: 5
risk:
  max_contracts_per_trade: 10
  max_open_exposure_usd: 25
  daily_loss_limit_usd: 20
  max_consecutive_losses: 5
  max_trades_per_hour: 12
```

## 7. Modes

| Mode | Target | Money | How to enable |
|------|--------|-------|---------------|
| `record` | prod market data, read-only | none | `btcbot record` |
| `paper` | prod data, simulated fills | none | default |
| `demo` | Kalshi demo environment | fake | `KALSHI_ENV=demo` |
| `live` | Kalshi prod | real | requires ALL of: `mode: live` in config, `--i-understand-real-money` CLI flag, `KALSHI_ENV=prod`, and an interactive typed confirmation |

Paper fills must model **queue position**: a resting order fills only after the contracts ahead of it at that price level trade or cancel. Do not assume instant fills at the mid. Apply the fee schedule to every simulated fill.

## 8. Build phases (stop and report after each)

1. **Skeleton + client.** Repo scaffold, config loading, Kalshi client with RSA-PSS signing (unit-tested against a known vector), market discovery for the current `KXBTC15M` window. Print strike, close time, and top of book.
2. **Data.** Spot feed and recorder. Run for 24h+ collecting order books, spot ticks, and settlement results.
3. **Model + calibration.** Implement v1 model, produce a calibration report on recorded data (Brier score, reliability plot, comparison vs. market mid).
4. **Backtest + paper broker.** Replay recorded data with queue-aware fills and fees. Report PnL, win rate, max drawdown, trades/day, and edge vs. realized results. Include a clear statement on whether results beat a "trade nothing" baseline after fees.
5. **Live paper.** Run the paper strategy in real time for at least several days. Compare live paper results to the backtest.
6. **Demo environment.** Place real orders against Kalshi's demo environment to validate order/cancel/fill handling.
7. **Live (optional, only if phases 4-6 show positive edge after fees).** Tiny size, all risk limits on, monitoring and alerts.

## 9. Testing and quality

- Unit tests for signing, model math, risk limits, and paper-broker queue logic
- A `--dry-run` flag that runs the full loop and logs intended orders without sending anything
- Structured JSON logs, one line per decision: inputs, `p_model`, `p_market`, edge, action, reason
- Daily `btcbot report` command: PnL, calibration, fill rate, skipped-window reasons

## 10. Explicitly out of scope / do not do

- No martingale, doubling, or size increases after losses
- No scraping Robinhood or reverse-engineering its private endpoints
- No use of leaked or unofficial data sources
- No auto-enabling live mode, and no storing keys in code or logs
- No claims of profitability in docs or README unless supported by recorded out-of-sample results

## 11. Known unknowns to resolve early

- Exact current fee schedule and whether maker fees differ
- Kalshi rate limits and WebSocket channels for order book deltas
- Whether the demo environment lists `KXBTC15M` with realistic liquidity
- How closely a Coinbase-only proxy tracks BRTI around window boundaries (measure it; add Kraken/Bitstamp if the gap is large)
- Kalshi account eligibility and API terms for automated trading (I need to confirm these myself before going live)

## 12. Definition of done (per phase)

Each phase ends with: passing tests, a short written summary of what was built, the commands to run it, and any open questions. Do not proceed to the next phase without my go-ahead.
