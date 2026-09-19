# Multi-timeframe strategy findings

210 variants were screened across 3,000 Kalshi BTC 15-minute markets. None is established as a profitable live strategy. All PnL below is for five contracts per entry after estimated per-leg fees. The table uses the previously uninspected middle 1,000-market validation period (August 29–September 8 UTC).

| Rule near 60 cents; decision with 9 minutes left | Trades | Net PnL | With 1-cent extra slippage per fill |
|---|---:|---:|---:|
| Leader -> 80c | 83 | $-18.89 | $-25.73 |
| 15m agreement -> 80c | 53 | $-1.09 | $-5.55 |
| All four agree -> 80c | 21 | $-1.51 | $-3.20 |
| All four agree -> 99c | 21 | $0.86 | $-0.21 |
| 15m agreement -> 80c, one late switch | 53 | $-0.94 | $-6.97 |

The development-only winner was leader/minute 6/hold to settlement: +$33.75 in development, -$36.43 in validation. Selecting the best-looking historical result did not generalize.

One exploratory candidate was positive after slippage in all three periods: 15-minute momentum agrees with leader, decide three minutes after open, execute one minute later at 55–65 cents, hold to settlement. Results:

| Period | Trades | Net PnL | Extra slippage |
|---|---:|---:|---:|
| development | 74 | $19.29 | $15.73 |
| validation | 89 | $39.59 | $35.22 |
| previously_seen | 84 | $4.54 | $0.42 |

This candidate was identified after inspecting all 210 variants. The latest period retained only $0.42 under stress. It is a hypothesis for fresh validation, not an out-of-sample success claim.

## The math behind your trade

Under the modeled fee scenario, five contracts bought at 60 cents cost $3.09. Selling all five at 80 cents returns $3.94 after the exit fee: $0.85 profit. A total settlement loss costs $3.09. If outcomes were only target exits or complete losses, the required target-hit rate is about 78.4%. At a 99-cent exit, net profit is $1.85 and that simplified break-even rate is about 62.6%. Actual outcomes include partial exits, stops, slippage and unresolved trades.

A 40-cent stop would produce about a $1.18 loss for the five-contract position if it filled exactly there; the 80-cent target versus that stop would require about 58.1% winning trades. A stop instruction does not guarantee a 40-cent execution.

At any point, compare three choices using current probabilities, not the original entry price:

- Hold the current side: expected remaining payout = p_old.
- Sell and stay in cash: sell_bid minus sale fee.
- Sell and switch: sell_bid minus sale fee minus new_ask minus buy fee plus p_new.

Switch only if its estimated value exceeds BOTH holding and cash, with an allowance for uncertainty and slippage. For complementary outcomes p_new = 1 - p_old. The prior loss still belongs in your PnL, but it is not a reason to increase size or force a recovery trade.

The approximate model uses distance from the strike divided by volatility times the square root of remaining time, then maps that standardized distance into a probability. As time shrinks, the same favorable price distance usually means a higher estimated winning probability. A 24-hour downtrend can coexist with an upward 15-minute contract: settlement is relative to this window’s own opening benchmark.

## What other builders are doing

- [Simple Kalshi Bot](https://github.com/DeweyMarco/simple-kalshi-bot) combines previous-market direction with short-term spot momentum and trades their agreement in paper mode. This supports testing agreement as a hypothesis, not assuming it is profitable.
- [quant-kbtc](https://github.com/TylerShep/quant-kbtc) documents order-book imbalance plus 15-minute rate of change and volatility filtering. Its displayed performance is paper trading; it is not verified live profitability. Order-book filters need event/depth data beyond historical candles.
- [prediction-market-btc15](https://github.com/bikigrg11/prediction-market-btc15) describes fee-aware models and explicit anti-overfitting gates, but its author reports that automated strategies remain net-negative while manual trading produced account gains. This is a self-report, not an audited account.

## Interpretation and data limits

The live experiments keep a small fixed set of rules. Shorter trend agreement and taking 80 cents are worth measuring, but this sample does not prove that four timeframe confirmations or side-switching improve expected return. Candle exits use the next minute closing bid after an observed target; they do not assume a resting target fills merely because a candle high touches it. This coarse lag may miss a fast manual exit, but removing it without tick data would make fills look better without evidence.

Fees use 0.07 × quantity × price × (1-price), rounded up per order for the scenario. Exact account rounding/rebates and historical series schedules are not reconstructed. See [Kalshi fee rounding](https://docs.kalshi.com/getting_started/fee_rounding) and [Coinbase candle timestamp conventions](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles).

Full historical results: ../btc-research/trend-results.json and ../btc-research/trend-trades.json relative to the worktree; active fresh-data reports: data/live-research/latest-replay.json.
