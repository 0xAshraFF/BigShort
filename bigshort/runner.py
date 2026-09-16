import json
import logging
import time
from pathlib import Path
from datetime import datetime, timezone
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


def emit(kind, **values):
    print(json.dumps({"kind":kind,"timestamp":datetime.now(timezone.utc).isoformat(),**values}),flush=True)


def cycle(api, engine, store, kill_file):
    started = time.monotonic()
    stats = {"scan_id":time.time_ns(),"status":"started","scanned":0,"signals":0,
             "entries":0,"reasons":{}}
    emit("scan_started",scan_id=stats["scan_id"])
    try:
        _cycle(api, engine, store, kill_file, stats)
    except Exception as exc:
        stats.update(status="error",error=str(exc))
        raise
    finally:
        stats["duration_seconds"] = round(time.monotonic()-started,3)
        emit("scan_summary",**stats)


def _cycle(api, engine, store, kill_file, stats):
    now = api.now()
    stats["exchange_time_ms"] = now
    manage(api, engine, store, now)
    if Path(kill_file).exists():
        stats["status"] = "kill_switch"
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
        stats["status"] = "position_open" if engine.position else "risk_halted"
        return
    universe = api.universe(engine.cfg, now)
    stats["universe"] = dict(getattr(api,"universe_stats",{"eligible":len(universe)}))
    stats["status"] = "completed" if universe else "empty_universe"
    for item in universe:
        symbol = item["symbol"]
        stats["scanned"] += 1
        detail = {"scan_id":stats["scan_id"],"symbol":symbol,"candles":{}}
        try:
            # Resync now for each symbol; do not enter on a scan that took minutes.
            current = api.now()
            frames = [api.bars(symbol, interval, current) for interval in ["4h", "15m", "1m"]]
            for name, bars, interval in zip(["4h","15m","1m"],frames, [14_400_000, 900_000, 60_000]):
                age = current-bars[-1].t if bars else None
                detail["candles"][name] = {"count":len(bars),"last_close_ms":bars[-1].t if bars else None,
                                          "age_ms":age,"fresh":age is not None and 0<=age<interval+5_000}
                if len(bars) < 21:
                    raise ValueError(f"insufficient_candles:{name}")
                if not 0 <= age < interval + 5_000:
                    raise ValueError(f"stale_candles:{name}")
                if any(b.t-a.t != interval for a,b in zip(bars, bars[1:])):
                    raise ValueError(f"candle_gap:{name}")
            checks = {}
            found = signal(*frames, mode=engine.cfg.mode, diagnostics=checks)
            detail.update(reason=checks["reason"],checks=checks)
            if found:
                stats["signals"] += 1
                price = api.quote(symbol, engine.cfg, api.now())
                # Store the last closed minute; next closed candle includes the fill moment.
                if engine.enter(symbol, *found, price, frames[-1][-1].t, item["step"], item["minimum"]):
                    stats["entries"] += 1
                    detail["reason"] = "entry_opened"
                    store.save(engine)
                    break
                detail["reason"] = "entry_rejected_by_risk_cooldown_or_size"
        except ValueError as exc:
            detail["reason"] = str(exc)
        except Exception as exc:
            detail.update(reason="data_error",error=str(exc))
            raise
        finally:
            reason = detail.get("reason","unknown")
            stats["reasons"][reason] = stats["reasons"].get(reason,0)+1
            emit("symbol_scan",**detail)


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
