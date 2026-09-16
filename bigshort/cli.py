import argparse
import fcntl
import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from .core import Bar, Config, Engine
from .market import Binance
from .store import Store
from .runner import run
from .research import download, replay


def main():
    parser = argparse.ArgumentParser(description="BigShort — PAPER ONLY; no live execution")
    parser.add_argument("--db", default="data/paper.sqlite")
    parser.add_argument("--config", help="JSON Config overrides; immutable per database")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("paper")
    p.add_argument("--once",action="store_true")
    p.add_argument("--kill-file",default="data/STOP")
    sub.add_parser("report")
    sub.add_parser("demo",help="Synthetic execution smoke test, NOT market performance")
    p = sub.add_parser("download")
    p.add_argument("symbol")
    p.add_argument("--start",required=True,help="UTC ISO date, e.g. 2026-08-01")
    p.add_argument("--end",required=True)
    p.add_argument("--out",required=True)
    p = sub.add_parser("research")
    p.add_argument("dataset")
    p.add_argument("--out",default="data/research.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config(**json.loads(Path(args.config).read_text())) if args.config else Config()
    if args.command == "download":
        def stamp(x):
            return int(datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp()*1000)
        start,end=stamp(args.start),stamp(args.end)
        if start>=end or start%60_000 or end%60_000:
            parser.error("Dates must be minute-aligned and start < end")
        data=download(Binance(),args.symbol,start,end)
        Path(args.out).parent.mkdir(parents=True,exist_ok=True)
        Path(args.out).write_text(json.dumps(data))
        print(f"Saved {len(data['bars'])} candles to {args.out}")
        return
    if args.command == "research":
        data=json.loads(Path(args.dataset).read_text())
        bars=[Bar(**b) for b in data["bars"]]
        if len(bars)<20_000:
            parser.error("Need at least 20,000 contiguous minutes for train/holdout with warmup")
        split=int(len(bars)*.7)
        variants=[]
        for mode in ["rejection","momentum","both"]:
            c=replace(cfg,mode=mode)
            train=replay(bars[:split],c,data["symbol"],data.get("funding",[]))
            variants.append((train["net"],c,train))
        # Holdout is evaluated ONCE on the training-selected configuration.
        _,selected,train=max(variants,key=lambda x:x[0])
        holdout=replay(bars[max(0,split-5040):],selected,data["symbol"],data.get("funding",[]),bars[split].t)
        result={"label":"Exploratory chronological 70/30 split; not live approval",
                "training":[{k:v for k,v in x[2].items() if k!='events'} for x in variants],
                "selected_mode":selected.mode,"holdout":holdout,"live_ready":False,
                "next":"Multi-symbol walk-forward, delisted symbols, spread/liquidation stress, then 30+ days forward paper"}
        Path(args.out).parent.mkdir(parents=True,exist_ok=True)
        Path(args.out).write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k not in ['training','holdout']},indent=2))
        return
    Path(args.db).parent.mkdir(parents=True,exist_ok=True)
    with open(args.db+".lock","w") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another process owns this paper account")
        store=Store(args.db,cfg)
        engine=Engine(cfg,store.load())
        if args.command=="report":
            print(json.dumps(store.report(),indent=2))
        elif args.command=="demo":
            if store.load():
                parser.error("Demo requires a NEW database; do not mix synthetic and real data")
            engine.enter("SYNTHETIC","execution_smoke_test",102,100,60_000)
            for b in [Bar(120_000,100,100.5,95,96,10),Bar(180_000,96,101,95,100,10)]:
                engine.bar(b)
            engine.event("provenance",source="SYNTHETIC — not Binance data or evidence of returns")
            store.save(engine)
            print(json.dumps(store.report(),indent=2))
        else:
            run(Binance(),engine,store,args.kill_file,args.once)


if __name__=="__main__":
    main()
