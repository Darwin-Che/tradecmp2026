"""Print the complete RTM security record from the RIT DMA API."""

import base64
import json
import signal
from time import monotonic, sleep

import requests


API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16655/v1"
USERNAME = "goal-1"
PASSWORD = "credit"
AUTHORIZATION = {
    "Authorization": "Basic "
    + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
}

RTM_TICKER = "RTM"
OUTPUT_INTERVAL = 5.0
LOOP_SLEEP = 0.5

shutdown = False


class ApiException(Exception):
    pass


def signal_handler(signum, frame):
    """Request a graceful shutdown when Ctrl+C is pressed."""
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True


def handle_rate_limit(response):
    """Wait for the server-requested interval after HTTP 429."""
    if response.status_code != 429:
        return False

    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        try:
            retry_after = response.json().get("wait", 1)
        except ValueError:
            retry_after = 1

    wait_time = float(retry_after)
    print(f"Rate limit exceeded. Waiting {wait_time} seconds.")
    sleep(wait_time)
    return True


def api_get(session, endpoint, params=None):
    """Send a read-only API request with rate-limit and auth handling."""
    global shutdown

    while not shutdown:
        response = session.get(
            f"{API_ENDPOINT}/{endpoint}",
            params=params,
            timeout=5,
        )
        if response.status_code == 401:
            print("Authentication failed. Check USERNAME and PASSWORD.")
            shutdown = True
            return None
        if handle_rate_limit(response):
            continue
        if response.ok:
            try:
                return response.json()
            except ValueError as exc:
                raise ApiException("API returned invalid JSON") from exc
        raise ApiException(f"API request failed: {response.text}")

    return None


def get_tick_status(session):
    """Return the current simulation tick and status."""
    case = api_get(session, "case")
    if case is None:
        return None, None
    return case.get("tick"), case.get("status")


def get_rtm_details(session):
    """Return the complete RTM record from the /securities response."""
    securities = api_get(session, "securities")
    if securities is None:
        return None

    for security in securities:
        if security.get("ticker") == RTM_TICKER:
            return security

    raise ApiException("RTM was not present in the /securities response")


def get_rtm_order_book(session):
    """Return all RTM bid and ask levels from the order-book endpoint."""
    order_book = api_get(
        session,
        "securities/book",
        params={"ticker": RTM_TICKER},
    )
    if order_book is None:
        return None
    return {
        "bids": order_book.get("bids", []),
        "asks": order_book.get("asks", []),
    }


def main():
    with requests.Session() as session:
        session.headers.update(AUTHORIZATION)
        last_output = monotonic() - OUTPUT_INTERVAL
        last_status = None

        while not shutdown:
            try:
                tick, status = get_tick_status(session)
                if shutdown:
                    break

                if status != last_status:
                    if status == "ACTIVE":
                        print(f"Case is ACTIVE at tick {tick}; monitoring RTM.")
                    else:
                        print(
                            f"Case status is {status!r}; waiting for ACTIVE "
                            "(Ctrl+C to stop)."
                        )
                    last_status = status

                if status != "ACTIVE":
                    sleep(1)
                    continue

                now = monotonic()
                if now - last_output >= OUTPUT_INTERVAL:
                    details = get_rtm_details(session)
                    order_book = get_rtm_order_book(session)
                    if details is not None and order_book is not None:
                        print(f"tick={tick} RTM details:")
                        print(
                            json.dumps(details, indent=2, sort_keys=True),
                        )
                        print("RTM order book:")
                        print(
                            json.dumps(order_book, indent=2, sort_keys=True),
                            flush=True,
                        )
                    last_output = now

                sleep(LOOP_SLEEP)
            except (ApiException, requests.RequestException) as exc:
                print(f"API error: {exc}")
                sleep(1)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    main()
