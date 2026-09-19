"""Fair-probability model. Placeholder for Phase 3: nothing is implemented yet.

Planned per docs/btc15m-bot-spec.md (section 4): a plug-in interface `predict(state) -> float`. Version 1 applies a
standard normal CDF to ln(S/K) scaled by EWMA per-second volatility over the effective time remaining, clamped to
[0.02, 0.98], with special handling once part of the closing 60 s average is already observed, and a blend with the
market mid. Every prediction is logged with its inputs so the Brier score and a reliability curve can be computed
after settlement.
"""
