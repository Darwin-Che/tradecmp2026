"""Safety regression tests for status, news timing, resets, and tradeability."""

import contextlib
import io
import unittest

from case1_strategy_v1_2_1 import (
    BASELINE_WEEKLY_VOL_RANGES,
    best_robust_straddle,
    build_straddles,
    build_weekly_vol_ranges,
    display_snapshot,
    reset_detected,
)


ACTUAL_BODY = "The analysts have informed you that the realized volatility of RTM this week will be 28%"
FORECAST_BODY = "The analysts have informed you that the realized volatility of RTM next week will be between 28% and 33%"
INITIAL_BODY = (
    "The current risk free rate is 0%. RTM is an ETF and its current "
    "annualized realized volatility is 14%."
)


def option_row(ticker, tradeable=True):
    return {
        "ticker": ticker, "bid": 1.0, "ask": 1.1,
        "fair_low": 1.5, "fair_mid": 1.6, "fair_high": 1.7,
        "stress_low": 1.5, "stress_high": 1.8,
        "delta": 0.4 if ticker.endswith("C") else -0.5,
        "multiplier": 100, "position": 0, "tradeable": tradeable,
    }


class SafetyCorrectionTests(unittest.TestCase):
    def test_stopped_case_produces_no_valuation_or_recommendation(self):
        stale_securities = [
            {"ticker": "RTM", "bid": 48.9, "ask": 49.0, "position": 0},
            {"ticker": "RTM49C", "bid": 1.0, "ask": 1.1, "position": 0},
            {"ticker": "RTM49P", "bid": 1.0, "ask": 1.1, "position": 0},
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            display_snapshot(
                {"tick": 0, "period": 1, "status": "STOPPED"},
                stale_securities,
                [{"body": ACTUAL_BODY, "tick": 0}],
            )
        text = output.getvalue()
        self.assertIn("CASE NOT ACTIVE", text)
        for forbidden in ("effective_vol_low", "STRONG BUY", "STRONG SELL",
                          "BEST ROBUST STRADDLE", "SIMULATED ORDERS"):
            self.assertNotIn(forbidden, text)

    def test_tick_zero_ignores_all_future_news(self):
        news = [
            {"body": ACTUAL_BODY, "tick": tick, "news_id": index}
            for index, tick in enumerate((75, 112, 150, 225), 1)
        ]
        ranges, sources, warnings = build_weekly_vol_ranges(news, 1, 0)
        self.assertEqual(ranges, BASELINE_WEEKLY_VOL_RANGES)
        self.assertTrue(all(source == "BASELINE" for source in sources.values()))
        self.assertEqual(warnings, [])

    def test_tick_157_uses_only_news_through_157(self):
        news = [
            {"headline": "Risk free rate and current annualized volatility of RTM",
             "body": INITIAL_BODY, "tick": 0, "period": 1, "news_id": 1},
            {"body": ACTUAL_BODY, "tick": 75, "period": 1, "news_id": 2},
            {"body": FORECAST_BODY, "tick": 112, "period": 1, "news_id": 3},
            {"body": ACTUAL_BODY.replace("28%", "31%"),
             "tick": 150, "period": 1, "news_id": 4},
            {"body": ACTUAL_BODY.replace("28%", "39%"),
             "tick": 225, "period": 1, "news_id": 5},
        ]
        ranges, sources, _ = build_weekly_vol_ranges(news, 1, 157)
        self.assertEqual(ranges, {
            0: (0.14, 0.14), 1: (0.28, 0.28),
            2: (0.31, 0.31), 3: (0.25, 0.25),
        })
        self.assertEqual(sources[2], "ACTUAL")
        self.assertEqual(sources[3], "BASELINE")

    def test_active_case_resumes_normal_valuation(self):
        securities = [
            {"ticker": "RTM", "bid": 48.90, "ask": 48.92,
             "last": 48.91, "position": 0, "is_tradeable": True},
            {"ticker": "RTM49C", "bid": 1.0, "ask": 1.1,
             "position": 0, "size": 100, "is_tradeable": True},
            {"ticker": "RTM49P", "bid": 1.0, "ask": 1.1,
             "position": 0, "size": 100, "is_tradeable": True},
        ]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            display_snapshot(
                {"tick": 1, "period": 1, "status": "active"},
                securities, [],
            )
        text = output.getvalue()
        self.assertIn("effective_vol_low", text)
        self.assertIn("RTM49C", text)
        self.assertNotIn("CASE NOT ACTIVE", text)

    def test_nontradeable_leg_cannot_be_recommended(self):
        rows = [option_row("RTM49C", tradeable=False), option_row("RTM49P")]
        straddles, _ = build_straddles(rows, 48.91)
        self.assertEqual(len(straddles), 1)
        self.assertFalse(straddles[0]["tradeable"])
        self.assertIsNone(best_robust_straddle(straddles))

    def test_reset_detection(self):
        self.assertTrue(reset_detected(1, 200, {"period": 2, "tick": 0}))
        self.assertTrue(reset_detected(1, 200, {"period": 1, "tick": 10}))
        self.assertFalse(reset_detected(1, 10, {"period": 1, "tick": 11}))


if __name__ == "__main__":
    unittest.main()
