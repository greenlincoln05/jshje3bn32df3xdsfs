from datetime import datetime, timedelta, timezone

import pytest

from btcbot.market_discovery import find_current_market
from btcbot.models import Market

UTC = timezone.utc
NOW = datetime(2026, 9, 19, 1, 30, 41, tzinfo=UTC)


def market(ticker: str, *, status: str = "active", opens: str = "01:30:00", closes: str = "01:45:00") -> Market:
    return Market.from_api(
        {
            "ticker": ticker,
            "event_ticker": ticker.rsplit("-", 1)[0],
            "status": status,
            "open_time": f"2026-09-19T{opens}Z",
            "close_time": f"2026-09-19T{closes}Z",
            "floor_strike": 81238.12,
        }
    )


class StubClient:
    """Stands in for KalshiClient.list_markets and records how it was called."""

    def __init__(self, markets: list[Market]) -> None:
        self.markets = markets
        self.calls: list[dict] = []

    async def list_markets(self, **kwargs) -> list[Market]:
        self.calls.append(kwargs)
        return self.markets


async def test_asks_the_series_for_open_markets_without_any_hard_coded_ticker():
    client = StubClient([market("KXBTC15M-26SEP182145-45")])

    found = await find_current_market(client, "KXBTC15M", now=NOW)

    assert found.ticker == "KXBTC15M-26SEP182145-45"
    assert client.calls == [{"series_ticker": "KXBTC15M", "status": "open"}]


async def test_returns_none_when_nothing_is_listed():
    assert await find_current_market(StubClient([]), "KXBTC15M", now=NOW) is None


async def test_ignores_a_market_that_has_not_opened_yet():
    upcoming = market("KXBTC15M-26SEP182200-00", status="initialized", opens="01:45:00", closes="02:00:00")
    assert await find_current_market(StubClient([upcoming]), "KXBTC15M", now=NOW) is None


async def test_does_not_trust_the_server_side_open_filter():
    # Seen on demo: a market closed months ago came back under status=open. It must be ignored.
    stale = market("KXBTC15M-26MAY050000-00", status="closed", opens="00:00:00", closes="01:00:00")
    current = market("KXBTC15M-26SEP182145-45")

    found = await find_current_market(StubClient([stale, current]), "KXBTC15M", now=NOW)

    assert found.ticker == "KXBTC15M-26SEP182145-45"


async def test_a_finalized_market_listed_alongside_the_new_one_is_skipped():
    # At a live rollover the previous window's market lingered (finalized) next to the newly active one.
    old = market("KXBTC15M-26SEP182130-30", status="finalized", opens="01:15:00", closes="01:30:00")
    new = market("KXBTC15M-26SEP182145-45")

    found = await find_current_market(StubClient([old, new]), "KXBTC15M", now=NOW)

    assert found.ticker == "KXBTC15M-26SEP182145-45"


async def test_a_paused_market_is_not_current():
    paused = market("KXBTC15M-26SEP182145-45", status="inactive")
    assert await find_current_market(StubClient([paused]), "KXBTC15M", now=NOW) is None


async def test_time_check_applies_even_when_status_says_active():
    # A market still flagged active a moment after its close_time must not be reported as current.
    lingering = market("KXBTC15M-26SEP182130-30", opens="01:15:00", closes="01:30:00")
    assert await find_current_market(StubClient([lingering]), "KXBTC15M", now=NOW) is None


async def test_earliest_closing_open_market_wins():
    later = market("KXBTC15M-26SEP182145-45", opens="01:00:00", closes="01:45:00")
    sooner = market("KXBTC15M-26SEP182140-40", opens="01:00:00", closes="01:40:00")

    found = await find_current_market(StubClient([later, sooner]), "KXBTC15M", now=NOW)

    assert found.ticker == "KXBTC15M-26SEP182140-40"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 9, 19, 1, 30, 0, tzinfo=UTC), "KXBTC15M-26SEP182145-45"),  # rollover: new window opens
        (datetime(2026, 9, 19, 1, 29, 59, 999999, tzinfo=UTC), "KXBTC15M-26SEP182130-30"),  # last instant of old one
    ],
)
async def test_window_rollover_boundary(now, expected):
    old = market("KXBTC15M-26SEP182130-30", opens="01:15:00", closes="01:30:00")
    new = market("KXBTC15M-26SEP182145-45", opens="01:30:00", closes="01:45:00")

    found = await find_current_market(StubClient([old, new]), "KXBTC15M", now=now)

    assert found.ticker == expected


async def test_defaults_to_the_current_clock():
    now = datetime.now(UTC)
    live = Market.from_api(
        {
            "ticker": "KXBTC15M-LIVE-00",
            "event_ticker": "KXBTC15M-LIVE",
            "status": "active",
            "open_time": (now - timedelta(minutes=1)).isoformat(),
            "close_time": (now + timedelta(minutes=14)).isoformat(),
        }
    )
    assert (await find_current_market(StubClient([live]), "KXBTC15M")).ticker == "KXBTC15M-LIVE-00"
