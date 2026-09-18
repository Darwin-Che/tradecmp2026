"""Entry point and polling driver for the ETF arbitrage strategy."""

import os
import signal
import sys
from time import sleep

import _example as strategy
from api import ApiException, RITClient
from dashboard import TerminalDashboard


API_ENDPOINT = os.getenv(
    "RIT_API_ENDPOINT",
    "http://flserver.rotman.utoronto.ca:16635/v1",
)
USERNAME = os.getenv("RIT_USERNAME", "goal")
PASSWORD = os.getenv("RIT_PASSWORD", "credit")
LOOP_SLEEP = float(os.getenv("RIT_LOOP_SLEEP", "0.5"))
PRINT_DETAIL = os.getenv("RIT_PRINT_DETAIL", "compact")

shutdown_requested = False


def signal_handler(signum, frame):
    """Request a clean stop after the current API operation completes."""
    global shutdown_requested
    shutdown_requested = True


def run(client, strategy_module=strategy, loop_sleep=LOOP_SLEEP,
        print_detail=PRINT_DETAIL, dashboard=None):
    """Poll the case and invoke one strategy step while trading is active."""
    def display_state():
        if dashboard is None:
            strategy_module.STATE.print_state(print_detail)
        else:
            dashboard.refresh()

    tick, status = strategy_module.get_tick_status(client)
    strategy_module.STATE.update_case(tick, status)
    display_state()

    while status == "ACTIVE" and not shutdown_requested:
        try:
            strategy_module.step_once(client)
            display_state()
            sleep(loop_sleep)
            tick, status = strategy_module.get_tick_status(client)
            strategy_module.STATE.update_case(tick, status)
        except ApiException as exc:
            print(f"API error: {exc}", file=sys.stderr)
            sleep(1)
    display_state()


def main():
    signal.signal(signal.SIGINT, signal_handler)
    with RITClient(API_ENDPOINT, USERNAME, PASSWORD) as client:
        dashboard = TerminalDashboard(strategy.STATE, plain_detail=PRINT_DETAIL)
        with dashboard.capture_output():
            dashboard.start()
            try:
                run(client, dashboard=dashboard)
            except BaseException:
                dashboard.stop(clear=True)
                raise
            else:
                dashboard.stop()


if __name__ == "__main__":
    main()
