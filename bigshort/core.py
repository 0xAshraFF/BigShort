from dataclasses import dataclass, asdict
from math import floor, isfinite
from statistics import mean, pstdev


@dataclass(frozen=True)
class Config:
    capital: float = 100.0
    risk_fraction: float = .01
    leverage: float = 2.0
    margin_cap: float = 10.0
    daily_loss_fraction: float = .03
    drawdown_fraction: float = .10
    fee: float = .0005
    slippage: float = .001
    max_hold_minutes: int = 240
    max_age_days: int = 90
    min_quote_volume: float = 5_000_000
    max_spread_bps: float = 20
    mode: str = "both"

    def __post_init__(self):
        if not all(isfinite(x) and x > 0 for x in
                   [self.capital, self.risk_fraction, self.leverage, self.margin_cap,
                    self.daily_loss_fraction, self.drawdown_fraction, self.max_hold_minutes,
                    self.max_age_days, self.min_quote_volume, self.max_spread_bps]):
            raise ValueError("Risk settings must be finite and positive")
        if not 1 <= self.leverage <= 3 or self.risk_fraction > .02:
            raise ValueError("Paper safety profile: leverage 1–3; risk <= 2%")
        if not 0 <= self.fee <= .01 or not 0 <= self.slippage <= .02:
            raise ValueError("Invalid cost assumptions")
        if not 0 < self.daily_loss_fraction <= .05 or not 0 < self.drawdown_fraction <= .2:
            raise ValueError("Invalid circuit breakers")
        if self.mode not in {"both", "rejection", "momentum"}:
            raise ValueError("Unknown strategy mode")


@dataclass(frozen=True)
class Bar:
    t: int  # exclusive end time in milliseconds
    o: float
    h: float
    l: float
    c: float
    v: float

    def __post_init__(self):
        if not all(isfinite(x) for x in [self.o, self.h, self.l, self.c, self.v]):
            raise ValueError("Non-finite candle")
        if not 0 < self.l <= min(self.o, self.c) <= max(self.o, self.c) <= self.h or self.v < 0:
            raise ValueError("Invalid OHLCV")


def upper(bars):
    values = [b.c for b in bars[-20:]]
    return mean(values) + 2 * pstdev(values)


def signal(h4, m15, m1, mode="both", diagnostics=None):
    """Only closed candles. Return (setup, structural stop), or None."""
    if min(len(h4), len(m15), len(m1)) < 21:
        if diagnostics is not None:
            diagnostics.update(reason="insufficient_candles")
        return None
    a, b, c = h4[-1], m15[-1], m1[-1]
    # Upper-band touch alone does not establish a reversal.
    extended = a.h >= upper(h4[:-1])
    width = max(b.h - b.l, 1e-12)
    weak = abs(b.c - b.o) / width <= .35 and (b.h - max(b.c, b.o)) / width >= .4
    rejection = extended and b.h >= upper(m15[:-1]) and weak and c.c < m1[-2].l
    typical = mean(x.h - x.l for x in m15[-21:-1])
    momentum = (extended and b.c < b.o and (b.o - b.c) >= 1.5 * typical
                and (b.c - b.l) / width <= .2 and c.c < m1[-2].l)
    setup = "rejection" if rejection and mode != "momentum" else (
        "momentum" if momentum and mode != "rejection" else None)
    stop = max(b.h, max(x.h for x in m1[-3:])) * 1.001
    if diagnostics is not None:
        distance = stop / c.c - 1
        reason = ("no_4h_extension" if not extended else
                  "no_1m_breakdown" if not c.c < m1[-2].l else
                  "no_enabled_15m_setup" if not setup else
                  "stop_distance_out_of_range" if not .003 <= distance <= .05 else "signal")
        diagnostics.update(reason=reason, mode=mode, extended_4h=extended,
                           band_touch_15m=b.h >= upper(m15[:-1]), weak_candle_15m=weak,
                           breakdown_1m=c.c < m1[-2].l, rejection=bool(rejection),
                           momentum=bool(momentum), stop_distance_pct=round(distance*100,4))
    # Do not chase a candle after an excessively large displacement.
    return (setup, stop) if setup and .003 <= stop / c.c - 1 <= .05 else None


@dataclass
class Position:
    symbol: str
    setup: str
    entry: float
    qty: float
    initial_qty: float
    stop: float
    initial_margin: float
    opened: int
    last_t: int
    costs: float
    gross: float = 0
    partial: bool = False
    peak_roe: float = 0
    funding_t: int = 0


class Engine:
    def __init__(self, config=Config(), state=None):
        self.cfg = config
        self.cash = config.capital
        self.peak = config.capital
        self.day = None
        self.day_start = config.capital
        self.halted = False
        self.position = None
        self.events = []
        self.seen = {}
        if state:
            for key in ["cash", "peak", "day", "day_start", "halted", "seen"]:
                setattr(self, key, state[key])
            self.position = Position(**state["position"]) if state["position"] else None

    def state(self):
        return {k: getattr(self, k) for k in ["cash", "peak", "day", "day_start", "halted", "seen"]} | {
            "position": asdict(self.position) if self.position else None}

    def event(self, kind, **values):
        self.events.append({"kind": kind, **values})

    def equity(self, price=None):
        p = self.position
        return self.cash + ((p.entry - price) * p.qty - price * p.qty * self.cfg.fee if p and price else 0)

    def clock(self, t, equity):
        day = t // 86_400_000
        if day != self.day:
            self.day, self.day_start = day, equity
        self.peak = max(self.peak, equity)
        if equity <= self.peak * (1 - self.cfg.drawdown_fraction):
            self.halted = True
        return self.halted or equity <= self.day_start * (1 - self.cfg.daily_loss_fraction)

    def enter(self, symbol, setup, stop, price, t, step=.000001, minimum=5):
        if self.position or self.clock(t, self.cash) or self.seen.get(symbol, 0) >= t:
            return False
        if not all(isfinite(x) and x > 0 for x in [price, stop, step, minimum]) or stop <= price:
            return False
        entry = price * (1 - self.cfg.slippage)
        # Risk includes adverse stop slippage and both taker fees.
        worst = stop * (1 + self.cfg.slippage)
        unit_risk = worst - entry + self.cfg.fee * (entry + worst)
        notional = min(self.cfg.margin_cap, self.cash * .1) * self.cfg.leverage
        qty = floor(min(self.cash * self.cfg.risk_fraction / unit_risk, notional / entry) / step) * step
        if qty <= 0 or qty * entry < minimum:
            return False
        fee = qty * entry * self.cfg.fee
        self.cash -= fee
        self.position = Position(symbol, setup, entry, qty, qty, stop,
                                 qty * entry / self.cfg.leverage, t, t, fee)
        self.seen[symbol] = t
        self.event("entry", t=t, symbol=symbol, setup=setup, price=entry, qty=qty,
                   stop=stop, planned_risk=qty * unit_risk)
        return True

    def close(self, price, t, reason, fraction=1):
        p = self.position
        fill = price * (1 + self.cfg.slippage)
        qty = p.qty * fraction
        fee = fill * qty * self.cfg.fee
        gross = (p.entry - fill) * qty
        self.cash += gross - fee
        p.costs += fee
        p.gross += gross
        p.qty -= qty
        self.event("exit", t=t, symbol=p.symbol, price=fill, qty=qty, reason=reason,
                   cash=self.cash, realized=gross - fee)
        if fraction == 1:
            self.event("trade", t=t, symbol=p.symbol, setup=p.setup,
                       net=p.gross - p.costs, margin=p.initial_margin)
            self.seen[p.symbol] = t + 900_000
            self.position = None
            self.clock(t, self.cash)

    def funding(self, rate, mark, t):
        p = self.position
        if p and p.last_t < t <= p.last_t + 60_000 and t > p.funding_t:
            # Positive funding pays shorts; negative funding charges shorts.
            amount = p.qty * mark * rate
            self.cash += amount
            p.gross += amount
            p.funding_t = t
            self.event("funding", t=t, symbol=p.symbol, amount=amount)

    def bar(self, b):
        p = self.position
        if not p or b.t <= p.last_t:
            return
        p.last_t = b.t
        # Stop-first ordering: no assumption that the profitable intrabar low came first.
        if b.h >= p.stop:
            self.close(max(b.o, p.stop), b.t, "stop")
            return
        equity = self.equity(b.c)
        if self.clock(b.t, equity):
            self.close(b.c, b.t, "circuit_breaker")
            return
        if b.t - p.opened >= self.cfg.max_hold_minutes * 60_000:
            self.close(b.c, b.t, "time_exit")
            return
        net = p.gross - p.costs + (p.entry - b.c) * p.qty - b.c * p.qty * self.cfg.fee
        roe = net / p.initial_margin
        p.peak_roe = max(p.peak_roe, roe)
        # ROE is net profit / INITIAL margin, not percentage price movement.
        if roe >= 1 and not p.partial:
            self.close(b.c, b.t, "take_half_at_100pct_roe", .5)
            p.partial = True
        # 5% net ROE -> breakeven; 10% -> lock 5%; 15% -> lock 10%.
        if p.peak_roe >= .05:
            locked = max(0, (floor((p.peak_roe + 1e-10) / .05) - 1) * .05) * p.initial_margin
            target = (p.gross - p.costs + p.entry * p.qty - locked) / (
                p.qty * (1 + self.cfg.fee) * (1 + self.cfg.slippage))
            new_stop = min(p.stop, target)
            if new_stop <= b.c:
                self.close(b.c, b.t, "profit_lock")
            elif new_stop < p.stop:
                p.stop = new_stop
                self.event("trail", t=b.t, symbol=p.symbol, stop=p.stop, locked=locked)
