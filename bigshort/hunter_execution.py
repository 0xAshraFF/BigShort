"""Hunter-only forward execution. Never replays pre-fill candle highs as fills."""
import math
import time
from dataclasses import asdict
from .core import Engine, Bar
from .store import Store


class HunterEngine(Engine):
    def __init__(self, config, state=None, legacy_events=None):
        super().__init__(config, state)
        extra = (state or {}).get('hunter_v2', {})
        self.settlements = extra.get('settlements', [])
        self.trade_id = extra.get('trade_id')
        self.last_observed = extra.get('last_observed')
        self.last_ask = extra.get('last_ask')
        self.max_drawdown_pct = extra.get('max_drawdown_pct',0)
        if self.position and not self.trade_id:
            p = self.position
            self.trade_id = f'{p.symbol}:{p.opened}'
            exposure=[{'t':p.opened,'qty':p.initial_qty}]
            qty=p.initial_qty
            if p.partial:
                if legacy_events is None:
                    raise ValueError('Legacy partial position requires its execution journal')
                for event in legacy_events:
                    if event['kind']=='exit' and event.get('symbol')==p.symbol and event.get('t',0)>=p.opened:
                        qty-=event['qty']
                        exposure.append({'t':event['t'],'qty':qty})
                if not math.isclose(qty,p.qty,rel_tol=1e-8,abs_tol=1e-8):
                    raise ValueError('Legacy exposure does not reconcile with position')
            self.settlements.append(dict(id=self.trade_id,symbol=p.symbol,
                cursor=max(p.opened,p.funding_t),closed=None,exposure=exposure))
            self.event('migration', note='Legacy open position preserved; future execution uses fresh quotes')

    def state(self):
        return super().state() | {'hunter_v2':dict(settlements=self.settlements,
            trade_id=self.trade_id,last_observed=self.last_observed,last_ask=self.last_ask,
            account_equity=self.equity(self.last_ask) if self.position and self.last_ask else self.cash,
            max_drawdown_pct=self.max_drawdown_pct)}

    def equity(self,price=None):
        if self.position and price:
            fill=price*(1+self.cfg.slippage)
            return self.cash+(self.position.entry-fill)*self.position.qty-fill*self.position.qty*self.cfg.fee
        return self.cash

    def event(self, kind, **values):
        values.setdefault('processed_at_ms',int(time.time()*1000))
        if kind in {'entry','exit','trade','trail','funding_adjustment'}:
            values.setdefault('trade_id',getattr(self,'trade_id',None))
        super().event(kind, **values)

    def enter(self, symbol, setup, stop, price, t, step=.000001, minimum=5):
        previous=self.trade_id
        self.trade_id=f'{symbol}:{t}'
        ok=super().enter(symbol,setup,stop,price,t,step,minimum)
        if ok:
            self.settlements.append(dict(id=self.trade_id,symbol=symbol,cursor=t,closed=None,
                exposure=[{'t':t,'qty':self.position.qty}]))
            self.last_observed=t
        else:
            self.trade_id=previous
        return ok

    def close(self, price, t, reason, fraction=1):
        tid=self.trade_id
        super().close(price,t,reason,fraction)
        job=next(j for j in self.settlements if j['id']==tid)
        job['exposure'].append({'t':t,'qty':self.position.qty if self.position else 0})
        if not self.position:
            job['closed']=t
            self.trade_id=None

    def observe(self, ask, now):
        if not self.position:
            return
        if now <= self.position.last_t:
            return
        gap=now-(self.last_observed or self.position.opened)
        self.event('management_observation',t=now,symbol=self.position.symbol,
                   observation_gap_ms=gap,price=ask,
                   note='Observed quote execution; no hypothetical outage fills')
        self.last_observed=now
        self.last_ask=ask
        equity=self.equity(ask)
        peak=max(self.peak,equity)
        self.max_drawdown_pct=max(self.max_drawdown_pct,max(0,1-equity/peak)*100)
        # A quote is a point observation, never a historical full-minute candle.
        self.bar(Bar(now,ask,ask,ask,ask,0))

    def reconcile_funding(self, api, now):
        for job in list(self.settlements):
            end=min(now,job['closed']) if job['closed'] is not None else now
            if end <= job['cursor']:
                continue
            while job['cursor'] < end:
                rows=api.get('fundingRate',symbol=job['symbol'],startTime=job['cursor']+1,endTime=end,limit=1000)
                for row in rows:
                    t=int(row['fundingTime'])
                    if not job['cursor'] < t <= end:
                        raise ValueError('Funding timestamps out of order')
                    rate,mark=float(row['fundingRate']),float(row['markPrice'])
                    if not math.isfinite(rate) or not math.isfinite(mark) or mark<=0:
                        raise ValueError('Invalid funding values')
                    # Quantity held immediately before the funding boundary.
                    held=[x for x in job['exposure'] if x['t'] < t]
                    amount=(held[-1]['qty'] if held else 0)*mark*rate
                    self.cash+=amount
                    if self.position and self.trade_id==job['id']:
                        self.position.gross+=amount
                        self.position.funding_t=t
                        included=True
                    else:
                        included=False
                    self.event('funding_adjustment',t=t,trade_id=job['id'],amount=amount,
                               included_in_trade_net=included)
                    job['cursor']=t
                if len(rows)<1000:
                    job['cursor']=end
            if job['closed'] is not None and job['cursor']>=job['closed']:
                self.settlements.remove(job)


class HunterStore(Store):
    def report(self):
        result=super().report()
        events=[__import__('json').loads(x[0]) for x in self.db.execute('SELECT payload FROM events ORDER BY id')]
        adjustments={}
        for e in events:
            if e['kind']=='funding_adjustment' and not e['included_in_trade_net']:
                tid=e['trade_id'];adjustments[tid]=adjustments.get(tid,0)+e['amount']
        nets=[e['net']+adjustments.get(e.get('trade_id'),0) for e in events if e['kind']=='trade']
        gains=sum(max(0,x) for x in nets);losses=-sum(min(0,x) for x in nets)
        result.update(net_closed_pnl=sum(nets),win_rate=sum(x>0 for x in nets)/len(nets) if nets else None,
                      profit_factor=gains/losses if losses else None)
        jobs=((result['state'] or {}).get('hunter_v2') or {}).get('settlements',[])
        result['funding_pending_closed_trades']=sum(j['closed'] is not None for j in jobs)
        return result
