"""Multi-timeframe, take-profit and one-switch research. Public candles only."""
import json, math, itertools, random
from pathlib import Path
from datetime import datetime
from decimal import Decimal as D, ROUND_CEILING
from collections import defaultdict, Counter

def ts(s): return int(datetime.fromisoformat(s.replace('Z','+00:00')).timestamp())
def fee(p): return (D('.07')*5*p*(1-p)).quantize(D('.01'),rounding=ROUND_CEILING)
def cash(side,p,slip=D(0)):
    p=min(D(1),p+slip) if side=='buy' else max(D(0),p-slip)
    return -5*p-fee(p) if side=='buy' else 5*p-fee(p)
def quote(c,side):
    try:
        b,a=(D(c[k]['close_dollars']) for k in ('yes_bid','yes_ask'))
        if not 0<=b<=a<=1: return None
        return (b,a) if side else (1-a,1-b)
    except (KeyError,TypeError): return None

def features(spot,t,opening):
    required=[t,t-900,t-1800,t-3600,t-86400,t-300,opening]
    if any(x not in spot for x in required): return None
    # Completed candles only. A Coinbase start timestamp is shifted to its end at load.
    r=[math.log(spot[t]/spot[t-h]) for h in (900,1800,3600,86400)]
    past=[spot.get(t-60*i) for i in range(61)]
    if any(x is None for x in past): return None
    sigma=math.sqrt(sum(math.log(past[i]/past[i+1])**2 for i in range(60))/60)
    tau=(opening+900-t)/60
    z=math.log(spot[t]/spot[opening])/max(sigma*math.sqrt(max(tau-.5,1/60)),1e-9)
    probability=max(.02,min(.98,.5*(1+math.erf(z/math.sqrt(2)))))
    return dict(returns=r,short=math.log(spot[t]/spot[t-300]),sigma=sigma,p=probability)

def passes(kind,side,f,ask):
    sign=1 if side else -1
    aligned=[x*sign>0 for x in f['returns']]
    if kind=='leader': return True
    if kind=='15m': return aligned[0]
    if kind=='short3': return all(aligned[:3])
    if kind=='all4': return all(aligned)
    if kind=='majority3': return sum(aligned)>=3
    if kind=='strong15': return aligned[0] and abs(f['returns'][0])>=f['sigma']*math.sqrt(15)
    if kind=='fair_edge':
        p=f['p'] if side else 1-f['p']
        return all(aligned[:3]) and p-float(ask)-float(fee(ask)/5)>=.04
    raise ValueError(kind)

def simulate(m,cs,fs,kind,minute,band,exit_rule):
    opening=ts(m['open_time']);close=ts(m['close_time']);decision=opening+minute*60;entry=decision+60
    q=quote(cs.get(decision),True);f=fs.get(decision)
    if q is None or f is None: return None
    side=sum(q)>=1
    signal=quote(cs.get(decision),side)
    lo,hi=band
    if not (lo<=signal[1]<=hi and signal[1]-signal[0]<=D('.06') and passes(kind,side,f,signal[1])): return None
    execution=quote(cs.get(entry),side)
    if execution is None or not (lo<=execution[1]<=hi and execution[1]-execution[0]<=D('.06')): return None
    fills=[('buy',execution[1],entry)]
    switched=False;reason='settlement';exit_time=close
    for t in range(entry+60,close-60,60):
        q=quote(cs.get(t),side)
        if q is None: continue
        target=D('.99') if exit_rule=='tp99' else D('.80')
        action=None
        if exit_rule!='settle' and q[0]>=target: action='target'
        elif exit_rule=='stop40' and q[0]<=D('.40'): action='stop'
        elif exit_rule=='flip_late' and not switched and close-t<=300:
            f=fs.get(t);other=quote(cs.get(t),not side)
            if f and other and other[0]>=D('.55') and f['short']*(1 if side else -1)<0: action='flip'
        if action:
            ex=quote(cs.get(t+60),side)
            if ex is None: continue
            if action=='flip':
                other=quote(cs.get(t+60),not side)
                if other is None or other[1]>D('.85') or other[1]-other[0]>D('.06'): continue
                fills.append(('sell',ex[0],t+60));fills.append(('buy',other[1],t+60))
                side=not side;switched=True
            else:
                fills.append(('sell',ex[0],t+60));reason=action;exit_time=t+60;break
    settlement= D(5*int((m['result']=='yes')==side)) if reason=='settlement' else D(0)
    pnl=sum((cash(a,p) for a,p,t in fills),D(0))+settlement
    stress=sum((cash(a,p,D('.01')) for a,p,t in fills),D(0))+settlement
    return dict(ticker=m['ticker'],date=m['close_time'][:10],pnl=float(pnl),stress=float(stress),reason=reason,switched=switched,
                entry=entry,exit_time=exit_time,fills=[(a,str(p),t) for a,p,t in fills],settlement=float(settlement))

def stats(rows):
    pnls=[r['pnl'] for r in rows];equity=peak=dd=0
    for v in pnls: equity+=v;peak=max(peak,equity);dd=max(dd,peak-equity)
    losses=-sum(v for v in pnls if v<0)
    return dict(n=len(rows),pnl=round(sum(pnls),3),stress=round(sum(r['stress'] for r in rows),3),win_rate=sum(v>0 for v in pnls)/len(rows) if rows else None,
                max_drawdown=round(dd,3),profit_factor=sum(v for v in pnls if v>0)/losses if losses else None,exits=dict(Counter(r['reason'] for r in rows)),switches=sum(r['switched'] for r in rows))
