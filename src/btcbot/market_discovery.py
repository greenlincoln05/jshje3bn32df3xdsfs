"""Find the market that is open right now in a rolling series, without hard-coding any ticker.

Kalshi lists a new KXBTC15M market every 15 minutes, so tickers change constantly. Each call asks the series
for its open markets and picks the one whose window contains "now".

The build spec suggested series -> events -> markets, but that path was measured to be unreliable. At the
2026-09-19 02:15Z rollover ``GET /events?status=open`` returned the finalized old event and first listed the new
window 60.0 s after it opened, whereas ``GET /markets?status=open`` listed it after 3.1 s. A ``/markets`` query by
close-time window (``min_close_ts``/``max_close_ts``, no status) is faster still (0.7 s) but bakes in the window
length. Markets carry their own ``event_ticker``, so the events hop adds nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from btcbot.models import Market


class MarketSource(Protocol):
    async def list_markets(self, *, series_ticker: str, status: str | None) -> list[Market]: ...


async def find_current_market(
    client: MarketSource,
    series_ticker: str,
    *,
    now: datetime | None = None,
) -> Market | None:
    """Return the market open for trading right now, or None between windows.

    The server-side ``status=open`` filter is not trusted on its own: the demo environment was seen
    returning a long-closed market under it. Each market is re-checked for status ``active`` and
    ``open_time <= now < close_time``, and the earliest-closing survivor wins.

    ``now`` defaults to the local UTC clock, so a skewed clock can misjudge a market for a moment around
    a window boundary. Callers already stay out of the last ``min_tau_sec`` seconds, so this is harmless.
    """
    when = now or datetime.now(timezone.utc)
    markets = await client.list_markets(series_ticker=series_ticker, status="open")
    open_markets = [market for market in markets if market.is_open_at(when)]
    return min(open_markets, key=lambda market: market.close_time, default=None)
