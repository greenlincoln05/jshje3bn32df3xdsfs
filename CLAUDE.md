# btc15m-bot: notes for Claude Code

Paper-first bot for Kalshi's rolling 15-minute BTC markets (`KXBTC15M`). `docs/btc15m-bot-spec.md` is the source of
truth for scope and phases; the README has the phase status and a dated table of verified Kalshi API facts.

## Working rules
- Build one phase at a time. When a phase is done, stop and report (passing tests, a short summary, the commands to run
  it, open questions) and wait for the owner's explicit go-ahead before starting the next one.
- Paper trading is the default, and **live** trading must be impossible to enable by accident (all four gates in spec
  section 7). Phase 6 added real order-placing code, but it only ever runs against Kalshi's **demo**
  environment: `kalshi_client.py`'s `create_order`/`cancel_order` carry a hard assertion refusing to sign
  against anything but `KalshiEnv.DEMO`, and nothing points a live backend at prod, because no live backend
  exists at all. Do not add one, or loosen that assertion, before the owner approves Phase 7.
- No martingale, doubling or any size increase after a loss. No secrets in the repo (`.env*`, `*.pem`, `*.key`, key-like `.txt` files and `secrets/` are
  gitignored). No profitability claims without recorded out-of-sample results. No Robinhood scraping and no unofficial
  data sources.
- No placeholder modules remain: `spot_feed`/`recorder` (Phase 2), `model` (Phase 3),
  `strategy`/`risk`/`execution`/`paper_broker`/`backtest` (Phase 4), `live_paper` (Phase 5), and
  `demo_check` (Phase 6) are all implemented. The next new module a phase adds should hold only a docstring
  until that phase actually replaces it, same as these did.
- No API key, demo or production, gets used or written to a repo file by a Claude Code session. A key pasted
  into any chat is treated as exposed; the fix is to revoke/reissue it, never to use it. Phase 2's recorder
  uses public, unauthenticated endpoints only for this reason (Kalshi's order-book WebSocket needs a key even
  for public data). `btcbot paper` (Phase 5) is the same: public data only, no key. `btcbot demo-check`
  (Phase 6c) is different -- it needs a demo key to place real (fake-money) test orders -- so the same rule
  means no Claude Code session has ever run it against real credentials, or ever will: Claude wrote the
  script and its offline tests (a fake client standing in for `KalshiClient`, per `tests/test_demo_check.py`),
  and running it for real, with a real demo key that goes straight into the owner's own `.env` and never into
  a chat, is entirely the owner's to do on their own machine. Phase 6's build itself skipped its own
  prerequisite (real `record`/`paper` data collected and reviewed) at the owner's explicit direction --
  writing and offline-testing the code did not need it -- but that data, plus an actual `demo-check` run,
  still has to happen before trusting any of this or considering Phase 7.
- `execution.py` has a paper backend and a demo backend (Phase 6, `DemoExecutionBackend`). `paper_broker.py`
  never talks to Kalshi; `DemoExecutionBackend` only ever calls `kalshi_client.py`'s demo-gated write
  methods. There is still no *live* order-placing code and no live backend anywhere in this repo; do not add
  one before the owner approves Phase 7 (all four gates, spec section 7).
- `webui.py` (`btcbot dashboard`) is a monitoring tool, not a phase -- it only reads local SQLite databases
  and rewrites three lines of a local `.env`. It binds to `127.0.0.1` only (never `0.0.0.0`) and must stay
  that way. Its Settings tab may write a key the owner enters into their own local `.env`, same as editing
  the file by hand; that is not a Claude Code session using a key (nobody here ever sees the value, since it
  goes from the owner's browser straight to their own disk), and it still doesn't add anywhere for that key
  to place an order -- the same Phase 6 gate applies to any future addition here.

- `stream_recorder.py` (`btcbot stream`) is a READ-ONLY authenticated WebSocket capture of BRTI
  (`cfbenchmarks_value`, `cfbenchmarks_value_5hz`) and order-book deltas. It sends only `subscribe` /
  `unsubscribe` on those market-data channels (a test enforces this) and has no order, cancel or portfolio
  command, so the Phase 6 gate is untouched. Its handshake needs the owner's own key, so Claude writes and
  tests it offline against fake sockets and the owner runs it; a session never uses the key. Message shapes
  come from docs.kalshi.com and are unverified against the live server until the owner's first run.

- `lab.py` (`btcbot lab`, dashboard "Strategy Lab" tab) sweeps entry timing, price bands, trend filters,
  account size and risk sizing over recorded data. It is offline research: no network, no key, no order code.
  Every combination is ranked on a training slice and judged on held-out windows; its verdict must never call
  a configuration profitable (a test checks the wording), because CLAUDE.md's "no profitability claims
  without recorded out-of-sample results" applies to its output. Percent-of-account sizing only ever shrinks
  after a loss, and `risk.py` still enforces "no size increase after a loss" on the ORDERED size.

## Commands
- Tests (offline): `.venv/Scripts/python.exe -m pytest`
- Read-only live check, no credentials needed: `.venv/Scripts/btcbot.exe discover --env prod`
- Record public data (no credentials needed): `.venv/Scripts/btcbot.exe record --env prod --hours 9`
- Live paper trading, no credentials, no real orders (Phase 5): `.venv/Scripts/btcbot.exe paper --env prod --hours 9`
- Calibration report from a recorder database: `.venv/Scripts/btcbot.exe calibrate --db data/recorder-....sqlite`
- Backtest a recorder database: `.venv/Scripts/btcbot.exe backtest --db data/recorder-....sqlite`
- BRTI + order-book stream, READ-ONLY, needs YOUR key in `.env` (run by the owner): `.venv/Scripts/btcbot.exe stream --env prod --hours 9`
- Strategy lab on recorded data (offline): `.venv/Scripts/btcbot.exe lab --grid min_edge=0.02,0.04 --grid max_price=none,0.6`
- Demo-only order validation, needs a demo key (Phase 6, owner runs this, never a Claude Code session): `.venv/Scripts/btcbot.exe demo-check`
- Local monitoring dashboard (binds to 127.0.0.1 only): `.venv/Scripts/btcbot.exe dashboard`
- Testing any of the above against the real network: see `docs/running-live.md` (this session's own
  environment cannot reach Kalshi/Coinbase; that has to happen on the owner's machine).

## Conventions
- Prices and contract counts are `Decimal`, never `float`. The client decodes JSON numbers straight to `Decimal`.
- Take prices from the orderbook endpoint, not from the market object: its bid/ask fields lag by several seconds.
- Find the current market with `GET /markets?status=open` and re-check status and times client-side. Do not use
  `GET /events?status=open`: it hides a new window for a full minute after each rollover.
- Kalshi demo and production credentials are separate. The client defaults to demo.
- API tests use `httpx.MockTransport` and the real public payloads in `tests/fixtures/`; they never touch the network.
- Keep console output ASCII so Windows consoles do not raise encoding errors.
