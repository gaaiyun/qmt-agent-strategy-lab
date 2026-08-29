# coding: utf-8
"""ETF_MOM_TREND_DEFENSIVE 的最小安全回归测试。"""

import unittest

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import qmt_etf_mom_trend_defensive as strategy


def _rising(length=520, step=0.25):
    return [100.0 + step * index for index in range(length)]


def _falling(length=520, step=0.15):
    return [200.0 - step * index for index in range(length)]


class _Context(object):
    barpos = 0
    trade_mode = "backtest"

    def __init__(self, history):
        self.history = history
        self.universe = None

    def set_universe(self, symbols):
        self.universe = list(symbols)

    def get_bar_timetag(self, barpos):
        return 1704153600 + int(barpos) * 86400

    def get_history_data(self, count, period, field, dividend_type):
        return self.history


class MomTrendDefensiveTests(unittest.TestCase):
    def test_skips_recent_bars_and_requires_history(self):
        prices = _rising()
        diagnostics = strategy.momentum_diagnostics(prices)
        self.assertIsNotNone(diagnostics)
        anchor = len(prices) - strategy.SKIP_BARS - 1
        expected = prices[anchor] / prices[anchor - 126] - 1.0
        self.assertAlmostEqual(diagnostics["momentum_6m"], expected)
        self.assertIsNone(strategy.momentum_diagnostics(prices[:200]))

    def test_top3_cap_and_defensive_fallback(self):
        history = {
            symbol: _rising(step=0.10 + 0.01 * index)
            for index, symbol in enumerate(strategy.RISK_UNIVERSE)
        }
        history[strategy.DEFENSIVE_SYMBOL] = _rising(step=0.01)
        weights = strategy.build_target_weights(history)
        risk = {symbol: value for symbol, value in weights.items() if symbol in strategy.RISK_UNIVERSE}
        self.assertLessEqual(len(risk), strategy.TOP_N)
        self.assertLessEqual(max(risk.values()), strategy.MAX_POSITION_WEIGHT)
        self.assertAlmostEqual(sum(weights.values()), 1.0)

        falling = {symbol: _falling() for symbol in strategy.RISK_UNIVERSE}
        falling[strategy.DEFENSIVE_SYMBOL] = _rising(step=0.01)
        self.assertEqual(strategy.build_target_weights(falling), {strategy.DEFENSIVE_SYMBOL: 1.0})

    def test_turnover_gate_and_t_plus_one(self):
        target = {"510300.SH": 0.8, strategy.DEFENSIVE_SYMBOL: 0.2}
        limited = strategy.apply_turnover_gate({strategy.DEFENSIVE_SYMBOL: 1.0}, target)
        self.assertLessEqual(
            strategy.one_way_turnover({strategy.DEFENSIVE_SYMBOL: 1.0}, limited),
            strategy.MAX_ONE_WAY_TURNOVER + 1e-12,
        )
        self.assertAlmostEqual(sum(limited.values()), 1.0)
        self.assertEqual(strategy.signal_history([1.0, 2.0, 3.0]), [1.0, 2.0])

    def test_qmt_init_is_safe_by_default(self):
        context = _Context({symbol: _rising() for symbol in strategy.UNIVERSE})
        calls = []
        previous = getattr(strategy, "passorder", None)
        strategy.passorder = lambda *args: calls.append(args) or 0
        try:
            strategy.init(context)
            strategy.handlebar(context)
        finally:
            if previous is None:
                del strategy.passorder
            else:
                strategy.passorder = previous
        self.assertEqual(context.universe, list(strategy.UNIVERSE))
        self.assertFalse(strategy.ENABLE_ORDERS)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
