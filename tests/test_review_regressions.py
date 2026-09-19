from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from btcbot import cli, market_discovery
from btcbot.kalshi_client import KalshiClient
from btcbot.models import Market, OrderBook, ParseError


async def test_market_listing_reads_every_page(load_fixture):
    payload = load_fixture("market_active.json")["market"]
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"markets": [payload], "cursor": "page2"})
        return httpx.Response(200, json={"markets": [{**payload, "ticker": "SECOND"}], "cursor": ""})

    async with KalshiClient(transport=httpx.MockTransport(handler)) as client:
        markets = await client.list_markets(series_ticker="KXBTC15M", status="settled")
    assert [m.ticker for m in markets] == [payload["ticker"], "SECOND"]
    assert calls[1] == {"series_ticker": "KXBTC15M", "status": "settled", "limit": "1000", "cursor": "page2"}


@pytest.mark.parametrize("payload", [{}, {"markets": None}, {"markets": {}}, {"markets": [None]}])
async def test_malformed_market_list_is_not_a_successful_empty_result(payload):
    async with KalshiClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ParseError):
            await client.list_markets(series_ticker="S")


async def test_discovery_samples_clock_after_network_delay(monkeypatch, load_fixture):
    payload = load_fixture("market_active.json")["market"]
    old = Market.from_api(payload)
    new = Market.from_api({**payload, "ticker": "NEXT", "open_time": old.close_time.isoformat(),
                           "close_time": (old.close_time + timedelta(minutes=15)).isoformat()})
    current = old.close_time - timedelta(seconds=1)

    class Clock:
        @staticmethod
        def now(tz):
            return current

    class Client:
        async def list_markets(self, **kwargs):
            nonlocal current
            current = old.close_time + timedelta(seconds=1)
            return [old, new]

    monkeypatch.setattr(market_discovery, "datetime", Clock)
    assert await market_discovery.find_current_market(Client(), "KXBTC15M") == new


@pytest.mark.parametrize("raw", [None, [], "bad", 1])
def test_malformed_book_object_raises_parse_error(raw):
    with pytest.raises(ParseError):
        OrderBook.from_api("T", {"orderbook_fp": raw})


@pytest.mark.parametrize("levels", [1, "12", {}, ["12"], [["1.1", "1"]],
                                      [["-0.1", "1"]], [["0.5", "-1"]],
                                      [["0.5", "1"], ["0.50", "2"]]])
def test_invalid_levels_cannot_create_quotes(levels):
    with pytest.raises(ParseError):
        OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": levels}})


def test_zero_size_level_is_not_executable_liquidity():
    book = OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": [["0.5", "1"], ["0.9", "0"]]}})
    assert str(book.best_bid("yes").price) == "0.5"


@pytest.mark.parametrize("interval", ["nan", "inf", "-inf"])
def test_nonfinite_watch_interval_is_rejected(interval, capsys, monkeypatch):
    async def unexpected_network(args):
        pytest.fail("invalid interval reached the command handler")
    monkeypatch.setattr(cli, "_cmd_discover", unexpected_network)
    assert cli.main(["discover", f"--watch={interval}"]) == 2
    assert "--watch" in capsys.readouterr().err


@pytest.mark.parametrize("watch", [False, True])
async def test_quote_fetch_crossing_close_does_not_display_tradable_quotes(watch, monkeypatch, capsys, load_fixture):
    payload = load_fixture("market_active.json")["market"]
    market = Market.from_api(payload)
    current = market.close_time - timedelta(seconds=1)

    class Clock:
        @staticmethod
        def now(tz):
            return current

    class Client:
        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_markets(self, **kwargs):
            return [market]

        async def get_series(self, ticker):
            return None

        async def get_orderbook(self, ticker):
            nonlocal current
            current = market.close_time + timedelta(seconds=1)
            return OrderBook.from_api(ticker, load_fixture("orderbook_prod.json"))

    class StopWatch(Exception):
        pass

    async def stop_sleep(interval):
        raise StopWatch

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(market_discovery, "datetime", Clock)
    monkeypatch.setattr(cli, "KalshiClient", Client)
    monkeypatch.setattr(cli, "load_config", lambda path: SimpleNamespace(series_ticker="KXBTC15M"))
    if watch:
        monkeypatch.setattr(cli.asyncio, "sleep", stop_sleep)
        with pytest.raises(StopWatch):
            await cli._watch(Client(), "KXBTC15M", 1)
    else:
        args = SimpleNamespace(config="unused", series=None, env="prod", watch=None)
        assert await cli._cmd_discover(args) == 1
    captured = capsys.readouterr()
    assert "closed during quote fetch" in captured.out + captured.err
    assert "YES" not in captured.out
