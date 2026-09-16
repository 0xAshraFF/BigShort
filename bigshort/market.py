"""Public GET endpoints only. No API secrets or order endpoints."""
import json
import time
import signal as os_signal
from contextlib import contextmanager
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from .core import Bar


@contextmanager
def deadline(seconds):
    """Bound DNS/proxy negotiation too; this synchronous CLI runs on the main thread."""
    def expired(*_):
        raise TimeoutError("Market data request exceeded wall-clock deadline")
    previous = os_signal.signal(os_signal.SIGALRM, expired)
    os_signal.setitimer(os_signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        os_signal.setitimer(os_signal.ITIMER_REAL, 0)
        os_signal.signal(os_signal.SIGALRM, previous)


class Binance:
    def get(self, path, **params):
        url = "https://fapi.binance.com/fapi/v1/" + path + "?" + urlencode(params)
        for attempt in range(3):
            try:
                time.sleep(.12)
                with deadline(15):
                    with urlopen(Request(url, headers={"User-Agent": "BigShort-paper/0.1"}), timeout=10) as r:
                        return json.load(r)
            except HTTPError as e:
                if e.code in (418, 429):
                    raise RuntimeError("Binance rate limited; stop requests and inspect Retry-After") from e
                if e.code < 500 or attempt == 2:
                    raise
            except (TimeoutError, OSError):
                if attempt == 2:
                    raise
            time.sleep(2 ** attempt)

    def now(self):
        return self.get("time")["serverTime"]

    def bars(self, symbol, interval, now, start=None):
        args = dict(symbol=symbol, interval=interval, limit=1000 if start else 100, endTime=now-1)
        if start is not None:
            args["startTime"] = start
        rows = self.get("klines", **args)
        return [Bar(int(r[6])+1, *map(float, r[1:6])) for r in rows if int(r[6])+1 <= now]

    def universe(self, cfg, now):
        tickers = {r["symbol"]: r for r in self.get("ticker/24hr")}
        result = []
        for s in self.get("exchangeInfo")["symbols"]:
            age = (now - s.get("onboardDate", 0)) / 86_400_000
            if not (s["status"] == "TRADING" and s["contractType"] == "PERPETUAL"
                    and s["quoteAsset"] == "USDT" and 4 <= age <= cfg.max_age_days):
                continue
            if float(tickers.get(s["symbol"], {}).get("quoteVolume", 0)) < cfg.min_quote_volume:
                continue
            filters = {x["filterType"]: x for x in s["filters"]}
            lot = filters.get("MARKET_LOT_SIZE", filters["LOT_SIZE"])
            result.append({"symbol": s["symbol"], "age_days": age,
                           "step": float(lot["stepSize"]),
                           "minimum": float(filters.get("MIN_NOTIONAL", {}).get("notional", 5))})
        return sorted(result, key=lambda r: r["age_days"])

    def quote(self, symbol, cfg, now):
        q = self.get("ticker/bookTicker", symbol=symbol)
        bid, ask = float(q["bidPrice"]), float(q["askPrice"])
        if not 0 < bid <= ask or not 0 <= now - int(q["time"]) <= 15_000:
            raise ValueError("Stale or invalid bid/ask")
        if (ask-bid)/bid*10_000 > cfg.max_spread_bps:
            raise ValueError("Spread too wide")
        return bid
