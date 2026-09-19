"""Pure unit tests for news parsing and volatility-range construction."""

import math
import unittest

from case1_strategy_v1 import effective_volatilities
from case1_strategy_v1_1 import build_weekly_vol_ranges, parse_volatility_range


INITIAL_BODY = (
    "The current risk free rate is 0%. RTM is an ETF that mimics one of the "
    "major indices in the simulated world and its current annualized realized "
    "volatility is 14%. This simulation consists of 20 trading days that are "
    "each 15 ticks in length."
)
ACTUAL_BODY = "The analysts have informed you that the realized volatility of RTM this week will be 28%"
FORECAST_BODY = "The analysts have informed you that the realized volatility of RTM next week will be between 28% and 33%"


class NewsVolatilityTests(unittest.TestCase):
    def test_exact_live_news_bodies(self):
        self.assertEqual(parse_volatility_range(INITIAL_BODY), (0.14, 0.14))
        self.assertEqual(parse_volatility_range(ACTUAL_BODY), (0.28, 0.28))
        self.assertEqual(parse_volatility_range(FORECAST_BODY), (0.28, 0.33))

    def test_risk_free_rate_is_not_parsed(self):
        self.assertNotEqual(parse_volatility_range(INITIAL_BODY), (0.0, 0.0))
        self.assertIsNone(parse_volatility_range("The current risk free rate is 0%."))

    def test_tick_110_ranges_and_effective_volatility(self):
        news = [
            {"headline": "Risk free rate and current annualized volatility of RTM",
             "body": INITIAL_BODY, "period": 1, "news_id": 1},
            {"headline": "Announcement 1", "body": ACTUAL_BODY,
             "tick": 75, "period": 1, "news_id": 2},
            {"headline": "News 1", "body": FORECAST_BODY,
             "tick": 110, "period": 1, "news_id": 3},
        ]
        ranges, sources, warnings = build_weekly_vol_ranges(news, 1)
        self.assertEqual(ranges, {
            0: (0.14, 0.14), 1: (0.28, 0.28),
            2: (0.28, 0.33), 3: (0.25, 0.25),
        })
        self.assertEqual(sources, {
            0: "ACTUAL", 1: "ACTUAL", 2: "FORECAST", 3: "BASELINE",
        })
        self.assertEqual(warnings, [])
        low, mid, high = effective_volatilities(110, ranges)
        self.assertTrue(math.isclose(low, 0.2686, abs_tol=0.00005))
        self.assertTrue(math.isclose(mid, 0.2791, abs_tol=0.00005))
        self.assertTrue(math.isclose(high, 0.2901, abs_tol=0.00005))

    def test_actual_overrides_forecast_for_same_week(self):
        news = [
            {"body": FORECAST_BODY, "tick": 100, "news_id": 1},
            {"body": ACTUAL_BODY.replace("28%", "31%"),
             "tick": 150, "news_id": 2},
        ]
        ranges, sources, _ = build_weekly_vol_ranges(news, 1)
        self.assertEqual(ranges[2], (0.31, 0.31))
        self.assertEqual(sources[2], "ACTUAL")

    def test_period_filter_and_invalid_noninitial_tick(self):
        news = [
            {"body": ACTUAL_BODY, "tick": 75, "period": 2, "news_id": 1},
            {"body": ACTUAL_BODY, "period": 1, "news_id": 2},
        ]
        ranges, sources, warnings = build_weekly_vol_ranges(news, 1)
        self.assertEqual(ranges, {week: (0.25, 0.25) for week in range(4)})
        self.assertTrue(all(source == "BASELINE" for source in sources.values()))
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
