"""Deterministic tender-route and lifecycle checks (no live API or replay)."""

import unittest
import sys
import io
from contextlib import redirect_stdout
from types import ModuleType, SimpleNamespace

# The pure strategy tests do not instantiate the HTTP client. Keep them runnable
# in minimal Python environments where the live client's requests dependency is absent.
try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = ModuleType("requests")

import strategy
from fulfillment import fulfill_intents
from state import TradingState
from tender_strategy import evaluate_fixed_tender


def market_state(positions=None):
    state = TradingState()
    if positions:
        state.positions.update(positions)
    for ticker, bid, ask in (
        ("BULL", 10.00, 10.02),
        ("BEAR", 15.90, 15.92),
        ("RITC", 25.47, 25.49),
        ("USD", 1.0000, 1.0010),
    ):
        state.record_book(
            ticker, ((bid, 200_000),), ((ask, 200_000),)
        )
    return state


def offer(action="BUY", quantity=50_000, price=25.61, tender_id=1):
    return {
        "tender_id": tender_id,
        "ticker": "RITC",
        "action": action,
        "quantity": quantity,
        "price": price,
        "is_fixed_bid": True,
    }


class FakeClient:
    def __init__(self, offers):
        self.offers = offers
        self.accepted = []
        self.orders = []

    def get_tenders(self):
        return self.offers

    def accept_tender(self, tender_id, price):
        self.accepted.append((tender_id, price))
        return {"success": True}

    def place_order(self, ticker, action, quantity, order_type="MARKET"):
        self.orders.append((ticker, action, quantity, order_type))
        return {
            "order_id": len(self.orders),
            "quantity_filled": quantity,
            "status": "TRANSACTED",
            "vwap": 10.0 if ticker == "BULL" else 15.9,
        }


class TenderRouteTests(unittest.TestCase):
    def test_identical_rejections_are_logged_once_by_default(self):
        original_state = strategy.STATE
        original_rejections = strategy.LOGGED_TENDER_REJECTIONS
        try:
            strategy.STATE = market_state()
            strategy.LOGGED_TENDER_REJECTIONS = set()
            client = FakeClient([offer(price=27.0, tender_id=71)])
            output = io.StringIO()
            with redirect_stdout(output):
                strategy.accept_active_tender_offers(client)
                strategy.accept_active_tender_offers(client)
            self.assertEqual(output.getvalue().count("TENDER 71 REJECT |"), 1)
        finally:
            strategy.STATE = original_state
            strategy.LOGGED_TENDER_REJECTIONS = original_rejections

    def evaluate(self, item, state=None, **kwargs):
        return evaluate_fixed_tender(
            item,
            state or market_state(),
            max_gross=300_000,
            min_net=-200_000,
            max_net=200_000,
            **kwargs,
        )

    def test_buy_tender_can_sell_existing_basket_instead_of_etf(self):
        state = market_state({"BULL": 50_000, "BEAR": 50_000})
        evaluation = self.evaluate(offer(), state)
        self.assertTrue(evaluation.should_accept)
        self.assertEqual(evaluation.route, "BASKET")
        self.assertEqual(evaluation.basket_quantity, 50_000)
        self.assertEqual(evaluation.direct_quantity, 0)
        self.assertEqual(
            {(leg.ticker, leg.signed_quantity) for leg in evaluation.exit_legs},
            {("BULL", -50_000), ("BEAR", -50_000)},
        )

    def test_hybrid_respects_gross_capacity(self):
        evaluation = self.evaluate(offer(quantity=98_000))
        self.assertTrue(evaluation.should_accept)
        self.assertEqual(evaluation.route, "HYBRID")
        self.assertEqual(
            evaluation.direct_quantity + evaluation.basket_quantity, 98_000
        )
        self.assertLessEqual(evaluation.projected_gross, 300_000)

    def test_acceptance_limit_is_checked_before_planned_hedge(self):
        state = market_state({"BULL": 98_000, "BEAR": 98_000})
        evaluation = self.evaluate(offer(quantity=98_000), state)
        self.assertFalse(evaluation.should_accept)
        self.assertIn("acceptance would exceed gross", evaluation.reason)

    def test_sell_tender_uses_reverse_basket(self):
        state = market_state()
        state.record_book(
            "BEAR", ((15.00, 200_000),), ((15.10, 200_000),)
        )
        evaluation = self.evaluate(offer(action="SELL", price=26.20), state)
        self.assertTrue(evaluation.should_accept)
        self.assertEqual(evaluation.route, "BASKET")
        self.assertEqual(
            {(leg.ticker, leg.signed_quantity) for leg in evaluation.exit_legs},
            {("BULL", 50_000), ("BEAR", 50_000)},
        )

    def test_rejected_offer_is_reconsidered_and_accepted_once(self):
        original_state = strategy.STATE
        original_ids = strategy.ACCEPTED_TENDER_IDS
        original_limits = (
            strategy.MAX_GROSS, strategy.MAX_SHORT_NET, strategy.MAX_LONG_NET
        )
        try:
            strategy.STATE = market_state()
            strategy.STATE.update_case(20, "ACTIVE")
            strategy.ACCEPTED_TENDER_IDS = set()
            strategy.MAX_GROSS = 300_000
            strategy.MAX_SHORT_NET = -200_000
            strategy.MAX_LONG_NET = 200_000
            client = FakeClient([offer(price=27.0, tender_id=7)])
            self.assertFalse(strategy.accept_active_tender_offers(client))
            self.assertEqual(client.accepted, [])

            client.offers = [offer(price=25.61, tender_id=7)]
            self.assertTrue(strategy.accept_active_tender_offers(client))
            tender = strategy.STATE.tenders[7]
            self.assertEqual(tender.route, "BASKET")
            self.assertEqual(len(tender.intent_ids), 2)
            self.assertEqual(len(client.accepted), 1)
            self.assertFalse(strategy.accept_active_tender_offers(client))
            self.assertEqual(len(client.accepted), 1)

            for _ in range(5):
                fulfill_intents(client, strategy.STATE)
                strategy.sync_tender_unwinds()
            self.assertEqual(strategy.STATE.tenders[7].quantity_unwound, 50_000)
            self.assertEqual(strategy.STATE.hedge_remaining, {})
            self.assertEqual(strategy.STATE.strategy_status, "IDLE")
            self.assertEqual(strategy.STATE.positions["RITC"], 50_000)
            self.assertEqual(strategy.STATE.positions["BULL"], -50_000)
            self.assertEqual(strategy.STATE.positions["BEAR"], -50_000)
            self.assertEqual(
                {(ticker, action) for ticker, action, _, _ in client.orders},
                {("BULL", "SELL"), ("BEAR", "SELL")},
            )
        finally:
            strategy.STATE = original_state
            strategy.ACCEPTED_TENDER_IDS = original_ids
            (
                strategy.MAX_GROSS,
                strategy.MAX_SHORT_NET,
                strategy.MAX_LONG_NET,
            ) = original_limits

    def test_arbitrage_deadline_and_unhedged_setting_are_independent(self):
        original_state = strategy.STATE
        original_deadline = strategy.ARB_INTENT_DEADLINE_TICKS
        original_unhedged = strategy.MAX_UNHEDGED_TICKS
        try:
            strategy.STATE = market_state()
            strategy.STATE.update_case(40, "ACTIVE")
            strategy.ARB_INTENT_DEADLINE_TICKS = 4
            strategy.MAX_UNHEDGED_TICKS = 7
            plan = SimpleNamespace(
                reason="ETF_ARB_BUY_ETF",
                quantity=100,
                expected_profit_cad=50.0,
                gross_profit_cad=56.0,
                fees_cad=6.0,
                edge_per_share_cad=0.5,
                projected_gross=400,
                projected_net=0,
                legs=(SimpleNamespace(
                    ticker="RITC", signed_quantity=100, limit_price=25.49,
                ),),
            )
            bundle = strategy.enqueue_arb(plan)
            self.assertEqual(bundle.max_unhedged_ticks, 7)
            intent = strategy.STATE.intents[bundle.intent_ids[0]]
            self.assertEqual(intent.deadline_tick, 44)
        finally:
            strategy.STATE = original_state
            strategy.ARB_INTENT_DEADLINE_TICKS = original_deadline
            strategy.MAX_UNHEDGED_TICKS = original_unhedged


if __name__ == "__main__":
    unittest.main()
