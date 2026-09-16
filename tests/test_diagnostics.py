import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from bigshort.core import Bar, Config, Engine, signal
from bigshort.market import Binance
from bigshort.runner import cycle


class DiagnosticsTests(unittest.TestCase):
    def api(self):
        api=Mock()
        api.now.return_value=360_000_000
        api.universe.return_value=[{"symbol":"TEST","step":.001,"minimum":5}]
        api.universe_stats={"total":1,"eligible":1}
        def bars(symbol,interval,now):
            span={"4h":14_400_000,"15m":900_000,"1m":60_000}[interval]
            end=now//span*span
            return [Bar(end-i*span,100,100.1,99.9,100,1) for i in range(20,-1,-1)]
        api.bars.side_effect=bars
        return api

    def capture(self,api,engine=None):
        out=io.StringIO()
        with redirect_stdout(out),patch("bigshort.runner.Path.exists",return_value=False):
            cycle(api,engine or Engine(),Mock(),"unused")
        return [json.loads(line) for line in out.getvalue().splitlines()]

    def test_no_setup_has_freshness_and_summary(self):
        events=self.capture(self.api())
        detail=events[1]
        self.assertEqual(detail["kind"],"symbol_scan")
        self.assertEqual(detail["reason"],"no_1m_breakdown")
        self.assertTrue(all(x["fresh"] for x in detail["candles"].values()))
        summary=events[-1]
        self.assertEqual(summary["scanned"],1)
        self.assertEqual(summary["entries"],0)
        self.assertIn("timestamp",summary)
        self.assertIn("duration_seconds",summary)

    def test_empty_universe(self):
        api=self.api()
        api.universe.return_value=[]
        api.universe_stats={"eligible":0}
        summary=self.capture(api)[-1]
        self.assertEqual(summary["status"],"empty_universe")
        self.assertEqual(summary["scanned"],0)

    def test_stale_candles(self):
        api=self.api()
        api.bars.side_effect=lambda *a:[Bar(i*60_000,100,101,99,100,1) for i in range(1,22)]
        events=self.capture(api)
        self.assertEqual(events[1]["reason"],"stale_candles:4h")
        self.assertFalse(events[1]["candles"]["4h"]["fresh"])

    def test_error_summary_is_not_success(self):
        api=self.api()
        api.now.side_effect=RuntimeError("network unavailable")
        out=io.StringIO()
        with redirect_stdout(out),self.assertRaises(RuntimeError):
            cycle(api,Engine(),Mock(),"unused")
        event=json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(event["status"],"error")
        self.assertEqual(event["scanned"],0)

    def test_halted_summary(self):
        e=Engine()
        e.halted=True
        api=self.api()
        self.assertEqual(self.capture(api,e)[-1]["status"],"risk_halted")
        api.universe.assert_not_called()

    def test_universe_counts_partition_exchange_symbols(self):
        api=Binance()
        now=100*86_400_000
        valid={"symbol":"OK","onboardDate":now-10*86_400_000,"status":"TRADING",
               "contractType":"PERPETUAL","quoteAsset":"USDT",
               "filters":[{"filterType":"LOT_SIZE","stepSize":"0.01"}]}
        symbols=[valid,dict(valid,symbol="OLD",onboardDate=0),dict(valid,symbol="SPOT",contractType="SPOT"),dict(valid,symbol="LOW")]
        api.get=Mock(side_effect=[[{"symbol":"OK","quoteVolume":"6000000"}],{"symbols":symbols}])
        self.assertEqual(len(api.universe(Config(),now)),1)
        self.assertEqual(api.universe_stats,{"total":4,"eligible":1,"contract_rejected":1,"age_rejected":1,"volume_rejected":1})

    def test_diagnostics_do_not_change_signal(self):
        base=[Bar(i*60_000,100,100.2,99.8,100,1) for i in range(1,21)]
        h=base+[Bar(21*60_000,100,103,99,102,1)]
        m=base+[Bar(21*60_000,100.8,101,99,99.1,1)]
        minute=base+[Bar(21*60_000,99.7,99.8,98.9,99,1)]
        for mode in ["both","momentum","rejection"]:
            detail={}
            expected=signal(h,m,minute,mode)
            self.assertEqual(signal(h,m,minute,mode,detail),expected)
            self.assertEqual(detail["reason"]=="signal",expected is not None)
