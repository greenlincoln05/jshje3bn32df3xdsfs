"""Spot price feed. Placeholder for Phase 2: nothing is implemented yet.

Planned per docs/btc15m-bot-spec.md (sections 2, 3 and 5): a Coinbase WebSocket ticker feeding a rolling 1 s price
buffer, with staleness detection (skip trading when the feed is more than 3 s old). Kraken and Bitstamp may be added
for a median. Kalshi settles on CF Benchmarks' BRTI, so any spot feed is a proxy, and the gap is model error that has
to be measured.
"""
