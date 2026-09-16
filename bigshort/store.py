import json
import sqlite3
from dataclasses import asdict
from pathlib import Path


class Store:
    def __init__(self, path, cfg):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        """)
        value = json.dumps(asdict(cfg), sort_keys=True)
        row = self.db.execute("SELECT payload FROM config WHERE id=1").fetchone()
        if row and row[0] != value:
            raise ValueError("Config differs from persisted account; use a new database for a new experiment")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO config VALUES(1,?)", (value,))

    def load(self):
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, engine):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(engine.state()),))
            self.db.executemany("INSERT INTO events(payload) VALUES(?)",
                                [(json.dumps(e),) for e in engine.events])
        engine.events.clear()

    def report(self):
        events = [json.loads(x[0]) for x in self.db.execute("SELECT payload FROM events ORDER BY id")]
        trades = [x for x in events if x["kind"] == "trade"]
        gains = sum(max(0, t["net"]) for t in trades)
        losses = -sum(min(0, t["net"]) for t in trades)
        return {"state": self.load(), "closed_trades": len(trades),
                "net_closed_pnl": sum(t["net"] for t in trades),
                "win_rate": sum(t["net"] > 0 for t in trades) / len(trades) if trades else None,
                "profit_factor": gains / losses if losses else None,
                "evidence": "Paper simulation; not proof of profitability",
                "recent_events": events[-20:]}
