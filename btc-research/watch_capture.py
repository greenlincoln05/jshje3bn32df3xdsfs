"""Bounded local replay loop; consumes one recorder, never sends network orders."""
import argparse
import json
import sqlite3
import time
import traceback
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal as D
from dataclasses import asdict
from collections import defaultdict

from btcbot.backtest import run_backtest
from btcbot.config import load_config
from btcbot.paper_broker import QueueAssumption
from trend_screen import features, simulate, stats, ts

# Frozen before future windows settle. This list is never selected on live PnL.
CANDIDATES=[('15m',3,'settle'),('leader',6,'tp80'),('15m',6,'tp80'),
            ('all4',6,'tp80'),('all4',6,'tp99'),('15m',6,'flip_late')]

def replay(path,root):
    # Consistent, read-only source snapshot; all work happens on an in-memory copy.
    source=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    db=sqlite3.connect(':memory:');source.backup(db);source.close()
    config=load_config('config.yaml')
    output={'at':datetime.now(timezone.utc).isoformat(),'database':str(path),'research_only':True,
            'counts':{},'model_replays':[],'trend_candidates':{}}
    for table in ('orderbook_snapshots','spot_ticks','settlements'):
        output['counts'][table]=db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    if not output['counts']['orderbook_snapshots']: db.close();return output
    for queue in (QueueAssumption.OPTIMISTIC,QueueAssumption.PESSIMISTIC):
        for multiplier in (D(0),D('.25')):
            output['model_replays'].append(asdict(run_backtest(db,config,queue_assumption=queue,maker_fee_multiplier=multiplier)))
    warmup=json.loads((root/'spot-warmup.json').read_text())
    spot={int(k):float(v) for k,v in warmup['prices'].items()}
    now=time.time();last_spot={}
    for stamp,price in db.execute('SELECT receive_ts,price FROM spot_ticks ORDER BY receive_ts'):
        stamp=datetime.fromisoformat(stamp).timestamp();end=int(stamp)//60*60+60
        if end<=now: last_spot[end]=(stamp,float(price))
    for end,(stamp,price) in last_spot.items():
        if end-stamp<=3: spot[end]=price
    books=defaultdict(dict);last_books={}
    for ticker,stamp,raw in db.execute('SELECT ticker,poll_ts,book_json FROM orderbook_snapshots ORDER BY poll_ts'):
        stamp=datetime.fromisoformat(stamp).timestamp();end=int(stamp)//60*60+60
        if end>now: continue
        b=json.loads(raw)
        yes=max((D(p) for p,s in b['yes'] if D(s)>0),default=None)
        no=max((D(p) for p,s in b['no'] if D(s)>0),default=None)
        if yes is None or no is None: continue
        last_books[ticker,end]=(stamp,{'end_period_ts':end,'yes_bid':{'close_dollars':str(yes)},'yes_ask':{'close_dollars':str(1-no)}})
    for (ticker,end),(stamp,candle) in last_books.items():
        if end-stamp<=5: books[ticker][end]=candle
    markets={}
    for ticker,opening,closing,poll in db.execute('SELECT ticker,open_time,close_time,poll_ts FROM market_state ORDER BY poll_ts'):
        if ticker not in markets: markets[ticker]={'ticker':ticker,'open_time':opening,'close_time':closing,'first_seen':poll}
    settled=dict(db.execute("SELECT ticker,result FROM settlements WHERE resolved=1 AND result IN ('yes','no')"))
    output['settled_windows']=len(settled)
    for kind,minute,exit_rule in CANDIDATES:
        rows=[];incomplete=0;gaps=0
        for ticker,m in markets.items():
            if ticker not in settled: incomplete+=1;continue
            opening=ts(m['open_time']);decision=opening+minute*60
            if ts(m['first_seen'])>decision: continue
            required=range(decision,ts(m['close_time']),60)
            if any(t not in books[ticker] for t in required):
                gaps+=1
                continue
            m={**m,'result':settled[ticker]}
            fs={t:features(spot,t,opening) for t in range(opening+180,opening+900,60)}
            r=simulate(m,books[ticker],fs,kind,minute,(D('.55'),D('.65')),exit_rule)
            if r: rows.append(r)
        key=f'{kind}|m{minute}|0.55-0.65|{exit_rule}'
        output['trend_candidates'][key]={'summary':stats(rows),'unsettled_windows_excluded':incomplete,
                                       'windows_excluded_for_quote_gaps':gaps,'trades':rows}
    db.close()
    return output

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--until',required=True);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    root=Path('data/live-research');deadline=datetime.fromisoformat(args.until).timestamp()
    path=next(root.glob('recorder-*.sqlite'))
    while True:
        try:
            output=replay(path,root)
            text=json.dumps(output,default=str,indent=2)
            temporary=root/'latest-replay.tmp';temporary.write_text(text);temporary.replace(root/'latest-replay.json')
            with (root/'replay-history.jsonl').open('a') as f: f.write(json.dumps(output,default=str)+'\n')
            print(output['at'],output['counts'],'settled',output.get('settled_windows'),flush=True)
        except Exception:
            traceback.print_exc()
            raise
        if args.once or time.time()>=deadline or Path('RESEARCH_KILL').exists(): break
        time.sleep(min(300,max(0,deadline-time.time())))

if __name__=='__main__': main()
