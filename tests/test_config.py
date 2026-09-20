from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from btcbot.config import BotConfig, ConfigError, KalshiEnv, KalshiSettings, Mode, SizingMode, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_yaml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestBotConfig:
    def test_shipped_config_yaml_holds_the_spec_values(self):
        cfg = load_config(PROJECT_ROOT / "config.yaml")

        assert cfg.mode is Mode.PAPER
        assert cfg.series_ticker == "KXBTC15M"
        assert cfg.spot_feeds == ("coinbase",)
        assert (cfg.vol_window_sec, cfg.vol_method, cfg.model_blend) == (900, "ewma", 0.5)
        assert (cfg.min_edge, cfg.min_depth, cfg.max_spread) == (Decimal("0.04"), Decimal(10), Decimal("0.06"))
        assert (cfg.min_tau_sec, cfg.max_tau_sec, cfg.cancel_before_close_sec) == (30, 780, 20)
        assert (cfg.min_price, cfg.max_price) == (Decimal("0.15"), Decimal("0.85"))
        assert cfg.sizing.contracts_per_trade == 5
        assert cfg.sizing.mode is SizingMode.PERCENT
        assert cfg.sizing.kelly_fraction_multiplier == 0.2
        assert cfg.risk.max_contracts_per_trade == 10
        assert cfg.risk.max_open_exposure_usd == Decimal(25)
        assert cfg.risk.daily_loss_limit_usd == Decimal(20)
        assert cfg.risk.max_open_exposure_pct == Decimal(5)
        assert cfg.risk.daily_loss_limit_pct == Decimal(4)
        assert cfg.risk.max_consecutive_losses == 5
        assert cfg.risk.max_trades_per_hour == 12
        assert cfg.exit.stop_loss_pct is None and cfg.exit.take_profit_pct is None
        assert cfg.exit.stop_min_hold_sec == 0 and cfg.exit.stop_min_tau_sec == 0

    def test_code_defaults_and_shipped_file_agree(self):
        assert load_config(PROJECT_ROOT / "config.yaml") == BotConfig()

    def test_default_mode_is_paper(self):
        assert BotConfig().mode is Mode.PAPER

    def test_enums_render_as_their_plain_values(self):
        # (str, Enum) prints as "Mode.PAPER" in f-strings on Python 3.12+; StrEnum must not.
        assert f"{Mode.PAPER}" == str(Mode.PAPER) == "paper"
        assert f"{KalshiEnv.DEMO}" == str(KalshiEnv.DEMO) == "demo"
        assert f"{SizingMode.KELLY}" == str(SizingMode.KELLY) == "kelly"

    def test_yaml_floats_become_exact_decimals(self, tmp_path):
        cfg = load_config(write_yaml(tmp_path, "min_edge: 0.04\nmax_spread: 0.06\n"))
        assert isinstance(cfg.min_edge, Decimal)
        assert str(cfg.min_edge) == "0.04"  # not 0.040000000000000000832...
        assert str(cfg.max_spread) == "0.06"

    def test_empty_file_means_all_defaults(self, tmp_path):
        assert load_config(write_yaml(tmp_path, "")) == BotConfig()

    def test_typo_in_a_key_is_an_error_not_a_silent_default(self, tmp_path):
        with pytest.raises(ConfigError, match="min_egde"):
            load_config(write_yaml(tmp_path, "min_egde: 0.10\n"))

    def test_typo_in_a_nested_key_is_an_error(self, tmp_path):
        with pytest.raises(ConfigError, match="max_trades_per_hr"):
            load_config(write_yaml(tmp_path, "risk:\n  max_trades_per_hr: 3\n"))

    def test_unknown_mode_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="mode"):
            load_config(write_yaml(tmp_path, "mode: yolo\n"))

    @pytest.mark.parametrize(
        "text",
        [
            "model_blend: 1.5",
            "model_blend: -0.1",
            "min_edge: -0.01",
            "min_edge: 1",
            "max_spread: 0",
            "max_tau_sec: 901",
            "min_tau_sec: -1",
            "spot_feeds: []",
            "spot_feeds: [binance]",
            "vol_method: garch",
            "sizing:\n  contracts_per_trade: 0",
            "risk:\n  daily_loss_limit_usd: 0",
            "risk:\n  max_consecutive_losses: 0",
            "min_price: -0.01",
            "min_price: 1",
            "max_price: 0",
            "max_price: 1.01",
            "sizing:\n  mode: yolo",
            "sizing:\n  kelly_fraction_multiplier: 0",
            "sizing:\n  kelly_fraction_multiplier: 1.01",
            "exit:\n  stop_loss_pct: 0",
            "exit:\n  stop_loss_pct: 101",
            "exit:\n  take_profit_pct: 0",
            "exit:\n  stop_min_hold_sec: -1",
            "exit:\n  stop_min_tau_sec: -1",
        ],
    )
    def test_out_of_range_values_are_rejected(self, tmp_path, text):
        with pytest.raises(ConfigError):
            load_config(write_yaml(tmp_path, text + "\n"))

    def test_exit_rules_can_be_configured(self, tmp_path):
        cfg = load_config(write_yaml(tmp_path, "exit:\n  stop_loss_pct: 20\n  take_profit_pct: 40\n"
                                                "  stop_min_hold_sec: 30\n  stop_min_tau_sec: 60\n"))
        assert cfg.exit.stop_loss_pct == Decimal(20) and cfg.exit.take_profit_pct == Decimal(40)
        assert cfg.exit.stop_min_hold_sec == 30 and cfg.exit.stop_min_tau_sec == 60

    def test_min_price_must_be_below_max_price(self, tmp_path):
        with pytest.raises(ConfigError, match="min_price"):
            load_config(write_yaml(tmp_path, "min_price: 0.80\nmax_price: 0.20\n"))

    def test_price_band_can_be_disabled_on_either_side(self, tmp_path):
        cfg = load_config(write_yaml(tmp_path, "min_price: null\nmax_price: null\n"))
        assert cfg.min_price is None and cfg.max_price is None

    def test_min_tau_must_be_below_max_tau(self, tmp_path):
        with pytest.raises(ConfigError, match="min_tau_sec"):
            load_config(write_yaml(tmp_path, "min_tau_sec: 400\nmax_tau_sec: 300\n"))

    def test_trade_size_cannot_exceed_the_risk_cap(self, tmp_path):
        text = "sizing:\n  contracts_per_trade: 11\nrisk:\n  max_contracts_per_trade: 10\n"
        with pytest.raises(ConfigError, match="contracts_per_trade"):
            load_config(write_yaml(tmp_path, text))

    def test_config_is_immutable(self):
        with pytest.raises(ValidationError):
            BotConfig().min_edge = Decimal("0.5")

    def test_missing_file_is_an_error_not_defaults(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "absent.yaml")

    def test_invalid_yaml(self, tmp_path):
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config(write_yaml(tmp_path, "mode: [unclosed\n"))

    def test_top_level_must_be_a_mapping(self, tmp_path):
        with pytest.raises(ConfigError, match="mapping"):
            load_config(write_yaml(tmp_path, "- a\n- b\n"))


class TestKalshiSettings:
    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for var in ("KALSHI_ENV", "KALSHI_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"):
            monkeypatch.delenv(var, raising=False)

    def test_defaults_to_the_demo_environment_with_no_credentials(self):
        settings = KalshiSettings(_env_file=None)
        assert settings.env is KalshiEnv.DEMO
        assert settings.key_id is None
        assert settings.private_key_path is None

    def test_reads_environment_variables(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KALSHI_ENV", "prod")
        monkeypatch.setenv("KALSHI_KEY_ID", "abc-123-secret")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(tmp_path / "k.key"))

        settings = KalshiSettings(_env_file=None)

        assert settings.env is KalshiEnv.PROD
        assert settings.key_id.get_secret_value() == "abc-123-secret"
        assert settings.private_key_path == tmp_path / "k.key"

    def test_key_id_is_masked_when_printed(self, monkeypatch):
        monkeypatch.setenv("KALSHI_KEY_ID", "abc-123-secret")
        assert "abc-123-secret" not in repr(KalshiSettings(_env_file=None))

    def test_unknown_environment_is_rejected(self, monkeypatch):
        monkeypatch.setenv("KALSHI_ENV", "staging")
        with pytest.raises(ValidationError):
            KalshiSettings(_env_file=None)

    def test_blank_values_mean_unset(self, monkeypatch):
        monkeypatch.setenv("KALSHI_KEY_ID", "")
        monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "")  # must not become Path(".")
        settings = KalshiSettings(_env_file=None)
        assert settings.key_id is None
        assert settings.private_key_path is None

    def test_reads_a_dotenv_file(self, tmp_path):
        dotenv = tmp_path / ".env"
        dotenv.write_text("KALSHI_ENV=prod\nUNRELATED=1\n", encoding="utf-8")
        assert KalshiSettings(_env_file=dotenv).env is KalshiEnv.PROD

    @pytest.mark.parametrize(
        "line",
        [
            r"KALSHI_PRIVATE_KEY_PATH=C:\Users\me\temp\kalshi.key",  # unquoted: backslashes are kept verbatim
            'KALSHI_PRIVATE_KEY_PATH="C:/Users/me/temp/kalshi.key"',  # forward slashes are safe even when quoted
        ],
        ids=["unquoted-backslashes", "quoted-forward-slashes"],
    )
    def test_windows_key_paths_survive_dotenv_parsing(self, tmp_path, line):
        dotenv = tmp_path / ".env"
        dotenv.write_text(line + "\n", encoding="utf-8")
        parsed = str(KalshiSettings(_env_file=dotenv).private_key_path)
        assert "\t" not in parsed
        assert parsed.replace("\\", "/") == "C:/Users/me/temp/kalshi.key"

    def test_shipped_env_example_parses_and_means_demo_without_credentials(self):
        settings = KalshiSettings(_env_file=PROJECT_ROOT / ".env.example")
        assert settings.env is KalshiEnv.DEMO
        assert settings.key_id is None and settings.private_key_path is None
