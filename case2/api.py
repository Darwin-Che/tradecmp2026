"""Small DMA REST API wrapper for the RIT Market Simulator."""

import base64
from time import sleep

import requests


class ApiException(Exception):
    """Raised when the RIT server rejects an API request."""


class RITClient:
    """Authenticated, rate-limit-aware client for the endpoints used here."""

    def __init__(self, base_url, username, password, timeout=5.0, session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.session.headers.update({"Authorization": f"Basic {token}"})

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        self.session.close()

    @staticmethod
    def _rate_limit_wait(response):
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return float(response.headers.get("Retry-After", payload.get("wait", 1)))

    def request(self, method, endpoint, params=None):
        """Send one request, retrying automatically after HTTP 429 responses."""
        method = method.upper()
        if method not in ("GET", "POST", "DELETE"):
            raise ValueError(f"Unsupported HTTP method: {method}")

        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        while True:
            response = self.session.request(
                method,
                url,
                params=params,
                timeout=self.timeout,
            )
            if response.status_code == 429:
                wait_time = self._rate_limit_wait(response)
                print(f"Rate limit exceeded. Retrying in {wait_time} seconds.")
                sleep(wait_time)
                continue
            if response.status_code == 401:
                raise ApiException(
                    "Authentication failed. Check the trader username and password."
                )
            if not response.ok:
                raise ApiException(
                    f"{method} {endpoint} failed with HTTP "
                    f"{response.status_code}: {response.text}"
                )
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    def get_case(self):
        return self.request("GET", "case")

    def get_securities(self):
        return self.request("GET", "securities")

    def get_order_book(self, ticker):
        return self.request("GET", "securities/book", {"ticker": ticker})

    def get_tenders(self):
        return self.request("GET", "tenders")

    def get_limits(self):
        return self.request("GET", "limits")

    def get_order(self, order_id):
        return self.request("GET", f"orders/{int(order_id)}")

    def cancel_order(self, order_id):
        return self.request("DELETE", f"orders/{int(order_id)}")

    def accept_tender(self, tender_id, price=None):
        if price is None:
            raise ValueError("A tender acceptance price is required by RIT API v1.0.4+")
        return self.request(
            "POST",
            f"tenders/{tender_id}",
            {"price": float(price)},
        )

    def place_order(
        self,
        ticker,
        action,
        quantity,
        order_type="MARKET",
        price=None,
    ):
        params = {
            "ticker": ticker,
            "type": order_type,
            "quantity": int(quantity),
            "action": action,
        }
        if price is not None:
            params["price"] = price
        return self.request("POST", "orders", params)
