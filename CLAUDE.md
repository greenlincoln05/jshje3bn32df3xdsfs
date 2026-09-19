# btc15m-bot: notes for Claude Code

Paper-first bot for Kalshi's rolling 15-minute BTC markets (`KXBTC15M`). `docs/btc15m-bot-spec.md` is the source of
truth for scope and phases; the README has the phase status and a dated table of verified Kalshi API facts.

## Working rules
- Build one phase at a time. When a phase is done, stop and report (passing tests, a short summary, the commands to run
  it, open questions) and wait for the owner's explicit go-ahead before starting the next one.
- Paper trading is the default, and live trading must be impossible to enable by accident (all four gates in spec
  section 7). There is no order-placing code yet; do not add any before the owner approves Phase 6.
- No martingale, doubling or any size increase after a loss. No secrets in the repo (`.env*`, `*.pem`, `*.key`, key-like `.txt` files and `secrets/` are
  gitignored). No profitability claims without recorded out-of-sample results. No Robinhood scraping and no unofficial
  data sources.
- The placeholder modules (`spot_feed`, `recorder`, `model`, `strategy`, `risk`, `execution`, `paper_broker`,
  `backtest`) hold only a docstring. Replacing one is part of its phase, not of housekeeping.

## Commands
- Tests (offline): `.venv/Scripts/python.exe -m pytest`
- Read-only live check, no credentials needed: `.venv/Scripts/btcbot.exe discover --env prod`

## Conventions
- Prices and contract counts are `Decimal`, never `float`. The client decodes JSON numbers straight to `Decimal`.
- Take prices from the orderbook endpoint, not from the market object: its bid/ask fields lag by several seconds.
- Find the current market with `GET /markets?status=open` and re-check status and times client-side. Do not use
  `GET /events?status=open`: it hides a new window for a full minute after each rollover.
- Kalshi demo and production credentials are separate. The client defaults to demo.
- API tests use `httpx.MockTransport` and the real public payloads in `tests/fixtures/`; they never touch the network.
- Keep console output ASCII so Windows consoles do not raise encoding errors.
