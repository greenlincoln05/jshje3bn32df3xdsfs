# September 20 demo loss streak review

Source: `demo-KXBTC15M-demo-20260920T040724Z.sqlite`. This is a diagnostic of demo
execution, not validation evidence for a trading edge.

## What stopped

The recorder did not stop. It continued to store order-book snapshots, spot ticks,
settlements, and predictions through 12:51 UTC. The ledger stopped receiving new
trades after the final loss resolved at the 06:45 UTC close because the configured
`risk.max_consecutive_losses: 5` pauses new orders for a manual restart. The daily
loss limit did not cause it: the five-loss total was $3.862 against a $20 limit.

## Five-loss sequence

| UTC fill | side | entry | filled contracts | result | P&L | probability at placement |
| --- | --- | ---: | ---: | --- | ---: | ---: |
| 05:37:25 | YES | $0.22 | 6.00 | NO | -$1.320 | 39.8% YES |
| 05:49:11 | YES | $0.20 | 2.46 | NO | -$0.492 | 29.9% YES |
| 06:02:43 | NO | $0.25 | 1.00 | YES | -$0.250 | 43.2% NO |
| 06:20:30 | YES | $0.18 | 5.00 | NO | -$0.900 | 27.9% YES |
| 06:32:34 | YES | $0.18 | 5.00 | NO | -$0.900 | 31.9% YES |

All five orders met the then-current four-cent raw edge rule because their low bids
made the modeled probability look favorable. That is not evidence of an edge: the
model probability was under 60% on every one, and the current config did not require
confidence, multi-timeframe agreement, or the owner's 55--65 cent / five-to-ten-minute
manual shape.

The first order was the clearest implementation failure. It rested for 5m25s: its YES
probability fell from 39.8% when placed to 15.8% when filled at the unchanged $0.22
bid. The previous implementation left a resting order active as long as the spot feed
was fresh and the close was not imminent. The accompanying fix cancels a resting bid
as soon as its current fee-adjusted edge falls below `min_edge`.

## Parameter alignment

The run used the mutable local `config.yaml`: 4-cent minimum edge, 15--85 cent price
band, 30--780 second entry window, 50/50 model-market blend, five-contract ramp
sizing, and a five-loss pause. That is the broad Candidate A baseline, not the
stronger exploratory shapes.

The frozen candidate suite has not produced enough post-freeze observations to call
any candidate profitable. Its most promising *exploratory* cap was 65 cents, which
would have prevented the earlier 74 and 80 cent losses but would not have rejected the
five 18--25 cent orders above. The pre-registered `O-confidence-060` filter would
have rejected each of those five on its placement probability; `M-manual-shape` and
`N-aligned-manual` would also reject them because their prices and/or timing were
outside the manual entry band. Those remain hypotheses for the one-second replay and
forward paper test, not settings to promote from this one streak.
