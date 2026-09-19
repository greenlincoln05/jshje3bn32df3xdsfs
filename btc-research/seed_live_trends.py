"""Public Coinbase warmup ending BEFORE the live capture started. No account access."""
import json
import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import httpx

root=Path('data/live-research')
db=next(root.glob('recorder-*.sqlite'))
with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as c:
    first=c.execute('SELECT MIN(poll_ts) FROM orderbook_snapshots').fetchone()[0]
end=int(datetime.fromisoformat(first).timestamp())//60*60
spot={}
with httpx.Client(timeout=30) as c:
    for start in range(end-90000,end,290*60):
        params={'start':datetime.fromtimestamp(start,timezone.utc).isoformat(),
                'end':datetime.fromtimestamp(min(start+290*60,end),timezone.utc).isoformat(),'granularity':60}
        r=c.get('https://api.exchange.coinbase.com/products/BTC-USD/candles',params=params)
        r.raise_for_status()
        for row in r.json():
            stamp=int(row[0])+60
            if stamp<=end: spot[stamp]=row[4]
        time.sleep(.2)
(root/'spot-warmup.json').write_text(json.dumps({'cutoff':end,'prices':spot}))
print('Saved',len(spot),'completed minutes; cutoff',end)
