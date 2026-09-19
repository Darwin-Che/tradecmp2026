"""Read-only RITCx volatility decision support. Dependencies: requests, py_vollib.

The DMA connection settings are configured below. Credentials and request
headers are never printed or logged.
"""

# MANUAL VOLATILITY CONFIGURATION (annualized decimals, e.g. 0.25 = 25%).
# Week 0: ticks 0–74; week 1: 75–149; week 2: 150–224; week 3: 225–299.
# Read the displayed raw news, edit these ranges, then restart this program.
# News is deliberately NOT parsed into the valuation model.
WEEKLY_VOL_RANGES = {
    0: (0.25, 0.25),
    1: (0.25, 0.25),
    2: (0.25, 0.25),
    3: (0.25, 0.25),
}

import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic, sleep

import requests
from requests.auth import HTTPBasicAuth
from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta

# DMA API CONNECTION CONFIGURATION
API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16595/v1"  # Volatility Trading case
USERNAME = "goal-2"
PASSWORD = "credit"

EXPIRY_TICK = 300
TICKS_PER_YEAR = 3600
MIN_NET_EDGE = 0.05
COMMISSION_PER_CONTRACT = 2.0
POLL_SECONDS = 1.0
REQUEST_TIMEOUT = (3.0, 5.0)
OPTION_PATTERN = re.compile(r"^RTM(?P<strike>\d+(?:\.\d+)?)(?P<kind>[CP])$")
BANNER = "DRY_RUN — read-only decision support; no orders or hedges are submitted."


class ApiError(Exception):
    """A sanitized API failure, safe to display."""


class AuthenticationError(ApiError):
    pass


def number(value):
    """Reject missing, Boolean, nonnumeric and infinite API values."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def positive(value):
    result = number(value)
    return result if result is not None and result > 0 else None


def rate_limit_delay(response):
    """Honor Retry-After seconds/date, or the DMA JSON wait field (seconds)."""
    header = response.headers.get("Retry-After")
    delay = number(header)
    if header is not None and delay is None:
        try:
            date = parsedate_to_datetime(header)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            pass
    if delay is None:
        try:
            body = response.json()
            delay = number(body.get("wait")) if isinstance(body, dict) else None
        except ValueError:
            pass
    return max(1.0, delay) if delay is not None else 1.0


def api_get(session, base_url, resource):
    """Only the three read endpoints are allowed; redirects are disabled."""
    if resource not in {"case", "securities", "news"}:
        raise ValueError("Unsupported read endpoint.")
    for attempt in range(3):
        try:
            response = session.get(f"{base_url}/{resource}",
                                   timeout=REQUEST_TIMEOUT, allow_redirects=False)
        except requests.Timeout:
            raise ApiError(f"GET /{resource} timed out.") from None
        except requests.RequestException:
            raise ApiError(f"GET /{resource} connection failed.") from None
        if response.status_code == 401:
            raise AuthenticationError("HTTP 401: check USERNAME and PASSWORD.")
        if response.status_code == 429:
            delay = rate_limit_delay(response)
            print(f"HTTP 429: waiting {delay:g} seconds.", flush=True)
            sleep(delay)
            if attempt == 2:
                raise ApiError("Rate limit persists; retrying next polling cycle.")
            continue
        if not 200 <= response.status_code < 300:
            raise ApiError(f"GET /{resource}: HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError:
            raise ApiError(f"GET /{resource} returned invalid JSON.") from None


def effective_volatilities(tick, ranges=None):
    """Average variance, not volatility, over the unelapsed parts of weeks."""
    ranges = WEEKLY_VOL_RANGES if ranges is None else ranges
    tick = number(tick)
    if tick is None or not 0 <= tick < EXPIRY_TICK:
        raise ValueError("Valuation requires 0 <= tick < 300.")
    variances = [0.0, 0.0, 0.0]
    for week in range(4):
        remaining = max(0.0, (week + 1) * 75 - max(tick, week * 75))
        if not remaining:
            continue
        low, high = ranges[week]
        low, high = positive(low), positive(high)
        if low is None or high is None or low > high:
            raise ValueError("Weekly volatility ranges must satisfy 0 < low <= high.")
        # The mid scenario uses the arithmetic midpoint of each week's range.
        for index, sigma in enumerate((low, (low + high) / 2, high)):
            variances[index] += sigma ** 2 * remaining
    return tuple(math.sqrt(v / (EXPIRY_TICK - tick)) for v in variances)


def identify_option(ticker):
    match = OPTION_PATTERN.fullmatch(ticker) if isinstance(ticker, str) else None
    if match is None:
        return None
    strike = positive(match.group("strike"))
    return (match.group("kind").lower(), strike) if strike is not None else None


def valid_quotes(security):
    # Zero is treated as an absent quote. A missing side disables only that side.
    bid, ask = positive(security.get("bid")), positive(security.get("ask"))
    if bid is not None and ask is not None and bid > ask:
        return None, None  # A crossed snapshot is not trustworthy.
    return bid, ask


def underlying_spot(rtm):
    bid, ask = valid_quotes(rtm)
    spot = (bid + ask) / 2 if bid is not None and ask is not None else positive(rtm.get("last"))
    if spot is None:
        raise ValueError("RTM has neither a valid midpoint nor a valid last price.")
    return spot


def classify_signal(buy_conservative, buy_mid, sell_conservative, sell_mid):
    for label, edge in (("STRONG BUY", buy_conservative),
                        ("STRONG SELL", sell_conservative),
                        ("WEAK BUY", buy_mid), ("WEAK SELL", sell_mid)):
        if edge is not None and edge >= MIN_NET_EDGE:
            return label
    return "NO TRADE"


def analyze_option(security, spot, years, vols):
    kind, strike = identify_option(security["ticker"])
    # Only a MISSING size gets the fallback; null/zero/invalid sizes are errors.
    multiplier = positive(security.get("size", 100))
    position = number(security.get("position"))
    if multiplier is None or position is None:
        raise ValueError("Invalid size or position; cannot compute reliable exposure.")
    fair_low, fair_mid, fair_high = (
        float(black_scholes(kind, spot, strike, years, 0, vol)) for vol in vols
    )
    delta = float(bs_delta(kind, spot, strike, years, 0, vols[1]))
    if not all(math.isfinite(v) for v in (fair_low, fair_mid, fair_high, delta)):
        raise ValueError("Nonfinite valuation.")
    bid, ask = valid_quotes(security)
    commission = COMMISSION_PER_CONTRACT / multiplier
    buy_conservative = fair_low - ask - commission if ask is not None else None
    buy_mid = fair_mid - ask - commission if ask is not None else None
    sell_conservative = bid - fair_high - commission if bid is not None else None
    sell_mid = bid - fair_mid - commission if bid is not None else None
    return dict(ticker=security["ticker"], bid=bid, ask=ask, position=position,
                multiplier=multiplier, delta=delta, fair_low=fair_low,
                fair_mid=fair_mid, fair_high=fair_high,
                buy_conservative=buy_conservative, buy_mid=buy_mid,
                sell_conservative=sell_conservative, sell_mid=sell_mid,
                signal=classify_signal(buy_conservative, buy_mid, sell_conservative, sell_mid))


def portfolio_summary(rows, rtm_position):
    exposure = sum(row["position"] * row["multiplier"] * row["delta"] for row in rows)
    target = round(-exposure)
    return dict(rtm_position=rtm_position, option_delta_exposure=exposure,
                total_portfolio_delta=rtm_position + exposure,
                target_rtm_position=target, rtm_trade_quantity=target - rtm_position)


def format_value(value):
    if value is None:
        return "N/A"
    return f"{value:.4f}" if isinstance(value, (float, int)) else str(value)


def print_table(rows):
    columns = [("ticker", "ticker"), ("bid", "bid"), ("ask", "ask"),
               ("position", "position"), ("delta", "delta"),
               ("fair_low", "fair_low"), ("fair_mid", "fair_mid"),
               ("fair_high", "fair_high"), ("buy_cons", "buy_conservative"),
               ("sell_cons", "sell_conservative"), ("signal", "signal")]
    rank = {"STRONG BUY": 0, "STRONG SELL": 0, "WEAK BUY": 1, "WEAK SELL": 1, "NO TRADE": 2}
    ordered = sorted(rows, key=lambda row: (rank[row["signal"]], row["ticker"]))
    cells = [[title for title, _ in columns]] + [
        [format_value(row.get(key)) for _, key in columns] for row in ordered]
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    for row in cells:
        print(" | ".join(value.rjust(width) for value, width in zip(row, widths)))
    print("buy_cons / sell_cons: conservative net edges per share; N/A: unusable data.")


def display_snapshot(case, securities, news):
    if not isinstance(case, dict) or not isinstance(securities, list) or not isinstance(news, list):
        raise ValueError("Expected /case object and /securities, /news arrays.")
    print(f"\n{BANNER}\ntick={case.get('tick')}  status={case.get('status')}")
    print("Raw news (manual review; no model updates):")
    for item in news:
        if isinstance(item, dict):
            print(f"Headline: {item.get('headline', '')}\nBody: {item.get('body', '')}")
    if not news:
        print("(none returned)")
    tick = number(case.get("tick"))
    if tick is None or tick < 0:
        raise ValueError("Missing or invalid case tick.")
    if tick >= EXPIRY_TICK:
        print("Expiry reached; valuation and proposed hedge unavailable.")
        return
    if not all(isinstance(s, dict) for s in securities):
        raise ValueError("Invalid security record.")
    rtms = [s for s in securities if s.get("ticker") == "RTM"]
    if len(rtms) != 1:
        raise ValueError("Expected exactly one RTM security.")
    rtm = rtms[0]
    spot = underlying_spot(rtm)
    vols = effective_volatilities(tick)
    print(f"RTM spot={spot:.4f}; effective_vol_low={vols[0]:.6f} "
          f"effective_vol_mid={vols[1]:.6f} effective_vol_high={vols[2]:.6f}")
    rows, errors = [], []
    for security in securities:
        ticker = security.get("ticker")
        if identify_option(ticker) is None:
            if ticker != "RTM" and number(security.get("position")) != 0:
                errors.append(f"Unrecognized security with nonzero/unknown position: {ticker}")
            continue
        try:
            rows.append(analyze_option(security, spot, (EXPIRY_TICK - tick) / TICKS_PER_YEAR, vols))
        except (ValueError, ArithmeticError):
            errors.append(f"{ticker}: invalid contract/position data or valuation.")
            rows.append(dict(ticker=ticker, bid=valid_quotes(security)[0],
                             ask=valid_quotes(security)[1], position=number(security.get("position")),
                             signal="NO TRADE"))
    print_table(rows)
    print("Portfolio summary (RTM and recognized RTM options):")
    rtm_position = number(rtm.get("position"))
    print(f"RTM position: {format_value(rtm_position)}")
    if errors or rtm_position is None:
        for error in errors:
            print(f"Data warning: {error}")
        print("Option delta exposure: N/A\nTotal portfolio delta: N/A\n"
              "Target RTM position: N/A\nProposed RTM trade: unavailable (incomplete data)")
        return
    summary = portfolio_summary(rows, rtm_position)
    print(f"Option delta exposure: {summary['option_delta_exposure']:.4f}\n"
          f"Total portfolio delta: {summary['total_portfolio_delta']:.4f}\n"
          f"Target RTM position: {summary['target_rtm_position']}")
    quantity = summary["rtm_trade_quantity"]
    trade = f"{'BUY' if quantity > 0 else 'SELL'} {abs(quantity):g} RTM" if quantity else "HOLD RTM (0 shares)"
    print(f"Proposed RTM trade: {trade} (display only)", flush=True)


def main():
    print(BANNER, flush=True)
    try:
        with requests.Session() as session:
            session.auth = HTTPBasicAuth(USERNAME, PASSWORD)
            while True:
                started = monotonic()
                try:
                    case = api_get(session, API_ENDPOINT, "case")
                    securities = api_get(session, API_ENDPOINT, "securities")
                    news = api_get(session, API_ENDPOINT, "news")
                    display_snapshot(case, securities, news)
                except AuthenticationError as exc:
                    print(exc)
                    break
                except (ApiError, ValueError, KeyError, TypeError) as exc:
                    # API errors are sanitized; no server bodies or request headers.
                    if isinstance(exc, (ApiError, ValueError)):
                        print(f"Snapshot unavailable: {exc}")
                    else:
                        print("Snapshot unavailable: invalid data/configuration shape.")
                sleep(max(0.0, POLL_SECONDS - (monotonic() - started)))
    except KeyboardInterrupt:
        print("\nDRY_RUN analyzer stopped.")


if __name__ == "__main__":
    main()
