from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from btcbot.models import Balance, Market, OrderBook, ParseError, PriceLevel, Series, parse_time, to_decimal

UTC = timezone.utc


class TestParsingHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0.5600", Decimal("0.5600")),
            ("2170302.50", Decimal("2170302.50")),
            (1, Decimal(1)),
            (Decimal("2.5"), Decimal("2.5")),
            (81263.65, Decimal("81263.65")),  # a float that slipped through must not carry binary noise
            (None, None),
            ("", None),
        ],
    )
    def test_to_decimal(self, raw, expected):
        assert to_decimal(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "NaN", "Infinity", "-Infinity"])
    def test_to_decimal_rejects_non_numbers_and_non_finite_values(self, raw):
        with pytest.raises(ParseError):
            to_decimal(raw, "price")

    def test_parse_time_handles_z_suffix_and_fractional_seconds(self):
        assert parse_time("2026-09-19T01:30:00Z") == datetime(2026, 9, 19, 1, 30, tzinfo=UTC)
        assert parse_time("2026-09-19T01:15:00.583627Z") == datetime(2026, 9, 19, 1, 15, 0, 583627, tzinfo=UTC)

    def test_parse_time_normalises_offsets_to_utc_and_assumes_utc_when_naive(self):
        assert parse_time("2026-09-18T21:30:00-04:00") == datetime(2026, 9, 19, 1, 30, tzinfo=UTC)
        assert parse_time("2026-09-19T01:30:00").tzinfo == UTC

    @pytest.mark.parametrize("raw", ["", None, 12345, "yesterday"])
    def test_parse_time_rejects_junk(self, raw):
        with pytest.raises(ParseError):
            parse_time(raw)


class TestOrderBook:
    def test_top_of_book_from_a_real_prod_payload(self, load_fixture):
        book = OrderBook.from_api("KXBTC15M-TEST", load_fixture("orderbook_prod.json"))

        assert book.best_bid("yes") == PriceLevel(Decimal("0.5400"), Decimal("1670.79"))
        assert book.best_bid("no") == PriceLevel(Decimal("0.4500"), Decimal("6445.03"))
        # Kalshi lists bids only: a NO bid at 0.45 is a YES ask at 0.55, with the same size.
        assert book.best_ask("yes") == PriceLevel(Decimal("0.5500"), Decimal("6445.03"))
        assert book.best_ask("no") == PriceLevel(Decimal("0.4600"), Decimal("1670.79"))
        assert book.spread("yes") == Decimal("0.0100")
        assert book.spread("no") == Decimal("0.0100")
        assert book.mid("yes") == Decimal("0.5450")
        assert book.mid("no") == Decimal("0.4550")
        assert len(book.yes_bids) == len(book.no_bids) == 6

    def test_matches_the_worked_example_in_kalshis_docs(self):
        # docs.kalshi.com/getting_started/orderbook_responses: best YES ask = 1.00 - 0.56 = 0.44 and "the spread is $0.02".
        payload = {
            "orderbook_fp": {
                "yes_dollars": [
                    ["0.0100", "200.00"], ["0.1500", "100.00"], ["0.2000", "50.00"], ["0.2500", "20.00"],
                    ["0.3000", "11.00"], ["0.3100", "10.00"], ["0.3200", "10.00"], ["0.3300", "11.00"],
                    ["0.3400", "9.00"], ["0.3500", "11.00"], ["0.4100", "10.00"], ["0.4200", "13.00"],
                ],
                "no_dollars": [
                    ["0.0100", "100.00"], ["0.1600", "3.00"], ["0.2500", "50.00"], ["0.2800", "19.00"],
                    ["0.3600", "5.00"], ["0.3700", "50.00"], ["0.3800", "300.00"], ["0.4400", "29.00"],
                    ["0.4500", "20.00"], ["0.5600", "17.00"],
                ],
            }
        }
        book = OrderBook.from_api("KXHIGHNY-24JAN01-T60", payload)

        assert book.best_bid("yes") == PriceLevel(Decimal("0.4200"), Decimal("13.00"))
        assert book.best_ask("yes") == PriceLevel(Decimal("0.4400"), Decimal("17.00"))
        assert book.spread("yes") == Decimal("0.02")
        assert book.best_bid("no") == PriceLevel(Decimal("0.5600"), Decimal("17.00"))
        assert book.best_ask("no") == PriceLevel(Decimal("0.5800"), Decimal("13.00"))
        assert book.spread("no") == Decimal("0.02")

    def test_levels_are_sorted_ascending_even_if_the_api_order_changes(self):
        payload = {"orderbook_fp": {"yes_dollars": [["0.5000", "1.00"], ["0.1000", "2.00"], ["0.3000", "3.00"]], "no_dollars": []}}
        book = OrderBook.from_api("T", payload)
        assert [level.price for level in book.yes_bids] == [Decimal("0.1000"), Decimal("0.3000"), Decimal("0.5000")]
        assert book.best_bid("yes").size == Decimal("1.00")

    def test_empty_book_has_no_top_of_book(self):
        book = OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
        for side in ("yes", "no"):
            assert book.best_bid(side) is None
            assert book.best_ask(side) is None
            assert book.spread(side) is None
            assert book.mid(side) is None

    def test_one_sided_book_derives_the_ask_but_has_no_spread(self):
        book = OrderBook.from_api("T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.3000", "10.00"]]}})
        assert book.best_bid("yes") is None
        assert book.best_ask("yes") == PriceLevel(Decimal("0.7000"), Decimal("10.00"))
        assert book.spread("yes") is None
        assert book.mid("yes") is None

    def test_missing_side_key_is_treated_as_empty(self):
        assert OrderBook.from_api("T", {"orderbook_fp": {}}).best_bid("yes") is None

    def test_subpenny_prices_and_fractional_sizes_are_exact(self):
        payload = {"orderbook_fp": {"yes_dollars": [["0.0010", "2170302.50"]], "no_dollars": [["0.9990", "0.01"]]}}
        book = OrderBook.from_api("T", payload)
        assert book.best_ask("no") == PriceLevel(Decimal("0.9990"), Decimal("2170302.50"))  # 1 - 0.0010
        assert book.best_ask("yes") == PriceLevel(Decimal("0.0010"), Decimal("0.01"))  # 1 - 0.9990
        assert all(isinstance(level.price, Decimal) and isinstance(level.size, Decimal) for level in book.yes_bids)

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"orderbook": {"yes": []}},
            {"orderbook_fp": {"yes_dollars": [["0.5000"]], "no_dollars": []}},
            {"orderbook_fp": {"yes_dollars": [["x", "1.00"]], "no_dollars": []}},
            {"orderbook_fp": {"yes_dollars": [["0.5000", ""]], "no_dollars": []}},
        ],
    )
    def test_malformed_payloads_raise_parse_error(self, payload):
        with pytest.raises(ParseError):
            OrderBook.from_api("T", payload)


class TestMarket:
    @pytest.fixture
    def payload(self, load_fixture):
        return load_fixture("market_active.json")["market"]

    def test_parses_a_real_payload(self, payload):
        market = Market.from_api(payload)

        assert market.ticker == "KXBTC15M-26SEP182130-30"
        assert market.event_ticker == "KXBTC15M-26SEP182130"
        assert market.status == "active"
        assert market.strike == Decimal("81263.65")
        assert isinstance(market.strike, Decimal)
        assert market.strike_type == "greater_or_equal"
        assert market.open_time == datetime(2026, 9, 19, 1, 15, tzinfo=UTC)
        assert market.close_time == datetime(2026, 9, 19, 1, 30, tzinfo=UTC)
        assert market.volume == Decimal("2184245.00")
        assert market.open_interest == Decimal("359884.97")

    def test_missing_or_null_strike_is_none_not_an_error(self, payload):
        assert Market.from_api({**payload, "floor_strike": None}).strike is None
        assert Market.from_api({k: v for k, v in payload.items() if k != "floor_strike"}).strike is None

    @pytest.mark.parametrize("missing", ["ticker", "event_ticker", "status", "open_time", "close_time"])
    def test_missing_required_field_raises_parse_error(self, payload, missing):
        with pytest.raises(ParseError, match=missing):
            Market.from_api({k: v for k, v in payload.items() if k != missing})

    def test_is_open_at_requires_active_status_and_a_half_open_window(self, payload):
        market = Market.from_api(payload)
        opens, closes = market.open_time, market.close_time

        assert market.is_open_at(opens)  # open_time is inclusive
        assert market.is_open_at(closes - timedelta(microseconds=1))
        assert not market.is_open_at(closes)  # close_time is exclusive
        assert not market.is_open_at(opens - timedelta(seconds=1))
        for status in ("initialized", "inactive", "closed", "determined", "finalized"):
            assert not Market.from_api({**payload, "status": status}).is_open_at(opens + timedelta(minutes=1))

    def test_seconds_to_close(self, payload):
        market = Market.from_api(payload)
        assert market.seconds_to_close(market.close_time - timedelta(seconds=219, milliseconds=100)) == pytest.approx(219.1)
        assert market.seconds_to_close(market.close_time + timedelta(seconds=5)) == pytest.approx(-5.0)

    def test_raw_payload_is_kept_but_hidden_from_repr(self, payload):
        market = Market.from_api(payload)
        assert market.raw["price_level_structure"] == "tapered_deci_cent"
        assert "tapered_deci_cent" not in repr(market)


class TestSeriesBalance:
    def test_series_carries_the_fee_model(self, load_fixture):
        series = Series.from_api(load_fixture("series_kxbtc15m.json")["series"])
        assert series.ticker == "KXBTC15M"
        assert series.frequency == "fifteen_min"
        assert series.fee_type == "quadratic"
        assert series.fee_multiplier == Decimal(1)

    def test_series_requires_fee_fields(self):
        with pytest.raises(ParseError, match="fee_type"):
            Series.from_api({"ticker": "X", "fee_multiplier": 1})

    def test_balance_prefers_fixed_point_dollars(self):
        balance = Balance.from_api({"balance": 12345, "balance_dollars": "123.4500", "portfolio_value": 6789})
        assert balance.available == Decimal("123.4500")
        assert balance.portfolio_value == Decimal("67.89")

    def test_balance_falls_back_to_integer_cents(self):
        assert Balance.from_api({"balance": 12345}).available == Decimal("123.45")
