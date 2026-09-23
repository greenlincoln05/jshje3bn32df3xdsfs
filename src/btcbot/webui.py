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
import re
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

import yaml
from pydantic import ValidationError

from btcbot.backtest import BacktestError, load_trades, run_backtest
from btcbot.config import BotConfig, ConfigError, load_config
from btcbot.dashboard_analytics import portfolio_view
from btcbot.dashboard_jobs import BacktestJobs, BacktestQueueFull
from btcbot.dashboard_market import market_quote, market_view
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


# --------------------------------------------------------------------------- config.yaml settings

# A deliberately small, numeric-only subset of config.yaml: sizing MODE and any other structural choice
# stays a file edit, not a dashboard click, but a number the owner already tunes by hand (an account size,
# a risk cap, a price band) is safe to expose here. None means a top-level key; otherwise the section it
# lives under.
EDITABLE_CONFIG_FIELDS: dict[str, str | None] = {
    "min_edge": None, "min_price": None, "max_price": None,
    "account_usd": "sizing", "risk_pct_per_trade": "sizing", "contracts_per_trade": "sizing",
    "max_open_exposure_pct": "risk", "daily_loss_limit_pct": "risk", "max_contracts_per_trade": "risk",
}


def config_summary(config_path: Path) -> dict[str, Any]:
    """The handful of numbers CLAUDE.md itself calls out as worth seeing/tuning at a glance, read straight
    off the currently loaded config -- so "what account size/risk caps is the bot actually running with"
    never requires opening config.yaml by hand. Read-only fields (everything not in
    EDITABLE_CONFIG_FIELDS) are still shown, just not writable from here."""
    config = load_config(str(config_path))
    return {
        "config_path": str(config_path),
        "mode": config.mode.value, "sizing_mode": config.sizing.mode.value,
        "min_edge": str(config.min_edge), "min_price": None if config.min_price is None else str(config.min_price),
        "max_price": None if config.max_price is None else str(config.max_price),
        "account_usd": str(config.sizing.account_usd), "risk_pct_per_trade": str(config.sizing.risk_pct_per_trade),
        "contracts_per_trade": config.sizing.contracts_per_trade,
        "max_open_exposure_pct": None if config.risk.max_open_exposure_pct is None else str(config.risk.max_open_exposure_pct),
        "daily_loss_limit_pct": None if config.risk.daily_loss_limit_pct is None else str(config.risk.daily_loss_limit_pct),
        "max_contracts_per_trade": config.risk.max_contracts_per_trade,
    }


def _patch_config_yaml_value(text: str, key: str, section: str | None, value: str) -> str:
    """Replaces just ONE key's value in raw config.yaml text, preserving every other line byte-for-byte --
    comments, ordering, unrelated sections -- the same "surgical text patch, not a re-serialize" approach
    write_env_settings takes for .env, so a heavily-commented file (this one) never loses its comments to a
    plain yaml.dump round-trip. A trailing inline comment on the patched line survives too, since the match
    only spans the key and its value token, never the rest of the line."""
    lines = text.splitlines(keepends=True)
    if section is None:
        pattern = re.compile(rf"^({re.escape(key)}\s*:\s*)(\S+)")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = pattern.sub(lambda m: m.group(1) + value, line, count=1)
                return "".join(lines)
        raise ValueError(f"could not find top-level key {key!r} in config.yaml")
    section_pattern = re.compile(rf"^{re.escape(section)}\s*:\s*$")
    key_pattern = re.compile(rf"^(\s+{re.escape(key)}\s*:\s*)(\S+)")
    in_section = False
    for i, line in enumerate(lines):
        if section_pattern.match(line):
            in_section = True
            continue
        if in_section and line.strip() and not line[0].isspace():
            in_section = False  # a later top-level key ended the section without finding ours
        if in_section:
            m = key_pattern.match(line)
            if m:
                lines[i] = key_pattern.sub(lambda mm: mm.group(1) + value, line, count=1)
                return "".join(lines)
    raise ValueError(f"could not find {section}.{key} in config.yaml")


def write_config_settings(config_path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    """Validates every patched value against BotConfig BEFORE writing anything to disk -- a bad edit (out of
    a Field's allowed range, wrong type) is reported cleanly and the file is left untouched, never partially
    patched or corrupted. Rejects any key not in EDITABLE_CONFIG_FIELDS explicitly rather than silently
    ignoring a typo."""
    unknown = set(updates) - set(EDITABLE_CONFIG_FIELDS)
    if unknown:
        raise ValueError(f"not editable here: {', '.join(sorted(unknown))}")
    if not updates:
        raise ValueError("nothing to update")
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    for key, raw_value in updates.items():
        value = str(raw_value).strip()
        if value.lower() in ("none", ""):
            value = "null"
        text = _patch_config_yaml_value(text, key, EDITABLE_CONFIG_FIELDS[key], value)
    try:
        parsed = yaml.safe_load(text) or {}
        BotConfig.model_validate(parsed)
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"edit would produce an invalid config.yaml: {exc}") from exc
    config_path.write_text(text, encoding="utf-8")
    return config_summary(config_path)


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
            # A separate venue (btcbot record-polymarket): its own pm_-prefixed tables, never Kalshi's
            # orderbook_snapshots/trades schema, so it must never be offered to the Kalshi-only market/
            # backtest/lab views the same way an "unknown" file implicitly could be.
            else "polymarket" if name.startswith("polymarket-")
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
    # pnl_usd, not result: an early stop-loss/take-profit exit resolves a trade's PnL without the market
    # itself ever settling (result stays None then) -- see btcbot.backtest.TradeRecord.exit_reason.
    resolved = [t for t in trades if t.pnl_usd is not None]
    unresolved = [t for t in trades if t.pnl_usd is None]
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


def lab_defaults(config_path: Path) -> dict[str, Any]:
    """``account`` mirrors the account size/risk percentages the bot is ACTUALLY configured with
    (``config.sizing.account_usd`` and ``config.risk``'s two account-relative caps), so the dashboard's lab
    account fields start in sync with live config instead of a hardcoded guess the owner has to remember to
    retype by hand. A live cap left ``None`` (percent sizing off) falls back to :class:`btcbot.lab.AccountSettings`'s
    own research defaults, since there is then no "live" percentage to mirror."""
    config = load_config(str(config_path))
    fallback = AccountSettings()
    return {
        "tunable": list(TUNABLE),
        "grid": {k: ", ".join("none" if v is None else str(v) for v in vals) for k, vals in DEFAULT_GRID.items()},
        "account": {
            "account_usd": str(config.sizing.account_usd),
            "max_exposure_pct": str(config.risk.max_open_exposure_pct) if config.risk.max_open_exposure_pct is not None else str(fallback.max_exposure_pct),
            "daily_loss_pct": str(config.risk.daily_loss_limit_pct) if config.risk.daily_loss_limit_pct is not None else str(fallback.daily_loss_pct),
        },
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
    if any("demo" in str(name).lower() for name in payload.get("dbs") or []):
        raise LabError("Strategy Lab excludes demo/synthetic files; select production recordings only.")
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
        self.send_header("Cache-Control", "no-store")
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
            elif parsed.path == "/api/portfolio":
                try:
                    result = portfolio_view(self._require_db(query), query.get("starting_balance"),
                                            self._configured_account())
                except (ValueError, ArithmeticError, sqlite3.Error) as exc:
                    raise _ApiError(400, f"portfolio error: {exc}") from exc
                self._send_json(200, result)
            elif parsed.path == "/api/quote":
                self._send_json(200, market_quote(self._require_db(query), query.get("ticker", "")))
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
                self._send_json(200, lab_defaults(self.server.config_path))
            elif parsed.path == "/api/lab/status":
                job = self.server.lab_jobs.get(query.get("id", ""))  # type: ignore[attr-defined]
                if job is None:
                    raise _ApiError(404, "no such lab run (the dashboard may have been restarted)")
                self._send_json(200, job.snapshot())
            elif parsed.path == "/api/backtest":
                self._send_json(200, self._backtest(query))
            elif parsed.path == "/api/backtest/status":
                job = self.server.backtest_jobs.get(query.get("id", ""))
                if job is None:
                    raise _ApiError(404, "no such backtest (the dashboard may have been restarted)")
                self._send_json(200, job)
            elif parsed.path == "/api/settings":
                self._send_json(200, settings_status(self.server.env_path))
            elif parsed.path == "/api/config":
                try:
                    self._send_json(200, config_summary(self.server.config_path))
                except ConfigError as exc:
                    raise _ApiError(400, str(exc)) from exc
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
            elif parsed.path == "/api/config":
                try:
                    self._send_json(200, write_config_settings(self.server.config_path, self._read_json_body()))
                except (ValueError, ConfigError) as exc:
                    raise _ApiError(400, str(exc)) from exc
            elif parsed.path == "/api/lab/preview":
                try:
                    self._send_json(200, lab_preview(self.server.data_dir, self.server.config_path, self._read_json_body()))
                except (LabError, ConfigError) as exc:
                    raise _ApiError(400, str(exc)) from exc
            elif parsed.path == "/api/lab/start":
                self._send_json(200, self._lab_start(self._read_json_body()))
            elif parsed.path == "/api/backtest/start":
                payload = self._read_json_body()
                path = self._require_db({"db": str(payload.get("db", ""))})
                try:
                    job = self.server.backtest_jobs.submit(
                        path, self.server.config_path, queue=str(payload.get("queue", "both")),
                        maker_fee_multiplier=str(payload.get("maker_fee_multiplier", "both")),
                    )
                except BacktestQueueFull as exc:
                    raise _ApiError(409, str(exc)) from exc
                except (ValueError, OSError) as exc:
                    raise _ApiError(400, str(exc)) from exc
                self._send_json(202, job)
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
                if not Decimal(maker_fee_multiplier).is_finite() or Decimal(maker_fee_multiplier) < 0:
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

    def _configured_account(self) -> Any:
        """The config's account size, the last-resort starting balance for a run that did not record its own."""
        try:
            return load_config(str(self.server.config_path)).sizing.account_usd
        except (ConfigError, OSError):
            return None

    def _require_db(self, query: dict[str, str]) -> Path:
        name = query.get("db")
        if not name:
            raise _ApiError(400, "missing required query parameter: db")
        db_path = _resolve_db(self.server.data_dir, name)
        if db_path is None:
            raise _ApiError(404, f"no such database in {self.server.data_dir}: {name}")
        return db_path


class DashboardServer(ThreadingHTTPServer):
    def server_close(self) -> None:
        if hasattr(self, "backtest_jobs"):
            self.backtest_jobs.close(wait=False)
        super().server_close()


def create_dashboard_server(*, data_dir: Path, env_path: Path, config_path: Path, port: int) -> ThreadingHTTPServer:
    """Binds to ``127.0.0.1`` only -- never ``0.0.0.0`` -- so the dashboard is reachable only from this
    machine. ``port=0`` lets the OS pick a free port (used by tests); the bound port is on
    ``server.server_address[1]`` either way."""
    server = DashboardServer(("127.0.0.1", port), DashboardHandler)
    server.data_dir = data_dir  # type: ignore[attr-defined]
    server.env_path = env_path  # type: ignore[attr-defined]
    server.config_path = config_path  # type: ignore[attr-defined]
    server.lab_jobs = {}  # type: ignore[attr-defined]
    server.backtest_jobs = BacktestJobs()
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------- frontend

INDEX_HTML = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
