# Candidate A validation — 2026-09-20 UTC

Reviewed main after PR26. Candidate A and config defaults are unchanged. Used consistent SQLite backups; manifest below identifies the snapshots. No network data acquisition or orders were started.

## Findings

- Exact reproduction succeeded: training 9 trades / 7 wins / +$114.54, test 4 / 3 / +$35.20.
- Six declared scenario settings, 22 replay evaluations. B/C model-weight variants were tested only on the already-inspected design data; no selection or tuning on confirmation outcomes.
- Forward snapshot spans 2026-09-19 23:43:38 through 2026-09-20 00:32:09 UTC: five observed windows, four settlement records. Frozen A: 3 trades, 1 win, -$21.58. With maker multiplier .25: 2 trades, 0 wins, -$50.97. All conclusions remain insufficient.
- Fee sensitivity changes entry timing and sometimes side. It is not a subtraction of fees from an unchanged ledger.
- Design continuous A yields +$114.54, not the +$149.74 split sum: fresh test risk state is a different experiment.
- Pessimistic execution fills nothing. This is not a bound on actual losses.
- After the causal-settlement and exact-ledger fixes below, all 22 evaluation rows were rerun. Their trade counts and PnL were unchanged on these snapshots; this means announcements happened soon enough not to alter these particular decisions, not that the old behavior was safe.

## Replay integrity review

- Fixed: settlement results now become available at recorded `finalized_poll_ts`; exposure remains reserved until then. Outcomes announced after the last book can score the report but cannot affect earlier decisions.
- Fixed: equivalent settings now require identical ordered trade ledgers, rather than matching only trade count, wins and total PnL.
- Fixed: `LabParams.from_config` now inherits the live `min_price` and `max_price` bounds. Candidate A still reproduces because none of its observed trades fell outside 0.15–0.85.
- Daily risk state resets across train/test. Always show a continuous control.

## Complete evaluation ledger

All accounts $500; minimum order premium 5% initial balance. A fee0/default loss stop10%; fees=.25; loss-stop sensitivity100% is research-only. B blend=.75, C blend=1, both fees=.25 and stop10%. All optimistic except A-pessimistic. Wilson intervals describe win proportions, not proof of a trading edge. Tiny-sample t statistics are unstable.

|Case|Slice|N|Wins|95% win interval|PnL|t|Max DD|Avg win|Avg loss|Risk blocked|Too small|
|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
|A|design_train|9|7|45.3–93.7%|114.54|1.59|25.20|23.56|-25.20|314|0|
|A|design_test|4|3|30.1–95.4%|35.20|0.62|25.16|20.12|-25.16|170|0|
|A|design_continuous|9|7|45.3–93.7%|114.54|1.59|25.20|23.56|-25.20|923|0|
|A|confirmation|3|1|6.1–79.2%|-21.58|-0.40|50.20|28.62|-25.10|0|0|
|A-fees|design_train|9|7|45.3–93.7%|108.86|1.50|25.58|22.84|-25.51|291|0|
|A-fees|design_test|4|3|30.1–95.4%|3.44|0.10|25.19|9.54|-25.19|143|0|
|A-fees|design_continuous|9|7|45.3–93.7%|108.86|1.50|25.58|22.84|-25.51|732|0|
|A-fees|confirmation|2|0|0.0–65.8%|-50.97|-209.01|50.97|—|-25.49|511|0|
|A-pessimistic|design_train|0|0|—|0.00|—|0.00|—|—|0|0|
|A-pessimistic|design_test|0|0|—|0.00|—|0.00|—|—|0|0|
|A-pessimistic|design_continuous|0|0|—|0.00|—|0.00|—|—|0|0|
|A-pessimistic|confirmation|0|0|—|0.00|—|0.00|—|—|0|0|
|A-loss-stop-sensitivity|design_train|12|9|46.8–91.1%|105.25|1.30|35.50|20.18|-25.45|0|0|
|A-loss-stop-sensitivity|design_test|4|3|30.1–95.4%|3.44|0.10|25.19|9.54|-25.19|143|0|
|A-loss-stop-sensitivity|design_continuous|17|12|46.9–86.7%|83.00|0.89|58.07|17.52|-25.45|143|0|
|A-loss-stop-sensitivity|confirmation|3|0|0.0–56.1%|-76.66|-260.70|76.66|—|-25.55|95|0|
|B-model75|design_train|7|5|35.9–91.8%|65.63|0.96|50.82|23.29|-25.41|1231|0|
|B-model75|design_test|4|3|30.1–95.4%|56.38|0.87|25.37|27.25|-25.37|0|0|
|B-model75|design_continuous|7|5|35.9–91.8%|65.63|0.96|50.82|23.29|-25.41|2385|0|
|C-model100|design_train|7|5|35.9–91.8%|61.29|0.92|50.89|22.44|-25.45|1832|0|
|C-model100|design_test|4|3|30.1–95.4%|56.40|0.87|25.35|27.25|-25.35|0|0|
|C-model100|design_continuous|7|5|35.9–91.8%|61.29|0.92|50.89|22.44|-25.45|3446|0|

## Segments

|Case|Slice|Segment|Trades|PnL|
|---|---|---|---:|---:|
|A|design_train|settled_no|7|164.94|
|A|design_train|entry_55to70|2|24.76|
|A|design_train|bought_no|9|114.54|
|A|design_train|entry_70plus|1|9.45|
|A|design_train|entry_below55|6|80.33|
|A|design_train|settled_yes|2|-50.40|
|A|design_test|settled_no|3|60.36|
|A|design_test|entry_70plus|3|-9.60|
|A|design_test|bought_no|4|35.20|
|A|design_test|entry_below55|1|44.80|
|A|design_test|settled_yes|1|-25.16|
|A|design_continuous|settled_no|7|164.94|
|A|design_continuous|entry_55to70|2|24.76|
|A|design_continuous|bought_no|9|114.54|
|A|design_continuous|entry_70plus|1|9.45|
|A|design_continuous|entry_below55|6|80.33|
|A|design_continuous|settled_yes|2|-50.40|
|A|confirmation|settled_no|1|28.62|
|A|confirmation|entry_below55|2|3.62|
|A|confirmation|bought_no|3|-21.58|
|A|confirmation|settled_yes|2|-50.20|
|A|confirmation|entry_55to70|1|-25.20|
|A-fees|design_train|settled_no|7|159.89|
|A-fees|design_train|entry_55to70|3|43.20|
|A-fees|design_train|bought_no|9|108.86|
|A-fees|design_train|entry_70plus|1|9.33|
|A-fees|design_train|entry_below55|5|56.33|
|A-fees|design_train|settled_yes|2|-51.03|
|A-fees|design_test|settled_no|3|28.63|
|A-fees|design_test|entry_55to70|1|11.33|
|A-fees|design_test|bought_no|4|3.44|
|A-fees|design_test|entry_70plus|3|-7.89|
|A-fees|design_test|settled_yes|1|-25.19|
|A-fees|design_continuous|settled_no|7|159.89|
|A-fees|design_continuous|entry_55to70|3|43.20|
|A-fees|design_continuous|bought_no|9|108.86|
|A-fees|design_continuous|entry_70plus|1|9.33|
|A-fees|design_continuous|entry_below55|5|56.33|
|A-fees|design_continuous|settled_yes|2|-51.03|
|A-fees|confirmation|settled_no|1|-25.36|
|A-fees|confirmation|entry_55to70|2|-50.97|
|A-fees|confirmation|bought_yes|1|-25.36|
|A-fees|confirmation|settled_yes|1|-25.61|
|A-fees|confirmation|bought_no|1|-25.61|
|A-loss-stop-sensitivity|design_train|settled_no|9|181.61|
|A-loss-stop-sensitivity|design_train|entry_55to70|4|58.61|
|A-loss-stop-sensitivity|design_train|bought_no|12|105.25|
|A-loss-stop-sensitivity|design_train|entry_70plus|2|15.64|
|A-loss-stop-sensitivity|design_train|entry_below55|6|31.00|
|A-loss-stop-sensitivity|design_train|settled_yes|3|-76.35|
|A-loss-stop-sensitivity|design_test|settled_no|3|28.63|
|A-loss-stop-sensitivity|design_test|entry_55to70|1|11.33|
|A-loss-stop-sensitivity|design_test|bought_no|4|3.44|
|A-loss-stop-sensitivity|design_test|entry_70plus|3|-7.89|
|A-loss-stop-sensitivity|design_test|settled_yes|1|-25.19|
|A-loss-stop-sensitivity|design_continuous|settled_no|12|210.23|
|A-loss-stop-sensitivity|design_continuous|entry_55to70|5|69.94|
|A-loss-stop-sensitivity|design_continuous|bought_no|17|83.00|
|A-loss-stop-sensitivity|design_continuous|entry_70plus|5|7.75|
|A-loss-stop-sensitivity|design_continuous|entry_below55|7|5.31|
|A-loss-stop-sensitivity|design_continuous|settled_yes|5|-127.23|
|A-loss-stop-sensitivity|confirmation|settled_no|1|-25.36|
|A-loss-stop-sensitivity|confirmation|entry_55to70|2|-50.97|
|A-loss-stop-sensitivity|confirmation|bought_yes|1|-25.36|
|A-loss-stop-sensitivity|confirmation|settled_yes|2|-51.30|
|A-loss-stop-sensitivity|confirmation|bought_no|2|-51.30|
|A-loss-stop-sensitivity|confirmation|entry_70plus|1|-25.69|
|B-model75|design_train|settled_no|5|116.45|
|B-model75|design_train|entry_55to70|1|11.70|
|B-model75|design_train|bought_no|7|65.63|
|B-model75|design_train|entry_70plus|1|9.33|
|B-model75|design_train|entry_below55|5|44.60|
|B-model75|design_train|settled_yes|2|-50.82|
|B-model75|design_test|settled_no|3|81.75|
|B-model75|design_test|entry_70plus|1|8.73|
|B-model75|design_test|bought_no|4|56.38|
|B-model75|design_test|entry_below55|2|28.05|
|B-model75|design_test|settled_yes|1|-25.37|
|B-model75|design_test|entry_55to70|1|19.61|
|B-model75|design_continuous|settled_no|5|116.45|
|B-model75|design_continuous|entry_55to70|1|11.70|
|B-model75|design_continuous|bought_no|7|65.63|
|B-model75|design_continuous|entry_70plus|1|9.33|
|B-model75|design_continuous|entry_below55|5|44.60|
|B-model75|design_continuous|settled_yes|2|-50.82|
|C-model100|design_train|settled_no|5|112.18|
|C-model100|design_train|entry_55to70|1|11.70|
|C-model100|design_train|bought_no|7|61.29|
|C-model100|design_train|entry_70plus|1|9.33|
|C-model100|design_train|entry_below55|5|40.26|
|C-model100|design_train|settled_yes|2|-50.89|
|C-model100|design_test|settled_no|3|81.75|
|C-model100|design_test|entry_70plus|1|8.73|
|C-model100|design_test|bought_no|4|56.40|
|C-model100|design_test|entry_below55|2|28.07|
|C-model100|design_test|settled_yes|1|-25.35|
|C-model100|design_test|entry_55to70|1|19.61|
|C-model100|design_continuous|settled_no|5|112.18|
|C-model100|design_continuous|entry_55to70|1|11.70|
|C-model100|design_continuous|bought_no|7|61.29|
|C-model100|design_continuous|entry_70plus|1|9.33|
|C-model100|design_continuous|entry_below55|5|40.26|
|C-model100|design_continuous|settled_yes|2|-50.89|

## Snapshot manifest

SQLite backup hashes (not byte hashes of the live source/WAL combination):

- `paper-KXBTC15M-prod-20260919T170425Z.sqlite`: `a8b9cd8d5eeda4fa1ea7c8f38674dd63aa1c69cf4418a34e8952b8aa82df5494`
- `paper-KXBTC15M-prod-20260919T175817Z.sqlite`: `784c33246394483383e9398ad1167afffcf2e6bc80a662269e6f637fda3a04e0`
- `paper-KXBTC15M-prod-20260919T234337Z.sqlite`: `59374fd516823151f0ba1991e592281fa9286ddb9e97b1dc5800c51edcf0a9ec`

## Next test

Keep A frozen and append independent future windows. Do not rerank B/C on these five confirmation windows. Require at least 30 resolved confirmation trades and varied directional regimes before considering stronger conclusions. Resolve the settlement-timing and deduplication issues in separately versioned engine tests before relying on capital/drawdown results. Scheduled checks remain paused.
