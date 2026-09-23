"""Offline parsing tests for HistoricalCutoff and MarketCandle (Part A of
docs/research/kalshi-history-backfill-handoff.md). Payloads below are copied from real live responses read
2026-09-23 (see the handoff doc and CLAUDE.md), not guessed."""

from decimal import Decimal as D

import pytest

from btcbot.models import HistoricalCutoff, MarketCandle, ParseError

CUTOFF_PAYLOAD = {
    "market_positions_last_updated_ts": "2026-07-24T00:00:00Z",
    "market_settled_ts": "2026-07-24T00:00:00Z",
    "orders_updated_ts": "2026-07-24T00:00:00Z",
    "trades_created_ts": "2026-07-24T00:00:00Z",
}

# Real /historical/markets/{ticker}/candlesticks response shape: bare field names throughout.
HISTORICAL_CANDLE_PAYLOAD = {
    "end_period_ts": 1784331960,
    "open_interest": "62594.52",
    "price": {"close": "0.5700", "high": "0.5800", "low": "0.3900", "mean": "0.5043", "open": "0.4200", "previous": None},
    "volume": "103991.58",
    "yes_ask": {"close": "0.5800", "high": "0.9990", "low": "0.4000", "open": "0.9990"},
    "yes_bid": {"close": "0.5700", "high": "0.5700", "low": "0.0000", "open": "0.0000"},
}

# Real /series/{series}/markets/{ticker}/candlesticks (live) response shape: volume_fp/open_interest_fp, AND
# every price/yes_bid/yes_ask sub-field suffixed _dollars -- the doc's own warning only covered the first rename.
LIVE_CANDLE_PAYLOAD = {
    "end_period_ts": 1790127960,
    "open_interest_fp": "142895.97",
    "price": {"close_dollars": "0.4300", "high_dollars": "0.5500", "low_dollars": "0.4000",
              "mean_dollars": "0.4635", "open_dollars": "0.5000"},
    "volume_fp": "196204.59",
    "yes_ask": {"close_dollars": "0.4400", "high_dollars": "1.0000", "low_dollars": "0.4100", "open_dollars": "1.0000"},
    "yes_bid": {"close_dollars": "0.4300", "high_dollars": "0.5100", "low_dollars": "0.0030", "open_dollars": "0.0030"},
}


class TestHistoricalCutoff:
    def test_parses_the_live_shape(self):
        c = HistoricalCutoff.from_api(CUTOFF_PAYLOAD)
        assert c.market_settled_ts.isoformat() == "2026-07-24T00:00:00+00:00"
        assert c.trades_created_ts == c.market_settled_ts == c.orders_updated_ts == c.market_positions_last_updated_ts

    def test_optional_fields_may_be_absent(self):
        c = HistoricalCutoff.from_api({"market_settled_ts": "2026-07-24T00:00:00Z", "trades_created_ts": "2026-07-24T00:00:00Z"})
        assert c.orders_updated_ts is None and c.market_positions_last_updated_ts is None

    @pytest.mark.parametrize("missing", ["market_settled_ts", "trades_created_ts"])
    def test_the_two_required_fields_are_parse_errors_if_missing(self, missing):
        p = dict(CUTOFF_PAYLOAD)
        del p[missing]
        with pytest.raises(ParseError):
            HistoricalCutoff.from_api(p)

    @pytest.mark.parametrize("field", ["orders_updated_ts", "market_positions_last_updated_ts"])
    def test_a_malformed_optional_field_is_a_parse_error_not_a_silent_none(self, field):
        p = {**CUTOFF_PAYLOAD, field: "not-a-date"}
        with pytest.raises(ParseError):
            HistoricalCutoff.from_api(p)


class TestMarketCandleHistoricalShape:
    def test_parses_bare_field_names(self):
        c = MarketCandle.from_api("T", HISTORICAL_CANDLE_PAYLOAD)
        assert c.price_close == D("0.5700") and c.price_previous is None
        assert c.yes_bid_close == D("0.5700") and c.yes_ask_open == D("0.9990")
        assert c.volume == D("103991.58") and c.open_interest == D("62594.52")
        assert c.end_ts.isoformat() == "2026-07-17T23:46:00+00:00"

    def test_a_missing_volume_is_a_parse_error_not_a_silent_zero(self):
        p = {k: v for k, v in HISTORICAL_CANDLE_PAYLOAD.items() if k != "volume"}
        with pytest.raises(ParseError, match="volume"):
            MarketCandle.from_api("T", p)

    def test_a_missing_open_interest_is_a_parse_error(self):
        p = {k: v for k, v in HISTORICAL_CANDLE_PAYLOAD.items() if k != "open_interest"}
        with pytest.raises(ParseError, match="open_interest"):
            MarketCandle.from_api("T", p)

    def test_all_price_fields_nullable_no_trades_that_minute(self):
        p = {**HISTORICAL_CANDLE_PAYLOAD, "price": {"close": None, "high": None, "low": None, "mean": None, "open": None, "previous": None}}
        c = MarketCandle.from_api("T", p)
        assert c.price_close is None and c.volume == D("103991.58")  # volume/open_interest still required

    def test_a_missing_price_group_entirely_is_all_none(self):
        p = {k: v for k, v in HISTORICAL_CANDLE_PAYLOAD.items() if k != "price"}
        c = MarketCandle.from_api("T", p)
        assert c.price_close is None and c.price_mean is None

    def test_a_non_object_price_group_is_a_parse_error(self):
        with pytest.raises(ParseError):
            MarketCandle.from_api("T", {**HISTORICAL_CANDLE_PAYLOAD, "price": "not an object"})

    def test_missing_end_period_ts_is_a_parse_error(self):
        p = {k: v for k, v in HISTORICAL_CANDLE_PAYLOAD.items() if k != "end_period_ts"}
        with pytest.raises(ParseError):
            MarketCandle.from_api("T", p)

    def test_an_unparseable_end_period_ts_is_a_parse_error(self):
        with pytest.raises(ParseError):
            MarketCandle.from_api("T", {**HISTORICAL_CANDLE_PAYLOAD, "end_period_ts": "not-a-number"})

    def test_a_null_bare_volume_falls_back_to_the_fp_sibling_instead_of_erroring(self):
        # A bare key present with an explicit JSON null (not merely absent) while the _fp sibling has real
        # data must still parse -- this mirrors the nested price/yes_bid/yes_ask field() helper's "accept
        # either shape" contract, which the bare volume/open_interest lookup didn't previously honor.
        p = {**HISTORICAL_CANDLE_PAYLOAD, "volume": None, "volume_fp": "103991.58"}
        c = MarketCandle.from_api("T", p)
        assert c.volume == D("103991.58")

    def test_a_null_bare_open_interest_falls_back_to_the_fp_sibling_instead_of_erroring(self):
        p = {**HISTORICAL_CANDLE_PAYLOAD, "open_interest": None, "open_interest_fp": "62594.52"}
        c = MarketCandle.from_api("T", p)
        assert c.open_interest == D("62594.52")


class TestMarketCandleLiveShape:
    def test_parses_dollars_suffixed_field_names(self):
        c = MarketCandle.from_api("T", LIVE_CANDLE_PAYLOAD)
        assert c.price_close == D("0.4300") and c.yes_bid_close == D("0.4300")
        assert c.volume == D("196204.59") and c.open_interest == D("142895.97")

    def test_live_price_previous_absent_is_none(self):
        # the live sample payload has no previous_dollars key at all (unlike the historical sample's explicit null)
        assert "previous_dollars" not in LIVE_CANDLE_PAYLOAD["price"] and "previous" not in LIVE_CANDLE_PAYLOAD["price"]
        c = MarketCandle.from_api("T", LIVE_CANDLE_PAYLOAD)
        assert c.price_previous is None

    def test_bare_key_is_preferred_if_somehow_both_are_present(self):
        p = {**LIVE_CANDLE_PAYLOAD, "price": {**LIVE_CANDLE_PAYLOAD["price"], "close": "0.9900"}}
        c = MarketCandle.from_api("T", p)
        assert c.price_close == D("0.9900")  # bare "close" wins over "close_dollars"
