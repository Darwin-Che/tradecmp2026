"""Read-only RITCx analyzer with case-state and news-time safety gates."""

import math
from time import monotonic, sleep

import requests
from requests.auth import HTTPBasicAuth

from case1_strategy_v1 import (
    API_ENDPOINT,
    USERNAME,
    PASSWORD,
    BANNER,
    EXPIRY_TICK,
    POLL_SECONDS,
    TICKS_PER_YEAR,
    ApiError,
    AuthenticationError,
    analyze_option,
    api_get,
    effective_volatilities,
    format_value,
    identify_option,
    number,
    portfolio_summary,
    underlying_spot,
    valid_quotes,
)
from case1_strategy_v1_1 import (
    BASELINE_WEEKLY_VOL_RANGES,
    INITIAL_HEADLINE,
    SOURCE_RANK,
    _integer,
    _news_id_key,
    _same_period,
    parse_volatility_range,
    print_volatility_table,
)
from case1_strategy_v1_2 import (
    SIMULATED_STRADDLE_QTY,
    UNKNOWN_WEEK_VOL_RANGE,
    add_stress_valuations,
    build_straddles as _build_straddles,
    calculate_stress_volatilities,
    print_option_table,
    print_simulated_orders,
    simulated_recommendation,
)


INACTIVE_MESSAGE = (
    "CASE NOT ACTIVE — no valuation, signal, hedge, or recommendation generated."
)
RESET_MESSAGE = (
    "CASE RESET DETECTED — rebuilding state from currently applicable news."
)


def normalized_status(case):
    return str(case.get("status", "")).upper()


def valid_valuation_tick(case):
    tick = number(case.get("tick"))
    return tick if tick is not None and 0 <= tick < EXPIRY_TICK else None


def security_is_tradeable(security):
    """An absent flag is allowed; an included flag must explicitly be true."""
    return "is_tradeable" not in security or security.get("is_tradeable") is True


def build_weekly_vol_ranges(news, current_period, current_case_tick):
    """Rebuild ranges using only news published by the current case tick."""
    ranges = dict(BASELINE_WEEKLY_VOL_RANGES)
    sources = {week: "BASELINE" for week in ranges}
    warnings = []
    applicable = []
    case_tick = number(current_case_tick)
    if case_tick is None or not 0 <= case_tick < EXPIRY_TICK:
        raise ValueError("News filtering requires a current tick from 0 through 299.")

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
        is_initial = (
            isinstance(headline, str)
            and headline.strip().casefold() == INITIAL_HEADLINE.casefold()
        )
        news_tick = _integer(item.get("tick"))
        if news_tick is None:
            if is_initial and item.get("tick") is None:
                news_tick = 0
            else:
                label = item.get("news_id", item.get("headline", "unknown"))
                warnings.append(
                    f"Ignored volatility news {label!r}: missing or invalid tick."
                )
                continue
        if news_tick > case_tick:
            continue

        normalized_body = item.get("body", "").casefold()
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
        if SOURCE_RANK[source] >= SOURCE_RANK[sources[target_week]]:
            ranges[target_week] = parsed_range
            sources[target_week] = source
    return ranges, sources, warnings


def build_straddles(option_rows, spot):
    """Preserve straddle economics and attach both-leg tradeability."""
    straddles, warnings = _build_straddles(option_rows, spot)
    tradeable = {
        row.get("ticker"): row.get("tradeable", True) for row in option_rows
    }
    for straddle in straddles:
        straddle["tradeable"] = (
            tradeable.get(straddle["call_ticker"], True)
            and tradeable.get(straddle["put_ticker"], True)
        )
    return straddles, warnings


def robust_shortlist(straddles):
    """Use the v1_2 ranking rules, excluding non-tradeable option pairs."""
    candidates = [
        row for row in straddles
        if row.get("tradeable", True)
        and row.get("stress_low_net_edge") is not None
        and row["stress_low_net_edge"] > 0
    ]
    if not candidates:
        return [], None, None
    max_stress_edge = max(row["stress_low_net_edge"] for row in candidates)
    cutoff = 0.95 * max_stress_edge
    shortlist = [
        row for row in candidates if row["stress_low_net_edge"] >= cutoff
    ]

    def rank(row):
        edge_return = row.get("edge_return_on_premium")
        base_edge = row.get("net_edge_per_straddle")
        spread = row.get("combined_spread")
        return (
            abs(row["distance_from_spot"]),
            abs(row["delta_shares_per_straddle"]),
            -edge_return if edge_return is not None else math.inf,
            -base_edge if base_edge is not None else math.inf,
            spread if spread is not None else math.inf,
        )

    return sorted(shortlist, key=rank), max_stress_edge, cutoff


def best_robust_straddle(straddles):
    shortlist, _, _ = robust_shortlist(straddles)
    return shortlist[0] if shortlist else None


def rank_straddles(straddles):
    shortlist, _, _ = robust_shortlist(straddles)
    selected = {id(row) for row in shortlist}

    def remaining_rank(row):
        edge = row.get("stress_low_net_edge")
        return (0 if edge is not None and edge > 0 else 1,
                -edge if edge is not None else math.inf,
                abs(row["distance_from_spot"]))

    remaining = sorted(
        (row for row in straddles if id(row) not in selected),
        key=remaining_rank,
    )
    return shortlist + remaining


def print_straddle_table(straddles):
    columns = [
        ("strike", "strike"), ("distance", "distance_from_spot"),
        ("call_ask", "call_ask"), ("put_ask", "put_ask"),
        ("straddle_ask", "straddle_ask"), ("fair_low", "fair_low"),
        ("fair_mid", "fair_mid"), ("fair_high", "fair_high"),
        ("net_edge_$", "net_edge_per_straddle"),
        ("stress_low_edge_$", "stress_low_net_edge"),
        ("delta_shares", "delta_shares_per_straddle"),
        ("initial_rtm_hedge", "initial_rtm_hedge"),
        ("initial_hedge_fee_$", "initial_rtm_hedge_fee"),
        ("edge_return", "edge_return_on_premium"),
        ("tradeable", "tradeable"),
    ]
    cells = [[title for title, _ in columns]]
    for row in rank_straddles(straddles):
        values = []
        for _, key in columns:
            value = row.get(key)
            if key == "edge_return_on_premium" and value is not None:
                values.append(f"{value:.2%}")
            elif key == "initial_rtm_hedge" and value is not None:
                values.append((f"{'BUY' if value > 0 else 'SELL'} {abs(value)} RTM"
                               if value else "HOLD RTM"))
            else:
                values.append(format_value(value))
        cells.append(values)
    widths = [max(len(row[index]) for row in cells) for index in range(len(columns))]
    print("Straddles (one call contract plus one put contract):")
    for row in cells:
        print(" | ".join(value.rjust(width) for value, width in zip(row, widths)))
    shortlist, max_edge, cutoff = robust_shortlist(straddles)
    if shortlist:
        strikes = ", ".join(f"{row['strike']:g}" for row in shortlist)
        print(f"Max stress-low edge: ${max_edge:.4f}; "
              f"95% shortlist cutoff: ${cutoff:.4f}; "
              f"robust shortlist strikes: {strikes}")
    else:
        print("Max stress-low edge: N/A; 95% shortlist cutoff: N/A; "
              "robust shortlist strikes: none")


def display_snapshot(case, securities, news):
    """Display an ACTIVE, pre-expiry snapshot; return before all valuation otherwise."""
    if not isinstance(case, dict):
        raise ValueError("Expected /case object.")
    status = normalized_status(case)
    print(f"\n{BANNER}\ntick={case.get('tick')}  period={case.get('period')}  status={status}")
    if status != "ACTIVE":
        print(INACTIVE_MESSAGE, flush=True)
        return
    tick = valid_valuation_tick(case)
    if tick is None:
        print("INVALID OR EXPIRED TICK — no valuation, signal, hedge, or recommendation generated.",
              flush=True)
        return
    if not isinstance(securities, list) or not isinstance(news, list):
        raise ValueError("Expected /securities and /news arrays.")

    print("Raw news:")
    for item in news:
        if isinstance(item, dict):
            print(f"Headline: {item.get('headline', '')}\nBody: {item.get('body', '')}")
    if not news:
        print("(none returned)")
    ranges, sources, news_warnings = build_weekly_vol_ranges(
        news, case.get("period"), tick
    )
    for warning in news_warnings:
        print(f"News warning: {warning}")
    print_volatility_table(ranges, sources)
    stress_low, stress_high, unknown_weeks = calculate_stress_volatilities(
        tick, ranges, sources
    )
    if unknown_weeks:
        labels = ", ".join(str(week + 1) for week in unknown_weeks)
        print(f"Unknown future week(s) {labels}: sensitivity assumption "
              f"{UNKNOWN_WEEK_VOL_RANGE[0]:.0%}-{UNKNOWN_WEEK_VOL_RANGE[1]:.0%}; "
              "these are not official forecasts.")

    if not all(isinstance(security, dict) for security in securities):
        raise ValueError("Invalid security record.")
    rtms = [security for security in securities if security.get("ticker") == "RTM"]
    if len(rtms) != 1:
        raise ValueError("Expected exactly one RTM security.")
    rtm = rtms[0]
    rtm_tradeable = security_is_tradeable(rtm)
    spot = underlying_spot(rtm)
    vols = effective_volatilities(tick, ranges)
    print(f"RTM spot={spot:.4f}; effective_vol_low={vols[0]:.6f} "
          f"effective_vol_mid={vols[1]:.6f} effective_vol_high={vols[2]:.6f}; "
          f"stress_low={stress_low:.6f} stress_high={stress_high:.6f}")

    years = (EXPIRY_TICK - tick) / TICKS_PER_YEAR
    rows, errors = [], []
    for security in securities:
        ticker = security.get("ticker")
        if identify_option(ticker) is None:
            if ticker != "RTM" and number(security.get("position")) != 0:
                errors.append(f"Unrecognized security with nonzero/unknown position: {ticker}")
            continue
        try:
            row = analyze_option(security, spot, years, vols)
            row["tradeable"] = security_is_tradeable(security)
            rows.append(add_stress_valuations(
                security, row, spot, years, stress_low, stress_high
            ))
        except (ValueError, ArithmeticError):
            errors.append(f"{ticker}: invalid contract/position data or valuation.")
            rows.append(dict(ticker=ticker, bid=valid_quotes(security)[0],
                             ask=valid_quotes(security)[1],
                             position=number(security.get("position")),
                             signal="NO TRADE"))
    print_option_table(rows)

    complete_rows = [row for row in rows if "delta" in row]
    straddles, straddle_warnings = build_straddles(complete_rows, spot)
    for warning in straddle_warnings:
        print(f"Straddle warning: {warning}")
    print_straddle_table(straddles)
    best = best_robust_straddle(straddles)
    if best is None:
        print("No robust trade is available: no tradeable straddle has positive stress-low edge.")

    print("Portfolio summary (RTM and recognized RTM options):")
    rtm_position = number(rtm.get("position"))
    print(f"RTM position: {format_value(rtm_position)}")
    if errors or rtm_position is None:
        for error in errors:
            print(f"Data warning: {error}")
        print("Option delta exposure: N/A\nTotal portfolio delta: N/A\n"
              "Target RTM position: N/A\nProposed RTM trade: unavailable (incomplete data)\n"
              "Simulated straddle recommendation unavailable (incomplete portfolio data).")
        return
    summary = portfolio_summary(rows, rtm_position)
    print(f"Option delta exposure: {summary['option_delta_exposure']:.4f}\n"
          f"Total portfolio delta: {summary['total_portfolio_delta']:.4f}\n"
          f"Target RTM position: {summary['target_rtm_position']}")
    current_hedge = summary["rtm_trade_quantity"]
    current_trade = (f"{'BUY' if current_hedge > 0 else 'SELL'} {abs(current_hedge):g} RTM"
                     if current_hedge else "HOLD RTM (0 shares)")
    print(f"Proposed current-position RTM trade: {current_trade} (display only)")
    if best is not None:
        if not rtm_tradeable:
            print("Simulated straddle recommendation unavailable: RTM is not tradeable.")
            return
        recommendation = simulated_recommendation(
            best, SIMULATED_STRADDLE_QTY, summary["total_portfolio_delta"]
        )
        print_simulated_orders(best, recommendation)


def reset_detected(previous_period, previous_tick, case):
    current_tick = number(case.get("tick"))
    period_changed = case.get("period") != previous_period
    tick_rewound = (previous_tick is not None and current_tick is not None
                    and current_tick < previous_tick)
    return period_changed or tick_rewound


def main():
    print(BANNER, flush=True)
    previous_period = None
    previous_tick = None
    have_previous_snapshot = False
    try:
        with requests.Session() as session:
            session.auth = HTTPBasicAuth(USERNAME, PASSWORD)
            while True:
                started = monotonic()
                try:
                    case = api_get(session, API_ENDPOINT, "case")
                    if not isinstance(case, dict):
                        raise ValueError("Expected /case object.")
                    if (have_previous_snapshot
                            and reset_detected(previous_period, previous_tick, case)):
                        print(RESET_MESSAGE)
                        # There are no persistent news/recommendation caches; each
                        # ACTIVE snapshot is rebuilt from baseline below.
                    previous_period = case.get("period")
                    previous_tick = number(case.get("tick"))
                    have_previous_snapshot = True

                    if normalized_status(case) != "ACTIVE":
                        print(INACTIVE_MESSAGE, flush=True)
                    elif valid_valuation_tick(case) is None:
                        print("INVALID OR EXPIRED TICK — no valuation, signal, hedge, "
                              "or recommendation generated.", flush=True)
                    else:
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
