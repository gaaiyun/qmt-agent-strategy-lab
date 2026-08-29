# coding: utf-8
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "examples"))


def _bar(day, close, volume=1000):
    return {
        "date": "2024-01-%02d" % day,
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": volume,
    }


class PureResearchTests(unittest.TestCase):
    def test_validate_bars_rejects_ohlc_range_error(self):
        from pure_code_research import validate_bars

        rows = [_bar(day, 10.0 + day * 0.01) for day in range(1, 4)]
        rows[0]["high"] = 9.0
        result = validate_bars(rows)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("OHLC range" in issue for issue in result["issues"]))

    def test_transaction_costs_are_explicit(self):
        from qmt_backtest_research import TransactionCosts

        costs = TransactionCosts(commission_rate=0.001, stamp_duty_rate=0.001)
        buy = costs.charges(10000, "BUY")
        sell = costs.charges(10000, "SELL")
        self.assertEqual(buy["total_cost"], 10.0)
        self.assertEqual(sell["total_cost"], 20.0)

    def test_market_regime_is_unknown_before_long_window(self):
        from pure_code_research import market_regimes

        rows = [_bar(day, 10.0 + day * 0.01) for day in range(1, 11)]
        regimes = market_regimes(rows, short_window=2, long_window=5)
        self.assertEqual(regimes[rows[0]["date"]], "unknown")
        self.assertIn(regimes[rows[-1]["date"]], {"bull", "neutral", "bear"})


if __name__ == "__main__":
    unittest.main()
