"""Chronological replay. Signals at close, fills at next open, no future candles."""
from dataclasses import asdict
from .core import Bar, Engine, signal


def aggregate(bars, minutes):
    span = minutes * 60_000
    groups, result = {}, []
    for b in bars:
        end = ((b.t-1)//span+1)*span
        groups.setdefault(end, []).append(b)
    for end, group in sorted(groups.items()):
        if len(group) == minutes and group[0].t == end-span+60_000 and group[-1].t == end:
            result.append(Bar(end, group[0].o, max(b.h for b in group), min(b.l for b in group),
                              group[-1].c, sum(b.v for b in group)))
    return result


def replay(bars, cfg, symbol="REPLAY", funding=(), start_at=0):
    if any(b.t-a.t != 60_000 for a,b in zip(bars,bars[1:])):
        raise ValueError("Replay requires sorted contiguous 1m candles")
    h4, m15 = aggregate(bars,240), aggregate(bars,15)
    ih, im, pending = 0, 0, None
    e = Engine(cfg)
    equity_curve = []
    funding_rows = sorted(funding,key=lambda f:int(f["fundingTime"]))
    fi = 0
    for i,b in enumerate(bars):
        if pending and not e.position and b.t >= start_at:
            e.enter(symbol, *pending, b.o, b.t-60_000)
        pending = None
        while fi < len(funding_rows) and int(funding_rows[fi]["fundingTime"]) <= b.t:
            f = funding_rows[fi]
            e.funding(float(f["fundingRate"]),float(f["markPrice"]),int(f["fundingTime"]))
            fi += 1
        e.bar(b)
        equity_curve.append(e.equity(b.c))
        while ih < len(h4) and h4[ih].t <= b.t:
            ih += 1
        while im < len(m15) and m15[im].t <= b.t:
            im += 1
        if b.t >= start_at and not e.position:
            pending = signal(h4[max(0,ih-25):ih],m15[max(0,im-25):im],bars[max(0,i-24):i+1],cfg.mode)
    if e.position and bars:
        e.close(bars[-1].c,bars[-1].t,"end_of_replay")
    trades = [x for x in e.events if x["kind"] == "trade"]
    peak, dd = cfg.capital, 0
    for equity in equity_curve+[e.cash]:
        peak = max(peak,equity)
        dd = max(dd, 1-equity/peak)
    gains = sum(max(0,t["net"]) for t in trades)
    losses = -sum(min(0,t["net"]) for t in trades)
    return {"config": asdict(cfg), "symbol": symbol, "trades": len(trades),
            "net": e.cash-cfg.capital, "ending_cash": e.cash,
            "max_drawdown": dd, "profit_factor": gains/losses if losses else None,
            "win_rate": sum(t["net"]>0 for t in trades)/len(trades) if trades else None,
            "funding_supplied": bool(funding), "events": e.events,
            "limitations": "Single-symbol replay; current filters not historical. No order-book/liquidation model."}


def download(api, symbol, start, end):
    bars, cursor = [], start
    while cursor < end:
        batch = api.bars(symbol,"1m",end,start=cursor)
        if not batch:
            break
        bars.extend(batch)
        cursor = batch[-1].t
    funding, cursor = [], start
    while cursor < end:
        batch = api.get("fundingRate",symbol=symbol,startTime=cursor,endTime=end,limit=1000)
        funding.extend(batch)
        if len(batch)<1000:
            break
        cursor=int(batch[-1]["fundingTime"])+1
    return {"source":"Binance public futures API", "symbol":symbol,
            "requested_start":start,"requested_end":end,
            "bars":[asdict(b) for b in bars],"funding":funding}
