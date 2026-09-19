"""Local monitoring dashboard: backtests, live paper PnL and trades, and local Kalshi settings.

Not one of the numbered build phases (`docs/btc15m-bot-spec.md` section 8) -- a monitoring tool that reads
what those phases already produce. `btcbot dashboard` starts a small HTTP server that binds to
``127.0.0.1`` only, so nothing it serves is reachable from another machine.

The "Settings" panel edits the same local ``.env`` file every other command already reads
(``KALSHI_ENV`` / ``KALSHI_KEY_ID`` / ``KALSHI_PRIVATE_KEY_PATH``, see :mod:`btcbot.config`), from the
owner's own browser to their own disk -- the same file `docs/running-live.md` already tells the owner to
edit by hand for `auth-check`. This module never transmits a key anywhere, never logs one (a POST body
is not part of the request line `BaseHTTPRequestHandler` logs), and a GET of the current settings always
masks the key id. There is still no order-placing code anywhere in this repo (CLAUDE.md's Phase 6 gate):
this dashboard cannot place, cancel, or modify a Kalshi order, in demo or prod, no matter what is entered
in Settings -- it only ever reads local SQLite databases and rewrites three lines of a local text file.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from btcbot.backtest import BacktestError, load_trades, run_backtest
from btcbot.config import ConfigError, load_config
from btcbot.models import ParseError
from btcbot.paper_broker import QueueAssumption

# --------------------------------------------------------------------------- local .env settings

_ENV_KEYS = ("KALSHI_ENV", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH")


def read_env_settings(env_path: Path) -> dict[str, str]:
    """The three Kalshi settings this dashboard edits, straight off disk. Deliberately not read through
    :class:`btcbot.config.KalshiSettings` (pydantic-settings): that loader can't tell us which lines existed
    on disk versus which are defaults, and :func:`write_env_settings` needs that to avoid clobbering the
    rest of the file."""
    values: dict[str, str] = {}
    if not env_path.is_file():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key in _ENV_KEYS:
            values[key] = value.strip().strip('"')
    return values


def settings_status(env_path: Path) -> dict[str, Any]:
    """Never includes the actual key id -- only whether one is set and its last 4 characters, the same
    amount :class:`btcbot.config.KalshiSettings`'s ``SecretStr`` reveals nowhere at all; this is already
    more than the CLI shows, so it stays deliberately short."""
    values = read_env_settings(env_path)
    key_id = values.get("KALSHI_KEY_ID", "")
    return {
        "env_file": str(env_path),
        "env_file_exists": env_path.is_file(),
        "kalshi_env": values.get("KALSHI_ENV") or "demo",
        "key_id_set": bool(key_id),
        "key_id_last4": key_id[-4:] if key_id else "",
        "private_key_path": values.get("KALSHI_PRIVATE_KEY_PATH", ""),
    }


def write_env_settings(env_path: Path, updates: dict[str, str]) -> None:
    """Merges ``updates`` into ``env_path``, preserving every other line (comments, blank lines, unrelated
    variables) and appending any of ``_ENV_KEYS`` not already present. A key absent from ``updates`` is left
    untouched; an empty string clears that key's value but keeps the line."""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if stripped and not stripped.startswith("#") and "=" in stripped else None
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _settings_updates_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    updates: dict[str, str] = {}
    if "kalshi_env" in payload:
        env_value = str(payload["kalshi_env"]).strip().lower()
        if env_value not in ("demo", "prod"):
            raise ValueError("kalshi_env must be 'demo' or 'prod'")
        updates["KALSHI_ENV"] = env_value
    if "key_id" in payload:
        updates["KALSHI_KEY_ID"] = str(payload["key_id"]).strip()
    if "private_key_path" in payload:
        updates["KALSHI_PRIVATE_KEY_PATH"] = str(payload["private_key_path"]).strip()
    if not updates:
        raise ValueError("nothing to update: expected kalshi_env, key_id, and/or private_key_path")
    return updates


# --------------------------------------------------------------------------- database views


def list_databases(data_dir: Path) -> list[dict[str, Any]]:
    if not data_dir.is_dir():
        return []
    entries = []
    for path in sorted(data_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
        name = path.name
        kind = "paper" if name.startswith("paper-") else "recorder" if name.startswith("recorder-") else "unknown"
        stat = path.stat()
        entries.append({"name": name, "kind": kind, "size_bytes": stat.st_size, "modified_ts": stat.st_mtime})
    return entries


def _resolve_db(data_dir: Path, name: str) -> Path | None:
    """Only a bare filename that is actually one of ``data_dir``'s own ``*.sqlite`` files is accepted, so a
    ``db`` query parameter can't be used to read a file outside ``data_dir``."""
    if name != Path(name).name:
        return None
    candidate = data_dir / name
    return candidate if candidate.is_file() and candidate.suffix == ".sqlite" else None


def _safe_trades(conn: sqlite3.Connection) -> list[Any]:
    """A database that has not made a trade yet (or a plain recorder database) has no ``trades`` table."""
    try:
        return load_trades(conn)
    except sqlite3.OperationalError:
        return []


def paper_summary(db_path: Path) -> dict[str, Any]:
    """Live trading state for a database :mod:`btcbot.live_paper` may still be writing: a plain read-write
    handle is fine to open concurrently because ``recorder.py`` runs its database in WAL mode, and this
    function never issues a write of its own."""
    conn = sqlite3.connect(str(db_path))
    try:
        trades = _safe_trades(conn)
        try:
            tickers_seen = {row[0] for row in conn.execute("SELECT DISTINCT ticker FROM orderbook_snapshots")}
            span = conn.execute("SELECT MIN(poll_ts), MAX(poll_ts) FROM orderbook_snapshots").fetchone()
        except sqlite3.OperationalError:
            tickers_seen, span = set(), (None, None)
    finally:
        conn.close()

    windows_traded = {t.ticker for t in trades}
    resolved = [t for t in trades if t.result is not None]
    unresolved = [t for t in trades if t.result is None]
    wins = sum(1 for t in resolved if t.pnl_usd is not None and t.pnl_usd > 0)
    total_pnl = sum((t.pnl_usd for t in resolved if t.pnl_usd is not None), Decimal("0"))

    cumulative: list[dict[str, Any]] = []
    running = Decimal("0")
    for trade in trades:
        if trade.pnl_usd is not None:
            running += trade.pnl_usd
            cumulative.append({"ts": trade.entry_ts, "cumulative_pnl_usd": running})

    return {
        "trade_count": len(trades),
        "resolved_count": len(resolved),
        "unresolved_count": len(unresolved),
        "wins": wins,
        "win_rate": (wins / len(resolved)) if resolved else None,
        "total_pnl_usd": total_pnl,
        "windows_seen": len(tickers_seen),
        "windows_traded": len(windows_traded),
        "first_ts": span[0] if span else None,
        "last_ts": span[1] if span else None,
        "cumulative_pnl": cumulative,
        "trades": [asdict(t) for t in reversed(trades)],  # newest first for the table
    }


def _downsample(rows: list[Any], limit: int) -> list[Any]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)] + [rows[-1]]


def market_view(db_path: Path, ticker: str | None = None) -> dict[str, Any]:
    """Everything the Market tab draws for one window of a recorder/paper database: market metadata, the
    latest order book, spot and YES-mid history, and this window's paper trades. Read-only."""
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            tickers = [r[0] for r in conn.execute(
                "SELECT ticker FROM orderbook_snapshots GROUP BY ticker ORDER BY MAX(id) DESC LIMIT 50")]
        except sqlite3.OperationalError:
            return {"tickers": [], "ticker": None}
        if not tickers:
            return {"tickers": [], "ticker": None}
        if ticker is None or ticker not in tickers:
            ticker = tickers[0]
        meta = conn.execute(
            "SELECT status, strike, open_time, close_time, volume, open_interest FROM market_state "
            "WHERE ticker=? ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        book_row = conn.execute(
            "SELECT poll_ts, book_json FROM orderbook_snapshots WHERE ticker=? ORDER BY id DESC LIMIT 1",
            (ticker,)).fetchone()
        book = json.loads(book_row[1]) if book_row else {"yes": [], "no": []}
        mids = conn.execute(
            "SELECT poll_ts, yes_bid_price, yes_ask_price FROM orderbook_snapshots WHERE ticker=? ORDER BY id",
            (ticker,)).fetchall()
        first_ts = mids[0][0] if mids else None
        spot = conn.execute(
            "SELECT receive_ts, price FROM spot_ticks WHERE receive_ts>=? ORDER BY id",
            (first_ts or "",)).fetchall() if first_ts else []
        latest_spot = conn.execute("SELECT price, receive_ts FROM spot_ticks ORDER BY id DESC LIMIT 1").fetchone()
        settlement = conn.execute(
            "SELECT result, settled_avg FROM settlements WHERE ticker=?", (ticker,)).fetchone()
        trades = [asdict(t) for t in reversed(_safe_trades(conn)) if t.ticker == ticker]
    finally:
        conn.close()
    return {
        "tickers": tickers, "ticker": ticker,
        "status": meta[0] if meta else None, "strike": meta[1] if meta else None,
        "open_time": meta[2] if meta else None, "close_time": meta[3] if meta else None,
        "volume": meta[4] if meta else None, "open_interest": meta[5] if meta else None,
        "book": book, "book_ts": book_row[0] if book_row else None,
        "mid_series": _downsample([[r[0], r[1], r[2]] for r in mids], 400),
        "spot_series": _downsample([[r[0], r[1]] for r in spot], 400),
        "spot": latest_spot[0] if latest_spot else None, "spot_ts": latest_spot[1] if latest_spot else None,
        "settlement": {"result": settlement[0], "settled_avg": settlement[1]} if settlement else None,
        "trades": trades,
    }


def backtest_reports(db_path: Path, config_path: Path, *, queue: str, maker_fee_multiplier: str) -> list[dict[str, Any]]:
    config = load_config(str(config_path))
    queues = (
        (QueueAssumption.OPTIMISTIC, QueueAssumption.PESSIMISTIC) if queue == "both" else (QueueAssumption(queue),)
    )
    multipliers = (Decimal("0"), Decimal("0.25")) if maker_fee_multiplier == "both" else (Decimal(maker_fee_multiplier),)
    conn = sqlite3.connect(str(db_path))
    try:
        reports = [
            run_backtest(conn, config, queue_assumption=q, maker_fee_multiplier=m) for q in queues for m in multipliers
        ]
    finally:
        conn.close()
    return [asdict(report) for report in reports]


# --------------------------------------------------------------------------- JSON plumbing


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


def _dump(payload: Any) -> bytes:
    return json.dumps(payload, default=_json_default).encode("utf-8")


class _ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- HTTP handler


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ThreadingHTTPServer  # narrows the type of self.server for the attributes set below

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 -- stdlib's own parameter name
        pass  # the request line alone (method + path) is unremarkable for a single-user localhost tool

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        self._send_bytes(status, _dump(payload), "application/json; charset=utf-8")

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _ApiError(400, f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise _ApiError(400, "request body must be a JSON object")
        return data

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming convention
        parsed = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/":
                self._send_bytes(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/databases":
                self._send_json(200, {"databases": list_databases(self.server.data_dir)})
            elif parsed.path == "/api/paper_summary":
                self._send_json(200, self._paper_summary(query))
            elif parsed.path == "/api/market":
                db_path = self._require_db(query)
                try:
                    self._send_json(200, market_view(db_path, query.get("ticker")))
                except (sqlite3.OperationalError, ParseError, ValueError) as exc:
                    raise _ApiError(400, f"market view error: {exc}") from exc
            elif parsed.path == "/api/backtest":
                self._send_json(200, self._backtest(query))
            elif parsed.path == "/api/settings":
                self._send_json(200, settings_status(self.server.env_path))
            else:
                self._send_json(404, {"error": f"no such endpoint: {parsed.path}"})
        except _ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # a JSON 500 beats a hung connection; this is a request boundary
            self._send_json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/settings":
                payload = self._read_json_body()
                try:
                    updates = _settings_updates_from_payload(payload)
                except ValueError as exc:
                    raise _ApiError(400, str(exc)) from exc
                write_env_settings(self.server.env_path, updates)
                self._send_json(200, settings_status(self.server.env_path))
            else:
                self._send_json(404, {"error": f"no such endpoint: {parsed.path}"})
        except _ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # a JSON 500 beats a hung connection; this is a request boundary
            self._send_json(500, {"error": str(exc)})

    def _paper_summary(self, query: dict[str, str]) -> dict[str, Any]:
        db_path = self._require_db(query)
        return paper_summary(db_path)

    def _backtest(self, query: dict[str, str]) -> dict[str, Any]:
        db_path = self._require_db(query)
        queue = query.get("queue", "both")
        if queue not in ("optimistic", "pessimistic", "both"):
            raise _ApiError(400, "queue must be 'optimistic', 'pessimistic', or 'both'")
        maker_fee_multiplier = query.get("maker_fee_multiplier", "both")
        if maker_fee_multiplier != "both":
            try:
                if Decimal(maker_fee_multiplier) < 0:
                    raise _ApiError(400, "maker_fee_multiplier must be 'both' or a non-negative number")
            except InvalidOperation as exc:
                raise _ApiError(400, "maker_fee_multiplier must be 'both' or a non-negative number") from exc
        try:
            reports = backtest_reports(
                db_path, self.server.config_path, queue=queue, maker_fee_multiplier=maker_fee_multiplier
            )
        except ConfigError as exc:
            raise _ApiError(400, f"config error: {exc}") from exc
        except (BacktestError, sqlite3.OperationalError, ParseError) as exc:
            raise _ApiError(400, f"backtest error: {exc}") from exc
        return {"reports": reports}

    def _require_db(self, query: dict[str, str]) -> Path:
        name = query.get("db")
        if not name:
            raise _ApiError(400, "missing required query parameter: db")
        db_path = _resolve_db(self.server.data_dir, name)
        if db_path is None:
            raise _ApiError(404, f"no such database in {self.server.data_dir}: {name}")
        return db_path


def create_dashboard_server(*, data_dir: Path, env_path: Path, config_path: Path, port: int) -> ThreadingHTTPServer:
    """Binds to ``127.0.0.1`` only -- never ``0.0.0.0`` -- so the dashboard is reachable only from this
    machine. ``port=0`` lets the OS pick a free port (used by tests); the bound port is on
    ``server.server_address[1]`` either way."""
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    server.data_dir = data_dir  # type: ignore[attr-defined]
    server.env_path = env_path  # type: ignore[attr-defined]
    server.config_path = config_path  # type: ignore[attr-defined]
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------- frontend

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>btc15m-bot dashboard</title>
<style>
  :root { color-scheme: dark; --bg:#07090b; --panel:#0e1114; --panel2:#12161a; --line:#1c2126; --text:#e9edf0;
          --muted:#7d8791; --green:#2ee6a6; --greenbg:#0f2a22; --red:#ff5a6a; --redbg:#2a1216; --orange:#ff9b3d;
          --blue:#3b82f6; }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); margin: 0; padding: 20px;
         font: 13px/1.45 Inter, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; }
  .shell { max-width: 1240px; margin: 0 auto; background: var(--panel); border: 1px solid var(--line);
           border-radius: 14px; overflow: hidden; }
  .top { display: flex; gap: 14px; align-items: center; padding: 14px 18px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .coin { width: 32px; height: 32px; border-radius: 50%; background: var(--orange); color: #fff; display: grid;
          place-items: center; font-weight: 700; flex: none; }
  .title { font-weight: 700; font-size: 15px; }
  .sub { color: var(--muted); font-size: 12px; }
  .sub b { color: var(--text); font-weight: 600; }
  .grow { flex: 1; }
  select, input { background: var(--panel2); color: var(--text); border: 1px solid var(--line); border-radius: 8px;
                  padding: 7px 10px; font: inherit; max-width: 100%; }
  button { font: inherit; }
  button.action { background: var(--panel2); color: var(--text); border: 1px solid var(--line); border-radius: 8px;
                  padding: 7px 12px; cursor: pointer; }
  button.action:hover { border-color: var(--green); }
  button.action:disabled { opacity: .5; cursor: wait; }
  nav { display: flex; gap: 4px; padding: 0 18px; border-bottom: 1px solid var(--line); }
  nav button { background: none; border: 0; border-bottom: 2px solid transparent; color: var(--muted);
               padding: 11px 14px; cursor: pointer; font-weight: 600; }
  nav button.active { color: var(--text); border-bottom-color: var(--green); }
  .stats { display: flex; gap: 26px; padding: 10px 18px; border-bottom: 1px solid var(--line); flex-wrap: wrap; font-size: 11px;
           color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .stats b { color: var(--text); font-size: 13px; margin-left: 4px; letter-spacing: 0; }
  section { display: none; } section.active { display: block; }
  .chartwrap { padding: 8px 18px 0; }
  canvas { width: 100%; display: block; }
  .cols { display: grid; grid-template-columns: 1.05fr 1.15fr 1fr; border-top: 1px solid var(--line); }
  .col { padding: 14px 16px; border-right: 1px solid var(--line); min-width: 0; }
  .col:last-child { border-right: 0; }
  @media (max-width: 960px) { .cols { grid-template-columns: 1fr; } .col { border-right: 0; border-bottom: 1px solid var(--line); } }
  .facts { display: flex; gap: 22px; margin-bottom: 8px; flex-wrap: wrap; }
  .fact .k { color: var(--muted); font-size: 11px; } .fact .v { font-weight: 600; font-size: 14px; }
  .orange { color: var(--orange); } .green { color: var(--green); } .red { color: var(--red); }
  .toggle { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .toggle button { background: none; border: 0; color: var(--muted); padding: 5px 14px; cursor: pointer; font-weight: 700; font-size: 11px; }
  .toggle button.on { background: var(--greenbg); color: var(--green); }
  .toggle.no button.on { background: var(--redbg); color: var(--red); }
  .ladder-head, .lrow { display: grid; grid-template-columns: 60px 1fr 1fr; padding: 3px 8px; font-variant-numeric: tabular-nums; }
  .ladder-head { color: var(--muted); font-size: 11px; margin-top: 10px; }
  .ladder-head span:nth-child(n+2), .lrow span:nth-child(n+2) { text-align: right; }
  .lrow { position: relative; }
  .lrow .bar { position: absolute; right: 0; top: 0; bottom: 0; opacity: .22; }
  .lrow span { position: relative; }
  .lrow.ask .p { color: var(--red); } .lrow.ask .bar { background: var(--red); }
  .lrow.bid .p { color: var(--green); } .lrow.bid .bar { background: var(--green); }
  .mid { display: flex; justify-content: space-between; align-items: baseline; padding: 6px 8px; margin: 3px 0;
         border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
  .mid .big { font-size: 18px; font-weight: 700; } .mid .sp { color: var(--muted); font-size: 11px; }
  .quote { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 0 0 10px; }
  .quote div { text-align: center; padding: 8px; border-radius: 8px; font-weight: 700; }
  .quote .y { background: var(--greenbg); color: var(--green); border: 1px solid #1b5a45; }
  .quote .n { background: var(--redbg); color: var(--red); border: 1px solid #5a1f28; }
  h3 { margin: 14px 0 6px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .tiles { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 18px; }
  .tile { background: var(--panel2); border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; min-width: 120px; }
  .tile .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  .tile .value { font-size: 19px; margin-top: 3px; font-weight: 600; }
  .pnl-pos { color: var(--green); } .pnl-neg { color: var(--red); }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; }
  .pad { padding: 14px 18px; }
  .row { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
  .note { color: var(--muted); font-size: 12px; margin-top: 8px; }
  .error { color: var(--red); margin-top: 8px; } .ok { color: var(--green); margin-top: 8px; }
  .empty { color: var(--muted); text-align: center; padding: 20px 10px; }
  .warn { background: #2a1d10; border: 1px solid #6b4a1c; color: #fbbf77; border-radius: 8px; padding: 10px 14px; margin: 10px 0; display: none; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 99px; border: 1px solid var(--line); color: var(--muted); }
  .badge.live { color: var(--green); border-color: #1b5a45; } .badge.closed { color: var(--orange); border-color: #6b4a1c; }
  .foot { padding: 10px 18px; border-top: 1px solid var(--line); color: var(--muted); font-size: 11.5px; }
  @media (max-width: 700px) { body { padding: 8px; } table { display: block; overflow-x: auto; } }
</style>
</head>
<body>
<div class="shell">
  <div class="top">
    <div class="coin">&#8383;</div>
    <div>
      <div class="title" id="hdr-title">BTC 15 min</div>
      <div class="sub" id="hdr-sub">No data loaded</div>
    </div>
    <span class="badge" id="hdr-badge"></span>
    <div class="grow"></div>
    <div class="row" id="db-row" style="margin:0">
      <select id="db-select" title="Database"></select>
      <select id="ticker-select" title="Market window"></select>
      <button class="action" id="refresh-databases">Refresh</button>
    </div>
  </div>
  <nav>
    <button data-tab="market" class="active">Market</button>
    <button data-tab="monitor">Paper PnL</button>
    <button data-tab="backtest">Backtest</button>
    <button data-tab="settings">Settings</button>
  </nav>

  <section id="tab-market" class="active">
    <div class="stats">
      <span>Vol <b id="st-vol">--</b></span><span>Open int <b id="st-oi">--</b></span>
      <span>Spread <b id="st-spread">--</b></span><span>Time left <b id="st-left">--</b></span>
      <span>Snapshot <b id="st-ts">--</b></span>
    </div>
    <div class="chartwrap"><canvas id="mid-chart" height="190"></canvas></div>
    <div class="cols">
      <div class="col">
        <div class="facts">
          <div class="fact"><div class="k">Expiration</div><div class="v orange" id="f-exp">--</div></div>
          <div class="fact"><div class="k">To beat</div><div class="v" id="f-strike">--</div></div>
          <div class="fact"><div class="k">Current price</div><div class="v" id="f-spot">--</div></div>
        </div>
        <canvas id="spot-chart" height="210"></canvas>
        <div class="note" id="spot-note"></div>
      </div>
      <div class="col">
        <div class="row" style="justify-content:space-between;margin-bottom:0">
          <span class="sub">Order book</span>
          <div class="toggle" id="side-toggle"><button data-side="yes" class="on">YES</button><button data-side="no">NO</button></div>
        </div>
        <div class="ladder-head"><span>Price</span><span>Contracts</span><span>Total</span></div>
        <div id="asks"></div>
        <div class="mid"><span class="big" id="mid-price">--</span><span class="sp" id="mid-spread"></span></div>
        <div id="bids"></div>
        <div class="note" id="market-note"></div>
      </div>
      <div class="col">
        <div class="quote"><div class="y" id="q-yes">Yes --</div><div class="n" id="q-no">No --</div></div>
        <h3>Paper trades, this window</h3>
        <table id="win-trades"><thead><tr><th>Side</th><th>Size</th><th>Entry</th><th>Result</th><th>PnL</th></tr></thead><tbody></tbody></table>
        <h3>Settlement</h3>
        <div id="settle" class="sub">Not settled yet.</div>
        <div class="note">Read-only view of recorded data. There is no order ticket: nothing here can place, cancel or modify a Kalshi order.</div>
      </div>
    </div>
  </section>

  <section id="tab-monitor">
    <div class="tiles" id="summary-tiles"></div>
    <div class="chartwrap"><canvas id="pnl-chart" height="220"></canvas></div>
    <div class="pad"><table id="trades-table">
      <thead><tr><th>Ticker</th><th>Side</th><th>Size</th><th>Entry price</th><th>Entry time (UTC)</th><th>Result</th><th>PnL (USD)</th></tr></thead>
      <tbody></tbody></table>
      <div class="note" id="monitor-note"></div></div>
  </section>

  <section id="tab-backtest">
    <div class="pad">
      <div class="row">
        <label for="queue-select">Queue assumption</label>
        <select id="queue-select"><option value="both">both</option><option value="optimistic">optimistic</option><option value="pessimistic">pessimistic</option></select>
        <label for="fee-input">Maker fee multiplier</label>
        <select id="fee-input"><option value="both">both (0 and 0.25)</option><option value="0">0 (makers pay no fee)</option><option value="1">1 (makers pay the full fee)</option></select>
        <button class="action" id="run-backtest">Run backtest</button>
      </div>
      <table id="backtest-table">
        <thead><tr><th>Queue</th><th>Fee mult.</th><th>Trades</th><th>Win rate</th><th>Total PnL</th><th>Max drawdown</th><th>Trades/day</th><th>Beats trade-nothing?</th><th>Sample size</th></tr></thead>
        <tbody></tbody></table>
      <div class="note" id="backtest-note"></div>
    </div>
  </section>

  <section id="tab-settings">
    <div class="pad">
      <p class="note">Stored only in the local settings file shown below, from your browser to this localhost server to that file. A key pasted anywhere else (a chat, an issue, a screenshot) should be treated as exposed and reissued.</p>
      <div class="row">
        <label><input type="radio" name="kalshi-env" value="demo"> Demo</label>
        <label><input type="radio" name="kalshi-env" value="prod"> Live (prod)</label>
      </div>
      <div class="row"><label for="key-id-input">Key ID</label><input id="key-id-input" type="password" placeholder="leave blank to keep current value" size="36"></div>
      <div class="row"><label for="key-path-input">Private key path</label><input id="key-path-input" placeholder="/path/to/key.pem" size="36"></div>
      <div class="warn" id="prod-warn">Live (prod) is selected. Nothing in this repo can place orders yet, but keep this on Demo until Phase 6 is done and you mean it.</div>
      <div class="row"><button class="action" id="save-settings">Save</button></div>
      <div id="settings-status"></div>
    </div>
  </section>

  <div class="foot">Local only: this page and everything it fetches stays on this machine. Read-only monitoring plus a local settings editor.</div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const $q = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function fmtUsd(value) {
  if (value === null || value === undefined) return "--";
  const n = Number(value);
  return `<span class="${n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : ""}">${n < 0 ? "-" : ""}$${Math.abs(n).toFixed(2)}</span>`;
}
const fmtPct = (v) => (v === null || v === undefined ? "--" : (Number(v) * 100).toFixed(1) + "%");
const cents = (p) => (p === null || p === undefined || p === "" ? "--" : Number((Number(p) * 100).toFixed(1)) + "¢");
const num = (v) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });

async function getJson(url) {
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function drawSeries(canvas, points, opts) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = Number(canvas.getAttribute("height"));
  canvas.width = w * dpr; canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr); ctx.clearRect(0, 0, w, h);
  ctx.font = "11px sans-serif";
  if (points.length < 2) {
    ctx.fillStyle = css("--muted"); ctx.fillText(opts.empty || "Not enough data yet.", 10, 22); return;
  }
  const padL = 6, padR = 60, padT = 10, padB = 22;
  const xs = points.map((p) => p[0]), ys = points.map((p) => p[1]);
  let lo = Math.min(...ys, ...(opts.include || [])), hi = Math.max(...ys, ...(opts.include || []));
  if (opts.min !== undefined) lo = Math.min(lo, opts.min);
  if (opts.max !== undefined) hi = Math.max(hi, opts.max);
  if (hi === lo) { hi += 1; lo -= 1; }
  const x0 = xs[0], x1 = xs[xs.length - 1] === x0 ? x0 + 1 : xs[xs.length - 1];
  const X = (x) => padL + ((x - x0) / (x1 - x0)) * (w - padL - padR);
  const Y = (y) => padT + (1 - (y - lo) / (hi - lo)) * (h - padT - padB);
  ctx.strokeStyle = css("--line"); ctx.fillStyle = css("--muted"); ctx.lineWidth = 1;
  for (let i = 0; i <= 3; i++) {
    const y = lo + ((hi - lo) * i) / 3;
    ctx.beginPath(); ctx.moveTo(padL, Y(y)); ctx.lineTo(w - padR, Y(y)); ctx.stroke();
    ctx.fillText(opts.yfmt(y), w - padR + 6, Y(y) + 4);
  }
  const xTicks = Math.max(1, Math.min(3, Math.floor((w - padL - padR) / 95)));
  for (let i = 0; i <= xTicks; i++) {
    const x = x0 + ((x1 - x0) * i) / xTicks;
    const lbl = new Date(x).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: opts.seconds ? "2-digit" : undefined });
    ctx.fillText(lbl, Math.max(padL, Math.min(X(x) - 22, w - padR - 58)), h - 6);
  }
  (opts.lines || []).forEach((l) => {
    ctx.setLineDash([2, 3]); ctx.strokeStyle = l.color; ctx.beginPath();
    ctx.moveTo(padL, Y(l.y)); ctx.lineTo(w - padR, Y(l.y)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = l.color; ctx.fillText(l.label, padL + 4, Y(l.y) - 4);
  });
  const path = () => {
    ctx.beginPath();
    points.forEach((p, i) => {
      const x = X(p[0]), y = Y(p[1]);
      if (i === 0) ctx.moveTo(x, y);
      else if (opts.step) { ctx.lineTo(x, Y(points[i - 1][1])); ctx.lineTo(x, y); } else ctx.lineTo(x, y);
    });
  };
  if (opts.fill) {
    path(); ctx.lineTo(X(x1), h - padB); ctx.lineTo(X(x0), h - padB); ctx.closePath();
    const g = ctx.createLinearGradient(0, padT, 0, h - padB);
    g.addColorStop(0, opts.color + "66"); g.addColorStop(1, opts.color + "00");
    ctx.fillStyle = g; ctx.fill();
  }
  path(); ctx.strokeStyle = opts.color; ctx.lineWidth = 2; ctx.stroke();
  const last = points[points.length - 1];
  ctx.fillStyle = opts.color; ctx.beginPath(); ctx.arc(X(last[0]), Y(last[1]), 3.5, 0, 7); ctx.fill();
}

let market = null, side = "yes";

async function refreshDatabases() {
  const { databases } = await getJson("/api/databases");
  const select = $("db-select"), previous = select.value;
  select.innerHTML = databases.length ? "" : '<option value="">(no databases)</option>';
  for (const db of databases) {
    const opt = document.createElement("option");
    opt.value = db.name; opt.textContent = `${db.name} (${db.kind})`; select.appendChild(opt);
  }
  if (databases.some((db) => db.name === previous)) select.value = previous;
  return databases;
}

function ladder(levels, isAsk) {
  const sorted = levels.slice().sort((a, b) => (isAsk ? a[0] - b[0] : b[0] - a[0]));
  let run = 0;
  const rows = sorted.map(([p, s]) => { run += p * s; return { p, s, t: run }; });
  const max = Math.max(1, ...rows.map((r) => r.s));
  const shown = isAsk ? rows.slice(0, 10).reverse() : rows.slice(0, 10);
  return shown.map((r) => `<div class="lrow ${isAsk ? "ask" : "bid"}"><div class="bar" style="width:${(r.s / max) * 100}%"></div>
    <span class="p">${cents(r.p)}</span><span>${num(r.s)}</span><span>${num(r.t)}</span></div>`).join("")
    || `<div class="empty">no ${isAsk ? "asks" : "bids"}</div>`;
}

function renderBook() {
  if (!market || !market.book) return;
  const yesBids = market.book.yes.map(([p, s]) => [Number(p), Number(s)]);
  const noBids = market.book.no.map(([p, s]) => [Number(p), Number(s)]);
  const ownBids = side === "yes" ? yesBids : noBids, otherBids = side === "yes" ? noBids : yesBids;
  const asks = otherBids.map(([p, s]) => [1 - p, s]);
  $("asks").innerHTML = ladder(asks, true);
  $("bids").innerHTML = ladder(ownBids, false);
  const bestBid = ownBids.length ? Math.max(...ownBids.map((l) => l[0])) : null;
  const bestAsk = asks.length ? Math.min(...asks.map((l) => l[0])) : null;
  $("mid-price").textContent = bestBid !== null && bestAsk !== null ? cents((bestBid + bestAsk) / 2) : "--";
  $("mid-price").className = "big " + (side === "yes" ? "green" : "red");
  $("mid-spread").textContent = bestBid !== null && bestAsk !== null ? "SPREAD: " + cents(bestAsk - bestBid) : "";
  const yb = yesBids.length ? Math.max(...yesBids.map((l) => l[0])) : null;
  const nb = noBids.length ? Math.max(...noBids.map((l) => l[0])) : null;
  $("q-yes").textContent = "Yes " + (nb !== null ? cents(1 - nb) : "--");
  $("q-no").textContent = "No " + (yb !== null ? cents(1 - yb) : "--");
  $("st-spread").textContent = yb !== null && nb !== null ? cents(1 - nb - yb) : "--";
}

function timeLeft() {
  if (!market || !market.close_time) return { text: "--", live: false };
  const ms = new Date(market.close_time) - Date.now();
  if (ms <= 0) return { text: "closed", live: false };
  return { text: `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, "0")}`, live: true };
}

async function refreshMarket() {
  const db = $("db-select").value, note = $("market-note");
  if (!db) { note.textContent = "No databases found in ./data yet. Run btcbot paper or btcbot record, then click Refresh."; return; }
  try {
    const t = $("ticker-select").value;
    market = await getJson(`/api/market?db=${encodeURIComponent(db)}` + (t ? `&ticker=${encodeURIComponent(t)}` : ""));
    note.className = "note"; note.textContent = "";
    const sel = $("ticker-select");
    sel.innerHTML = market.tickers.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
    if (market.ticker) sel.value = market.ticker;
    if (!market.ticker) { note.textContent = "This database has no order-book snapshots yet."; return; }
    const strike = market.strike ? Number(market.strike) : null;
    $("hdr-title").textContent = "BTC 15 min" + (strike ? " · $" + num(strike) + " target" : "");
    $("hdr-sub").innerHTML = `Target price: <b>${strike ? "$" + num(strike) : "--"}</b> · <b>${esc(market.ticker)}</b>`;
    const tl = timeLeft();
    $("hdr-badge").textContent = tl.live ? "LIVE" : "CLOSED";
    $("hdr-badge").className = "badge " + (tl.live ? "live" : "closed");
    $("st-vol").textContent = market.volume ? num(market.volume) : "--";
    $("st-oi").textContent = market.open_interest ? num(market.open_interest) : "--";
    $("st-left").textContent = tl.text;
    $("st-ts").textContent = market.book_ts ? new Date(market.book_ts).toLocaleTimeString() : "--";
    $("f-exp").textContent = market.close_time ? new Date(market.close_time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "--";
    $("f-strike").textContent = strike ? "$" + num(strike) : "--";
    const spot = market.spot ? Number(market.spot) : null;
    $("f-spot").innerHTML = spot ? `<span class="orange">$${num(spot)}</span>` + (strike ? ` <span class="${spot >= strike ? "green" : "red"}" style="font-size:11px">${spot >= strike ? "+" : "-"}$${num(Math.abs(spot - strike))}</span>` : "") : "--";
    renderBook();
    const toMs = (s) => new Date(s).getTime();
    drawSeries($("mid-chart"), market.mid_series.filter((r) => r[1] !== null && r[2] !== null)
      .map((r) => [toMs(r[0]), (Number(r[1]) + Number(r[2])) / 2 * 100]),
      { color: css("--green"), step: true, yfmt: (v) => v.toFixed(1), min: 0, max: 100, seconds: true, empty: "No YES price history yet." });
    drawSeries($("spot-chart"), market.spot_series.map((r) => [toMs(r[0]), Number(r[1])]),
      { color: css("--orange"), fill: true, seconds: true, yfmt: (v) => "$" + num(v), include: strike ? [strike] : [],
        lines: strike ? [{ y: strike, color: css("--muted"), label: "Target: $" + num(strike) }] : [], empty: "No spot ticks recorded for this window." });
    $("spot-note").textContent = "Spot is the Coinbase BTC-USD proxy the model uses, not Kalshi's BRTI.";
    $q("#win-trades tbody").innerHTML = market.trades.length ? market.trades.map((x) => `<tr><td>${esc(x.side)}</td><td>${esc(x.size)}</td>
      <td>${cents(x.entry_price)}</td><td>${esc(x.result ?? "pending")}</td><td>${fmtUsd(x.pnl_usd)}</td></tr>`).join("")
      : '<tr><td colspan="5" class="empty">No paper trades in this window.</td></tr>';
    $("settle").textContent = market.settlement && market.settlement.result
      ? `Result: ${market.settlement.result.toUpperCase()}` + (market.settlement.settled_avg ? ` · settled avg $${num(market.settlement.settled_avg)}` : "")
      : "Not settled yet.";
  } catch (err) { note.className = "error"; note.textContent = "Error: " + err.message; }
}

function drawPnlChart(points) {
  drawSeries($("pnl-chart"), points.map((p) => [new Date(p.ts).getTime(), Number(p.cumulative_pnl_usd)]),
    { color: css("--blue"), fill: true, yfmt: (v) => "$" + v.toFixed(2), min: 0, max: 0, empty: "Not enough resolved trades yet for a chart." });
}

async function refreshMonitor() {
  const db = $("db-select").value, note = $("monitor-note");
  if (!db) { note.textContent = "No databases found in ./data yet. Run btcbot paper or btcbot record first, then click Refresh."; return; }
  try {
    const s = await getJson(`/api/paper_summary?db=${encodeURIComponent(db)}`);
    note.textContent = "";
    $("summary-tiles").innerHTML = [
      ["Trades", s.trade_count], ["Resolved", s.resolved_count], ["Unresolved", s.unresolved_count],
      ["Win rate", fmtPct(s.win_rate)], ["Total PnL", fmtUsd(s.total_pnl_usd)],
      ["Windows seen", s.windows_seen], ["Windows traded", s.windows_traded],
    ].map(([label, value]) => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");
    drawPnlChart(s.cumulative_pnl);
    $q("#trades-table tbody").innerHTML = s.trades.length ? s.trades.map((t) => `<tr>
      <td>${esc(t.ticker)}</td><td>${esc(t.side)}</td><td>${esc(t.size)}</td><td>${esc(t.entry_price)}</td>
      <td>${esc(t.entry_ts)}</td><td>${esc(t.result ?? "pending")}</td><td>${fmtUsd(t.pnl_usd)}</td></tr>`).join("")
      : '<tr><td colspan="7" class="empty">No trades yet in this database. That is normal for a short run or a quiet market.</td></tr>';
  } catch (err) { note.className = "error"; note.textContent = `Error: ${err.message}`; }
}

async function runBacktest() {
  const db = $("db-select").value, note = $("backtest-note");
  if (!db) { note.className = "error"; note.textContent = "No databases found in ./data yet. Run btcbot paper or btcbot record first, then click Refresh."; return; }
  note.className = "note"; note.textContent = "Running backtest..."; $("run-backtest").disabled = true;
  try {
    const { reports } = await getJson(`/api/backtest?db=${encodeURIComponent(db)}&queue=${$("queue-select").value}&maker_fee_multiplier=${encodeURIComponent($("fee-input").value)}`);
    $q("#backtest-table tbody").innerHTML = reports.length ? reports.map((r) => `<tr>
      <td>${esc(r.queue_assumption)}</td><td>${esc(r.maker_fee_multiplier)}</td><td>${esc(r.trades)}</td>
      <td>${fmtPct(r.win_rate)}</td><td>${fmtUsd(r.total_pnl_usd)}</td><td>${fmtUsd(r.max_drawdown_usd)}</td>
      <td>${r.trades_per_day === null ? "--" : Number(r.trades_per_day).toFixed(2)}</td>
      <td>${r.beats_trade_nothing === null ? "n/a" : (r.beats_trade_nothing ? "yes" : "no")}</td>
      <td>${esc(r.sample_size_note)}</td></tr>`).join("")
      : '<tr><td colspan="9" class="empty">The backtest returned no rows.</td></tr>';
    note.className = "note";
    note.textContent = "Done. " + reports.length + " scenario(s). Results come from recorded data only, so treat small samples as noise.";
  } catch (err) { note.className = "error"; note.textContent = `Error: ${err.message}`; }
  finally { $("run-backtest").disabled = false; }
}

async function loadSettings() {
  const s = await getJson("/api/settings");
  const radio = document.querySelector(`input[name="kalshi-env"][value="${s.kalshi_env}"]`) || document.querySelector('input[name="kalshi-env"][value="demo"]');
  radio.checked = true;
  $("prod-warn").style.display = radio.value === "prod" ? "block" : "none";
  $("key-id-input").placeholder = s.key_id_set ? `current key ends in ...${s.key_id_last4}` : "not set";
  $("key-path-input").value = s.private_key_path || "";
  $("settings-status").innerHTML = `<div class="note">Settings file: ${esc(s.env_file)}${s.env_file_exists ? "" : " (does not exist yet -- Save will create it)"}</div>`;
}

async function saveSettings() {
  const payload = { kalshi_env: $q('input[name="kalshi-env"]:checked').value };
  if ($("key-id-input").value) payload.key_id = $("key-id-input").value;
  if ($("key-path-input").value) payload.private_key_path = $("key-path-input").value;
  const status = $("settings-status");
  try {
    const res = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    $("key-id-input").value = "";
    status.innerHTML = '<div class="ok">Saved.</div>';
    await loadSettings();
  } catch (err) { status.innerHTML = `<div class="error">Error: ${esc(err.message)}</div>`; }
}

let tab = "market";
const refreshTab = () => { if (tab === "market") refreshMarket(); else if (tab === "monitor") refreshMonitor(); };
document.querySelectorAll("nav button").forEach((btn) => btn.addEventListener("click", () => {
  tab = btn.dataset.tab;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll("section").forEach((s) => s.classList.toggle("active", s.id === `tab-${tab}`));
  $("db-row").style.display = tab === "settings" ? "none" : "";
  refreshTab();
}));
document.querySelectorAll("#side-toggle button").forEach((b) => b.addEventListener("click", () => {
  side = b.dataset.side;
  document.querySelectorAll("#side-toggle button").forEach((x) => x.classList.toggle("on", x === b));
  $("side-toggle").classList.toggle("no", side === "no");
  renderBook();
}));
$("refresh-databases").addEventListener("click", async () => { await refreshDatabases(); refreshTab(); });
$("db-select").addEventListener("change", () => { $("ticker-select").innerHTML = ""; refreshTab(); });
$("ticker-select").addEventListener("change", refreshTab);
$("run-backtest").addEventListener("click", runBacktest);
$("save-settings").addEventListener("click", saveSettings);
document.querySelectorAll('input[name="kalshi-env"]').forEach((r) => r.addEventListener("change", () => {
  $("prod-warn").style.display = $q('input[name="kalshi-env"]:checked').value === "prod" ? "block" : "none";
}));
window.addEventListener("resize", () => { if (tab === "market" || tab === "monitor") refreshTab(); });

(async function init() {
  try { await refreshDatabases(); } catch (e) { $("market-note").textContent = "Error: " + e.message; }
  await refreshMarket();
  try { await loadSettings(); } catch (e) { $("settings-status").innerHTML = '<div class="error">Error: ' + esc(e.message) + "</div>"; }
  setInterval(() => { if (tab === "market" || tab === "monitor") refreshTab(); }, 3000);
})();
</script>
</body>
</html>
"""
