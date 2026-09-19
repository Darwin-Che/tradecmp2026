"""Opposite trades must not reopen a tender hedge through lot accounting."""

import sys
import unittest
from types import ModuleType

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = ModuleType("requests")

import strategy
from arbitrage import ArbitragePlan, PlannedLeg
from convergence import find_convergence_exit, update_convergence
from fulfillment import fulfill_intents
from test_execution_policy import FakeMarketClient
from test_tender_strategy import market_state


def sell_etf_plan(quantity):
    return ArbitragePlan(
        reason="ETF_ARB_SELL_ETF", quantity=quantity,
        gross_profit_cad=200.0, fees_cad=60.0,
        expected_profit_cad=140.0, edge_per_share_cad=140 / quantity,
        projected_gross=0, projected_net=0,
        legs=(
            PlannedLeg("BULL", quantity, 10.02),
            PlannedLeg("BEAR", quantity, 15.92),
            PlannedLeg("RITC", -quantity, 25.47),
        ),
    )


class InventoryNettingTests(unittest.TestCase):
    def test_three_offsets_do_not_create_convergence_lots(self):
        original_state = strategy.STATE
        try:
            strategy.STATE = market_state({
                "BULL": -42_400, "BEAR": -42_400, "RITC": 42_400,
            })
            client = FakeMarketClient()
            for tick in (130, 137, 138):
                strategy.STATE.update_case(tick, "ACTIVE")
                bundle = strategy.enqueue_arb(sell_etf_plan(5_000))
                self.assertEqual(bundle.offset_quantity, 5_000)
                self.assertEqual(bundle.open_quantity, 0)
                fulfill_intents(client, strategy.STATE, policy="market")
                self.assertEqual(bundle.status, "FILLED")
            self.assertEqual(strategy.STATE.positions["RITC"], 27_400)
            self.assertFalse(update_convergence(strategy.STATE))
            self.assertIsNone(find_convergence_exit(strategy.STATE))
        finally:
            strategy.STATE = original_state

    def test_mixed_trade_tracks_only_new_inventory_as_open(self):
        original_state = strategy.STATE
        try:
            strategy.STATE = market_state({
                "BULL": -3_000, "BEAR": -3_000, "RITC": 3_000,
            })
            strategy.STATE.update_case(100, "ACTIVE")
            bundle = strategy.enqueue_arb(sell_etf_plan(5_000))
            self.assertEqual(bundle.offset_quantity, 3_000)
            self.assertEqual(bundle.open_quantity, 2_000)
            fulfill_intents(FakeMarketClient(), strategy.STATE, policy="market")
            self.assertEqual(strategy.STATE.positions["RITC"], -2_000)
            self.assertEqual(len(update_convergence(strategy.STATE)), 1)
        finally:
            strategy.STATE = original_state


if __name__ == "__main__":
    unittest.main()
