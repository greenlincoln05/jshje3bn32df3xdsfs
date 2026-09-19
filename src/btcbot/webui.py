"""Local monitoring dashboard: backtests, live paper PnL and trades, and local Kalshi settings.

Not one of the numbered build phases (`docs/btc15m-bot-spec.md` section 8) -- a monitoring tool that reads
what those phases already produce. `btcbot dashboard` starts a small HTTP server that binds to
``127.0.0.1`` only, so nothing it serves is reachable from another machine.

The "Settings" panel edits the same local ``.env`` file every other command already reads
(``KALSHI_ENV`` / ``KALSHI_KEY_ID`` / ``KALSHI_PRIVATE_KEY_PATH``, see :mod:`btcbot.config`), from the
owner's own browser to their own disk -- the same file `docs/running-live.md` already tells the owner to
edit by hand for `auth-check`. This module never transmits a key anywhere, never logs one (a POST body
is not part of the request line `BaseHTTPRequestHandler` logs), and a GET of the current settings always
masks the key id. This dashboard has no path to `kalshi_client.py`'s order endpoints at all, so it cannot
place, cancel, or modify a Kalshi order, in demo or prod, no matter what is entered in Settings -- it only
ever reads local SQLite databases and rewrites three lines of a local text file. (Phase 6 did add real,
demo-only order-placing code elsewhere in this repo -- `kalshi_client.py`/`execution.py`/`demo_check.py` --
but nothing here calls any of it; entering a key just lets the owner run `auth-check`/`demo-check`
themselves, exactly as if they'd edited `.env` directly.)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from btcbot.backtest import BacktestError, load_trades, run_backtest
from btcbot.config import ConfigError, load_config
from btcbot.lab import (
    DEFAULT_GRID, TUNABLE, AccountSettings, LabError, LabParams, expand_grid, load_lab_data, parse_values, run_lab,
)
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
        kind = (
            "paper" if name.startswith("paper-")
            else "recorder" if name.startswith("recorder-")
            else "stream" if name.startswith("stream-")
            else "demo" if name.startswith("demo-")
            else "unknown"
        )
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
        all_trades = _safe_trades(conn)
        trades = [asdict(t) for t in reversed(all_trades) if t.ticker == ticker]
        closes = dict(conn.execute("SELECT ticker, MAX(close_time) FROM market_state GROUP BY ticker"))
        results = dict(conn.execute("SELECT ticker, result FROM settlements"))
        trade_counts: dict[str, int] = {}
        for t in all_trades:
            trade_counts[t.ticker] = trade_counts.get(t.ticker, 0) + 1
        windows = [
            {"ticker": tk, "close_time": closes.get(tk), "result": results.get(tk), "trades": trade_counts.get(tk, 0)}
            for tk in tickers
        ]
    finally:
        conn.close()
    return {
        "tickers": tickers, "windows": windows, "ticker": ticker,
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


def demo_view(db_path: Path, audit_path: Path, *, audit_tail: int = 40) -> dict[str, Any]:
    """What ``btcbot demo`` has done so far, read straight from its database and the order ledger: each real
    order beside its paper twin, the run's problem events, and the tail of ``order-audit.jsonl`` (the bot's own
    record of every request it sent to Kalshi). Read-only; safe while the run is still writing."""
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            cols = ("id", "ticker", "side", "price", "size", "placed_ts", "order_id", "demo_filled", "demo_cost", "demo_fee",
                    "demo_first_fill_ts", "paper_filled", "paper_cost", "paper_fee", "paper_first_fill_ts", "closed_ts",
                    "result", "demo_pnl", "paper_pnl")
            orders = [dict(zip(cols, row, strict=True)) for row in conn.execute(
                f"SELECT {', '.join(cols)} FROM demo_orders ORDER BY id DESC LIMIT 200")]
        except sqlite3.OperationalError:
            orders = []  # not a demo database (or the run has not created its tables yet)
        try:
            events = [{"ts": r[0], "ticker": r[1], "event": r[2], "detail": r[3]} for r in conn.execute(
                "SELECT ts, ticker, event, detail FROM demo_events ORDER BY id DESC LIMIT 50")]
        except sqlite3.OperationalError:
            events = []
        try:
            last_snapshot = conn.execute("SELECT MAX(poll_ts) FROM orderbook_snapshots").fetchone()[0]
        except sqlite3.OperationalError:
            last_snapshot = None
    finally:
        conn.close()

    for order in orders:
        filled, size = Decimal(order["demo_filled"]), Decimal(order["size"])
        if order["result"]:
            state = "settled " + order["result"].upper()
        elif filled >= size:
            state = "filled"
        elif order["closed_ts"]:
            state = "partly filled, closed" if filled > 0 else "cancelled, unfilled"
        else:
            state = "partly filled, resting" if filled > 0 else "resting"
        order["state"] = state
        order["demo_avg_price"] = str(Decimal(order["demo_cost"]) / filled) if filled > 0 else None

    def total(key: str) -> str:
        return str(sum((Decimal(o[key]) for o in orders if o[key] is not None), Decimal(0)))

    audit: list[dict[str, Any]] = []
    try:
        for line in audit_path.read_text(encoding="utf-8").splitlines()[-audit_tail:]:
            try:
                audit.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return {
        "orders": orders, "events": events, "audit": list(reversed(audit)), "last_snapshot": last_snapshot,
        "summary": {
            "placed": len(orders),
            "filled_on_demo": sum(1 for o in orders if Decimal(o["demo_filled"]) > 0),
            "filled_in_paper": sum(1 for o in orders if Decimal(o["paper_filled"]) > 0),
            "rejected": sum(1 for e in events if e["event"] == "order_rejected"),
            "problems": len(events),
            "demo_pnl": total("demo_pnl"), "paper_pnl": total("paper_pnl"), "demo_fees": total("demo_fee"),
        },
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


# --------------------------------------------------------------------------- strategy lab jobs


@dataclass
class LabJob:
    """One background lab run. The sweep can take minutes on days of data, so it runs on a thread and the
    page polls :meth:`snapshot`."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: str = "running"  # running | done | error | cancelled
    done: int = 0
    total: int = 0
    label: str = "loading recorded data"
    error: str | None = None
    report: dict[str, Any] | None = None
    started: float = field(default_factory=time.monotonic)
    cancel: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id, "state": self.state, "done": self.done, "total": self.total, "label": self.label,
            "error": self.error, "report": self.report, "elapsed_sec": time.monotonic() - self.started,
        }


def lab_defaults() -> dict[str, Any]:
    return {
        "tunable": list(TUNABLE),
        "grid": {k: ", ".join("none" if v is None else str(v) for v in vals) for k, vals in DEFAULT_GRID.items()},
    }


def _lab_grid_from_payload(payload: dict[str, Any]) -> dict[str, list[Any]]:
    grid: dict[str, list[Any]] = {}
    for key, raw in (payload.get("grid") or {}).items():
        if raw is None or str(raw).strip() == "":
            continue  # a blank field means "do not vary this; keep the config default"
        grid[key] = parse_values(key, str(raw))
    if not grid:
        raise LabError("choose at least one thing to vary (fill in one or more of the value lists)")
    return grid


def _lab_number(payload: dict[str, Any], key: str, default: str, *, lo: Decimal, hi: Decimal | None = None) -> Decimal:
    try:
        value = Decimal(str(payload.get(key, default) if payload.get(key, "") != "" else default))
    except InvalidOperation:
        raise LabError(f"{key} is not a number") from None
    if not value.is_finite() or value < lo or (hi is not None and value > hi):
        raise LabError(f"{key} is out of range")
    return value


def lab_preview(data_dir: Path, config_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """How many combinations the grid expands to, and how many market windows the chosen files hold."""
    grid = _lab_grid_from_payload(payload)
    combos = expand_grid(LabParams.from_config(load_config(str(config_path))), grid)
    windows: set[str] = set()
    for name in payload.get("dbs") or []:
        path = _resolve_db(data_dir, str(name))
        if path is None:
            continue
        conn = sqlite3.connect(str(path))
        try:
            windows.update(r[0] for r in conn.execute("SELECT DISTINCT ticker FROM orderbook_snapshots"))
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    return {"combinations": len(combos), "windows": len(windows)}


def _run_lab_job(job: LabJob, paths: list[Path], config_path: Path, grid: dict[str, list[Any]], kwargs: dict[str, Any]) -> None:
    def progress(done: int, total: int, label: str) -> None:
        job.done, job.total, job.label = done, total, label

    try:
        data = load_lab_data(paths)
        job.label = "running"
        report = run_lab(data, load_config(str(config_path)), grid, progress=progress, cancelled=job.cancel.is_set, **kwargs)
        job.report = asdict(report)
        job.state = "done"
    except LabError as exc:
        job.state = "cancelled" if str(exc) == "cancelled" else "error"
        job.error = None if job.state == "cancelled" else str(exc)
    except (ConfigError, BacktestError, sqlite3.Error, ParseError, ValueError) as exc:
        job.state, job.error = "error", f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # a background thread must always end in a visible state, never vanish
        job.state, job.error = "error", f"unexpected {type(exc).__name__}: {exc}"


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
            elif parsed.path == "/api/demo":
                db_path = self._require_db(query)
                try:
                    self._send_json(200, demo_view(db_path, self.server.data_dir / "order-audit.jsonl"))
                except (sqlite3.OperationalError, ValueError, ArithmeticError) as exc:
                    raise _ApiError(400, f"demo view error: {exc}") from exc
            elif parsed.path == "/api/lab/defaults":
                self._send_json(200, lab_defaults())
            elif parsed.path == "/api/lab/status":
                job = self.server.lab_jobs.get(query.get("id", ""))  # type: ignore[attr-defined]
                if job is None:
                    raise _ApiError(404, "no such lab run (the dashboard may have been restarted)")
                self._send_json(200, job.snapshot())
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
            elif parsed.path == "/api/lab/preview":
                try:
                    self._send_json(200, lab_preview(self.server.data_dir, self.server.config_path, self._read_json_body()))
                except (LabError, ConfigError) as exc:
                    raise _ApiError(400, str(exc)) from exc
            elif parsed.path == "/api/lab/start":
                self._send_json(200, self._lab_start(self._read_json_body()))
            elif parsed.path == "/api/lab/cancel":
                job = self.server.lab_jobs.get(str(self._read_json_body().get("id", "")))  # type: ignore[attr-defined]
                if job is None:
                    raise _ApiError(404, "no such lab run")
                job.cancel.set()
                self._send_json(200, job.snapshot())
            else:
                self._send_json(404, {"error": f"no such endpoint: {parsed.path}"})
        except _ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # a JSON 500 beats a hung connection; this is a request boundary
            self._send_json(500, {"error": str(exc)})

    def _lab_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        jobs: dict[str, LabJob] = self.server.lab_jobs  # type: ignore[attr-defined]
        if any(j.state == "running" for j in jobs.values()):
            raise _ApiError(409, "a lab run is already in progress; wait for it or cancel it")
        names = payload.get("dbs")
        if not isinstance(names, list) or not names:
            raise _ApiError(400, "pick at least one data file")
        paths = []
        for name in names:
            path = _resolve_db(self.server.data_dir, str(name))
            if path is None:
                raise _ApiError(404, f"no such database in {self.server.data_dir}: {name}")
            paths.append(path)
        try:
            grid = _lab_grid_from_payload(payload)
            account = AccountSettings(
                account_usd=_lab_number(payload, "account_usd", "500", lo=Decimal(1)),
                max_exposure_pct=_lab_number(payload, "max_exposure_pct", "25", lo=Decimal(1), hi=Decimal(100)),
                daily_loss_pct=_lab_number(payload, "daily_loss_pct", "10", lo=Decimal(1), hi=Decimal(100)),
            )
            queue = str(payload.get("queue", "optimistic"))
            if queue not in ("optimistic", "pessimistic"):
                raise LabError("queue must be optimistic or pessimistic")
            split = float(_lab_number(payload, "split", "0.7", lo=Decimal("0.2"), hi=Decimal("0.9")))
            kwargs = {
                "account": account, "train_fraction": split, "queue": QueueAssumption(queue),
                "maker_fee_multiplier": _lab_number(payload, "maker_fee_multiplier", "0", lo=Decimal(0)),
                "top_k": int(_lab_number(payload, "top_k", "8", lo=Decimal(1), hi=Decimal(25))),
                "min_train_trades": int(_lab_number(payload, "min_train_trades", "20", lo=Decimal(1), hi=Decimal(10000))),
            }
            expand_grid(LabParams.from_config(load_config(str(self.server.config_path))), grid)  # fail fast on a bad grid
        except (LabError, ConfigError) as exc:
            raise _ApiError(400, str(exc)) from exc
        job = LabJob()
        jobs[job.id] = job
        threading.Thread(
            target=_run_lab_job, args=(job, paths, self.server.config_path, grid, kwargs), daemon=True, name=f"lab-{job.id}"
        ).start()
        return job.snapshot()

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
    server.lab_jobs = {}  # type: ignore[attr-defined]
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
  .pick { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
  .pick label { display: flex; flex-direction: column; gap: 3px; font-size: 10.5px; color: var(--muted);
                text-transform: uppercase; letter-spacing: .05em; }
  .pick select { min-width: 230px; text-transform: none; letter-spacing: 0; font-size: 13px; color: var(--text); }
  .pickhelp { padding: 8px 18px; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 12px; }
  .pickhelp b { color: var(--text); font-weight: 600; }
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
  .lab { display: grid; grid-template-columns: minmax(300px, 380px) 1fr; gap: 0; }
  .lab > div { padding: 14px 18px; min-width: 0; }
  .lab .setup { border-right: 1px solid var(--line); }
  @media (max-width: 960px) { .lab { grid-template-columns: 1fr; } .lab .setup { border-right: 0; border-bottom: 1px solid var(--line); } }
  .lab h3 { margin-top: 16px; } .lab h3:first-child { margin-top: 0; }
  .field { display: grid; grid-template-columns: 1fr 1.1fr; gap: 4px 10px; align-items: center; margin-bottom: 7px; }
  .field label { color: var(--text); font-size: 12.5px; }
  .field small { grid-column: 1 / -1; color: var(--muted); font-size: 11px; margin-top: -3px; }
  .field input { width: 100%; }
  .checks label { display: block; font-size: 12.5px; padding: 2px 0; cursor: pointer; }
  .presets { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
  .presets button { font-size: 11.5px; padding: 4px 9px; }
  .bar { height: 6px; background: var(--panel2); border-radius: 4px; overflow: hidden; margin: 8px 0; }
  .bar div { height: 100%; background: var(--green); width: 0; transition: width .3s; }
  .verdict { border-radius: 10px; padding: 12px 14px; margin: 0 0 14px; border: 1px solid var(--line); }
  .verdict b { display: block; margin-bottom: 3px; }
  .verdict.insufficient { background: #2a1d10; border-color: #6b4a1c; color: #fbbf77; }
  .verdict.not_supported { background: var(--redbg); border-color: #5a1f28; color: #ff9aa6; }
  .verdict.weak_signal { background: #0f1f33; border-color: #1f4a80; color: #8ec2ff; }
  .lab td.num, .lab th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .lab tr.base td { color: var(--muted); font-style: italic; }
  .lab ul.warns { color: var(--muted); font-size: 12px; padding-left: 18px; }
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
    <div class="pick" id="db-row">
      <label>1. Data file
        <select id="db-select" title="Which run to look at. The newest is at the top."></select></label>
      <label>2. Market window
        <select id="ticker-select" title="Each 15-minute market. Leave on Latest to follow along."></select></label>
      <button class="action" id="refresh-databases" title="Look for new files">Refresh</button>
    </div>
  </div>
  <div class="pickhelp" id="pickhelp">Pick the newest <b>Paper trading</b> file to watch a live run. Leave the window on <b>Latest</b> to follow the current 15-minute market automatically.</div>
  <nav>
    <button data-tab="market" class="active">Market</button>
    <button data-tab="monitor">Paper PnL</button>
    <button data-tab="backtest">Backtest</button>
    <button data-tab="lab">Strategy Lab</button>
    <button data-tab="demo">Demo orders</button>
    <button data-tab="settings">Settings</button>
  </nav>

  <section id="tab-market" class="active">
    <div class="stats">
      <span>Vol <b id="st-vol">--</b></span><span>Open int <b id="st-oi">--</b></span>
      <span>Spread <b id="st-spread">--</b></span><span>Time left <b id="st-left">--</b></span>
      <span>Last book <b id="st-ts">--</b></span><span>Data age <b id="st-age">--</b></span>
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

  <section id="tab-demo">
    <div class="tiles" id="demo-tiles"></div>
    <div class="pad">
      <div class="note" id="demo-note"></div>
      <h3>Each real demo order next to its paper simulation</h3>
      <table id="demo-orders"><thead><tr><th>Placed (UTC)</th><th>Window</th><th>Side</th><th class="num">Price</th><th class="num">Size</th>
        <th>State</th><th class="num">Demo filled</th><th class="num">Demo avg</th><th class="num">Demo fee</th><th class="num">Paper filled</th>
        <th class="num">Demo PnL</th><th class="num">Paper PnL</th></tr></thead><tbody></tbody></table>
      <h3>Problems this run <span class="sub">(rejected orders, failed cancels, unavailable fills)</span></h3>
      <table id="demo-events"><thead><tr><th>Time (UTC)</th><th>Window</th><th>Event</th><th>Detail</th></tr></thead><tbody></tbody></table>
      <h3>Order ledger: everything the bot sent to Kalshi <span class="sub">(data/order-audit.jsonl, newest first)</span></h3>
      <table id="demo-audit"><thead><tr><th>Time (UTC)</th><th>Event</th><th>Side</th><th class="num">Price</th><th class="num">Count</th><th>Order id</th><th>Error</th></tr></thead><tbody></tbody></table>
      <div class="note">Compare this ledger with the Orders and History tabs on Kalshi's demo Portfolio page. Any order there that is not in this ledger did not come from this bot.</div>
    </div>
  </section>

  <section id="tab-lab">
    <div class="lab">
      <div class="setup">
        <h3>1. Data to test on</h3>
        <div class="checks" id="lab-dbs"></div>
        <div class="note">Use real (PROD) recordings. Demo books are mostly synthetic. More days of data = results worth reading; you need at least 6 market windows, and hundreds before anything is believable.</div>

        <h3>2. Account and risk</h3>
        <div class="field"><label for="lab-account_usd">Account size ($)</label><input id="lab-account_usd" value="500"></div>
        <div class="field"><label for="lab-max_exposure_pct">Max at risk at once (%)</label><input id="lab-max_exposure_pct" value="25"></div>
        <div class="field"><label for="lab-daily_loss_pct">Daily loss stop (%)</label><input id="lab-daily_loss_pct" value="10"></div>

        <h3>3. What to vary <span class="sub">(comma lists; blank = keep the default)</span></h3>
        <div class="presets" id="lab-presets"></div>
        <div id="lab-fields"></div>

        <h3>4. How to judge it</h3>
        <div class="field"><label for="lab-split">Train share of windows</label><input id="lab-split" value="0.7"><small>Ranked on this earlier slice, then shown on the held-out rest.</small></div>
        <div class="field"><label for="lab-min_train_trades">Min trades to rank</label><input id="lab-min_train_trades" value="20"></div>
        <div class="field"><label for="lab-queue">Fill assumption</label>
          <select id="lab-queue"><option value="optimistic">optimistic (best case)</option><option value="pessimistic">pessimistic (never fills)</option></select></div>
        <div class="field"><label for="lab-maker_fee_multiplier">Maker fee multiplier</label>
          <select id="lab-maker_fee_multiplier"><option value="0">0 (makers pay nothing)</option><option value="0.25">0.25</option><option value="1">1 (full fee)</option></select></div>

        <div class="row" style="margin-top:14px">
          <button class="action" id="lab-run">Run lab</button>
          <button class="action" id="lab-cancel" disabled>Cancel</button>
          <span class="note" id="lab-preview"></span>
        </div>
        <div class="bar" id="lab-bar" style="display:none"><div id="lab-bar-fill"></div></div>
        <div class="note" id="lab-status"></div>
      </div>
      <div class="results" id="lab-results">
        <div class="empty">Pick data, choose what to vary, and press <b>Run lab</b>. Results appear here: every combination is ranked on the training windows and then shown on windows it never saw.</div>
      </div>
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
      <div class="warn" id="prod-warn">Live (prod) is selected. This dashboard still cannot place, cancel, or modify any order -- but Phase 6 did add real (demo-only) order-placing code elsewhere in this repo (`btcbot demo-check`), so a key entered here now lets you run that yourself. Keep this on Demo unless you mean to.</div>
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

const KIND_LABELS = { paper: "Paper trading", recorder: "Data recording", stream: "BRTI + book stream", demo: "Demo orders (fake money)", unknown: "Data file" };
function dbLabel(db) {
  const m = /^[a-z]+-[A-Z0-9]+-(demo|prod)-/.exec(db.name);
  const env = m ? m[1].toUpperCase() : "";
  const when = new Date(db.modified_ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const mb = db.size_bytes / 1048576;
  return `${KIND_LABELS[db.kind] || KIND_LABELS.unknown}${env ? " \u00b7 " + env : ""} \u00b7 ${when} \u00b7 ${mb < 0.1 ? "<0.1" : mb.toFixed(1)} MB`;
}

function windowLabel(w, isLatest) {
  const close = w.close_time ? new Date(w.close_time) : null;
  const time = close ? close.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : w.ticker;
  const state = close && close > Date.now() ? "live" : w.result ? "settled " + w.result.toUpperCase() : "closed";
  return `${time} close \u00b7 ${state}` + (w.trades ? ` \u00b7 ${w.trades} trade${w.trades > 1 ? "s" : ""}` : "");
}

async function refreshDatabases() {
  const { databases } = await getJson("/api/databases");
  const select = $("db-select"), previous = select.value;
  select.innerHTML = databases.length ? "" : '<option value="">(no data files yet - run btcbot paper or record)</option>';
  databases.forEach((db, i) => {
    const opt = document.createElement("option");
    opt.value = db.name; opt.textContent = dbLabel(db) + (i === 0 ? "  (newest)" : ""); select.appendChild(opt);
  });
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

function updateAge() {
  const el = $("st-age");
  if (!el || !market || !market.book_ts) return;
  const age = (Date.now() - new Date(market.book_ts).getTime()) / 1000;
  el.textContent = age < 60 ? age.toFixed(1) + "s" : "stale (" + Math.round(age / 60) + "m)";
  el.className = age < 3 ? "green" : age < 10 ? "orange" : "red";
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
    const sel = $("ticker-select"), chosen = sel.value;
    sel.innerHTML = '<option value="">Latest window (follows automatically)</option>' +
      market.windows.map((w) => `<option value="${esc(w.ticker)}">${esc(windowLabel(w))}</option>`).join("");
    sel.value = market.windows.some((w) => w.ticker === chosen) ? chosen : "";
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
    updateAge();
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

// ---------------------------------------------------------------- strategy lab
const LAB_FIELDS = [
  ["min_edge", "Min edge", "0.02, 0.04, 0.06", "How far the model's win chance must beat your bid price (after fees)."],
  ["max_spread", "Max spread ($)", "0.02, 0.06", "Skip thin books: bid-ask gap must be at most this."],
  ["max_tau_sec", "Earliest entry (secs left)", "480, 600, 780", "Only enter once this many seconds or fewer remain in the 15-minute window."],
  ["min_tau_sec", "Latest entry (secs left)", "30, 120, 300", "Stop entering when fewer than this many seconds remain."],
  ["min_price", "Min entry price ($)", "none, 0.20", "0.20 = 20 cents. 'none' = no floor."],
  ["max_price", "Max entry price ($)", "none, 0.60", "0.60 = 60 cents. 'none' = no cap."],
  ["trend_mode", "Trend filter", "off, with, against", "with = only the side spot is moving toward; against = fade the move."],
  ["trend_lookback_sec", "Trend lookback (secs)", "60, 180", "How far back to measure the move."],
  ["trend_min_move_usd", "Trend min move ($)", "0, 10, 25", "Ignore moves smaller than this."],
  ["model_blend", "Model weight (0-1)", "0.3, 0.5, 0.8", "1 = trust the model only, 0 = trust the market mid only."],
  ["risk_pct", "Risk per trade (% of account)", "1, 2, 5", "Blank = fixed number of contracts instead."],
  ["contracts", "Fixed contracts", "5, 10", "Used when risk % is blank."],
];
const LAB_PRESETS = {
  "Entry timing": { max_tau_sec: "480, 600, 780", min_tau_sec: "30, 60, 120, 300" },
  "Entry price": { min_price: "none, 0.15, 0.30", max_price: "none, 0.50, 0.60, 0.70" },
  "Trend": { trend_mode: "off, with, against", trend_lookback_sec: "60, 180", trend_min_move_usd: "0, 10, 25" },
  "Risk sizing": { risk_pct: "1, 2, 5, 10" },
  "Edge and spread": { min_edge: "0.01, 0.02, 0.04, 0.06", max_spread: "0.02, 0.04, 0.06" },
  "A bit of everything": { min_edge: "0.02, 0.04", min_tau_sec: "30, 120", max_price: "none, 0.60", trend_mode: "off, with" },
};
let labJob = null, labTimer = null, labPreviewTimer = null;

function buildLabForm(databases) {
  $("lab-fields").innerHTML = LAB_FIELDS.map(([key, label, ph, help]) =>
    `<div class="field"><label for="lab-${key}">${label}</label><input id="lab-${key}" placeholder="${esc(ph)}"><small>${esc(help)}</small></div>`).join("");
  $("lab-presets").innerHTML = Object.keys(LAB_PRESETS).map((n) => `<button class="action" data-preset="${esc(n)}">${esc(n)}</button>`).join("");
  document.querySelectorAll("#lab-presets button").forEach((b) => b.addEventListener("click", () => {
    LAB_FIELDS.forEach(([key]) => { $("lab-" + key).value = LAB_PRESETS[b.dataset.preset][key] || ""; });
    labPreview();
  }));
  document.querySelectorAll("#lab-fields input, #lab-account_usd").forEach((i) => i.addEventListener("input", labPreviewSoon));
  renderLabDbs(databases);
  LAB_PRESETS["Entry timing"] && Object.entries(LAB_PRESETS["Entry timing"]).forEach(([k, v]) => { $("lab-" + k).value = v; });
  labPreview();
}

function renderLabDbs(databases) {
  const usable = databases.filter((d) => d.kind !== "unknown");
  $("lab-dbs").innerHTML = usable.length ? usable.map((d) => {
    const demo = /-demo-/.test(d.name);
    return `<label><input type="checkbox" value="${esc(d.name)}" ${demo ? "" : "checked"}> ${esc(dbLabel(d))}${demo ? ' <span class="orange">(demo, synthetic)</span>' : ""}</label>`;
  }).join("") : '<div class="empty">No recordings yet. Run <code>btcbot paper</code> or <code>btcbot record</code> first.</div>';
  document.querySelectorAll("#lab-dbs input").forEach((i) => i.addEventListener("change", labPreviewSoon));
}

function labPayload() {
  const grid = {};
  LAB_FIELDS.forEach(([key]) => { grid[key] = $("lab-" + key).value; });
  const dbs = [...document.querySelectorAll("#lab-dbs input:checked")].map((i) => i.value);
  const out = { dbs, grid };
  ["account_usd", "max_exposure_pct", "daily_loss_pct", "split", "min_train_trades", "queue", "maker_fee_multiplier"].forEach((k) => { out[k] = $("lab-" + k).value; });
  return out;
}

async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function labPreviewSoon() { clearTimeout(labPreviewTimer); labPreviewTimer = setTimeout(labPreview, 350); }
async function labPreview() {
  const el = $("lab-preview");
  try {
    const r = await postJson("/api/lab/preview", labPayload());
    el.className = r.combinations > 400 ? "error" : "note";
    el.textContent = `${r.combinations.toLocaleString()} combinations \u00b7 ${r.windows.toLocaleString()} windows in the selected files`;
  } catch (err) { el.className = "note"; el.textContent = err.message; }
}

const mUsd = (v) => `<span class="${Number(v) > 0 ? "pnl-pos" : Number(v) < 0 ? "pnl-neg" : ""}">${Number(v) < 0 ? "-" : ""}$${Math.abs(Number(v)).toFixed(2)}</span>`;
function metricCells(m) {
  const wr = m.win_rate === null ? "--" : (m.win_rate * 100).toFixed(0) + "%";
  const t = m.t_stat === null ? "--" : (m.t_stat > 0 ? "+" : "") + m.t_stat.toFixed(1);
  return `<td class="num">${m.resolved}</td><td class="num">${wr}</td><td class="num">${mUsd(m.pnl)}</td><td class="num">${t}</td>`;
}

function renderLabReport(r) {
  const rows = r.rows.map((row) => `<tr><td class="num">${row.rank}</td><td>${esc(row.description)}${row.equivalent ? ` <span class="sub">(+${row.equivalent} equivalent)</span>` : ""}</td>${metricCells(row.train)}${metricCells(row.test)}
    <td class="num">${row.test.return_pct === null ? "--" : row.test.return_pct.toFixed(1) + "%"}</td>
    <td class="num">${row.test.max_drawdown_pct === null ? "--" : row.test.max_drawdown_pct.toFixed(1) + "%"}</td></tr>`).join("");
  const base = `<tr class="base"><td class="num">base</td><td>config.yaml defaults</td>${metricCells(r.baseline_train)}${metricCells(r.baseline_test)}
    <td class="num">${r.baseline_test.return_pct === null ? "--" : r.baseline_test.return_pct.toFixed(1) + "%"}</td>
    <td class="num">${r.baseline_test.max_drawdown_pct === null ? "--" : r.baseline_test.max_drawdown_pct.toFixed(1) + "%"}</td></tr>`;
  $("lab-results").innerHTML = `
    <div class="verdict ${esc(r.verdict_level)}"><b>${{ insufficient: "Not enough data to conclude", not_supported: "Did not hold up on unseen windows", weak_signal: "Held up on unseen windows (weakly)" }[r.verdict_level] || ""}</b>${esc(r.verdict)}</div>
    <div class="sub" style="margin-bottom:8px">${r.windows_total} windows: ${r.windows_train} train, ${r.windows_test} test (1 skipped between). ${r.combinations.toLocaleString()} combinations, ${r.ranked_combinations.toLocaleString()} with at least ${r.min_train_trades} training trades. Account $${esc(r.account_usd)} \u00b7 ${esc(r.queue)} fills \u00b7 ${r.seconds.toFixed(1)}s.</div>
    <table><thead><tr><th class="num">#</th><th>What changed</th>
      <th class="num" colspan="4" style="text-align:center">TRAIN (used to rank)</th><th class="num" colspan="4" style="text-align:center">TEST (never seen)</th><th class="num">Return</th><th class="num">Max DD</th></tr>
      <tr><th></th><th></th><th class="num">Trades</th><th class="num">Win</th><th class="num">PnL</th><th class="num">t</th><th class="num">Trades</th><th class="num">Win</th><th class="num">PnL</th><th class="num">t</th><th class="num">(test)</th><th class="num">(test)</th></tr></thead>
      <tbody>${base}${rows || '<tr><td colspan="12" class="empty">Nothing had enough training trades to rank.</td></tr>'}</tbody></table>
    <h3>Read this before believing anything</h3><ul class="warns">${r.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}<li>t is the average PnL per trade divided by its noise; roughly, below 2 is indistinguishable from luck.</li></ul>`;
}

function labSetRunning(running) {
  $("lab-run").disabled = running; $("lab-cancel").disabled = !running;
  $("lab-bar").style.display = running ? "block" : "none";
}

async function labPoll() {
  if (!labJob) return;
  try {
    const st = await getJson("/api/lab/status?id=" + encodeURIComponent(labJob));
    const pct = st.total ? (st.done / st.total) * 100 : 5;
    $("lab-bar-fill").style.width = pct + "%";
    $("lab-status").className = "note";
    $("lab-status").textContent = st.state === "running"
      ? `${st.done}/${st.total || "?"} \u00b7 ${st.label} \u00b7 ${Math.round(st.elapsed_sec)}s` : "";
    if (st.state !== "running") {
      clearInterval(labTimer); labJob = null; labSetRunning(false);
      if (st.state === "done") renderLabReport(st.report);
      else if (st.state === "cancelled") { $("lab-status").textContent = "Cancelled."; }
      else { $("lab-status").className = "error"; $("lab-status").textContent = "Error: " + st.error; }
    }
  } catch (err) { clearInterval(labTimer); labJob = null; labSetRunning(false); $("lab-status").className = "error"; $("lab-status").textContent = "Error: " + err.message; }
}

async function labRun() {
  $("lab-status").className = "note"; $("lab-status").textContent = "Starting...";
  try {
    const st = await postJson("/api/lab/start", labPayload());
    labJob = st.id; labSetRunning(true);
    clearInterval(labTimer); labTimer = setInterval(labPoll, 1000); labPoll();
  } catch (err) { $("lab-status").className = "error"; $("lab-status").textContent = "Error: " + err.message; }
}

async function labCancel() { if (labJob) { try { await postJson("/api/lab/cancel", { id: labJob }); } catch (e) { /* the poll will surface it */ } } }

let tab = "market";
async function refreshDemo() {
  const db = $("db-select").value, note = $("demo-note");
  if (!db) { note.className = "note"; note.textContent = "No data files yet. Start btcbot demo, then click Refresh."; return; }
  try {
    const d = await getJson(`/api/demo?db=${encodeURIComponent(db)}`);
    const s = d.summary, t = (iso) => (iso ? iso.slice(11, 19) : "--");
    const age = d.last_snapshot ? (Date.now() - new Date(d.last_snapshot).getTime()) / 1000 : null;
    $("demo-tiles").innerHTML = [
      ["Orders placed", s.placed], ["Filled on demo", s.filled_on_demo], ["Filled in paper", s.filled_in_paper],
      ["Rejected", s.rejected], ["Problems", s.problems], ["Demo PnL (settled)", fmtUsd(s.demo_pnl)],
      ["Paper PnL (settled)", fmtUsd(s.paper_pnl)], ["Data age", age === null ? "--" : (age < 60 ? age.toFixed(1) + "s" : "stale")],
    ].map(([label, value]) => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");
    note.className = "note";
    note.textContent = d.orders.length || d.events.length ? "" : "No demo orders yet. Normal: the strategy only orders when it sees an edge. If this is not a demo file, pick the newest Demo orders file above.";
    $q("#demo-orders tbody").innerHTML = d.orders.length ? d.orders.map((o) => `<tr><td>${t(o.placed_ts)}</td><td>${esc(o.ticker.slice(-8))}</td>
      <td>${esc(o.side)}</td><td class="num">${cents(o.price)}</td><td class="num">${esc(o.size)}</td><td>${esc(o.state)}</td>
      <td class="num">${num(o.demo_filled)}</td><td class="num">${o.demo_avg_price === null ? "--" : cents(o.demo_avg_price)}</td>
      <td class="num">$${Number(o.demo_fee).toFixed(4)}</td><td class="num">${num(o.paper_filled)}</td>
      <td class="num">${o.demo_pnl === null ? "--" : fmtUsd(o.demo_pnl)}</td><td class="num">${o.paper_pnl === null ? "--" : fmtUsd(o.paper_pnl)}</td></tr>`).join("")
      : '<tr><td colspan="12" class="empty">No orders yet.</td></tr>';
    $q("#demo-events tbody").innerHTML = d.events.length ? d.events.map((e) => `<tr><td>${t(e.ts)}</td><td>${esc((e.ticker || "").slice(-8))}</td><td>${esc(e.event)}</td><td>${esc(e.detail)}</td></tr>`).join("")
      : '<tr><td colspan="4" class="empty">No problems. </td></tr>';
    $q("#demo-audit tbody").innerHTML = d.audit.length ? d.audit.map((a) => `<tr><td>${t(a.ts)}</td><td>${esc(a.event)}</td><td>${esc(a.side || "")}</td>
      <td class="num">${a.price ? cents(a.price) : ""}</td><td class="num">${esc(a.count || "")}</td><td>${esc((a.order_id || "").slice(0, 13))}</td><td>${esc(a.error || "")}</td></tr>`).join("")
      : '<tr><td colspan="7" class="empty">No ledger yet.</td></tr>';
  } catch (err) { note.className = "error"; note.textContent = "Error: " + err.message; }
}

let refreshing = false;
async function refreshTab() {
  if (refreshing) return;  // a slow response must not pile up requests behind it
  refreshing = true;
  try {
    if (tab === "market") await refreshMarket(); else if (tab === "monitor") await refreshMonitor(); else if (tab === "demo") await refreshDemo();
  } finally { refreshing = false; }
}
document.querySelectorAll("nav button").forEach((btn) => btn.addEventListener("click", () => {
  tab = btn.dataset.tab;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll("section").forEach((s) => s.classList.toggle("active", s.id === `tab-${tab}`));
  const noPicker = tab === "settings" || tab === "lab";
  $("db-row").style.display = noPicker ? "none" : "";
  $("pickhelp").style.display = noPicker ? "none" : "";
  $q("#ticker-select").parentElement.style.display = tab === "market" ? "" : "none";
  refreshTab();
}));
document.querySelectorAll("#side-toggle button").forEach((b) => b.addEventListener("click", () => {
  side = b.dataset.side;
  document.querySelectorAll("#side-toggle button").forEach((x) => x.classList.toggle("on", x === b));
  $("side-toggle").classList.toggle("no", side === "no");
  renderBook();
}));
$("refresh-databases").addEventListener("click", async () => { const dbs = await refreshDatabases(); renderLabDbs(dbs); refreshTab(); });
$("db-select").addEventListener("change", () => { $("ticker-select").innerHTML = ""; refreshTab(); });
$("ticker-select").addEventListener("change", refreshTab);
$("run-backtest").addEventListener("click", runBacktest);
$("lab-run").addEventListener("click", labRun);
$("lab-cancel").addEventListener("click", labCancel);
$("save-settings").addEventListener("click", saveSettings);
document.querySelectorAll('input[name="kalshi-env"]').forEach((r) => r.addEventListener("change", () => {
  $("prod-warn").style.display = $q('input[name="kalshi-env"]:checked').value === "prod" ? "block" : "none";
}));
window.addEventListener("resize", () => { if (tab === "market" || tab === "monitor") refreshTab(); });

(async function init() {
  let dbList = [];
  try { dbList = await refreshDatabases(); } catch (e) { $("market-note").textContent = "Error: " + e.message; }
  buildLabForm(dbList);
  await refreshMarket();
  try { await loadSettings(); } catch (e) { $("settings-status").innerHTML = '<div class="error">Error: ' + esc(e.message) + "</div>"; }
  setInterval(() => { if (tab === "market" || tab === "monitor" || tab === "demo") refreshTab(); }, 1000);
  setInterval(updateAge, 200);
})();
</script>
</body>
</html>
"""
