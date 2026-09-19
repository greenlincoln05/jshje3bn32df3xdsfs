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


class _Strict(BaseModel):
    # Unknown keys are errors, so a typo like "min_egde" cannot silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Sizing(_Strict):
    contracts_per_trade: int = Field(5, gt=0)


class RiskLimits(_Strict):
    max_contracts_per_trade: int = Field(10, gt=0)
    max_open_exposure_usd: Decimal = Field(Decimal("25"), gt=0)
    daily_loss_limit_usd: Decimal = Field(Decimal("20"), gt=0)
    max_consecutive_losses: int = Field(5, gt=0)
    max_trades_per_hour: int = Field(12, gt=0)


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
    sizing: Sizing = Field(default_factory=Sizing)
    risk: RiskLimits = Field(default_factory=RiskLimits)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.min_tau_sec >= self.max_tau_sec:
            raise ValueError("min_tau_sec must be less than max_tau_sec")
        if self.sizing.contracts_per_trade > self.risk.max_contracts_per_trade:
            raise ValueError("sizing.contracts_per_trade exceeds risk.max_contracts_per_trade")
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
