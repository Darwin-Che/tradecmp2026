"""Read-only RITCx analyzer with news, option, and straddle analysis.

Unknown future weeks are stress tested for sensitivity only. The stress range
is not an official forecast. This program displays simulated orders but never
submits, modifies, or cancels them.
"""

import math
from time import monotonic, sleep

import requests
from requests.auth import HTTPBasicAuth

from case1_strategy_v1 import (
    API_ENDPOINT,
    USERNAME,
    PASSWORD,
    BANNER,
    COMMISSION_PER_CONTRACT,
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
    build_weekly_vol_ranges,
    print_volatility_table,
)


UNKNOWN_WEEK_VOL_RANGE = (0.15, 0.40)
SIMULATED_STRADDLE_QTY = 10
RTM_HEDGE_FEE_PER_SHARE = 0.02


def calculate_stress_volatilities(tick, ranges, sources):
    """Return effective low/high vols after stressing unknown future weeks."""
    current_week = min(int(tick // 75), 3)
    low_ranges = dict(ranges)
    high_ranges = dict(ranges)
    unknown_weeks = []
    for week in range(current_week + 1, 4):
        if sources.get(week) == "BASELINE":
            unknown_weeks.append(week)
            low_ranges[week] = (UNKNOWN_WEEK_VOL_RANGE[0],) * 2
            high_ranges[week] = (UNKNOWN_WEEK_VOL_RANGE[1],) * 2
    stress_low = effective_volatilities(tick, low_ranges)[0]
    stress_high = effective_volatilities(tick, high_ranges)[2]
    return stress_low, stress_high, unknown_weeks


def add_stress_valuations(security, row, spot, years, stress_low, stress_high):
    """Add option prices under the unknown-week sensitivity assumptions."""
    stress_mid = (stress_low + stress_high) / 2
    stressed = analyze_option(
        security, spot, years, (stress_low, stress_mid, stress_high)
    )
    row["stress_low"] = stressed["fair_low"]
    row["stress_high"] = stressed["fair_high"]
    return row


def build_straddles(option_rows, spot):
    """Pair calls and puts by strike and calculate one-contract economics."""
    pairs = {}
    warnings = []
    for row in option_rows:
        identified = identify_option(row.get("ticker"))
        if identified is None:
            continue
        kind, strike = identified
        pairs.setdefault(strike, {})[kind] = row

    straddles = []
    for strike, pair in pairs.items():
        if "c" not in pair or "p" not in pair:
            continue
        call, put = pair["c"], pair["p"]
        call_multiplier = number(call.get("multiplier"))
        put_multiplier = number(put.get("multiplier"))
        if (call_multiplier is None or put_multiplier is None
                or call_multiplier <= 0 or call_multiplier != put_multiplier):
            warnings.append(f"Strike {strike:g}: call/put contract multipliers do not match.")
            continue
        multiplier = call_multiplier

        call_ask, put_ask = call.get("ask"), put.get("ask")
        call_bid, put_bid = call.get("bid"), put.get("bid")
        straddle_ask = (call_ask + put_ask
                        if call_ask is not None and put_ask is not None else None)
        straddle_bid = (call_bid + put_bid
                        if call_bid is not None and put_bid is not None else None)
        fair_low = call["fair_low"] + put["fair_low"]
        fair_mid = call["fair_mid"] + put["fair_mid"]
        fair_high = call["fair_high"] + put["fair_high"]
        stress_low_fair = call["stress_low"] + put["stress_low"]
        stress_high_fair = call["stress_high"] + put["stress_high"]
        delta_shares = (call["delta"] + put["delta"]) * multiplier

        net_edge = ((fair_low - straddle_ask) * multiplier
                    - 2 * COMMISSION_PER_CONTRACT
                    if straddle_ask is not None else None)
        stress_low_edge = ((stress_low_fair - straddle_ask) * multiplier
                           - 2 * COMMISSION_PER_CONTRACT
                           if straddle_ask is not None else None)
        premium_with_commission = (
            straddle_ask * multiplier + 2 * COMMISSION_PER_CONTRACT
            if straddle_ask is not None else None
        )
        edge_return = (net_edge / premium_with_commission
                       if net_edge is not None and premium_with_commission > 0 else None)
        combined_spread = (straddle_ask - straddle_bid
                           if straddle_ask is not None and straddle_bid is not None else None)
        initial_rtm_hedge = round(-delta_shares)
        straddles.append({
            "strike": strike,
            "distance_from_spot": abs(strike - spot),
            "call_ticker": call["ticker"],
            "put_ticker": put["ticker"],
            "call_ask": call_ask,
            "put_ask": put_ask,
            "straddle_ask": straddle_ask,
            "straddle_bid": straddle_bid,
            "fair_low": fair_low,
            "fair_mid": fair_mid,
            "fair_high": fair_high,
            "stress_low_fair": stress_low_fair,
            "stress_high_fair": stress_high_fair,
            "net_edge_per_straddle": net_edge,
            "stress_low_net_edge": stress_low_edge,
            "delta_shares_per_straddle": delta_shares,
            "initial_rtm_hedge": initial_rtm_hedge,
            "initial_rtm_hedge_fee": abs(initial_rtm_hedge) * RTM_HEDGE_FEE_PER_SHARE,
            "edge_return_on_premium": edge_return,
            "combined_spread": combined_spread,
            "multiplier": multiplier,
        })
    return straddles, warnings


def robust_shortlist(straddles):
    """Return positive-edge straddles within 95% of the best stress edge."""
    candidates = [
        row for row in straddles
        if row.get("stress_low_net_edge") is not None
        and row["stress_low_net_edge"] > 0
    ]
    if not candidates:
        return [], None, None
    max_stress_edge = max(row["stress_low_net_edge"] for row in candidates)
    cutoff = 0.95 * max_stress_edge
    shortlist = [
        row for row in candidates if row["stress_low_net_edge"] >= cutoff
    ]

    def shortlist_rank(row):
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

    return sorted(shortlist, key=shortlist_rank), max_stress_edge, cutoff


def rank_straddles(straddles):
    """Show the robust shortlist first, followed by all remaining strikes."""
    shortlist, _, _ = robust_shortlist(straddles)
    shortlisted = {id(row) for row in shortlist}

    def remaining_rank(row):
        edge = row.get("stress_low_net_edge")
        return (
            0 if edge is not None and edge > 0 else 1,
            -edge if edge is not None else math.inf,
            abs(row["distance_from_spot"]),
        )

    remaining = sorted(
        (row for row in straddles if id(row) not in shortlisted),
        key=remaining_rank,
    )
    return shortlist + remaining


def best_robust_straddle(straddles):
    shortlist, _, _ = robust_shortlist(straddles)
    return shortlist[0] if shortlist else None


def simulated_recommendation(straddle, quantity, existing_portfolio_delta):
    """Calculate a display-only trade and hedge around all existing positions."""
    added_delta = quantity * straddle["delta_shares_per_straddle"]
    before_hedge = existing_portfolio_delta + added_delta
    rtm_trade_quantity = round(-before_hedge)
    after_hedge = before_hedge + rtm_trade_quantity
    return {
        "quantity": quantity,
        "rtm_trade_quantity": rtm_trade_quantity,
        "total_option_premium": (
            quantity * straddle["straddle_ask"] * straddle["multiplier"]
        ),
        "total_option_commission": (
            quantity * 2 * COMMISSION_PER_CONTRACT
        ),
        "estimated_rtm_hedge_fee": (
            abs(rtm_trade_quantity) * RTM_HEDGE_FEE_PER_SHARE
        ),
        "total_base_model_edge": quantity * straddle["net_edge_per_straddle"],
        "total_stress_low_model_edge": quantity * straddle["stress_low_net_edge"],
        "projected_delta_before_hedge": before_hedge,
        "projected_delta_after_hedge": after_hedge,
    }


def print_option_table(rows):
    columns = [
        ("ticker", "ticker"), ("bid", "bid"), ("ask", "ask"),
        ("position", "position"), ("delta", "delta"),
        ("fair_low", "fair_low"), ("fair_mid", "fair_mid"),
        ("fair_high", "fair_high"), ("stress_low", "stress_low"),
        ("stress_high", "stress_high"), ("buy_cons", "buy_conservative"),
        ("sell_cons", "sell_conservative"), ("signal", "signal"),
    ]
    signal_rank = {
        "STRONG BUY": 0, "STRONG SELL": 0, "WEAK BUY": 1,
        "WEAK SELL": 1, "NO TRADE": 2,
    }
    ordered = sorted(rows, key=lambda row: (signal_rank[row["signal"]], row["ticker"]))
    cells = [[title for title, _ in columns]] + [
        [format_value(row.get(key)) for _, key in columns] for row in ordered
    ]
    widths = [max(len(row[index]) for row in cells) for index in range(len(columns))]
    for row in cells:
        print(" | ".join(value.rjust(width) for value, width in zip(row, widths)))
    print("stress_low / stress_high are sensitivity values, not official forecasts.")


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
    ]
    ranked = rank_straddles(straddles)
    cells = [[title for title, _ in columns]]
    for row in ranked:
        values = []
        for _, key in columns:
            value = row.get(key)
            if key == "edge_return_on_premium" and value is not None:
                values.append(f"{value:.2%}")
            elif key == "initial_rtm_hedge" and value is not None:
                if value:
                    values.append(f"{'BUY' if value > 0 else 'SELL'} {abs(value)} RTM")
                else:
                    values.append("HOLD RTM")
            else:
                values.append(format_value(value))
        cells.append(values)
    widths = [max(len(row[index]) for row in cells) for index in range(len(columns))]
    print("Straddles (one call contract plus one put contract):")
    for row in cells:
        print(" | ".join(value.rjust(width) for value, width in zip(row, widths)))
    shortlist, max_stress_edge, cutoff = robust_shortlist(straddles)
    if shortlist:
        strikes = ", ".join(f"{row['strike']:g}" for row in shortlist)
        print(f"Max stress-low edge: ${max_stress_edge:.4f}; "
              f"95% shortlist cutoff: ${cutoff:.4f}; "
              f"robust shortlist strikes: {strikes}")
    else:
        print("Max stress-low edge: N/A; 95% shortlist cutoff: N/A; "
              "robust shortlist strikes: none")


def print_simulated_orders(straddle, recommendation):
    quantity = recommendation["quantity"]
    hedge = recommendation["rtm_trade_quantity"]
    print(f"BEST ROBUST STRADDLE: strike {straddle['strike']:g}")
    print("SIMULATED ORDERS (display only; nothing is submitted):")
    print(f"BUY {quantity} {straddle['call_ticker']}")
    print(f"BUY {quantity} {straddle['put_ticker']}")
    if hedge:
        print(f"{'BUY' if hedge > 0 else 'SELL'} {abs(hedge)} RTM")
    else:
        print("HOLD RTM (0-share hedge)")
    print(f"Total option premium: ${recommendation['total_option_premium']:.2f}\n"
          f"Total option commission: ${recommendation['total_option_commission']:.2f}\n"
          f"Estimated RTM hedge fee: ${recommendation['estimated_rtm_hedge_fee']:.2f}\n"
          f"Total base-case model edge: ${recommendation['total_base_model_edge']:.2f}\n"
          f"Total stress-low model edge: ${recommendation['total_stress_low_model_edge']:.2f}\n"
          f"Projected portfolio delta before hedge: "
          f"{recommendation['projected_delta_before_hedge']:.4f}\n"
          f"Projected portfolio delta after rounded hedge: "
          f"{recommendation['projected_delta_after_hedge']:.4f}")


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
    ranges, sources, news_warnings = build_weekly_vol_ranges(news, case.get("period"))
    for warning in news_warnings:
        print(f"News warning: {warning}")
    print_volatility_table(ranges, sources)
    if tick >= EXPIRY_TICK:
        print("Expiry reached; valuation and proposed hedge unavailable.")
        return
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
        print("No robust trade is available: no straddle has positive stress-low edge.")

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
        recommendation = simulated_recommendation(
            best, SIMULATED_STRADDLE_QTY, summary["total_portfolio_delta"]
        )
        print_simulated_orders(best, recommendation)


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
