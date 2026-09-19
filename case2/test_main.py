"""Driver transition tests without a live simulator."""

import sys
import unittest
from types import ModuleType
from unittest.mock import patch

try:
    import requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = ModuleType("requests")

import main
import strategy as live_strategy


class FakeState:
    def __init__(self):
        self.updates = []

    def update_case(self, tick, status):
        self.updates.append((tick, status))

    def print_state(self, detail):
        pass


class FakeStrategy:
    EXECUTION_POLICY = "market"

    def __init__(self, statuses, stop_after_steps):
        self.statuses = iter(statuses)
        self.stop_after_steps = stop_after_steps
        self.STATE = FakeState()
        self.steps = []
        self.resets = 0

    def get_tick_status(self, client):
        return next(self.statuses)

    def step_once(self, client):
        self.steps.append(self.STATE.updates[-1])
        if len(self.steps) >= self.stop_after_steps:
            main.shutdown_requested = True

    def reset_for_new_heat(self):
        self.resets += 1
        self.STATE = FakeState()


class DriverTests(unittest.TestCase):
    def tearDown(self):
        main.shutdown_requested = False

    def test_waits_before_and_between_heats(self):
        strategy = FakeStrategy(
            [(0, "STOPPED"), (0, "STOPPED"),
             (1, "ACTIVE"), (2, "ACTIVE"),
             (300, "STOPPED"), (1, "ACTIVE")],
            stop_after_steps=3,
        )
        with patch.object(main, "sleep") as sleeper:
            main.run(None, strategy_module=strategy,
                     loop_sleep=0.5, idle_sleep=1.0)
        self.assertEqual(
            strategy.steps,
            [(1, "ACTIVE"), (2, "ACTIVE"), (1, "ACTIVE")],
        )
        self.assertEqual(strategy.resets, 1)
        self.assertEqual(sleeper.call_count, 6)

    def test_tick_reset_starts_new_heat_if_stop_poll_was_missed(self):
        strategy = FakeStrategy(
            [(299, "ACTIVE"), (0, "ACTIVE")], stop_after_steps=2,
        )
        with patch.object(main, "sleep"):
            main.run(None, strategy_module=strategy,
                     loop_sleep=0, idle_sleep=0)
        self.assertEqual(strategy.resets, 1)
        self.assertEqual(strategy.steps, [(299, "ACTIVE"), (0, "ACTIVE")])

    def test_new_heat_clears_tender_ids_and_reloads_limits(self):
        original = (
            live_strategy.STATE,
            set(live_strategy.ACCEPTED_TENDER_IDS),
            live_strategy.RISK_LIMITS_LOADED,
            live_strategy.MAX_GROSS,
            live_strategy.MAX_LONG_NET,
            live_strategy.MAX_SHORT_NET,
        )
        try:
            live_strategy.STATE.positions["RITC"] = 123
            live_strategy.ACCEPTED_TENDER_IDS.add(77)
            live_strategy.RISK_LIMITS_LOADED = True
            live_strategy.MAX_GROSS = 111
            live_strategy.reset_for_new_heat()
            self.assertEqual(live_strategy.STATE.positions["RITC"], 0)
            self.assertFalse(live_strategy.ACCEPTED_TENDER_IDS)
            self.assertFalse(live_strategy.RISK_LIMITS_LOADED)
            self.assertEqual(
                live_strategy.MAX_GROSS, live_strategy.DEFAULT_MAX_GROSS,
            )
        finally:
            (
                live_strategy.STATE,
                ids,
                live_strategy.RISK_LIMITS_LOADED,
                live_strategy.MAX_GROSS,
                live_strategy.MAX_LONG_NET,
                live_strategy.MAX_SHORT_NET,
            ) = original
            live_strategy.ACCEPTED_TENDER_IDS.clear()
            live_strategy.ACCEPTED_TENDER_IDS.update(ids)


if __name__ == "__main__":
    unittest.main()
