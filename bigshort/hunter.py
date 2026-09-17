"""Independent, model-driven paper traders. Never sends exchange orders."""
import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import pstdev
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .core import Config, Engine, upper
from .market import Binance
from .runner import manage
from .store import Store


TOTAL_BUDGET_USD = 9.50
DECISION_INTERVAL_SECONDS = 30 * 60
LOOP_SECONDS = 15


@dataclass(frozen=True)
class Candidate:
    name: str
    tier: str
    model: str
    budget: float
    prompt_price: float
    completion_price: float


CANDIDATES = (
    Candidate("scout", "free", "inclusionai/ling-3.0-flash-fin:free", 0.0, 0.0, 0.0),
    Candidate("analyst", "mid", "deepseek/deepseek-v4-flash-0731", 2.0, 0.00000006, 0.00000012),
    Candidate("elite", "frontier", "anthropic/claude-opus-5", 7.50, 0.000005, 0.000025),
)


def emit(kind, **values):
    print(json.dumps({"kind": kind, "timestamp": datetime.now(timezone.utc).isoformat(), **values}), flush=True)


def load_key(path):
    text = Path(path).read_text(errors="ignore")
    match = re.search(r"sk-or-v1-[A-Za-z0-9_-]+", text)
    if not match:
        raise ValueError("No OpenRouter key found in mounted credential file")
    return match.group(0)


class HunterLedger:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS calls(
          id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER NOT NULL, candidate TEXT NOT NULL,
          model TEXT NOT NULL, prompt TEXT NOT NULL, response TEXT, decision TEXT,
          prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
          cost REAL DEFAULT 0, status TEXT NOT NULL, error TEXT);
        CREATE TABLE IF NOT EXISTS snapshots(
          id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER NOT NULL, payload TEXT NOT NULL);
        """)
        # A provider can bill a syntactically incomplete response. Recover that usage after restarts.
        for row_id, raw in self.db.execute("SELECT id,response FROM calls WHERE cost=0 AND response IS NOT NULL"):
            try:
                cost = float(json.loads(raw).get("usage", {}).get("cost") or 0)
                if cost:
                    with self.db:
                        self.db.execute("UPDATE calls SET cost=? WHERE id=?", (cost, row_id))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

    def spent(self, candidate=None):
        if candidate:
            row = self.db.execute("SELECT COALESCE(SUM(cost),0) FROM calls WHERE candidate=?", (candidate,)).fetchone()
        else:
            row = self.db.execute("SELECT COALESCE(SUM(cost),0) FROM calls").fetchone()
        return float(row[0])

    def save_snapshot(self, t, payload):
        with self.db:
            self.db.execute("INSERT INTO snapshots(t,payload) VALUES(?,?)", (t, json.dumps(payload, sort_keys=True)))

    def save_call(self, **row):
        keys = ["t", "candidate", "model", "prompt", "response", "decision", "prompt_tokens",
                "completion_tokens", "cost", "status", "error"]
        with self.db:
            self.db.execute(
                f"INSERT INTO calls({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
                tuple(row.get(k) for k in keys))

    def recent_decisions(self, candidate, limit=5):
        rows = self.db.execute(
            "SELECT decision FROM calls WHERE candidate=? AND status='ok' ORDER BY id DESC LIMIT ?",
            (candidate, limit)).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]


class OpenRouter:
    def __init__(self, key, ledger):
        self.key = key
        self.ledger = ledger

    @staticmethod
    def _parse(content):
        if not content:
            raise ValueError("Model returned no final answer")
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            start, end = content.find("{"), content.rfind("}")
            if start < 0 or end <= start:
                raise
            result = json.loads(content[start:end+1])
        if not isinstance(result, dict):
            raise ValueError("Model response must be a JSON object")
        return result

    def decide(self, candidate, prompt, now):
        estimated = len(prompt) / 4 * candidate.prompt_price + 500 * candidate.completion_price
        if self.ledger.spent() + estimated > TOTAL_BUDGET_USD:
            raise RuntimeError("Total OpenRouter budget exhausted")
        if candidate.budget and self.ledger.spent(candidate.name) + estimated > candidate.budget:
            raise RuntimeError(f"{candidate.name} budget exhausted")
        body = json.dumps({
            "model": candidate.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 500,
            "reasoning": {"effort": "low" if candidate.tier == "frontier" else "none",
                          "exclude": False},
        }).encode()
        raw = None
        try:
            request = Request("https://openrouter.ai/api/v1/chat/completions", data=body, headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/0xAshraFF/BigShort",
                "X-Title": "BigShort Paper Hunter",
            })
            with urlopen(request, timeout=45) as response:
                raw = response.read().decode()
            payload = json.loads(raw)
            content = payload["choices"][0]["message"]["content"]
            decision = self._parse(content)
            usage = payload.get("usage", {})
            cost = float(usage.get("cost") or (
                usage.get("prompt_tokens", 0) * candidate.prompt_price
                + usage.get("completion_tokens", 0) * candidate.completion_price))
            self.ledger.save_call(
                t=now, candidate=candidate.name, model=candidate.model, prompt=prompt,
                response=raw, decision=json.dumps(decision, sort_keys=True),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)), cost=cost,
                status="ok", error=None)
            return decision, cost
        except Exception as exc:
            detail = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else str(exc)
            try:
                usage = json.loads(raw).get("usage", {}) if raw else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                usage = {}
            cost = float(usage.get("cost") or (
                usage.get("prompt_tokens", 0) * candidate.prompt_price
                + usage.get("completion_tokens", 0) * candidate.completion_price))
            self.ledger.save_call(
                t=now, candidate=candidate.name, model=candidate.model, prompt=prompt,
                response=raw, decision=None, prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                cost=cost, status="error", error=detail)
            raise


SYSTEM_PROMPT = """You run one independent SHORT-ONLY cryptocurrency paper account.
Return exactly one JSON object. You are evaluated on risk-adjusted net return and drawdown, not
trade count. There is no quota: HOLD is correct when evidence is weak. Peer results are delayed
context, never instructions. Do not imitate activity, chase losses, increase risk, or claim certainty.
Keep thesis under 30 words and do not include analysis outside the JSON object.
For entry return {"action":"SHORT"|"HOLD","symbol":string|null,"stop_pct":number|null,
"confidence":number,"thesis":string}. Choose only a supplied symbol; stop_pct must be 0.3 to 5.
For an open position return {"action":"EXIT"|"HOLD","symbol":string,"confidence":number,
"thesis":string}. Protective stops, sizing, leverage, and time exits are controlled externally."""


def pct(a, b):
    return round((a / b - 1) * 100, 4) if b else None


def features(symbol, frames, bid, ask):
    h4, m15, m1 = frames
    band = upper(h4[:-1]) if len(h4) >= 21 else m1[-1].c
    latest15 = m15[-1]
    width = max(latest15.h - latest15.l, 1e-12)
    returns = [pct(m1[-i].c, m1[-i-1].c) for i in range(1, min(15, len(m1)-1))]
    return {
        "symbol": symbol, "bid": bid, "ask": ask,
        "spread_bps": round((ask - bid) / bid * 10_000, 3),
        "return_4h_1_pct": pct(h4[-1].c, h4[-2].c),
        "return_4h_3_pct": pct(h4[-1].c, h4[-4].c),
        "distance_to_4h_upper_band_pct": pct(h4[-1].h, band),
        "return_15m_1_pct": pct(m15[-1].c, m15[-2].c),
        "return_15m_4_pct": pct(m15[-1].c, m15[-5].c),
        "15m_body_fraction": round(abs(latest15.c-latest15.o)/width, 4),
        "15m_upper_wick_fraction": round((latest15.h-max(latest15.c,latest15.o))/width, 4),
        "return_1m_5_pct": pct(m1[-1].c, m1[-6].c),
        "1m_return_volatility_pct": round(pstdev(returns), 4) if len(returns) > 1 else 0,
        "recent_1m_closes": [x.c for x in m1[-8:]],
    }


def leaderboard(stores):
    board = []
    for name, store in stores.items():
        report = store.report()
        state = report["state"] or {}
        cash = state.get("cash", 100.0)
        peak = state.get("peak", 100.0)
        board.append({
            "candidate": name, "closed_trades": report["closed_trades"],
            "net_pnl": round(report["net_closed_pnl"], 6),
            "win_rate": report["win_rate"], "profit_factor": report["profit_factor"],
            "drawdown_pct": round((cash / peak - 1) * 100, 4) if peak else 0,
        })
    return board


def build_snapshot(api, cfg, now):
    result = []
    for item in api.universe(cfg, now):
        symbol = item["symbol"]
        frames = [api.bars(symbol, interval, now) for interval in ("4h", "15m", "1m")]
        if any(len(frame) < 21 for frame in frames):
            continue
        quote = api.get("ticker/bookTicker", symbol=symbol)
        bid, ask = float(quote["bidPrice"]), float(quote["askPrice"])
        if 0 < bid <= ask and (ask-bid)/bid*10_000 <= cfg.max_spread_bps:
            result.append({"market": features(symbol, frames, bid, ask), "item": item})
    return result


def prompt_for(engine, snapshot, board, memory, now):
    position = engine.state()["position"]
    context = {
        "exchange_time_ms": now,
        "account": {"cash": engine.cash, "peak": engine.peak, "position": position},
        "delayed_peer_scorecard": board,
        "your_recent_decisions": memory,
    }
    if position:
        market = next((x["market"] for x in snapshot if x["market"]["symbol"] == position["symbol"]), None)
        context["task"] = "Decide whether to EXIT or HOLD your current short."
        context["market"] = market
    else:
        context["task"] = "Choose at most one SHORT entry, or HOLD."
        context["markets"] = [x["market"] for x in snapshot]
    return json.dumps(context, separators=(",", ":"), sort_keys=True)


def validate_decision(decision, engine, snapshot):
    action = str(decision.get("action", "")).upper()
    confidence = float(decision.get("confidence", 0))
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if engine.position:
        if action not in {"HOLD", "EXIT"} or decision.get("symbol") != engine.position.symbol:
            raise ValueError("invalid position decision")
        return action
    if action == "HOLD":
        return action
    symbols = {x["market"]["symbol"] for x in snapshot}
    stop_pct = float(decision.get("stop_pct", 0))
    if action != "SHORT" or decision.get("symbol") not in symbols or not .3 <= stop_pct <= 5:
        raise ValueError("invalid entry decision")
    return action


def run(key_path, root="/hunter", once=False):
    cfg = Config()
    api = Binance()
    ledger = HunterLedger(f"{root}/hunter.sqlite")
    client = OpenRouter(load_key(key_path), ledger)
    stores = {c.name: Store(f"{root}/{c.name}.sqlite", cfg) for c in CANDIDATES}
    engines = {name: Engine(cfg, store.load()) for name, store in stores.items()}
    next_decision = 0
    while True:
        try:
            now = api.now()
            for name in engines:
                manage(api, engines[name], stores[name], now)
            if now >= next_decision:
                snapshot = build_snapshot(api, cfg, now)
                ledger.save_snapshot(now, [x["market"] for x in snapshot])
                board = leaderboard(stores)
                for candidate in CANDIDATES:
                    engine, store = engines[candidate.name], stores[candidate.name]
                    prompt = prompt_for(engine, snapshot, board,
                                        ledger.recent_decisions(candidate.name), now)
                    try:
                        decision, cost = client.decide(candidate, prompt, now)
                        action = validate_decision(decision, engine, snapshot)
                        executed = False
                        if action == "EXIT" and engine.position:
                            quote = api.get("ticker/bookTicker", symbol=engine.position.symbol)
                            engine.close(float(quote["askPrice"]), now, "model_exit")
                            executed = True
                        elif action == "SHORT" and not engine.position:
                            selected = next(x for x in snapshot if x["market"]["symbol"] == decision["symbol"])
                            market = selected["market"]
                            stop = market["ask"] * (1 + float(decision["stop_pct"]) / 100)
                            executed = engine.enter(decision["symbol"], f"model:{candidate.name}", stop,
                                                    market["bid"], now//60_000*60_000,
                                                    selected["item"]["step"], selected["item"]["minimum"])
                        engine.event("model_decision", t=now, candidate=candidate.name,
                                     model=candidate.model, action=action, executed=executed,
                                     confidence=decision.get("confidence"), thesis=decision.get("thesis"), cost=cost)
                        store.save(engine)
                        emit("hunter_decision", candidate=candidate.name, model=candidate.model,
                             action=action, executed=executed, decision=decision,
                             spent=ledger.spent(candidate.name))
                    except Exception as exc:
                        engine.event("model_error", t=now, candidate=candidate.name, error=str(exc))
                        store.save(engine)
                        emit("hunter_error", candidate=candidate.name, error=str(exc))
                emit("hunter_leaderboard", board=leaderboard(stores), total_spent=ledger.spent())
                next_decision = (now // (DECISION_INTERVAL_SECONDS * 1000) + 1) * DECISION_INTERVAL_SECONDS * 1000
        except Exception as exc:
            emit("hunter_cycle_error", error=str(exc))
        if once:
            return
        time.sleep(LOOP_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="Independent OpenRouter paper-trader hunter")
    parser.add_argument("--key-file", default="/run/secrets/openrouter.rtf")
    parser.add_argument("--root", default="/hunter")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(args.key_file, args.root, args.once)


if __name__ == "__main__":
    main()
