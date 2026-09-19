"""Deterministic tests for market and adaptive-limit executors."""

import unittest
import sys
import io
from contextlib import redirect_stdout
from unittest.mock import patch
from types import ModuleType

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = ModuleType("requests")

from fulfillment import fulfill_intents
from api import ApiException
from intentions import OrderIntent, TradeBundle
from state import TradingState


def arb_state():
    state = TradingState()
    state.update_case(10, "ACTIVE")
    for ticker, bid, ask in (
        ("BULL", 10.00, 10.02),
        ("BEAR", 15.90, 15.92),
        ("RITC", 25.47, 25.49),
    ):
        state.record_book(ticker, ((bid, 1000),), ((ask, 1000),))
    bundle = state.add_bundle(TradeBundle(
        bundle_id="arb-10-1", reason="ETF_ARB_BUY_ETF",
        created_tick=10, expected_profit_cad=20.0,
        max_unhedged_ticks=2, quantity=100,
    ))
    for ticker, quantity, price in (
        ("BULL", -100, 10.00),
        ("BEAR", -100, 15.90),
        ("RITC", 100, 25.49),
    ):
        intent = state.add_intent(OrderIntent(
            intent_id=f"{bundle.bundle_id}-{ticker}",
            ticker=ticker, quantity=quantity, reason=bundle.reason,
            created_tick=10, deadline_tick=12, limit_price=price,
            bundle_id=bundle.bundle_id,
        ))
        bundle.intent_ids.append(intent.intent_id)
    return state


class FakeMarketClient:
    def __init__(self):
        self.orders = []

    def place_order(self, ticker, action, quantity, order_type="MARKET", price=None):
        self.orders.append((ticker, action, quantity, order_type, price))
        return {
            "order_id": len(self.orders), "quantity_filled": quantity,
            "status": "TRANSACTED", "vwap": price or 10.0,
        }


class FakeLimitClient(FakeMarketClient):
    def __init__(self, passive_fill=0):
        super().__init__()
        self.passive_fill = passive_fill
        self.snapshots = {}
        self.cancelled = []

    def place_order(self, ticker, action, quantity, order_type="MARKET", price=None):
        self.orders.append((ticker, action, quantity, order_type, price))
        order_id = len(self.orders)
        is_passive = order_id == 1
        snapshot = {
            "order_id": order_id,
            "quantity": quantity,
            "quantity_filled": self.passive_fill if is_passive else quantity,
            "status": "OPEN" if is_passive else "TRANSACTED",
            "vwap": price,
        }
        self.snapshots[order_id] = snapshot
        return dict(snapshot)

    def get_order(self, order_id):
        return dict(self.snapshots[order_id])

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.snapshots[order_id]["status"] = "CANCELLED"
        return {"success": True}


class ExecutionPolicyTests(unittest.TestCase):
    def test_default_order_log_is_compact_and_completion_is_once(self):
        state = arb_state()
        output = io.StringIO()
        with patch.dict("os.environ", {"RIT_VERBOSE_ORDERS": "0"}), redirect_stdout(output):
            fulfill_intents(FakeMarketClient(), state, policy="market")
            fulfill_intents(FakeMarketClient(), state, policy="market")
        self.assertEqual(output.getvalue().count("BUNDLE COMPLETE |"), 1)
        self.assertNotIn("ORDER SUBMIT |", output.getvalue())
        self.assertNotIn("ORDER FILL |", output.getvalue())
        self.assertIn("fee_est=4.00CAD+2.00USD", output.getvalue())

    def test_verbose_order_log_restores_child_order_detail(self):
        output = io.StringIO()
        with patch.dict("os.environ", {"RIT_VERBOSE_ORDERS": "1"}), redirect_stdout(output):
            fulfill_intents(FakeMarketClient(), arb_state(), policy="market")
        self.assertEqual(output.getvalue().count("ORDER SUBMIT |"), 3)
        self.assertEqual(output.getvalue().count("ORDER FILL |"), 3)

    def test_market_policy_only_submits_market_orders(self):
        state = arb_state()
        client = FakeMarketClient()
        fulfill_intents(client, state, policy="market")
        self.assertEqual(len(client.orders), 3)
        self.assertTrue(all(order[3] == "MARKET" for order in client.orders))
        self.assertEqual(state.bundles["arb-10-1"].status, "FILLED")

    def test_limit_policy_passive_then_cancel_and_cross(self):
        state = arb_state()
        client = FakeLimitClient()
        fulfill_intents(client, state, policy="adaptive_limit")
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(client.orders[0], ("BULL", "SELL", 100, "LIMIT", 10.02))
        state.update_case(11, "ACTIVE")
        fulfill_intents(client, state, policy="adaptive_limit")
        self.assertEqual(client.cancelled, [1])
        self.assertEqual(len(client.orders), 4)
        self.assertTrue(all(order[3] == "LIMIT" for order in client.orders))
        self.assertEqual(state.bundles["arb-10-1"].status, "FILLED")

    def test_partial_passive_fill_is_not_counted_twice(self):
        state = arb_state()
        client = FakeLimitClient(passive_fill=40)
        fulfill_intents(client, state, policy="adaptive_limit")
        self.assertEqual(state.positions["BULL"], -40)
        state.update_case(11, "ACTIVE")
        fulfill_intents(client, state, policy="adaptive_limit")
        self.assertEqual(state.positions["BULL"], -100)
        self.assertEqual(state.positions["BEAR"], -100)
        self.assertEqual(state.positions["RITC"], 100)
        self.assertEqual(state.intents["arb-10-1-BULL"].filled_quantity, 100)

    def test_tender_exit_starts_with_marketable_limit(self):
        state = arb_state()
        state.intents.clear()
        state.bundles.clear()
        state.add_intent(OrderIntent(
            intent_id="tender-exit", ticker="RITC", quantity=-100,
            reason="TENDER_DIRECT_EXIT", created_tick=10,
            deadline_tick=300, limit_price=25.47,
        ))
        client = FakeMarketClient()
        fulfill_intents(client, state, policy="adaptive_limit")
        self.assertEqual(
            client.orders, [("RITC", "SELL", 100, "LIMIT", 25.47)]
        )

    def test_failed_cancel_does_not_create_replacement(self):
        class FailedCancelClient(FakeLimitClient):
            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                return {"success": False}

        state = arb_state()
        client = FailedCancelClient()
        fulfill_intents(client, state, policy="adaptive_limit")
        state.update_case(11, "ACTIVE")
        fulfill_intents(client, state, policy="adaptive_limit")
        self.assertEqual(len(client.orders), 1)
        self.assertEqual(client.cancelled, [1])

    def test_uncertain_limit_submission_halts_instead_of_retrying(self):
        class UncertainClient(FakeMarketClient):
            def place_order(self, *args, **kwargs):
                raise ApiException("HTTP 500 after POST")

        with self.assertRaisesRegex(RuntimeError, "outcome is uncertain"):
            fulfill_intents(
                UncertainClient(), arb_state(), policy="adaptive_limit",
            )

    def test_fill_arriving_during_cancel_reduces_replacement_quantity(self):
        class LateFillClient(FakeLimitClient):
            def cancel_order(self, order_id):
                self.snapshots[order_id]["quantity_filled"] = 30
                return super().cancel_order(order_id)

        state = arb_state()
        client = LateFillClient()
        fulfill_intents(client, state, policy="adaptive_limit")
        state.update_case(11, "ACTIVE")
        fulfill_intents(client, state, policy="adaptive_limit")
        self.assertEqual(client.orders[1][2], 70)
        self.assertEqual(state.positions["BULL"], -100)


if __name__ == "__main__":
    unittest.main()
