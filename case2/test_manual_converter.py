"""Manual converter advisory and fallback tests; no simulator replay."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from types import ModuleType
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = ModuleType("requests")

import strategy
from intentions import TradeBundle
from manual_converter import (
    ConverterProposal, apply_conversion_to_open_lots, converted_blocks,
    propose_conversion, start_window,
)
from test_tender_strategy import market_state


def converter_market(positions=None):
    state = market_state(positions or {
        "BULL": 10_000, "BEAR": 10_000, "RITC": -10_000,
    })
    state.record_book("RITC", ((26.02, 100_000),), ((26.04, 100_000),))
    state.update_case(100, "ACTIVE")
    return state


def proposal(state, **overrides):
    settings = dict(
        max_blocks=1, market_fee=0.02,
        delay_buffer_cad_per_share=0.03,
        min_advantage_cad=100,
        max_fallback_loss_cad_per_block=2500,
    )
    settings.update(overrides)
    return propose_conversion(state, **settings)


class ManualConverterTests(unittest.TestCase):
    def setUp(self):
        self.originals = (
            strategy.STATE, strategy.MANUAL_CONVERTER_ENABLED,
            strategy.CONVERTER_WINDOW,
            strategy.CONVERTER_SUPPRESSED_POSITIONS,
            strategy.MAX_GROSS,
        )
        strategy.STATE = converter_market()
        strategy.MAX_GROSS = 50_000
        strategy.MANUAL_CONVERTER_ENABLED = True
        strategy.CONVERTER_WINDOW = None
        strategy.CONVERTER_SUPPRESSED_POSITIONS = None

    def tearDown(self):
        (strategy.STATE, strategy.MANUAL_CONVERTER_ENABLED,
         strategy.CONVERTER_WINDOW,
         strategy.CONVERTER_SUPPRESSED_POSITIONS,
         strategy.MAX_GROSS) = self.originals

    def test_disabled_by_default_and_bounded_market_comparison(self):
        self.assertIsNotNone(proposal(strategy.STATE))
        self.assertIsNone(proposal(
            strategy.STATE, max_fallback_loss_cad_per_block=1000,
        ))
        strategy.MANUAL_CONVERTER_ENABLED = False
        self.assertFalse(strategy.start_manual_converter_if_better())
        self.assertIsNone(strategy.CONVERTER_WINDOW)

    def test_does_not_pay_converter_fee_without_reduction_pressure(self):
        strategy.MAX_GROSS = 300_000
        self.assertFalse(strategy.start_manual_converter_if_better())
        self.assertIsNone(strategy.CONVERTER_WINDOW)

    def test_manual_creation_confirms_position_change_and_closes_lot(self):
        opening = strategy.STATE.add_bundle(TradeBundle(
            bundle_id="opening", reason="ETF_ARB_SELL_ETF", created_tick=20,
            expected_profit_cad=500, max_unhedged_ticks=2,
            quantity=10_000, open_quantity=10_000, status="FILLED",
        ))
        with redirect_stdout(io.StringIO()):
            self.assertTrue(strategy.start_manual_converter_if_better())
        window = strategy.CONVERTER_WINDOW
        self.assertEqual(window.proposal.action, "CREATE")
        self.assertEqual(converted_blocks(window, strategy.STATE.positions), 0)
        strategy.STATE.positions.update({
            "BULL": 0, "BEAR": 0, "RITC": 0,
        })
        with redirect_stdout(io.StringIO()):
            blocked, fallback = strategy.progress_manual_converter(
                now=window.deadline_at - 1,
            )
        self.assertTrue(blocked)
        self.assertIsNone(fallback)
        self.assertIsNone(strategy.CONVERTER_WINDOW)
        self.assertEqual(opening.open_quantity, 0)

    def test_timeout_uses_market_fallback_once(self):
        with redirect_stdout(io.StringIO()):
            self.assertTrue(strategy.start_manual_converter_if_better())
        window = strategy.CONVERTER_WINDOW
        with redirect_stdout(io.StringIO()):
            blocked, fallback = strategy.progress_manual_converter(
                now=window.deadline_at + 0.1,
            )
        self.assertTrue(blocked)
        self.assertEqual(fallback.quantity, 10_000)
        self.assertEqual(fallback.reason, "CONVERTER_FALLBACK_BUY_ETF")
        self.assertIsNone(strategy.CONVERTER_WINDOW)
        self.assertFalse(strategy.start_manual_converter_if_better())

    def test_unexpected_position_change_aborts_without_duplicate_orders(self):
        with redirect_stdout(io.StringIO()):
            self.assertTrue(strategy.start_manual_converter_if_better())
        strategy.STATE.positions["BULL"] -= 10_000
        with redirect_stdout(io.StringIO()):
            blocked, fallback = strategy.progress_manual_converter()
        self.assertTrue(blocked)
        self.assertIsNone(fallback)
        self.assertIsNone(strategy.CONVERTER_WINDOW)

    def test_partial_manual_conversion_falls_back_only_for_remainder(self):
        strategy.STATE = converter_market({
            "BULL": 20_000, "BEAR": 20_000, "RITC": -20_000,
        })
        with patch.object(strategy, "MANUAL_CONVERTER_MAX_BLOCKS", 2):
            with redirect_stdout(io.StringIO()):
                self.assertTrue(strategy.start_manual_converter_if_better())
        window = strategy.CONVERTER_WINDOW
        self.assertEqual(window.proposal.blocks, 2)
        strategy.STATE.positions.update({
            "BULL": 10_000, "BEAR": 10_000, "RITC": -10_000,
        })
        with redirect_stdout(io.StringIO()):
            waiting, no_fallback = strategy.progress_manual_converter(
                now=window.deadline_at - 1,
            )
            timed_out, fallback = strategy.progress_manual_converter(
                now=window.deadline_at + 1,
            )
        self.assertTrue(waiting)
        self.assertIsNone(no_fallback)
        self.assertTrue(timed_out)
        self.assertEqual(fallback.quantity, 10_000)

    def test_expired_window_does_not_force_loss_beyond_bound(self):
        with redirect_stdout(io.StringIO()):
            self.assertTrue(strategy.start_manual_converter_if_better())
        window = strategy.CONVERTER_WINDOW
        strategy.STATE.record_book(
            "RITC", ((26.48, 100_000),), ((26.50, 100_000),)
        )
        with redirect_stdout(io.StringIO()):
            blocked, fallback = strategy.progress_manual_converter(
                now=window.deadline_at + 1,
            )
        self.assertTrue(blocked)
        self.assertIsNone(fallback)

    def test_strategy_holds_new_orders_during_manual_window(self):
        state = strategy.STATE
        with (patch.object(strategy, "load_risk_limits"),
              patch.object(strategy, "get_order_book",
                           side_effect=lambda _client, ticker:
                           state.current_books[ticker]),
              patch.object(strategy, "positions_map",
                           side_effect=lambda _client: dict(state.positions)),
              patch.object(strategy, "accept_active_tender_offers",
                           return_value=False),
              patch.object(strategy, "fulfill_intents", return_value=False),
              patch.object(strategy, "enqueue_arb") as enqueue):
            with redirect_stdout(io.StringIO()):
                strategy.step_once(None)
                self.assertIsNotNone(strategy.CONVERTER_WINDOW)
                strategy.STATE.update_case(101, "ACTIVE")
                strategy.step_once(None)
            enqueue.assert_not_called()
            self.assertEqual(len(strategy.STATE.active_intents()), 0)

    def test_redemption_delta_is_distinct_from_creation(self):
        state = converter_market({
            "BULL": -10_000, "BEAR": -10_000, "RITC": 10_000,
        })
        window = start_window(
            ConverterProposal(
                action="REDEEM", direction="SELL_ETF", blocks=1,
                market_close_cad=-2_000, converter_cost_cad=1_800,
                advantage_cad=200,
            ),
            state.positions, 5, now=0,
        )
        state.positions.update({"BULL": 0, "BEAR": 0, "RITC": 0})
        self.assertEqual(converted_blocks(window, state.positions), 1)
        opening = state.add_bundle(TradeBundle(
            bundle_id="short-basket", reason="ETF_ARB_BUY_ETF",
            created_tick=1, expected_profit_cad=500,
            max_unhedged_ticks=2, quantity=10_000,
            open_quantity=10_000, status="FILLED",
        ))
        self.assertEqual(apply_conversion_to_open_lots(state, "REDEEM", 10_000),
                         10_000)
        self.assertEqual(opening.open_quantity, 0)


if __name__ == "__main__":
    unittest.main()
