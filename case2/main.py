"""Entry point and polling driver for the ETF arbitrage strategy."""

import os
import signal
import sys
from time import sleep

import strategy
from api import ApiException, RITClient
from dashboard import TerminalDashboard


API_ENDPOINT = os.getenv(
    "RIT_API_ENDPOINT",
    "http://flserver.rotman.utoronto.ca:16635/v1",
)
USERNAME = os.getenv("RIT_USERNAME", "goal")
PASSWORD = os.getenv("RIT_PASSWORD", "credit")
LOOP_SLEEP = float(os.getenv("RIT_LOOP_SLEEP", "0.5"))
IDLE_SLEEP = float(os.getenv("RIT_IDLE_SLEEP", "1.0"))
PRINT_DETAIL = os.getenv("RIT_PRINT_DETAIL", "compact")
if LOOP_SLEEP < 0 or IDLE_SLEEP < 0:
    raise ValueError("RIT_LOOP_SLEEP and RIT_IDLE_SLEEP must be non-negative")

shutdown_requested = False


def signal_handler(signum, frame):
    """Request a clean stop after the current API operation completes."""
    global shutdown_requested
    shutdown_requested = True


def run(client, strategy_module=strategy, loop_sleep=LOOP_SLEEP,
        print_detail=PRINT_DETAIL, dashboard=None, idle_sleep=IDLE_SLEEP):
    """Keep polling through stopped periods and trade each active heat."""
    def display_state():
        if dashboard is None:
            strategy_module.STATE.print_state(print_detail)
        else:
            dashboard.state = strategy_module.STATE
            dashboard.refresh()

    last_status = None
    last_tick = None
    seen_heat = False
    while not shutdown_requested:
        try:
            tick, status = strategy_module.get_tick_status(client)
            new_heat = status == "ACTIVE" and (
                last_status != "ACTIVE"
                or (last_tick is not None and tick is not None and tick < last_tick)
            )
            if new_heat:
                if seen_heat:
                    if strategy_module.EXECUTION_POLICY == "adaptive_limit":
                        from limit_fulfillment import cancel_live_orders

                        cancel_live_orders(client, strategy_module.STATE)
                    strategy_module.reset_for_new_heat()
                seen_heat = True
                print(f"CASE ACTIVE | tick={tick}")
            elif last_status == "ACTIVE" and status != "ACTIVE":
                if strategy_module.EXECUTION_POLICY == "adaptive_limit":
                    from limit_fulfillment import cancel_live_orders

                    cancel_live_orders(client, strategy_module.STATE)
                print(f"CASE {status} | waiting for next heat")
            elif status != last_status:
                print(f"CASE {status} | waiting for ACTIVE")

            strategy_module.STATE.update_case(tick, status)
            if status == "ACTIVE" and not shutdown_requested:
                strategy_module.step_once(client)
                display_state()
                sleep(loop_sleep)
            else:
                if status != last_status:
                    display_state()
                sleep(idle_sleep)
            last_status, last_tick = status, tick
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
            finally:
                if strategy.EXECUTION_POLICY == "adaptive_limit":
                    from limit_fulfillment import cancel_live_orders

                    cancel_live_orders(client, strategy.STATE)


if __name__ == "__main__":
    main()
