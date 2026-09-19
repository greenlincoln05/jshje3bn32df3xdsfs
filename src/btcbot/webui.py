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


def paper_summary(db_path: Path) -> dict[str, Any]:
    """Live trading state for a database :mod:`btcbot.live_paper` may still be writing: a plain read-write
    handle is fine to open concurrently because ``recorder.py`` runs its database in WAL mode, and this
    function never issues a write of its own."""
    conn = sqlite3.connect(str(db_path))
    try:
        trades = load_trades(conn)
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

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>btc15m-bot dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { background: #0f1115; color: #e6e6e6; font: 14px/1.5 -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
         margin: 0; padding: 24px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .subtitle { color: #9aa4b2; margin-bottom: 20px; }
  .banner { background: #1a2230; border: 1px solid #2c3a52; border-radius: 8px; padding: 10px 14px;
            margin-bottom: 20px; color: #9fd3ff; font-size: 13px; }
  nav { display: flex; gap: 8px; margin-bottom: 16px; }
  nav button { background: #1b1e26; color: #cfd3da; border: 1px solid #2a2e38; border-radius: 6px;
               padding: 8px 14px; cursor: pointer; font-size: 13px; }
  nav button.active { background: #2b6cb0; color: white; border-color: #2b6cb0; }
  section { display: none; }
  section.active { display: block; }
  .row { display: flex; gap: 10px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
  select, input, button.action { background: #1b1e26; color: #e6e6e6; border: 1px solid #2a2e38;
        border-radius: 6px; padding: 7px 10px; font-size: 13px; }
  button.action { cursor: pointer; }
  button.action:hover { border-color: #2b6cb0; }
  .tiles { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
  .tile { background: #161a22; border: 1px solid #262b36; border-radius: 8px; padding: 12px 16px; min-width: 130px; }
  .tile .label { color: #9aa4b2; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  .tile .value { font-size: 20px; margin-top: 4px; }
  .pnl-pos { color: #4ade80; } .pnl-neg { color: #f87171; }
  table { border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #22262f; }
  th { color: #9aa4b2; font-weight: 600; font-size: 11px; text-transform: uppercase; }
  canvas { background: #12151b; border: 1px solid #262b36; border-radius: 8px; }
  .note { color: #9aa4b2; font-size: 12px; margin-top: 10px; }
  .error { color: #f87171; margin-top: 10px; }
  .ok { color: #4ade80; margin-top: 10px; }
</style>
</head>
<body>
<h1>btc15m-bot dashboard</h1>
<div class="subtitle">Local only -- this page and everything it fetches stays on this machine.</div>
<div class="banner">
  Read-only monitoring plus a local settings editor. There is no order-placing code anywhere in this repo
  (Phase 6 is not approved yet) -- nothing on this page can place, cancel, or modify a Kalshi order, in
  demo or in prod.
</div>

<nav>
  <button data-tab="monitor" class="active">Live / Paper monitor</button>
  <button data-tab="backtest">Backtest</button>
  <button data-tab="settings">Settings</button>
</nav>

<div class="row">
  <label for="db-select">Database</label>
  <select id="db-select"></select>
  <button class="action" id="refresh-databases">Refresh list</button>
</div>

<section id="tab-monitor" class="active">
  <div class="tiles" id="summary-tiles"></div>
  <canvas id="pnl-chart" width="900" height="220"></canvas>
  <table id="trades-table">
    <thead><tr><th>Ticker</th><th>Side</th><th>Size</th><th>Entry price</th><th>Entry time (UTC)</th>
      <th>Result</th><th>PnL (USD)</th></tr></thead>
    <tbody></tbody>
  </table>
  <div class="note" id="monitor-note"></div>
</section>

<section id="tab-backtest">
  <div class="row">
    <label for="queue-select">Queue assumption</label>
    <select id="queue-select">
      <option value="both">both</option>
      <option value="optimistic">optimistic</option>
      <option value="pessimistic">pessimistic</option>
    </select>
    <label for="fee-input">Maker fee multiplier</label>
    <input id="fee-input" value="both" size="8">
    <button class="action" id="run-backtest">Run backtest</button>
  </div>
  <table id="backtest-table">
    <thead><tr><th>Queue</th><th>Fee mult.</th><th>Trades</th><th>Win rate</th><th>Total PnL</th>
      <th>Max drawdown</th><th>Trades/day</th><th>Beats trade-nothing?</th><th>Sample size</th></tr></thead>
    <tbody></tbody>
  </table>
  <div class="note" id="backtest-note"></div>
</section>

<section id="tab-settings">
  <p class="note">
    Stored only in the local settings file shown below. Never sent anywhere except from your browser to this
    localhost server, and this server only ever writes it to that file -- the same file every other
    <code>btcbot</code> command reads via <code>KALSHI_ENV</code> / <code>KALSHI_KEY_ID</code> /
    <code>KALSHI_PRIVATE_KEY_PATH</code>. A key pasted anywhere else (a chat, an issue, a screenshot) should
    be treated as exposed and reissued, not reused.
  </p>
  <div class="row">
    <label><input type="radio" name="kalshi-env" value="demo"> Demo</label>
    <label><input type="radio" name="kalshi-env" value="prod"> Live (prod)</label>
  </div>
  <div class="row">
    <label for="key-id-input">Key ID</label>
    <input id="key-id-input" type="password" placeholder="leave blank to keep current value" size="36">
  </div>
  <div class="row">
    <label for="key-path-input">Private key path</label>
    <input id="key-path-input" placeholder="/path/to/key.pem" size="36">
  </div>
  <div class="row">
    <button class="action" id="save-settings">Save</button>
  </div>
  <div id="settings-status"></div>
</section>

<script>
const $ = (id) => document.getElementById(id);

function fmtUsd(value) {
  if (value === null || value === undefined) return "--";
  const n = Number(value);
  const cls = n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "";
  return `<span class="${cls}">$${n.toFixed(2)}</span>`;
}
function fmtPct(value) {
  return value === null || value === undefined ? "--" : (Number(value) * 100).toFixed(1) + "%";
}

async function getJson(url) {
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

async function refreshDatabases() {
  const { databases } = await getJson("/api/databases");
  const select = $("db-select");
  const previous = select.value;
  select.innerHTML = "";
  for (const db of databases) {
    const opt = document.createElement("option");
    opt.value = db.name;
    opt.textContent = `${db.name} (${db.kind})`;
    select.appendChild(opt);
  }
  if (databases.some((db) => db.name === previous)) select.value = previous;
  return databases;
}

function drawPnlChart(points) {
  const canvas = $("pnl-chart");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (points.length < 2) {
    ctx.fillStyle = "#9aa4b2";
    ctx.fillText("Not enough resolved trades yet for a chart.", 10, 20);
    return;
  }
  const values = points.map((p) => Number(p.cumulative_pnl_usd));
  const min = Math.min(0, ...values), max = Math.max(0, ...values);
  const pad = 24;
  const xStep = (canvas.width - 2 * pad) / (points.length - 1);
  const yScale = (canvas.height - 2 * pad) / ((max - min) || 1);
  const yOf = (v) => canvas.height - pad - (v - min) * yScale;
  ctx.strokeStyle = "#3a4152"; ctx.beginPath();
  ctx.moveTo(pad, yOf(0)); ctx.lineTo(canvas.width - pad, yOf(0)); ctx.stroke();
  ctx.strokeStyle = "#60a5fa"; ctx.lineWidth = 2; ctx.beginPath();
  values.forEach((v, i) => {
    const x = pad + i * xStep, y = yOf(v);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();
}

async function refreshMonitor() {
  const db = $("db-select").value;
  const note = $("monitor-note");
  if (!db) { note.textContent = "No databases found in the data directory yet."; return; }
  try {
    const s = await getJson(`/api/paper_summary?db=${encodeURIComponent(db)}`);
    note.textContent = "";
    $("summary-tiles").innerHTML = [
      ["Trades", s.trade_count], ["Resolved", s.resolved_count], ["Unresolved", s.unresolved_count],
      ["Win rate", fmtPct(s.win_rate)], ["Total PnL", fmtUsd(s.total_pnl_usd)],
      ["Windows seen", s.windows_seen], ["Windows traded", s.windows_traded],
    ].map(([label, value]) => `<div class="tile"><div class="label">${label}</div><div class="value">${value}</div></div>`).join("");
    drawPnlChart(s.cumulative_pnl);
    $("trades-table tbody").innerHTML = s.trades.map((t) => `<tr>
      <td>${t.ticker}</td><td>${t.side}</td><td>${t.size}</td><td>${t.entry_price}</td>
      <td>${t.entry_ts}</td><td>${t.result ?? "pending"}</td><td>${fmtUsd(t.pnl_usd)}</td></tr>`).join("");
  } catch (err) {
    note.textContent = `Error: ${err.message}`;
  }
}

async function runBacktest() {
  const db = $("db-select").value;
  const note = $("backtest-note");
  if (!db) { note.textContent = "No databases found in the data directory yet."; return; }
  const queue = $("queue-select").value;
  const fee = $("fee-input").value || "both";
  note.textContent = "Running...";
  try {
    const { reports } = await getJson(
      `/api/backtest?db=${encodeURIComponent(db)}&queue=${queue}&maker_fee_multiplier=${encodeURIComponent(fee)}`
    );
    note.textContent = "";
    $("backtest-table tbody").innerHTML = reports.map((r) => `<tr>
      <td>${r.queue_assumption}</td><td>${r.maker_fee_multiplier}</td><td>${r.trades}</td>
      <td>${fmtPct(r.win_rate)}</td><td>${fmtUsd(r.total_pnl_usd)}</td><td>${fmtUsd(r.max_drawdown_usd)}</td>
      <td>${r.trades_per_day === null ? "--" : Number(r.trades_per_day).toFixed(2)}</td>
      <td>${r.beats_trade_nothing === null ? "n/a" : (r.beats_trade_nothing ? "yes" : "no")}</td>
      <td>${r.sample_size_note}</td></tr>`).join("");
  } catch (err) {
    note.textContent = `Error: ${err.message}`;
  }
}

async function loadSettings() {
  const s = await getJson("/api/settings");
  document.querySelector(`input[name="kalshi-env"][value="${s.kalshi_env}"]`).checked = true;
  $("key-id-input").placeholder = s.key_id_set ? `current key ends in ...${s.key_id_last4}` : "not set";
  $("key-path-input").value = s.private_key_path || "";
  $("settings-status").innerHTML = `<div class="note">Settings file: ${s.env_file}${s.env_file_exists ? "" : " (does not exist yet -- Save will create it)"}</div>`;
}

async function saveSettings() {
  const payload = { kalshi_env: document.querySelector('input[name="kalshi-env"]:checked').value };
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
  } catch (err) {
    status.innerHTML = `<div class="error">Error: ${err.message}</div>`;
  }
}

document.querySelectorAll("nav button").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
  document.querySelectorAll("section").forEach((s) => s.classList.remove("active"));
  btn.classList.add("active");
  $(`tab-${btn.dataset.tab}`).classList.add("active");
  if (btn.dataset.tab === "monitor") refreshMonitor();
}));
$("refresh-databases").addEventListener("click", async () => { await refreshDatabases(); refreshMonitor(); });
$("db-select").addEventListener("change", refreshMonitor);
$("run-backtest").addEventListener("click", runBacktest);
$("save-settings").addEventListener("click", saveSettings);

(async function init() {
  await refreshDatabases();
  await refreshMonitor();
  await loadSettings();
  setInterval(() => { if ($("tab-monitor").classList.contains("active")) refreshMonitor(); }, 5000);
})();
</script>
</body>
</html>
"""
