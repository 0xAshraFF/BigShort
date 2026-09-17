"""Fresh Scout baseline versus a separately labelled rejection-confirmed GLM strategy."""
import fcntl
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import Config
from .hunter import (Candidate, DECISION_INTERVAL_SECONDS, LOOP_SECONDS, ROUND_MS,
                     HunterLedger, OpenRouter, build_snapshot, emit, execute_decision,
                     leaderboard, load_key, manage_hunter, prompt_for)
from .hunter_execution import HunterEngine, HunterStore
from .hunter_market import HunterMarket


ROUND_KEY = "round2_fair_start_ms"
CANDIDATES = (
    Candidate("scout_r2", "free", "inclusionai/ling-3.0-flash-fin:free", 0.0, 0.0, 0.0,
              round_key=ROUND_KEY),
    Candidate(
        "glm_rejection_r2", "frontier", "z-ai/glm-5.3", 3.0,
        0.0000014, 0.0000044, 2048, ROUND_KEY,
        "For a new short, select only a clearly overextended pump with confirmed reversal evidence. "
        "Do not re-enter the same coin unless a genuinely fresh setup has formed. These requirements "
        "are also enforced by deterministic execution checks.",
    ),
)


def round_start(ledger, now):
    start=ledger.metadata(ROUND_KEY)
    if start is None:
        original=ledger.metadata("round_start_ms")
        start=max(now,(original+ROUND_MS) if original is not None else now)
        ledger.metadata(ROUND_KEY,start)
    return start


def confirmed_rejection(market):
    return (market.get("return_4h_3_pct",0) >= 8
            and market.get("distance_to_4h_upper_band_pct",0) >= 3
            and market.get("return_15m_1_pct",0) < 0
            and market.get("return_1m_5_pct",0) < 0
            and market.get("15m_upper_wick_fraction",0) >= .15)


def fresh_same_coin_setup(engine, market):
    prior=[e for e in engine.events if e.get("kind")=="strategy_entry"
           and e.get("symbol")==market["symbol"]]
    return not prior or market["bar_close_4h_ms"] > prior[-1]["bar_close_4h_ms"]


def execute(candidate, api, engine, decision, snapshot, submitted, expected, deadline):
    if candidate.name=="glm_rejection_r2" and str(decision.get("action","")).upper()=="SHORT":
        market=next((x["market"] for x in snapshot if x["market"]["symbol"]==decision.get("symbol")),None)
        if market is None or not confirmed_rejection(market):
            raise ValueError("Revised GLM entry rejected: no confirmed pump rejection")
        if not fresh_same_coin_setup(engine,market):
            raise ValueError("Revised GLM entry rejected: no fresh 4h setup for same coin")
        action,done=execute_decision(api,engine,decision,snapshot,submitted,expected,deadline)
        if done:
            engine.event("strategy_entry",t=int(time.time()*1000),symbol=market["symbol"],
                         strategy="glm_confirmed_rejection_r2",
                         bar_close_4h_ms=market["bar_close_4h_ms"])
        return action,done
    return execute_decision(api,engine,decision,snapshot,submitted,expected,deadline)


def run(key_path="/run/secrets/openrouter.rtf",root="/hunter"):
    Path(root).mkdir(parents=True,exist_ok=True)
    with open(f"{root}/hunter-round2.lock","a") as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise RuntimeError("Another process owns hunter Round 2") from None
        cfg=Config();api=HunterMarket(root);ledger=HunterLedger(f"{root}/hunter.sqlite")
        client=OpenRouter(load_key(key_path),ledger)
        stores={c.name:HunterStore(f"{root}/{c.name}.sqlite",cfg) for c in CANDIDATES}
        engines={n:HunterEngine(cfg,s.load(),[json.loads(r[0]) for r in s.db.execute(
            "SELECT payload FROM events ORDER BY id")]) for n,s in stores.items()}
        started=round_start(ledger,int(time.time()*1000));deadline=started+ROUND_MS
        emit("hunter_round2_started",round_start_ms=started,deadline_ms=deadline,
             strategy="glm_confirmed_rejection_r2",spent=ledger.spent(),committed=ledger.committed(),
             models=[{"name":c.name,"model":c.model} for c in CANDIDATES])
        pool=ThreadPoolExecutor(max_workers=2);pending={}
        try:
            while True:
                now=int(time.time()*1000);active=now>=started;expired=now>=deadline
                healthy={n:manage_hunter(api,e,stores[n],expired=expired) for n,e in engines.items()}
                for name,job in list(pending.items()):
                    future,snapshot,submitted,expected,candidate=job;e=engines[name]
                    if not future.done(): continue
                    try:
                        decision,cost=future.result()
                        if not healthy[name]: raise ValueError("Management or funding unhealthy; signal rejected")
                        action,done=execute(candidate,api,e,decision,snapshot,submitted,expected,deadline)
                        e.event("model_decision",t=int(time.time()*1000),candidate=name,model=candidate.model,
                                action=action,executed=done,cost=cost,confidence=decision.get("confidence"),
                                thesis=decision.get("thesis"),strategy=("glm_confirmed_rejection_r2"
                                if name=="glm_rejection_r2" else "scout_baseline_r2"))
                        emit("hunter_round2_decision",candidate=name,action=action,executed=done)
                    except Exception as exc:
                        e.event("model_error",error=str(exc));emit("hunter_round2_error",candidate=name,error=str(exc))
                    finally: stores[name].save(e);del pending[name]
                next_at=ledger.metadata("round2_next_decision_ms") or 0
                if active and not expired and not pending and now>=next_at:
                    ledger.metadata("round2_next_decision_ms",now+DECISION_INTERVAL_SECONDS*1000)
                    try:
                        snapshot=build_snapshot(api,cfg,api.now());captured=int(time.time()*1000)
                        ledger.save_snapshot(captured,[x["market"] for x in snapshot])
                        board=leaderboard(stores,CANDIDATES)
                        for candidate in CANDIDATES:
                            e=engines[candidate.name]
                            if not snapshot or not healthy[candidate.name] or e.halted: continue
                            prompt=prompt_for(e,snapshot,board,
                                ledger.recent_decisions(candidate.name,model=candidate.model),captured)
                            pending[candidate.name]=(pool.submit(client.decide,candidate,prompt,captured),
                                                     snapshot,captured,e.trade_id,candidate)
                    except Exception as exc: emit("hunter_round2_cycle_error",error=str(exc))
                emit("hunter_round2_leaderboard",board=leaderboard(stores,CANDIDATES),total_spent=ledger.spent(),
                     committed=ledger.committed(),deadline_ms=deadline,expired=expired,
                     waiting_for_round1=not active,pending_models=list(pending))
                time.sleep(LOOP_SECONDS)
        finally: pool.shutdown(wait=False,cancel_futures=True)


if __name__=="__main__": run()
