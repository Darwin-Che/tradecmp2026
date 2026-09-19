"""The risk log components must match the strategy's liquidation mark."""

import unittest

from state import TradingState
from valuation import liquidation_components_cad, liquidation_value_cad


class ValuationTests(unittest.TestCase):
    def test_components_add_up_to_liquidation_value(self):
        state = TradingState()
        for ticker, bid, ask in (
            ("BULL", 10.0, 10.1),
            ("BEAR", 15.0, 15.1),
            ("RITC", 25.0, 25.1),
            ("USD", 1.08, 1.09),
        ):
            state.record_book(ticker, ((bid, 100),), ((ask, 100),))
        positions = {"BULL": 10, "BEAR": -5, "RITC": 2,
                     "USD": -10, "CAD": 100}
        components = liquidation_components_cad(positions, state.current_books)
        self.assertAlmostEqual(components["total"], liquidation_value_cad(
            positions, state.current_books))
        self.assertAlmostEqual(components["bull"], 100)
        self.assertAlmostEqual(components["bear"], -75.5)
        self.assertAlmostEqual(components["usd_block"], 40 * 1.08)


if __name__ == "__main__":
    unittest.main()
