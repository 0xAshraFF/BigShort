import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from bigshort.core import Bar, Config, Engine, signal
from bigshort.store import Store
from bigshort.runner import manage
from bigshort.research import aggregate, replay


class EngineTests(unittest.TestCase):
    def opened(self,cfg=Config()):
        e=Engine(cfg)
        self.assertTrue(e.enter("TEST","rejection",102,100,60_000))
        return e

    def test_risk_includes_costs(self):
        e=self.opened()
        planned=e.events[0]["planned_risk"]
        e.bar(Bar(120_000,100,103,99,102,1))
        self.assertIsNone(e.position)
        self.assertAlmostEqual(100-e.cash,planned)
        self.assertLessEqual(planned,1)

    def test_gap_can_exceed_planned_risk(self):
        e=self.opened()
        e.bar(Bar(120_000,120,125,119,120,1))
        self.assertGreater(100-e.cash,1)

    def test_stop_precedes_intrabar_profit(self):
        e=self.opened()
        e.bar(Bar(120_000,100,103,60,61,1))
        self.assertLess(e.cash,100)
        self.assertEqual(e.events[-2]["reason"],"stop")

    def test_trailing_never_loosens(self):
        e=self.opened()
        e.bar(Bar(120_000,100,100.1,95,96,1))
        stop=e.position.stop
        self.assertLess(stop,102)
        e.bar(Bar(180_000,96,96.9,95,96.5,1))
        self.assertLessEqual(e.position.stop,stop)

    def test_duplicate_bar_ignored(self):
        e=self.opened()
        b=Bar(120_000,100,100.1,95,96,1)
        e.bar(b)
        before=e.state()
        e.bar(b)
        self.assertEqual(e.state(),before)

    def test_single_position(self):
        e=self.opened()
        self.assertFalse(e.enter("OTHER","x",102,100,120_000))

    def test_breakeven_includes_fees(self):
        e=self.opened()
        e.bar(Bar(120_000,100,100.1,95,96,1))
        stop=e.position.stop
        e.bar(Bar(180_000,96,stop+.01,95,96,1))
        self.assertAlmostEqual(e.cash,100)

    def test_partial_once(self):
        e=self.opened()
        e.bar(Bar(120_000,100,100,40,45,1))
        p=e.position
        self.assertTrue(p.partial)
        self.assertAlmostEqual(p.qty,p.initial_qty/2)

    def test_funding_both_directions(self):
        e=self.opened()
        before=e.cash
        e.funding(-.01,100,120_000)
        self.assertLess(e.cash,before)
        charged=e.cash
        e.funding(-.01,100,120_000)
        self.assertEqual(e.cash,charged)
        e.position.last_t=120_000
        e.funding(.01,100,180_000)
        self.assertAlmostEqual(e.cash,before)

    def test_drawdown_halts_permanently(self):
        e=Engine()
        e.cash=89
        self.assertFalse(e.enter("X","x",102,100,60_000))
        self.assertTrue(e.halted)
        e.cash=100
        self.assertFalse(e.enter("X","x",102,100,86_400_000))

    def test_daily_limit_resets(self):
        e=Engine()
        e.clock(60_000,100)
        e.cash=96
        self.assertFalse(e.enter("X","x",102,100,120_000))
        self.assertTrue(e.enter("X","x",102,100,86_400_000))

    def test_min_notional_skips(self):
        e=Engine()
        self.assertFalse(e.enter("X","x",102,100,60_000,minimum=100))

    def test_max_hold(self):
        e=self.opened(replace(Config(),max_hold_minutes=1))
        e.bar(Bar(120_000,100,101,99,100,1))
        self.assertEqual(e.events[-2]["reason"],"time_exit")

    def test_bad_settings(self):
        for values in [{"leverage":20},{"capital":float("nan")},{"risk_fraction":.1}]:
            with self.assertRaises(ValueError): Config(**values)

    def test_restart_and_config_lock(self):
        with tempfile.TemporaryDirectory() as d:
            path=str(Path(d)/"test.sqlite")
            s=Store(path,Config())
            e=self.opened()
            s.save(e)
            recovered=Engine(Config(),s.load())
            self.assertEqual(e.state(),recovered.state())
            recovered.bar(Bar(120_000,100,103,99,102,1))
            s.save(recovered)
            self.assertEqual(s.report()["closed_trades"],1)
            with self.assertRaises(ValueError): Store(path,replace(Config(),leverage=3))

    def test_restart_replays_stop(self):
        class API:
            def bars(self,*args,**kwargs):
                return [Bar(120_000,100,103,99,102,1),Bar(180_000,102,103,98,99,1)]
            def get(self,*args,**kwargs): return []
        with tempfile.TemporaryDirectory() as d:
            e=self.opened()
            s=Store(str(Path(d)/"s.sqlite"),Config())
            manage(API(),e,s,240_000)
            self.assertIsNone(e.position)
            self.assertLess(e.cash,100)


class ResearchTests(unittest.TestCase):
    def test_aggregate_excludes_partial(self):
        bars=[Bar(i*60_000,100,101,99,100,1) for i in range(1,17)]
        result=aggregate(bars,15)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].t,900_000)
        self.assertEqual(result[0].v,15)

    def test_gap_rejected(self):
        bars=[Bar(60_000,100,101,99,100,1),Bar(180_000,100,101,99,100,1)]
        with self.assertRaises(ValueError): replay(bars,Config())

    def test_signal_no_warmup(self):
        self.assertIsNone(signal([],[],[]))

    def test_rejection_and_mode_filter(self):
        base=[Bar(i*60_000,100,100.2,99.8,100,1) for i in range(1,21)]
        h=base+[Bar(21*60_000,100,103,99,102,1)]
        m=base+[Bar(21*60_000,101,102,100,101.1,1)]
        minute=base[:-1]+[Bar(20*60_000,101,101.2,100.8,101,1),Bar(21*60_000,101,101.1,100.4,100.5,1)]
        self.assertEqual(signal(h,m,minute,"rejection")[0],"rejection")
        self.assertIsNone(signal(h,m,minute,"momentum"))

    def test_momentum_and_no_extension(self):
        base=[Bar(i*60_000,100,100.2,99.8,100,1) for i in range(1,21)]
        h=base+[Bar(21*60_000,100,103,99,102,1)]
        m=base+[Bar(21*60_000,100.8,101,99,99.1,1)]
        minute=base+[Bar(21*60_000,99.7,99.8,98.9,99,1)]
        self.assertEqual(signal(h,m,minute,"momentum")[0],"momentum")
        h[-1]=Bar(21*60_000,99,99.5,98,99,1)
        self.assertIsNone(signal(h,m,minute,"momentum"))

    def test_no_lookahead_and_next_open(self):
        bars=[Bar(i*60_000,100,101,99,100,1) for i in range(1,31)]
        calls=[]
        def fake(h,m,b,mode):
            calls.append(b[-1].t)
            return ("test",102) if b[-1].t==60_000 else None
        with patch("bigshort.research.signal",side_effect=fake):
            result=replay(bars,Config())
        entry=next(e for e in result["events"] if e["kind"]=="entry")
        self.assertEqual(entry["t"],60_000)
        self.assertEqual(entry["price"],99.9)
        self.assertEqual(calls[0],60_000)


if __name__=="__main__": unittest.main()
