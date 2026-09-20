"""Configuration.

Tunables live in ``config.yaml`` and are validated into :class:`BotConfig`. The Kalshi environment and
credentials are deliberately not part of that file: they come from ``KALSHI_*`` environment variables or
a gitignored ``.env`` file (:class:`KalshiSettings`), so the config can be shared without secrets.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """config.yaml is missing, is not valid YAML, or failed validation."""


class Mode(StrEnum):
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class KalshiEnv(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class SizingMode(StrEnum):
    FIXED = "fixed"  # always contracts_per_trade
    KELLY = "kelly"  # fractional Kelly on max_open_exposure_usd; see btcbot.strategy.kelly_size
    RAMP = "ramp"  # contracts_per_trade, +max(1, ramp_growth_pct%) after each settled win, back to base after a loss
    PERCENT = "percent"  # a percent of the current account per order, growing with settled wins; see percent_size


class _Strict(BaseModel):
    # Unknown keys are errors, so a typo like "min_egde" cannot silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Sizing(_Strict):
    contracts_per_trade: int = Field(5, gt=0)
    mode: SizingMode = SizingMode.RAMP
    # Fraction of full Kelly to actually stake when mode is "kelly" -- see kelly_fraction()'s docstring for
    # why staking full Kelly against an uncalibrated model is dangerous, especially near a price of 0 or 1.
    kelly_fraction_multiplier: float = Field(0.2, gt=0.0, le=1.0)
    # mode "percent": stake risk_pct_per_trade percent of the CURRENT account per order. The account starts at
    # account_usd and moves only with SETTLED profit and loss. After a win the next order may grow by at most
    # max_growth_per_win_pct percent; after a loss it can never grow (btcbot.strategy.percent_size).
    ramp_growth_pct: Decimal = Field(Decimal("20"), ge=0, le=100)  # mode "ramp": growth per settled win (min +1)
    # Ramp risk rules: each win-level of the ramp lowers the highest price the bot will pay by ramp_max_price_step
    # (never below ramp_max_price_floor), because a big order at a dear price risks a lot to win a little. And if the
    # bot opens no position in ramp_idle_reset_windows consecutive windows while ramped up, the ramp resets to base
    # (0 turns that off): a ramp that finds no trade has drifted away from what the market is offering.
    ramp_max_price_step: Decimal = Field(Decimal("0.03"), ge=0, lt=1)
    ramp_max_price_floor: Decimal = Field(Decimal("0.60"), gt=0, le=1)
    ramp_idle_reset_windows: int = Field(2, ge=0)
    account_usd: Decimal = Field(Decimal("500"), gt=0)
    risk_pct_per_trade: Decimal = Field(Decimal("2"), gt=0, le=10)
    max_growth_per_win_pct: Decimal = Field(Decimal("20"), ge=0, le=100)


class RiskLimits(_Strict):
    max_contracts_per_trade: int = Field(10, gt=0)
    max_open_exposure_usd: Decimal = Field(Decimal("25"), gt=0)
    daily_loss_limit_usd: Decimal = Field(Decimal("20"), gt=0)
    max_consecutive_losses: int = Field(5, gt=0)
    max_trades_per_hour: int = Field(12, gt=0)
    # Optional account-relative versions of the two dollar limits above. When set (and the trader knows the
    # account value, as in sizing mode "percent") the limit is that percent of the CURRENT account, so the caps
    # rise and fall with it; the dollar value is then only the fallback. Without this a fixed dollar cap would stop
    # bets from ever growing with the account.
    max_open_exposure_pct: Decimal | None = Field(None, gt=0, le=100)
    daily_loss_limit_pct: Decimal | None = Field(None, gt=0, le=100)


class ExitRules(_Strict):
    """Optional early exit, off unless a threshold is set (the default is still hold-to-settlement,
    per strategy.py's module docstring). See btcbot.strategy.should_exit for the shared pure decision
    function a backtest/lab replay and (later) a live trader both go through, per
    docs/research/stop-loss-handoff.md. Wired into btcbot.backtest's replay only for now; btcbot paper/demo
    are unchanged until that handoff's later steps land."""

    stop_loss_pct: Decimal | None = Field(None, gt=0, le=100)  # exit if the mark (best bid of the held side) falls this % below entry
    take_profit_pct: Decimal | None = Field(None, gt=0)  # exit if the mark rises this % above entry
    stop_min_hold_sec: int = Field(0, ge=0)  # do not exit before holding a position at least this long
    stop_min_tau_sec: int = Field(0, ge=0)  # do not exit within this many seconds of close; hold to settlement instead
    # With ramp sizing the stop tightens by this many percentage points per ramp level, never below the floor.
    stop_loss_tighten_pct_per_level: Decimal = Field(Decimal("8"), ge=0)
    stop_loss_floor_pct: Decimal = Field(Decimal("10"), gt=0, le=100)


class BotConfig(_Strict):
    mode: Mode = Mode.PAPER
    series_ticker: str = Field("KXBTC15M", min_length=1)
    spot_feeds: tuple[Literal["coinbase", "kraken", "bitstamp"], ...] = Field(("coinbase",), min_length=1)
    vol_window_sec: int = Field(900, gt=0)
    vol_method: Literal["ewma"] = "ewma"
    model_blend: float = Field(0.5, ge=0.0, le=1.0)
    min_edge: Decimal = Field(Decimal("0.04"), ge=0, lt=1)
    min_depth: Decimal = Field(Decimal("10"), ge=0)  # contracts; Kalshi counts can be fractional
    max_spread: Decimal = Field(Decimal("0.06"), gt=0, le=1)
    min_tau_sec: int = Field(30, ge=0)
    max_tau_sec: int = Field(780, gt=0, le=900)  # a window is 900s long
    cancel_before_close_sec: int = Field(20, ge=0)
    # A flat min_edge is not risk-adjusted: the same edge is a small, capped win against a much larger loss
    # (or the reverse) once price is near 0 or 1, and the model has no calibration check at those extremes
    # yet (see btcbot.strategy's module docstring and btcbot.calibrate). Refusing the extremes by default
    # is a reasoned starting guardrail, not a backtested-optimal cutoff -- tune it with `btcbot lab`.
    min_price: Decimal | None = Field(Decimal("0.15"), ge=0, lt=1)
    max_price: Decimal | None = Field(Decimal("0.85"), gt=0, le=1)
    sizing: Sizing = Field(default_factory=Sizing)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    exit: ExitRules = Field(default_factory=ExitRules)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.min_tau_sec >= self.max_tau_sec:
            raise ValueError("min_tau_sec must be less than max_tau_sec")
        if self.sizing.contracts_per_trade > self.risk.max_contracts_per_trade:
            raise ValueError("sizing.contracts_per_trade exceeds risk.max_contracts_per_trade")
        if self.min_price is not None and self.max_price is not None and self.min_price >= self.max_price:
            raise ValueError("min_price must be less than max_price")
        return self


class KalshiSettings(BaseSettings):
    """Kalshi environment and credentials, from ``KALSHI_*`` env vars or a gitignored ``.env`` file.

    ``key_id`` is a ``SecretStr`` so it is masked if these settings are ever printed or logged. The
    private key itself is never read into settings, only its path.
    """

    model_config = SettingsConfigDict(
        env_prefix="KALSHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # a blank `KALSHI_PRIVATE_KEY_PATH=` line must mean "unset", not Path(".")
    )

    env: KalshiEnv = KalshiEnv.DEMO
    key_id: SecretStr | None = None
    private_key_path: Path | None = None


def load_config(path: str | Path = "config.yaml") -> BotConfig:
    """Load and validate ``config.yaml``. Missing files are an error, never a silent fall-back to defaults."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p} (run from the project root or pass --config)")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: invalid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: top level must be a mapping of settings")
    try:
        return BotConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<config>'}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"{p}: {problems}") from exc
