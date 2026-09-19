"""Pure tests for straddle calculations, ranking, stress, and hedge sizing."""

import unittest

from case1_strategy_v1_2 import (
    best_robust_straddle,
    build_straddles,
    calculate_stress_volatilities,
    robust_shortlist,
    simulated_recommendation,
)


def option_row(ticker, bid, ask, fair, delta, stress_low=None):
    return {
        "ticker": ticker,
        "bid": bid,
        "ask": ask,
        "fair_low": fair,
        "fair_mid": fair,
        "fair_high": fair,
        "stress_low": fair if stress_low is None else stress_low,
        "stress_high": fair,
        "delta": delta,
        "multiplier": 100,
        "position": 0,
    }


class StraddleTests(unittest.TestCase):
    def test_observed_49_straddle(self):
        rows = [
            option_row("RTM49C", 0.60, 0.63, 1.0196, 0.4588),
            option_row("RTM49P", 1.00, 1.04, 1.4296, -0.5412),
        ]
        straddles, warnings = build_straddles(rows, 48.59)
        self.assertEqual(warnings, [])
        self.assertEqual(len(straddles), 1)
        result = straddles[0]
        self.assertAlmostEqual(result["straddle_ask"], 1.67)
        self.assertAlmostEqual(result["fair_mid"], 2.4492)
        self.assertAlmostEqual(result["net_edge_per_straddle"], 73.92)
        self.assertAlmostEqual(result["delta_shares_per_straddle"], -8.24)
        self.assertEqual(result["initial_rtm_hedge"], 8)
        self.assertAlmostEqual(result["initial_rtm_hedge_fee"], 0.16)

    def test_actual_portfolio_delta_changes_simulated_hedge(self):
        rows = [
            option_row("RTM49C", 0.60, 0.63, 1.0196, 0.4588),
            option_row("RTM49P", 1.00, 1.04, 1.4296, -0.5412),
        ]
        straddle = build_straddles(rows, 48.59)[0][0]
        recommendation = simulated_recommendation(straddle, 10, 25.0)
        self.assertAlmostEqual(recommendation["projected_delta_before_hedge"], -57.4)
        self.assertEqual(recommendation["rtm_trade_quantity"], 57)
        self.assertAlmostEqual(recommendation["projected_delta_after_hedge"], -0.4)
        self.assertAlmostEqual(recommendation["total_option_premium"], 1670.0)
        self.assertAlmostEqual(recommendation["total_option_commission"], 40.0)

    def test_positive_stress_edge_drives_ranking(self):
        near = build_straddles([
            option_row("RTM49C", 0.60, 0.63, 1.0, 0.45, 0.90),
            option_row("RTM49P", 1.00, 1.04, 1.4, -0.55, 1.00),
        ], 48.59)[0][0]
        far = build_straddles([
            option_row("RTM55C", 0.20, 0.25, 0.5, 0.20, 1.00),
            option_row("RTM55P", 6.50, 6.60, 6.7, -0.80, 6.20),
        ], 48.59)[0][0]
        self.assertIs(best_robust_straddle([near, far]), far)

    def test_no_positive_stress_edge_has_no_robust_trade(self):
        rows = [
            option_row("RTM49C", 0.60, 0.63, 0.50, 0.45, 0.40),
            option_row("RTM49P", 1.00, 1.04, 0.80, -0.55, 0.70),
        ]
        self.assertIsNone(best_robust_straddle(build_straddles(rows, 48.59)[0]))

    def test_95_percent_shortlist_prefers_near_low_delta_strike(self):
        strike_48 = {
            "strike": 48.0,
            "stress_low_net_edge": 59.2371,
            "distance_from_spot": 0.91,
            "delta_shares_per_straddle": 22.3830,
            "edge_return_on_premium": 0.40,
            "net_edge_per_straddle": 70.0,
            "combined_spread": 0.10,
        }
        strike_49 = {
            "strike": 49.0,
            "stress_low_net_edge": 58.7732,
            "distance_from_spot": 0.09,
            "delta_shares_per_straddle": 1.1210,
            "edge_return_on_premium": 0.45,
            "net_edge_per_straddle": 75.0,
            "combined_spread": 0.08,
        }
        shortlist, max_edge, cutoff = robust_shortlist([strike_48, strike_49])
        self.assertAlmostEqual(max_edge, 59.2371)
        self.assertAlmostEqual(cutoff, 0.95 * 59.2371)
        self.assertEqual({row["strike"] for row in shortlist}, {48.0, 49.0})
        self.assertEqual(shortlist[0]["strike"], 49.0)
        self.assertEqual(best_robust_straddle([strike_48, strike_49])["strike"], 49.0)

    def test_only_future_baseline_weeks_are_stressed(self):
        ranges = {0: (0.14, 0.14), 1: (0.28, 0.28),
                  2: (0.28, 0.33), 3: (0.25, 0.25)}
        sources = {0: "ACTUAL", 1: "ACTUAL", 2: "FORECAST", 3: "BASELINE"}
        low, high, weeks = calculate_stress_volatilities(110, ranges, sources)
        self.assertEqual(weeks, [3])
        self.assertLess(low, high)


if __name__ == "__main__":
    unittest.main()
