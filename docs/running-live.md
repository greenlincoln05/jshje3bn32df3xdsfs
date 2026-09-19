# Running btc15m-bot against live Kalshi and Coinbase

This is for running the bot's commands against the real internet, **on your own machine**. A Claude Code
remote session cannot do this: its network policy denies outbound access to `external-api.kalshi.com`
directly (confirmed via the proxy status endpoint as a 403 policy denial, not a transient failure), so
everything built so far (Phases 2-5) has only ever been validated offline, against fakes and fixtures. This
doc is the checklist for actually pointing it at the network.

Check the README's Status table first for which phase is current; this doc assumes at least Phase 2
(`record`) exists.

## Setup

```powershell
git clone <this repo>
cd jshje3bn32df3xdsfs
python -m venv .venv
.venv\Scripts\Activate.ps1          # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                           # confirm the offline suite is green before touching the network
```

## 1. Confirm connectivity (no credentials)

```powershell
btcbot discover --env prod
```

Expected: strike, close time and top of book for whatever `KXBTC15M` window is open right now. Exits 1 with
a clear message if you happen to call it during the ~3 s gap between windows -- just run it again.

To watch it live (including a rollover):

```powershell
btcbot discover --env prod --watch 5
```

## 2. Record real data (no credentials)

```powershell
btcbot record --env prod --hours 9
```

- Writes one timestamped SQLite file per run under `./data/` (gitignored).
- Stop it early by creating a file named `KILL` in the project root (or pass `--kill-file PATH`).
- It also stops itself on: the time limit, free disk space under 2 GB, the database passing 1 GB, or 20
  consecutive request failures -- none of these restart on their own. If it stops for a bad reason, fix the
  cause and run it again; the run's summary (printed at the end) says why it stopped and what it captured.
- This is REST polling, not Kalshi's authenticated WebSocket order-book feed (see the README's "Verified
  Kalshi API facts") -- label anything computed from it as coarse, not full order-flow.
- It runs in the foreground until it stops; use `tmux`/`screen`/a background process if you want to close
  the terminal. There is no daemon mode.

## 3. Run live paper trading (no credentials, Phase 5)

```powershell
btcbot paper --env prod --hours 9
```

This is `record` plus the Phase 4 strategy/risk/paper-broker stack, driven live instead of replayed: it
polls the same public endpoints (no credentials, no real orders -- `execution.py`'s paper backend is the
only thing it ever calls), logs every prediction into the database as it goes
(`btcbot.model.log_prediction`), and simulates fills with the same queue-position logic `backtest.py` uses.

- Writes one timestamped SQLite file per run under `./data/` (gitignored), same as `record`.
- `--kill-file` (default `./KILL`) cancels any open (simulated) order and stops the run early; the run also
  stops itself on the `--hours` limit or whatever stops `record` (see above) -- `paper` runs a `Recorder`
  underneath, so the same free-disk/database-size/consecutive-failure stops apply.
- A filled position that is still awaiting settlement when the run stops is reported unresolved, not guessed
  at as a win or loss -- settlement often isn't known yet a few seconds after a window closes.
- Prints its own end-of-run trading report in the same shape `backtest` prints. Spec section 8.5 asks to
  compare a live paper run to the backtest: run `btcbot backtest --db` against this exact same database
  afterward (see step 4) and put the two reports side by side.
- The spec asks for "at least several days" of this. It runs in the foreground until it stops; use
  `tmux`/`screen`/a background process if you want to close the terminal -- there is no daemon mode.

## 4. Analyze what you recorded (offline again, no network needed)

```powershell
btcbot backtest --db data/recorder-KXBTC15M-prod-<timestamp>.sqlite
```

Runs the Phase 4 strategy + paper-broker replay under all four queue-assumption / maker-fee-multiplier
sensitivity combinations and reports PnL, win rate, max drawdown, trades/day, and an explicit "beats
trade-nothing after fees?" line -- always with its sample size stated plainly. A few hours of data is nowhere
near enough to conclude anything; the report says so itself, and CLAUDE.md means it literally: no
profitability claims without recorded out-of-sample results. This works on a database from either `record`
or `paper` -- both have the same order-book/settlement tables underneath.

```powershell
btcbot calibrate --db data/recorder-KXBTC15M-prod-<timestamp>.sqlite
```

This only has something to report once predictions have actually been logged into that database (via
`btcbot.model.log_prediction`). A plain `record` run never logs any -- there is no strategy running -- so
this will say "nothing to score" against one. A `paper` run (step 3) logs predictions continuously as it
goes, so pointing `calibrate` at a `paper` run's database once some windows have settled will have real
predictions to score.

## 4b. Stream BRTI and the live order book (needs your own API key; read-only)

```powershell
btcbot stream --env prod --hours 9
```

Same database layout as `record`, plus `brti_ticks` (BRTI at 1 Hz and 5 Hz, with the trailing 60 s average Kalshi
settles on) and `ws_book_events` (order-book snapshots and deltas over WebSocket instead of one REST poll per
second). Kalshi's WebSocket handshake needs a signed request even for public channels, so put `KALSHI_KEY_ID` and
`KALSHI_PRIVATE_KEY_PATH` in `.env` (the dashboard's Settings tab writes the same file). **It sends only
subscribe/unsubscribe on market-data channels. It cannot place, cancel or modify an order.**

- Prints a status line every 10 s (`--status-every`). Stops on the time limit, the `KILL` file, or a refused
  handshake (bad key, or a demo key used on prod: it does not retry a rejected key).
- At the end it prints how far Coinbase sat from BRTI (mean/median/max absolute difference) and the feed lag, which
  is the number that tells you whether the Coinbase proxy is good enough near the strike.
- The channel names and message shapes come from docs.kalshi.com and have not been exercised against the live
  server. If the first run shows `unrecognised` message types, or 0 BRTI ticks, paste the summary and the
  `run_log` rows (`select * from run_log where level='warning'`); the parser is deliberately loud about this.
- If BRTI needs an entitlement on your account the server will say so in a `ws_error` row.

## 5. Dashboard (optional, no credentials, no network needed)

```powershell
btcbot dashboard
```

A local web UI (open `http://127.0.0.1:8765` in a browser) for browsing whatever `record`/`paper`/`backtest`
have produced under `./data/`, instead of re-running CLI commands by hand: a live-updating view of a `paper`
run's trades and PnL while it's still going, a backtest runner, and a Settings tab for editing the same
`.env` step 6 below describes editing by hand. It binds to `127.0.0.1` only. Unlike every other command on
this page, this one needs no real Kalshi/Coinbase access at all -- it only reads local SQLite files and
rewrites a few lines of a local text file -- so it's the one command here that was already exercised against
a real (if synthetic) database and a real temporary `.env` file inside the Claude Code session that built it,
not just the offline pytest suite. Browsing *real* recorded data with it still needs step 2 or 3 run first.

## 6. `auth-check` (optional -- needs your own credentials)

Only relevant once you want to verify a Kalshi API key and its signing work. **No Claude Code session uses
or stores a Kalshi key, demo or production, ever** -- and a key that has been pasted into a chat with any
assistant should be treated as exposed and revoked, regardless of which environment it targets.

To test a key safely:

1. In your Kalshi account, generate a **new** key (Account & security -> API Keys). Do not paste it into any
   chat, including this one.
2. Copy `.env.example` to `.env` (already gitignored) and fill in `KALSHI_KEY_ID` and
   `KALSHI_PRIVATE_KEY_PATH` directly in that file, on your machine.
   - Windows: write the path unquoted or with forward slashes -- a quoted `"C:\temp\..."` turns `\t` into a
     tab and silently breaks the path.
3. Run:

```powershell
btcbot auth-check
```

Expected: `OK: the demo environment accepted the signed request.` plus your balance. Demo and production
keys are separate: a demo key needs `KALSHI_ENV=demo` (the default), a production key needs `--env prod`.

`auth-check` makes exactly one authenticated call (`GET /portfolio/balance`) and places no orders -- there is
no order-placing code anywhere in this repo yet; see CLAUDE.md's gates on that.

## Safety reminders

- Paper is the only mode with any strategy logic. There is no live or demo order-placing code until Phase 6,
  and CLAUDE.md requires the owner's explicit approval before that phase starts.
- `data/`, `.env`, `*.pem`, `*.key`, and `KILL` are all gitignored. Run `git status` before committing if
  you've been testing in this same checkout, so nothing from a live run ends up in a commit by accident.
- No martingale or size-doubling exists anywhere in this codebase, by design (`risk.py` enforces it).
- `btcbot dashboard` binds to `127.0.0.1` only. Its Settings tab writes straight to your local `.env` --
  fine to use instead of editing the file by hand -- but don't run it with `--port` forwarded or exposed to
  another machine, since anyone who can reach it could read the masked settings status or overwrite `.env`.
