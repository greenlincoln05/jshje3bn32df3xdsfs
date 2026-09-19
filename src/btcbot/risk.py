"""Risk limits, kill switch and position/PnL tracking. Placeholder, first needed for Phase 4: nothing is implemented yet.

Planned per docs/btc15m-bot-spec.md (section 5), checked before every order: per-trade and open-exposure caps, trades per
hour, a daily loss limit, a pause after consecutive losses, and a KILL file that cancels open orders and exits. Position
size must never increase after a loss. On any unhandled exception or auth failure: cancel open orders, log, exit non-zero.
"""
