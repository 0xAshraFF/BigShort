import json
import logging
import time
from pathlib import Path
from .core import Engine, signal

log = logging.getLogger(__name__)


def manage(api, engine, store, now):
    p = engine.position
    if not p:
        return
    # Replay ALL closed minutes after downtime, with pagination. Never skip stop touches.
    cursor = p.last_t
    while engine.position and cursor < now - 60_000:
        bars = api.bars(p.symbol, "1m", now, start=cursor)
        if not bars:
            raise ValueError("Missing management candles: entries disabled")
        funding = api.get("fundingRate", symbol=p.symbol, startTime=cursor+1,
                          endTime=bars[-1].t, limit=1000)
        for b in bars:
            if b.t != cursor + 60_000:
                # Entry times are minute aligned by the runner.
                raise ValueError("Management data gap: entries disabled; manual review required")
            for f in funding:
                if cursor < int(f["fundingTime"]) <= b.t:
                    engine.funding(float(f["fundingRate"]), float(f["markPrice"]), int(f["fundingTime"]))
            engine.bar(b)
            cursor = b.t
            store.save(engine)
            if not engine.position:
                return
        if len(bars) < 1000:
            break


def cycle(api, engine, store, kill_file):
    now = api.now()
    manage(api, engine, store, now)
    if Path(kill_file).exists():
        engine.halted = True
        if engine.position:
            # Emergency close uses current executable ask; no fresh-entry spread constraint.
            q = api.get("ticker/bookTicker", symbol=engine.position.symbol)
            if not 0 <= now - int(q["time"]) <= 15_000:
                raise ValueError("Cannot simulate emergency exit on stale data")
            engine.close(float(q["askPrice"]), now, "kill_switch")
        store.save(engine)
        return
    if engine.position or engine.halted:
        return
    for item in api.universe(engine.cfg, now):
        symbol = item["symbol"]
        try:
            # Resync now for each symbol; do not enter on a scan that took minutes.
            current = api.now()
            frames = [api.bars(symbol, interval, current) for interval in ["4h", "15m", "1m"]]
            for bars, interval in zip(frames, [14_400_000, 900_000, 60_000]):
                if len(bars) < 21 or not 0 <= current - bars[-1].t < interval + 5_000:
                    raise ValueError("Insufficient or stale candles")
                if any(b.t-a.t != interval for a,b in zip(bars, bars[1:])):
                    raise ValueError("Non-contiguous candles")
            found = signal(*frames, mode=engine.cfg.mode)
            if found:
                price = api.quote(symbol, engine.cfg, api.now())
                # Store the last closed minute; next closed candle includes the fill moment.
                if engine.enter(symbol, *found, price, frames[-1][-1].t, item["step"], item["minimum"]):
                    store.save(engine)
                    break
        except ValueError as exc:
            log.info("Skipped %s: %s", symbol, exc)


def run(api, engine, store, kill_file, once=False):
    while True:
        try:
            cycle(api, engine, store, kill_file)
            store.save(engine)
            print(json.dumps(store.report()), flush=True)
        except Exception as exc:
            engine.event("data_error", t=int(time.time()*1000), error=str(exc))
            store.save(engine)
            log.exception("Cycle failed; no further entries this cycle")
            if once:
                raise
        if once:
            return
        time.sleep(15)
