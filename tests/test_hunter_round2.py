import unittest
from types import SimpleNamespace

from bigshort.hunter import HunterLedger, ROUND_MS
from bigshort.hunter_round2 import confirmed_rejection, fresh_same_coin_setup, round_start
import tempfile
from pathlib import Path


class RevisedGlmTests(unittest.TestCase):
    def market(self, **values):
        result={"symbol":"TEST","return_4h_3_pct":12,
                "distance_to_4h_upper_band_pct":5,"return_15m_1_pct":-1,
                "return_1m_5_pct":-.5,"15m_upper_wick_fraction":.25,
                "bar_close_4h_ms":200}
        result.update(values);return result

    def test_requires_confirmed_rejection(self):
        self.assertTrue(confirmed_rejection(self.market()))
        self.assertFalse(confirmed_rejection(self.market(return_15m_1_pct=.1)))
        self.assertFalse(confirmed_rejection(self.market(return_1m_5_pct=.1)))
        self.assertFalse(confirmed_rejection(self.market(**{"15m_upper_wick_fraction":.1})))

    def test_same_coin_requires_new_four_hour_setup(self):
        engine=SimpleNamespace(events=[{"kind":"strategy_entry","symbol":"TEST",
                                        "bar_close_4h_ms":200}])
        self.assertFalse(fresh_same_coin_setup(engine,self.market(bar_close_4h_ms=200)))
        self.assertTrue(fresh_same_coin_setup(engine,self.market(bar_close_4h_ms=201)))
        self.assertTrue(fresh_same_coin_setup(engine,self.market(symbol="OTHER")))

    def test_round_two_waits_for_original_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=HunterLedger(str(Path(directory)/"ledger.sqlite"))
            ledger.metadata("round_start_ms",100)
            self.assertEqual(round_start(ledger,200),100+ROUND_MS)


if __name__=="__main__": unittest.main()
