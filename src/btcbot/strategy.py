"""Entry and exit decisions. Placeholder, first needed for Phase 4: nothing is implemented yet.

Planned per docs/btc15m-bot-spec.md (section 5): for YES and NO compute edge = p_side - price_paid - expected_fee, trade
only when edge >= min_edge and the book is deep enough, prefer resting (maker) orders over crossing the spread, cancel
unfilled orders before the close, and hold to settlement by default.
"""
