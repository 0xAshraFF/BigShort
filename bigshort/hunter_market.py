"""Cached, rate-aware public data for the hunter only."""
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .market import Binance, deadline


class HunterMarket(Binance):
    def __init__(self, root):
        self.cache={}
        self.cooldown_path=Path(root)/'binance_cooldown.json'
        self.cooldown_path.parent.mkdir(parents=True,exist_ok=True)
        self.blocked_until=float(json.loads(self.cooldown_path.read_text())) if self.cooldown_path.exists() else 0

    def get(self,path,**params):
        now=time.time()
        if now<self.blocked_until:
            raise RuntimeError(f'Binance cooldown active until {self.blocked_until:.0f}')
        key=(path,tuple(sorted(params.items())))
        cached=self.cache.get(key)
        if cached and cached[0]>now:
            return cached[1]
        url='https://fapi.binance.com/fapi/v1/'+path+'?'+urlencode(params)
        for attempt in range(3):
            try:
                with deadline(12):
                    with urlopen(Request(url,headers={'User-Agent':'BigShort-hunter/0.2'}),timeout=8) as response:
                        data=json.load(response)
                ttl={'exchangeInfo':3600,'ticker/24hr':60,'ticker/bookTicker':.5,'time':.5}.get(path,0)
                if ttl:
                    if len(self.cache)>500: self.cache.clear()
                    self.cache[key]=(time.time()+ttl,data)
                return data
            except HTTPError as exc:
                if exc.code in (418,429):
                    try: retry=float(exc.headers.get('Retry-After',''))
                    except (TypeError,ValueError): retry=180 if exc.code==429 else 86400
                    self.blocked_until=time.time()+max(60,retry)
                    temp=self.cooldown_path.with_suffix('.tmp')
                    temp.write_text(json.dumps(self.blocked_until));temp.replace(self.cooldown_path)
                    raise RuntimeError(f'Binance HTTP {exc.code}; persistent cooldown set') from None
                if exc.code<500 or attempt==2: raise
            except (OSError,TimeoutError):
                if attempt==2: raise
            time.sleep(2**attempt)

    def bars(self,symbol,interval,now,start=None):
        span={'1m':60000,'15m':900000,'4h':14400000}[interval]
        key=('bars',symbol,interval,now//span)
        if start is None and key in self.cache:
            return self.cache[key][1]
        result=super().bars(symbol,interval,now,start)
        if start is None:
            if len(self.cache)>500: self.cache.clear()
            self.cache[key]=(now/1000+span/1000,result)
        return result


def fresh_quote(api,symbol,cfg,entry=False):
    q=api.get('ticker/bookTicker',symbol=symbol)
    now=api.now()  # Validate AFTER network completion.
    bid,ask=float(q['bidPrice']),float(q['askPrice'])
    if not 0<bid<=ask or not 0<=now-int(q['time'])<=15000:
        raise ValueError('Stale or invalid executable quote')
    if entry and (ask-bid)/bid*10000>cfg.max_spread_bps:
        raise ValueError('Entry spread exceeds limit')
    return bid,ask,now
