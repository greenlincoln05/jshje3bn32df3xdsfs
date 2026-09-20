# Frozen candidate suite v1

This suite compares 24 named, non-order strategies at four account sizes against one shared production
recording. It never calls a trading endpoint. The running production paper recorder
already captures every order-book snapshot and BTC spot tick needed by all candidates,
so starting 24 collectors would add API load without adding information.

The suite was frozen at `2026-09-20T03:00:31Z`. Only market windows first observed at
or after that timestamp count as forward confirmation. Do not edit v1 after inspecting
those results; copy it to a new version for the next hypothesis set.

The candidates cover six questions:

1. Does capping entries at 65 or 70 cents improve payoff asymmetry?
2. Are entries with 2–10 or 5–8 minutes left more reliable?
3. Does more model weight help, or does the market-price blend help?
4. Does BTC direction over 1m, 15m, 30m, 1h and 24h add value?
5. Do persistence, confidence and account-scaled sizing reduce drawdown?
6. Is the current regime contract-price momentum or a 3–8 cent panic fade?

The last question was added after reviewing Turbine's April 2026 report. Their report
found that a panic-fade family worked in one 30-day window while a price-threshold
family that had worked earlier collapsed after the window rolled. Their newer public
leaderboard shows momentum/VWAP families near the top. Those conflicting snapshots
are why this suite tests both directions on new local data instead of copying either
result.

Run the suite on one or more production recordings:

```powershell
btcbot --config docs/research/candidate-a.config.yaml lab-suite `
  --suite docs/research/candidate-suite-v1.yaml `
  --db data/paper-KXBTC15M-prod-YYYYMMDDTHHMMSSZ.sqlite `
  --after 2026-09-20T03:00:31Z `
  --output data/research/candidate-suite-v1.json
```

The 24 strategies produce 96 scenarios across $100, $500, $1,000 and $5,000 accounts.
The 5% minimum premium therefore scales from $5 to $25, $50 and $250. The output
includes every simulated trade. A candidate remains `insufficient` until it
has at least 30 resolved post-freeze trades. Use P&L, mean trade, Wilson win-rate
interval, t-statistic and drawdown together; raw P&L alone favors larger sizing. The
suite assumes a 25% exposure cap, a 10% daily loss halt, optimistic maker queue fills
and a 0.25 maker-fee multiplier.
Optimistic fills are an upper bound, so a candidate that fails there is rejected; a
candidate that passes still needs forward paper validation.
