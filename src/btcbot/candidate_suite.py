"""Frozen, named Strategy Lab candidates evaluated against one shared recording.

This is offline research. It reads SQLite recordings and never talks to an API or
places an order. Candidate files are deliberately explicit so every attempted
configuration remains countable after the data arrives.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from btcbot.backtest import BacktestError, ReplayData, prepare_replay, replay_prepared
from btcbot.config import BotConfig
from btcbot.lab import AccountSettings, LabError, LabParams, _config_for, _filters_for, _metrics, parse_values
from btcbot.models import parse_time
from btcbot.paper_broker import QueueAssumption


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    hypothesis: str
    params: LabParams


@dataclass(frozen=True, slots=True)
class CandidateSuite:
    version: int
    frozen_at: datetime
    description: str
    accounts: tuple[AccountSettings, ...]
    queue: QueueAssumption
    maker_fee_multiplier: Decimal
    candidates: tuple[Candidate, ...]


def load_candidate_suite(path: str | Path, base_config: BotConfig) -> CandidateSuite:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LabError("candidate suite must be a YAML object")
    assumptions = raw.get("assumptions") or {}
    account_values = assumptions.get("account_sizes", [assumptions.get("account_usd", 500)])
    if not isinstance(account_values, list) or not account_values:
        raise LabError("account_sizes must be a non-empty list")
    accounts = tuple(AccountSettings(
        account_usd=Decimal(str(value)),
        max_exposure_pct=Decimal(str(assumptions.get("max_exposure_pct", 25))),
        daily_loss_pct=Decimal(str(assumptions.get("daily_loss_pct", 10))),
    ) for value in account_values)
    queue = QueueAssumption(str(assumptions.get("queue", "optimistic")))
    fee = Decimal(str(assumptions.get("maker_fee_multiplier", "0.25")))
    if any(account.account_usd <= 0 for account in accounts) or fee < 0:
        raise LabError("account must be positive and maker fee multiplier non-negative")

    base = LabParams.from_config(base_config)
    candidates: list[Candidate] = []
    names: set[str] = set()
    for item in raw.get("candidates") or []:
        if not isinstance(item, dict) or not item.get("name"):
            raise LabError("each candidate needs a name")
        name = str(item["name"])
        if name in names:
            raise LabError(f"duplicate candidate name: {name}")
        names.add(name)
        overrides: dict[str, Any] = {}
        for key, value in (item.get("params") or {}).items():
            parsed = parse_values(str(key), [value])
            if len(parsed) != 1:
                raise LabError(f"{name}.{key}: expected one frozen value")
            overrides[str(key)] = parsed[0]
        candidates.append(Candidate(name, str(item.get("hypothesis", "")), replace(base, **overrides)))
    if not candidates:
        raise LabError("candidate suite has no candidates")
    return CandidateSuite(
        version=int(raw.get("version", 1)), frozen_at=parse_time(str(raw["frozen_at"])),
        description=str(raw.get("description", "")), accounts=accounts, queue=queue,
        maker_fee_multiplier=fee, candidates=tuple(candidates),
    )


def _wilson(wins: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.95996398454
    p = wins / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [centre - half, centre + half]


def run_candidate_suite(
    data: ReplayData, base_config: BotConfig, suite: CandidateSuite, *, after: datetime | None = None,
) -> dict[str, Any]:
    first_seen: dict[str, datetime] = {}
    for snapshot in data.snapshots:
        first_seen.setdefault(snapshot.ticker, snapshot.poll_ts)
    tickers = {ticker for ticker, ts in first_seen.items() if after is None or ts >= after}
    if not tickers:
        raise BacktestError("no recorded market windows meet the suite cutoff")

    cache: dict[float, Any] = {}
    results = []
    for account in suite.accounts:
        for candidate in suite.candidates:
            blend = candidate.params.model_blend
            if blend not in cache:
                cache[blend] = prepare_replay(data, base_config, tickers=tickers, model_blend=blend)
            prepared = cache[blend]
            replay = replay_prepared(
                prepared, _config_for(base_config, candidate.params, account),
                queue_assumption=suite.queue, maker_fee_multiplier=suite.maker_fee_multiplier,
                filters=_filters_for(candidate.params, account),
            )
            metrics = _metrics(replay, account.account_usd)
            resolved = [trade for trade in replay.trades if trade.pnl_usd is not None]
            wins = [trade.pnl_usd for trade in resolved if trade.pnl_usd > 0]
            losses = [trade.pnl_usd for trade in resolved if trade.pnl_usd < 0]
            results.append({
                "name": candidate.name,
                "account_usd": account.account_usd,
                "hypothesis": candidate.hypothesis,
                "params": asdict(candidate.params),
                "metrics": asdict(metrics),
                "win_rate_95": _wilson(len(wins), len(resolved)),
                "average_win": sum(wins, Decimal(0)) / len(wins) if wins else None,
                "average_loss": sum(losses, Decimal(0)) / len(losses) if losses else None,
                "evidence": "eligible_for_review" if len(resolved) >= 30 else "insufficient",
                "trades": [asdict(trade) for trade in replay.trades],
            })
    return {
        "suite_version": suite.version,
        "frozen_at": suite.frozen_at,
        "description": suite.description,
        "assumptions": {
            "accounts": [asdict(account) for account in suite.accounts], "queue": suite.queue.value,
            "maker_fee_multiplier": suite.maker_fee_multiplier,
        },
        "cutoff": after,
        "windows": len(tickers),
        "strategy_count": len(suite.candidates),
        "candidate_count": len(results),
        "results": results,
        "warning": "Ranking is exploratory until each candidate has at least 30 resolved post-freeze trades.",
    }


def render_candidate_suite(report: dict[str, Any]) -> str:
    lines = [
        f"Frozen suite v{report['suite_version']}: {report['candidate_count']} scenarios, "
        f"{report['windows']} windows.",
        "",
        f"{'candidate':<24} {'acct':>6} {'n':>4} {'win':>6} {'pnl':>10} {'t':>7} {'dd':>9} {'blocked':>8} {'evidence':>11}",
    ]
    for row in report["results"]:
        m = row["metrics"]
        win = "--" if m["win_rate"] is None else f"{m['win_rate'] * 100:.0f}%"
        t_stat = "--" if m["t_stat"] is None else f"{m['t_stat']:+.2f}"
        lines.append(
            f"{row['name']:<24} ${float(row['account_usd']):>5.0f} {m['resolved']:>4} {win:>6} ${float(m['pnl']):>9.2f} "
            f"{t_stat:>7} ${float(m['max_drawdown']):>8.2f} {m['filtered']['risk_blocked']:>8} "
            f"{row['evidence']:>11}"
        )
    lines += ["", report["warning"]]
    return "\n".join(lines)
