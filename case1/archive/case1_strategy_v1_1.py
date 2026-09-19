"""Read-only RITCx volatility analyzer with news-driven volatility ranges.

This version reuses the GET-only transport and option calculations from
case1_strategy_v1. It never submits, modifies, or cancels an order.
"""

import math
import re
from time import monotonic, sleep

import requests
from requests.auth import HTTPBasicAuth

from case1_strategy_v1 import (
    API_ENDPOINT,
    USERNAME,
    PASSWORD,
    BANNER,
    EXPIRY_TICK,
    TICKS_PER_YEAR,
    POLL_SECONDS,
    ApiError,
    AuthenticationError,
    analyze_option,
    api_get,
    effective_volatilities,
    format_value,
    identify_option,
    number,
    portfolio_summary,
    print_table,
    underlying_spot,
    valid_quotes,
)


# FALLBACK VOLATILITY CONFIGURATION (annualized decimals, 0.25 = 25%).
# Week 0: ticks 0-74; week 1: 75-149; week 2: 150-224; week 3: 225-299.
# A fresh copy is made on every poll so a case reset cannot retain old news.
BASELINE_WEEKLY_VOL_RANGES = {
    0: (0.25, 0.25),
    1: (0.25, 0.25),
    2: (0.25, 0.25),
    3: (0.25, 0.25),
}

INITIAL_HEADLINE = "Risk free rate and current annualized volatility of RTM"
RANGE_PATTERN = re.compile(
    r"\bvolatility\b[^.!?]*?\b(?:is|will\s+be)\s+between\s+"
    r"(?P<low>\d+(?:\.\d+)?)\s*%\s+and\s+"
    r"(?P<high>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
SINGLE_PATTERN = re.compile(
    r"\bvolatility\b[^.!?]*?\b(?:is|will\s+be)\s+"
    r"(?P<value>\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
SOURCE_RANK = {"BASELINE": 0, "FORECAST": 1, "ACTUAL": 2}


def parse_volatility_range(body):
    """Extract only a percentage attached to a volatility statement."""
    if not isinstance(body, str):
        return None
    range_match = RANGE_PATTERN.search(body)
    if range_match:
        low = float(range_match.group("low")) / 100
        high = float(range_match.group("high")) / 100
    else:
        single_match = SINGLE_PATTERN.search(body)
        if not single_match:
            return None
        low = high = float(single_match.group("value")) / 100
    if not (0 < low <= high <= 1):
        return None
    return low, high


def _integer(value):
    parsed = number(value)
    if parsed is None or parsed < 0 or not parsed.is_integer():
        return None
    return int(parsed)


def _same_period(news_period, case_period):
    if news_period == case_period:
        return True
    news_number, case_number = number(news_period), number(case_period)
    return news_number is not None and case_number is not None and news_number == case_number


def _news_id_key(value):
    numeric = number(value)
    return (0, numeric) if numeric is not None else (1, str(value))


def build_weekly_vol_ranges(news, current_period):
    """Build this poll's ranges and provenance entirely from current news."""
    ranges = dict(BASELINE_WEEKLY_VOL_RANGES)
    sources = {week: "BASELINE" for week in ranges}
    warnings = []
    applicable = []

    for item in news:
        if not isinstance(item, dict):
            warnings.append("Ignored a malformed news item.")
            continue
        if "period" in item and not _same_period(item.get("period"), current_period):
            continue
        parsed_range = parse_volatility_range(item.get("body"))
        if parsed_range is None:
            continue

        headline = item.get("headline", "")
        is_initial = isinstance(headline, str) and headline.strip().casefold() == INITIAL_HEADLINE.casefold()
        news_tick = _integer(item.get("tick"))
        if news_tick is None:
            if is_initial and item.get("tick") is None:
                news_tick = 0
            else:
                label = item.get("news_id", item.get("headline", "unknown"))
                warnings.append(f"Ignored volatility news {label!r}: missing or invalid tick.")
                continue

        body = item.get("body", "")
        normalized_body = body.casefold()
        source_week = news_tick // 75
        if "next week" in normalized_body:
            target_week, source = source_week + 1, "FORECAST"
        elif ("this week" in normalized_body
              or "current annualized realized volatility" in normalized_body):
            target_week, source = source_week, "ACTUAL"
        else:
            continue
        if not 0 <= target_week <= 3:
            continue
        applicable.append((news_tick, _news_id_key(item.get("news_id")),
                           target_week, parsed_range, source))

    for _, _, target_week, parsed_range, source in sorted(applicable):
        # An actual value is authoritative even if a later forecast also exists.
        if SOURCE_RANK[source] >= SOURCE_RANK[sources[target_week]]:
            ranges[target_week] = parsed_range
            sources[target_week] = source
    return ranges, sources, warnings


def print_volatility_table(ranges, sources):
    print("Week | Ticks   | Low   | High  | Source")
    for week in range(4):
        low, high = ranges[week]
        print(f"{week + 1:>4} | {week * 75:>3}-{week * 75 + 74:<3} | "
              f"{low:>5.1%} | {high:>5.1%} | {sources[week]}")


def display_snapshot(case, securities, news):
    if not isinstance(case, dict) or not isinstance(securities, list) or not isinstance(news, list):
        raise ValueError("Expected /case object and /securities, /news arrays.")
    print(f"\n{BANNER}\ntick={case.get('tick')}  period={case.get('period')}  status={case.get('status')}")
    print("Raw news:")
    for item in news:
        if isinstance(item, dict):
            print(f"Headline: {item.get('headline', '')}\nBody: {item.get('body', '')}")
    if not news:
        print("(none returned)")

    tick = number(case.get("tick"))
    if tick is None or tick < 0:
        raise ValueError("Missing or invalid case tick.")
    ranges, sources, warnings = build_weekly_vol_ranges(news, case.get("period"))
    for warning in warnings:
        print(f"News warning: {warning}")
    print_volatility_table(ranges, sources)
    if tick >= EXPIRY_TICK:
        print("Expiry reached; valuation and proposed hedge unavailable.")
        return
    if not all(isinstance(security, dict) for security in securities):
        raise ValueError("Invalid security record.")
    rtms = [security for security in securities if security.get("ticker") == "RTM"]
    if len(rtms) != 1:
        raise ValueError("Expected exactly one RTM security.")
    rtm = rtms[0]
    spot = underlying_spot(rtm)
    vols = effective_volatilities(tick, ranges)
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
            rows.append(analyze_option(
                security, spot, (EXPIRY_TICK - tick) / TICKS_PER_YEAR, vols
            ))
        except (ValueError, ArithmeticError):
            errors.append(f"{ticker}: invalid contract/position data or valuation.")
            rows.append(dict(ticker=ticker, bid=valid_quotes(security)[0],
                             ask=valid_quotes(security)[1],
                             position=number(security.get("position")),
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
    trade = (f"{'BUY' if quantity > 0 else 'SELL'} {abs(quantity):g} RTM"
             if quantity else "HOLD RTM (0 shares)")
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
                    if isinstance(exc, (ApiError, ValueError)):
                        print(f"Snapshot unavailable: {exc}")
                    else:
                        print("Snapshot unavailable: invalid data/configuration shape.")
                sleep(max(0.0, POLL_SECONDS - (monotonic() - started)))
    except KeyboardInterrupt:
        print("\nDRY_RUN analyzer stopped.")


if __name__ == "__main__":
    main()
