"""Stable drawdown and settlement-aware inventory policy tests."""

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = ModuleType("requests")

import strategy
from arbitrage import ArbitragePlan
from convergence import find_convergence_exit
from intentions import TradeBundle
from state import TradingState
from test_tender_strategy import market_state


class RiskPolicyTests(unittest.TestCase):
    def test_loss_guard_blocks_growth_but_allows_offsets(self):
        original_state = strategy.STATE
        try:
            strategy.STATE = TradingState()
            strategy.STATE.loss_growth_guard_active = True
            self.assertFalse(strategy.arb_plan_allowed(
                SimpleNamespace(projected_gross=120_000), 100_000,
            ))
            self.assertTrue(strategy.arb_plan_allowed(
                SimpleNamespace(projected_gross=80_000), 100_000,
            ))
            self.assertTrue(strategy.arb_plan_allowed(
                SimpleNamespace(projected_gross=100_000), 100_000,
            ))
        finally:
            strategy.STATE = original_state

    def test_loss_growth_guard_has_recovery_hysteresis(self):
        original_state = strategy.STATE
        try:
            strategy.STATE = TradingState()
            strategy.STATE.risk_pnl_cad = -6_000
            self.assertTrue(strategy.loss_growth_guard_active())
            strategy.STATE.loss_growth_guard_active = True
            strategy.STATE.risk_pnl_cad = -3_000
            self.assertTrue(strategy.loss_growth_guard_active())
            strategy.STATE.risk_pnl_cad = -2_000
            self.assertFalse(strategy.loss_growth_guard_active())
        finally:
            strategy.STATE = original_state

    def test_endgame_convergence_keeps_negative_close(self):
        state = TradingState()
        state.add_bundle(TradeBundle(
            bundle_id="opening", reason="ETF_ARB_BUY_ETF", created_tick=20,
            expected_profit_cad=200.0, max_unhedged_ticks=2,
            quantity=100, open_quantity=100, status="FILLED",
            convergence=1.0,
        ))
        close = ArbitragePlan(
            reason="ETF_ARB_SELL_ETF", quantity=100,
            gross_profit_cad=10.0, fees_cad=60.0,
            expected_profit_cad=-50.0, edge_per_share_cad=-0.5,
            projected_gross=0, projected_net=0, legs=(),
        )
        with patch("convergence.evaluate_arbitrage", return_value=close):
            self.assertIsNone(find_convergence_exit(
                state, minimum_round_trip_cad=100,
                minimum_close_profit_cad=0,
            ))
            self.assertIsNotNone(find_convergence_exit(
                state, minimum_round_trip_cad=100,
            ))

    def test_risk_mark_skips_hedging_and_confirms_breach_and_recovery(self):
        state = TradingState()
        for tick, pnl in ((1, 10_000), (2, 20_000), (3, 30_000)):
            state.update_pnl(pnl)
            state.update_risk_mark(tick, eligible=True)
        self.assertEqual(state.risk_high_water_cad, 20_000)
        state.update_pnl(100_000)
        state.update_risk_mark(4, eligible=False)
        self.assertEqual(state.risk_high_water_cad, 20_000)
        for tick in (5, 6):
            state.update_pnl(10_000)
            state.update_risk_mark(tick, eligible=True)
        self.assertFalse(state.pnl_drawdown_active)
        state.update_pnl(10_000)
        state.update_risk_mark(7, eligible=True)
        self.assertTrue(state.pnl_drawdown_active)
        state.update_risk_mark(7, eligible=True)
        self.assertTrue(state.pnl_drawdown_active)
        for tick in (8, 9, 10):
            state.update_pnl(20_000)
            state.update_risk_mark(tick, eligible=True)
        self.assertFalse(state.pnl_drawdown_active)

    def test_endgame_holds_negative_edge_but_allows_profitable_close(self):
        original_state, original_max = strategy.STATE, strategy.MAX_GROSS
        try:
            strategy.STATE = market_state({"BULL": -5_000, "BEAR": -5_000,
                                           "RITC": 5_000})
            strategy.STATE.strategy_status = "REDUCING_INVENTORY"
            strategy.MAX_GROSS = 20_000
            strategy.STATE.record_book(
                "RITC", ((25.96, 200_000),), ((25.98, 200_000),)
            )
            strategy.STATE.update_case(strategy.ENDGAME_HOLD_TICK - 1, "ACTIVE")
            early = strategy.inventory_reduction_plan()
            self.assertIsNotNone(early)
            self.assertLess(early.expected_profit_cad, 0)
            strategy.STATE.update_case(strategy.ENDGAME_HOLD_TICK, "ACTIVE")
            self.assertIsNone(strategy.inventory_reduction_plan())
            self.assertEqual(strategy.STATE.strategy_status,
                             "HOLDING_TO_SETTLEMENT")
            strategy.STATE.record_book(
                "RITC", ((26.10, 200_000),), ((26.12, 200_000),)
            )
            profitable = strategy.inventory_reduction_plan()
            self.assertIsNotNone(profitable)
            self.assertGreaterEqual(profitable.expected_profit_cad, 0)
        finally:
            strategy.STATE = original_state
            strategy.MAX_GROSS = original_max


if __name__ == "__main__":
    unittest.main()
