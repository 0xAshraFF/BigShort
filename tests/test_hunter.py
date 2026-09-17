import tempfile
import unittest
from pathlib import Path

from bigshort.core import Bar, Engine
from bigshort.hunter import HunterLedger, OpenRouter, features, load_key, validate_decision


class HunterTests(unittest.TestCase):
    def test_rtf_key_is_extracted_without_rtf_parser(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "key.rtf"
            path.write_text(r"{\rtf1 token sk-or-v1-abc_123}")
            self.assertEqual(load_key(path), "sk-or-v1-abc_123")

    def test_budget_ledger_is_durable(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "hunter.sqlite")
            ledger = HunterLedger(path)
            ledger.save_call(t=1, candidate="analyst", model="m", prompt="p", response="r",
                             decision="{}", prompt_tokens=10, completion_tokens=2,
                             cost=.25, status="ok", error=None)
            self.assertEqual(HunterLedger(path).spent("analyst"), .25)

    def test_json_response_parser(self):
        value = OpenRouter._parse('```json\n{"action":"HOLD"}\n```')
        self.assertEqual(value["action"], "HOLD")

    def test_entry_validation_has_no_trade_quota(self):
        snapshot = [{"market": {"symbol": "TEST"}}]
        self.assertEqual(validate_decision({"action":"HOLD", "confidence":.2}, Engine(), snapshot), "HOLD")
        self.assertEqual(validate_decision({"action":"SHORT", "symbol":"TEST", "stop_pct":1,
                                            "confidence":.8}, Engine(), snapshot), "SHORT")
        with self.assertRaises(ValueError):
            validate_decision({"action":"SHORT", "symbol":"TEST", "stop_pct":10,
                               "confidence":.8}, Engine(), snapshot)

    def test_market_features_are_compact_and_numeric(self):
        bars = [Bar(i*60_000, 100, 101, 99, 100+i/100, 1) for i in range(1, 31)]
        result = features("TEST", [bars, bars, bars], 100, 100.01)
        self.assertEqual(result["symbol"], "TEST")
        self.assertEqual(len(result["recent_1m_closes"]), 8)


if __name__ == "__main__":
    unittest.main()
