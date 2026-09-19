"""Offline test for fetch_kalshi_settlements.py: httpx.MockTransport + a real settled-market
payload shape, never touching the network (same convention as tests/test_client.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fetch_kalshi_settlements import fetch_settled, write_csv  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "markets_settled.json").read_text())


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.url.params["series_ticker"] == "KXBTC15M"
    assert request.url.params["status"] == "settled"
    return httpx.Response(200, json=FIXTURE)


@pytest.mark.asyncio
async def test_fetch_settled_extracts_real_ground_truth(monkeypatch):
    import btcbot.kalshi_client as kc

    real_client_cls = kc.KalshiClient

    def patched(env, **kwargs):
        return real_client_cls(env, transport=httpx.MockTransport(_handler))

    monkeypatch.setattr("fetch_kalshi_settlements.KalshiClient", patched)

    rows = await fetch_settled(limit=0)

    assert len(rows) == 2
    first = rows[0]
    assert first["ticker"] == "KXBTC15M-26SEP180000-00"
    assert str(first["floor_strike"]) == "80000.12"
    assert first["expiration_value"] == "80125.50"
    assert first["result"] == "yes"


def test_write_csv_sorts_by_open_time(tmp_path):
    rows = [
        {"ticker": "b", "open_time": "2026-09-18T00:15:00+00:00", "close_time": "x", "floor_strike": 1, "expiration_value": "1", "result": "no"},
        {"ticker": "a", "open_time": "2026-09-18T00:00:00+00:00", "close_time": "x", "floor_strike": 1, "expiration_value": "1", "result": "yes"},
    ]
    out = tmp_path / "settlements.csv"
    write_csv(rows, out)
    lines = out.read_text().splitlines()
    assert lines[1].startswith("a,")
    assert lines[2].startswith("b,")
