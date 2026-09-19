"""Final read-only RITCx Case 1 decision assistant.

All instructions are manual and snapshot-specific. This module reads only the
case, securities, and news resources through the inherited GET-only helper.
"""

import csv
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

import requests
from requests.auth import HTTPBasicAuth

from case1_strategy_v1 import (
    BANNER,
    EXPIRY_TICK,
    TICKS_PER_YEAR,
    ApiError,
    AuthenticationError,
    analyze_option,
    api_get,
    effective_volatilities,
    identify_option,
    number,
    underlying_spot,
    valid_quotes,
)
from case1_strategy_v1_1 import (
    BASELINE_WEEKLY_VOL_RANGES,
    INITIAL_HEADLINE,
    _integer,
    _same_period,
    parse_volatility_range,
)
from case1_strategy_v1_2_1 import (
    INACTIVE_MESSAGE,
    RESET_MESSAGE,
    build_weekly_vol_ranges,
    normalized_status,
    reset_detected,
    security_is_tradeable,
    valid_valuation_tick,
)


def env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


COMPACT_MODE = env_bool("RIT_COMPACT_MODE", True)
RIT_VERBOSE = env_bool("RIT_VERBOSE", False)
POLL_SECONDS = env_float("RIT_POLL_SECONDS", 0.5)

OPTION_COMMISSION_PER_CONTRACT_PER_SIDE = env_float("RIT_OPTION_COMMISSION", 2.00)
RTM_FEE_PER_SHARE_PER_SIDE = env_float("RIT_RTM_FEE_PER_SHARE", 0.02)
REHEDGE_COST_BUFFER_PER_STRADDLE = env_float("RIT_REHEDGE_BUFFER", 1.00)

MIN_LONG_ROUND_TRIP_EDGE_DOLLARS = env_float("RIT_MIN_LONG_EDGE", 10.00)
MIN_LONG_EDGE_RETURN = env_float("RIT_MIN_LONG_RETURN", 0.10)
MIN_SHORT_ROUND_TRIP_EDGE_DOLLARS = env_float("RIT_MIN_SHORT_EDGE", 15.00)
MIN_SHORT_EDGE_RETURN = env_float("RIT_MIN_SHORT_RETURN", 0.15)
ROBUST_SHORTLIST_RATIO = env_float("RIT_SHORTLIST_RATIO", 0.95)

SIMULATED_LONG_STRADDLE_QTY = env_int("RIT_LONG_QTY", 10)
SIMULATED_SHORT_STRADDLE_QTY = env_int("RIT_SHORT_QTY", 3)
NO_NEW_ENTRY_TICK = env_int("RIT_NO_NEW_ENTRY_TICK", 285)
FORCE_EXIT_TICK = env_int("RIT_FORCE_EXIT_TICK", 290)
EXIT_CAPTURE_FRACTION = env_float("RIT_EXIT_CAPTURE_FRACTION", 0.60)
DELTA_WARNING_LEVEL = env_float("RIT_DELTA_WARNING_LEVEL", 2000)
DELTA_HARD_SAFETY_LEVEL = env_float("RIT_DELTA_HARD_SAFETY_LEVEL", 5000)

PLAN_LOCK_TICKS = env_int("RIT_PLAN_LOCK_TICKS", 5)
INVALID_EDGE_CONFIRMATIONS = env_int("RIT_INVALID_EDGE_CONFIRMATIONS", 2)
CHALLENGER_RELATIVE_IMPROVEMENT = env_float("RIT_CHALLENGER_RELATIVE_IMPROVEMENT", 0.25)
CHALLENGER_ABSOLUTE_IMPROVEMENT = env_float("RIT_CHALLENGER_ABSOLUTE_IMPROVEMENT", 15.00)
NEWS_SETTLE_TICKS = env_int("RIT_NEWS_SETTLE_TICKS", 1)

UNKNOWN_WEEK_VOL_RANGE = (
    env_float("RIT_UNKNOWN_WEEK_VOL_LOW", 0.15),
    env_float("RIT_UNKNOWN_WEEK_VOL_HIGH", 0.40),
)

# DMA API CONNECTION CONFIGURATION — never printed or written to the event log.
API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16595/v1"  # Volatility Trading case
USERNAME = "goal-2"
PASSWORD = "credit"
ALERT_LOG_PATH = Path(__file__).with_name("case1_alerts.csv")
LOG_FIELDS = [
    "timestamp_utc", "period", "tick", "event_type", "action", "strike",
    "rtm_spot", "effective_vol_low", "effective_vol_mid", "effective_vol_high",
    "stress_low", "stress_high", "robust_edge_dollars", "edge_return",
    "call_bid", "call_ask", "put_bid", "put_ask", "current_portfolio_delta",
    "projected_portfolio_delta", "message",
]


def validate_configuration():
    if not 0 < ROBUST_SHORTLIST_RATIO <= 1:
        raise ValueError("RIT_SHORTLIST_RATIO must be in (0, 1].")
    if not 0 < UNKNOWN_WEEK_VOL_RANGE[0] <= UNKNOWN_WEEK_VOL_RANGE[1]:
        raise ValueError("Unknown-week volatility bounds are invalid.")
    return USERNAME, PASSWORD


def calculate_stress_volatilities(tick, ranges, sources):
    current_week = min(int(tick // 75), 3)
    low_ranges, high_ranges = dict(ranges), dict(ranges)
    unknown_weeks = []
    for week in range(current_week + 1, 4):
        if sources.get(week) == "BASELINE":
            unknown_weeks.append(week)
            low_ranges[week] = (UNKNOWN_WEEK_VOL_RANGE[0],) * 2
            high_ranges[week] = (UNKNOWN_WEEK_VOL_RANGE[1],) * 2
    return (
        effective_volatilities(tick, low_ranges)[0],
        effective_volatilities(tick, high_ranges)[2],
        unknown_weeks,
    )


def applicable_volatility_news(news, current_period, current_tick):
    """Return applicable volatility items in chronological order."""
    applicable = []
    for item in news:
        if not isinstance(item, dict):
            continue
        if "period" in item and not _same_period(item.get("period"), current_period):
            continue
        if parse_volatility_range(item.get("body")) is None:
            continue
        headline = item.get("headline", "")
        initial = (isinstance(headline, str)
                   and headline.strip().casefold() == INITIAL_HEADLINE.casefold())
        news_tick = _integer(item.get("tick"))
        if news_tick is None and initial and item.get("tick") is None:
            news_tick = 0
        if news_tick is None or news_tick > current_tick:
            continue
        applicable.append((news_tick, str(item.get("news_id", "")), item))
    return [item for _, _, item in sorted(applicable, key=lambda value: (value[0], value[1]))]


def news_identity(item):
    return str(item.get("news_id", f"{item.get('tick')}:{item.get('headline', '')}"))


def news_brief(item):
    if not item:
        return "No new applicable volatility news"
    parsed = parse_volatility_range(item.get("body"))
    if parsed is None:
        return str(item.get("headline", "Volatility news"))
    low, high = parsed
    value = f"{low:.1%}" if low == high else f"{low:.1%}-{high:.1%}"
    return f"{item.get('headline', 'Volatility update')}: {value}"


def build_option_rows(securities, spot, years, base_vols, stress_low, stress_high):
    rows, warnings = [], []
    stress_mid = (stress_low + stress_high) / 2
    for security in securities:
        ticker = security.get("ticker")
        if identify_option(ticker) is None:
            continue
        try:
            row = analyze_option(security, spot, years, base_vols)
            stressed = analyze_option(
                security, spot, years, (stress_low, stress_mid, stress_high)
            )
            row.update({
                "stress_low": stressed["fair_low"],
                "stress_high": stressed["fair_high"],
                "tradeable": security_is_tradeable(security),
                "vwap": number(security.get("vwap")),
            })
            if row["bid"] is None or row["ask"] is None:
                warnings.append(f"{ticker}: missing, crossed, or invalid executable quote.")
            rows.append(row)
        except (ValueError, ArithmeticError, KeyError):
            warnings.append(f"{ticker}: invalid quote, contract, position, or valuation data.")
    return rows, warnings


def straddle_economics(call, put, spot):
    """Return conservative completed-trade estimates for one straddle."""
    if not call.get("tradeable", True) or not put.get("tradeable", True):
        return None
    if identify_option(call.get("ticker"))[0] != "c" or identify_option(put.get("ticker"))[0] != "p":
        return None
    call_bid, call_ask = call.get("bid"), call.get("ask")
    put_bid, put_ask = put.get("bid"), put.get("ask")
    if any(value is None or value <= 0 for value in (call_bid, call_ask, put_bid, put_ask)):
        return None
    if call_bid > call_ask or put_bid > put_ask:
        return None
    multiplier = number(call.get("multiplier"))
    put_multiplier = number(put.get("multiplier"))
    if multiplier is None or multiplier <= 0 or multiplier != put_multiplier:
        return None

    _, strike = identify_option(call["ticker"])
    combined_bid = call_bid + put_bid
    combined_ask = call_ask + put_ask
    combined_spread = (call_ask - call_bid) + (put_ask - put_bid)
    fair_low = call["fair_low"] + put["fair_low"]
    fair_mid = call["fair_mid"] + put["fair_mid"]
    fair_high = call["fair_high"] + put["fair_high"]
    stress_low_fair = call["stress_low"] + put["stress_low"]
    stress_high_fair = call["stress_high"] + put["stress_high"]
    delta_shares = (call["delta"] + put["delta"]) * multiplier
    rounded_hedge_shares = round(abs(delta_shares))
    option_round_trip_commissions = 4 * OPTION_COMMISSION_PER_CONTRACT_PER_SIDE
    rtm_open_close_cost = (
        2 * rounded_hedge_shares * RTM_FEE_PER_SHARE_PER_SIDE
    )
    execution_buffer = (
        option_round_trip_commissions + rtm_open_close_cost
        + REHEDGE_COST_BUFFER_PER_STRADDLE
    )
    long_exit_base = fair_mid - combined_spread / 2
    long_exit_stress = stress_low_fair - combined_spread / 2
    short_cover_base = fair_mid + combined_spread / 2
    short_cover_stress = stress_high_fair + combined_spread / 2
    long_base_edge = (long_exit_base - combined_ask) * multiplier - execution_buffer
    long_stress_edge = (long_exit_stress - combined_ask) * multiplier - execution_buffer
    short_base_edge = (combined_bid - short_cover_base) * multiplier - execution_buffer
    short_stress_edge = (combined_bid - short_cover_stress) * multiplier - execution_buffer
    long_return_denominator = (
        combined_ask * multiplier
        + 2 * OPTION_COMMISSION_PER_CONTRACT_PER_SIDE
    )
    return {
        "strike": strike,
        "distance_from_spot": abs(strike - spot),
        "call_ticker": call["ticker"], "put_ticker": put["ticker"],
        "call_bid": call_bid, "call_ask": call_ask,
        "put_bid": put_bid, "put_ask": put_ask,
        "combined_bid": combined_bid, "combined_ask": combined_ask,
        "combined_spread": combined_spread,
        "fair_low": fair_low, "fair_mid": fair_mid, "fair_high": fair_high,
        "stress_low_fair": stress_low_fair, "stress_high_fair": stress_high_fair,
        "delta_shares_per_long_straddle": delta_shares,
        "rounded_hedge_shares": rounded_hedge_shares,
        "option_round_trip_commissions": option_round_trip_commissions,
        "estimated_rtm_open_close_cost": rtm_open_close_cost,
        "rehedge_cost_buffer": REHEDGE_COST_BUFFER_PER_STRADDLE,
        "total_execution_cost_buffer": execution_buffer,
        "estimated_long_exit_bid_base": long_exit_base,
        "estimated_long_exit_bid_stress": long_exit_stress,
        "estimated_short_cover_ask_base": short_cover_base,
        "estimated_short_cover_ask_stress": short_cover_stress,
        "long_base_edge_dollars": long_base_edge,
        "long_stress_edge_dollars": long_stress_edge,
        "short_base_edge_dollars": short_base_edge,
        "short_stress_edge_dollars": short_stress_edge,
        "long_edge_return": long_stress_edge / long_return_denominator,
        "short_edge_return": short_stress_edge / max(combined_bid * multiplier, 1.0),
        "multiplier": multiplier,
        "call_position": call["position"], "put_position": put["position"],
        "call_vwap": call.get("vwap"), "put_vwap": put.get("vwap"),
    }


def build_straddles(option_rows, spot):
    pairs = {}
    for row in option_rows:
        kind, strike = identify_option(row["ticker"])
        pairs.setdefault(strike, {})[kind] = row
    straddles = []
    for pair in pairs.values():
        if "c" in pair and "p" in pair:
            result = straddle_economics(pair["c"], pair["p"], spot)
            if result is not None:
                straddles.append(result)
    return straddles


def direction_candidate(row, direction):
    if direction == "LONG":
        return {
            **row, "direction": "LONG",
            "robust_edge_dollars": row["long_stress_edge_dollars"],
            "base_edge_dollars": row["long_base_edge_dollars"],
            "robust_edge_return": row["long_edge_return"],
            "delta_shares_per_straddle": row["delta_shares_per_long_straddle"],
        }
    return {
        **row, "direction": "SHORT",
        "robust_edge_dollars": row["short_stress_edge_dollars"],
        "base_edge_dollars": row["short_base_edge_dollars"],
        "robust_edge_return": row["short_edge_return"],
        "delta_shares_per_straddle": -row["delta_shares_per_long_straddle"],
    }


def candidate_passes_thresholds(candidate, tick):
    if candidate is None or tick >= NO_NEW_ENTRY_TICK:
        return False
    if candidate["direction"] == "LONG":
        return (candidate["robust_edge_dollars"] >= MIN_LONG_ROUND_TRIP_EDGE_DOLLARS
                and candidate["robust_edge_return"] >= MIN_LONG_EDGE_RETURN)
    return (candidate["robust_edge_dollars"] >= MIN_SHORT_ROUND_TRIP_EDGE_DOLLARS
            and candidate["robust_edge_return"] >= MIN_SHORT_EDGE_RETURN)


def entry_candidates(straddles, tick):
    if tick >= NO_NEW_ENTRY_TICK:
        return []
    candidates = []
    for row in straddles:
        for direction in ("LONG", "SHORT"):
            candidate = direction_candidate(row, direction)
            if candidate_passes_thresholds(candidate, tick):
                candidates.append(candidate)
    return candidates


def select_candidate(candidates):
    if not candidates:
        return None, [], None, None
    max_edge = max(row["robust_edge_dollars"] for row in candidates)
    cutoff = ROBUST_SHORTLIST_RATIO * max_edge
    shortlist = [row for row in candidates if row["robust_edge_dollars"] >= cutoff]
    shortlist.sort(key=lambda row: (
        row["distance_from_spot"],
        abs(row.get("projected_delta_for_ranking", row["delta_shares_per_straddle"])),
        -row["robust_edge_return"],
        -row["robust_edge_dollars"],
    ))
    return shortlist[0], shortlist, max_edge, cutoff


def option_delta_exposure(option_rows):
    return sum(
        row["position"] * row["multiplier"] * row["delta"] for row in option_rows
    )


def simulated_entry(candidate, existing_option_delta, current_rtm_position):
    direction_sign = 1 if candidate["direction"] == "LONG" else -1
    quantity = (SIMULATED_LONG_STRADDLE_QTY if direction_sign == 1
                else SIMULATED_SHORT_STRADDLE_QTY)
    added_option_delta = (
        direction_sign * quantity
        * candidate["delta_shares_per_long_straddle"]
    )
    projected_option_delta = existing_option_delta + added_option_delta
    projected_before_hedge = current_rtm_position + projected_option_delta
    target_rtm_position = round(-projected_option_delta)
    rtm_trade_quantity = target_rtm_position - current_rtm_position
    projected_after_hedge = projected_option_delta + target_rtm_position
    entry_price = (candidate["combined_ask"] if direction_sign == 1
                   else candidate["combined_bid"])
    return {
        "direction": candidate["direction"], "quantity": quantity,
        "projected_option_delta": projected_option_delta,
        "projected_portfolio_delta_before_hedge": projected_before_hedge,
        "target_rtm_position": target_rtm_position,
        "rtm_trade_quantity": rtm_trade_quantity,
        "projected_portfolio_delta_after_hedge": projected_after_hedge,
        "total_option_premium": quantity * entry_price * candidate["multiplier"],
        "total_entry_commissions": (
            quantity * 2 * OPTION_COMMISSION_PER_CONTRACT_PER_SIDE
        ),
        "estimated_round_trip_commissions": (
            quantity * 4 * OPTION_COMMISSION_PER_CONTRACT_PER_SIDE
        ),
        "estimated_rtm_open_close_fees": (
            2 * abs(rtm_trade_quantity) * RTM_FEE_PER_SHARE_PER_SIDE
        ),
        "rehedge_buffer": quantity * REHEDGE_COST_BUFFER_PER_STRADDLE,
        "total_base_model_edge": quantity * candidate["base_edge_dollars"],
        "total_stress_model_edge": quantity * candidate["robust_edge_dollars"],
        "risk_block": abs(projected_before_hedge) > DELTA_HARD_SAFETY_LEVEL,
    }


def detect_existing_positions(straddles):
    positions, warnings = [], []
    for row in straddles:
        call_position, put_position = row["call_position"], row["put_position"]
        if call_position > 0 and put_position > 0:
            direction = "LONG"
        elif call_position < 0 and put_position < 0:
            direction = "SHORT"
        else:
            direction = None
        if direction:
            matched = min(abs(call_position), abs(put_position))
            positions.append({**row, "direction": direction, "matched_quantity": matched})
            if abs(call_position) != abs(put_position):
                warnings.append(
                    f"Strike {row['strike']:g}: unmatched option quantity "
                    f"(call {call_position:g}, put {put_position:g})."
                )
        elif call_position or put_position:
            warnings.append(
                f"Strike {row['strike']:g}: unmatched option positions "
                f"(call {call_position:g}, put {put_position:g})."
            )
    return positions, warnings


def captured_fraction(position):
    call_vwap, put_vwap = position.get("call_vwap"), position.get("put_vwap")
    if call_vwap is None or put_vwap is None or call_vwap <= 0 or put_vwap <= 0:
        return None
    entry_price = call_vwap + put_vwap
    if position["direction"] == "LONG":
        current = position["combined_bid"]
        target = position["estimated_long_exit_bid_base"]
        denominator = target - entry_price
        numerator = current - entry_price
    else:
        current = position["combined_ask"]
        target = position["estimated_short_cover_ask_base"]
        denominator = entry_price - target
        numerator = entry_price - current
    return numerator / denominator if denominator > 0 else None


def exit_reasons(position, tick):
    reasons = []
    captured = captured_fraction(position)
    if captured is not None and captured >= EXIT_CAPTURE_FRACTION:
        reasons.append(f"captured opportunity {captured:.1%}")
    supported = (position["long_stress_edge_dollars"] > 0
                 if position["direction"] == "LONG"
                 else position["short_stress_edge_dollars"] > 0)
    if not supported:
        reasons.append("current model no longer supports the position direction")
    if tick >= FORCE_EXIT_TICK:
        reasons.append(f"force-exit tick {FORCE_EXIT_TICK} reached")
    return reasons, captured


def append_event(record, path=ALERT_LOG_PATH):
    """Append a sanitized event; logging errors never stop analysis."""
    safe = {field: record.get(field, "") for field in LOG_FIELDS}
    safe["timestamp_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        exists = path.exists() and path.stat().st_size > 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(safe)
    except (OSError, csv.Error):
        pass


@dataclass
class LockedPlan:
    direction: str
    strike: float
    created_tick: float
    created_news_identity: str
    lock_until_tick: float
    quantity: int
    initial_robust_edge: float
    consecutive_invalid_ticks: int = 0
    last_invalid_tick: float | None = None


@dataclass
class RuntimeState:
    previous_period: object = None
    previous_tick: float | None = None
    have_previous: bool = False
    seen_news_ids: set = field(default_factory=set)
    seen_exit_fingerprints: set = field(default_factory=set)
    last_fingerprint: tuple | None = None
    last_status_tick: float | None = None
    locked_plan: LockedPlan | None = None
    accepted_news_identity: tuple | None = None
    pending_news_identity: tuple | None = None
    news_detected_tick: float | None = None
    last_plan_executable: bool | None = None
    last_position_state: str = "EMPTY"
    last_position_signature: tuple = ()
    last_hedge_required: bool = False

    def clear_period_state(self):
        self.seen_news_ids.clear()
        self.seen_exit_fingerprints.clear()
        self.last_fingerprint = None
        self.last_status_tick = None
        self.locked_plan = None
        self.accepted_news_identity = None
        self.pending_news_identity = None
        self.news_detected_tick = None
        self.last_plan_executable = None
        self.last_position_state = "EMPTY"
        self.last_position_signature = ()
        self.last_hedge_required = False

    def new_news(self, items):
        fresh = [item for item in items if news_identity(item) not in self.seen_news_ids]
        self.seen_news_ids.update(news_identity(item) for item in items)
        return fresh

    def should_alert(self, fingerprint):
        changed = fingerprint != self.last_fingerprint
        self.last_fingerprint = fingerprint
        return changed

    def should_exit_alert(self, fingerprint):
        if fingerprint in self.seen_exit_fingerprints:
            return False
        self.seen_exit_fingerprints.add(fingerprint)
        return True


def event_record(snapshot, event_type, message, candidate=None, recommendation=None):
    candidate = candidate or {}
    return {
        "period": snapshot.get("period"), "tick": snapshot.get("tick"),
        "event_type": event_type, "action": snapshot.get("action", "WAIT"),
        "strike": candidate.get("strike", ""), "rtm_spot": snapshot.get("spot", ""),
        "effective_vol_low": snapshot.get("vols", ("", "", ""))[0],
        "effective_vol_mid": snapshot.get("vols", ("", "", ""))[1],
        "effective_vol_high": snapshot.get("vols", ("", "", ""))[2],
        "stress_low": snapshot.get("stress_low", ""),
        "stress_high": snapshot.get("stress_high", ""),
        "robust_edge_dollars": candidate.get("robust_edge_dollars", ""),
        "edge_return": candidate.get("robust_edge_return", ""),
        "call_bid": candidate.get("call_bid", ""), "call_ask": candidate.get("call_ask", ""),
        "put_bid": candidate.get("put_bid", ""), "put_ask": candidate.get("put_ask", ""),
        "current_portfolio_delta": snapshot.get("current_portfolio_delta", ""),
        "projected_portfolio_delta": (
            recommendation.get("projected_portfolio_delta_before_hedge", "")
            if recommendation else ""
        ),
        "message": message,
    }


def evaluate_active_snapshot(case, securities, news):
    """Calculate one active snapshot without producing output or side effects."""
    tick = valid_valuation_tick(case)
    if tick is None:
        return {"action": "WAIT", "reason": "invalid or expired tick", "tick": case.get("tick")}
    if not isinstance(securities, list) or not isinstance(news, list):
        return {"action": "WAIT", "reason": "missing security or news data", "tick": tick}
    if not all(isinstance(security, dict) for security in securities):
        return {"action": "WAIT", "reason": "malformed security data", "tick": tick}
    rtms = [security for security in securities if security.get("ticker") == "RTM"]
    if len(rtms) != 1:
        return {"action": "WAIT", "reason": "expected exactly one RTM security", "tick": tick}
    rtm = rtms[0]
    try:
        spot = underlying_spot(rtm)
        ranges, sources, news_warnings = build_weekly_vol_ranges(
            news, case.get("period"), tick
        )
        vols = effective_volatilities(tick, ranges)
        stress_low, stress_high, unknown_weeks = calculate_stress_volatilities(
            tick, ranges, sources
        )
        rows, data_warnings = build_option_rows(
            securities, spot, (EXPIRY_TICK - tick) / TICKS_PER_YEAR,
            vols, stress_low, stress_high,
        )
    except (ValueError, ArithmeticError, KeyError, TypeError) as exc:
        return {"action": "WAIT", "reason": f"data warning: {exc}", "tick": tick}
    straddles = build_straddles(rows, spot)
    candidates = entry_candidates(straddles, tick)
    current_option_delta = option_delta_exposure(rows)
    current_rtm_position = number(rtm.get("position"))
    if current_rtm_position is None:
        return {"action": "WAIT", "reason": "invalid RTM position", "tick": tick}
    current_portfolio_delta = current_option_delta + current_rtm_position
    for candidate in candidates:
        sign = 1 if candidate["direction"] == "LONG" else -1
        quantity = (SIMULATED_LONG_STRADDLE_QTY if sign == 1
                    else SIMULATED_SHORT_STRADDLE_QTY)
        candidate["projected_delta_for_ranking"] = (
            current_portfolio_delta
            + sign * quantity * candidate["delta_shares_per_long_straddle"]
        )
    selected, shortlist, max_edge, cutoff = select_candidate(candidates)
    recommendation = None
    action = "WAIT"
    reason = "no candidate passes the completed-trade thresholds"
    if tick >= NO_NEW_ENTRY_TICK:
        reason = f"new entries disabled at tick {NO_NEW_ENTRY_TICK}"
    elif selected is not None and not security_is_tradeable(rtm):
        reason = "RTM is not tradeable"
    elif selected is not None:
        recommendation = simulated_entry(selected, current_option_delta, current_rtm_position)
        if recommendation["risk_block"]:
            action, reason = "RISK BLOCK", "projected pre-hedge delta exceeds hard safety level"
        else:
            action = f"{selected['direction']} VOL"
            reason = "candidate passes completed-trade edge and return thresholds"
    if data_warnings and action in {"LONG VOL", "SHORT VOL", "RISK BLOCK"}:
        action = "WAIT"
        reason = "required option data is incomplete or ambiguous"
        recommendation = None
        selected = None

    existing_positions, position_warnings = detect_existing_positions(straddles)
    exit_alerts = []
    for position in existing_positions:
        reasons, captured = exit_reasons(position, tick)
        if reasons:
            exit_alerts.append({"position": position, "reasons": reasons, "captured": captured})
    if tick >= FORCE_EXIT_TICK:
        matched_by_ticker = {}
        for position in existing_positions:
            sign = 1 if position["direction"] == "LONG" else -1
            matched = sign * position["matched_quantity"]
            matched_by_ticker[position["call_ticker"]] = matched
            matched_by_ticker[position["put_ticker"]] = matched
        unmatched = []
        for row in rows:
            residual = row["position"] - matched_by_ticker.get(row["ticker"], 0)
            if residual:
                if row.get("tradeable", True):
                    unmatched.append((row["ticker"], residual))
                else:
                    position_warnings.append(
                        f"{row['ticker']}: nonzero position is not currently tradeable."
                    )
        if unmatched:
            exit_alerts.append({
                "position": None, "manual_options": unmatched,
                "reasons": [f"force-exit tick {FORCE_EXIT_TICK} reached with unmatched options"],
                "captured": None,
            })
    if tick >= FORCE_EXIT_TICK and current_rtm_position and not existing_positions:
        exit_alerts.append({
            "position": None,
            "reasons": [f"force-exit tick {FORCE_EXIT_TICK} reached with nonzero RTM position"],
            "captured": None,
        })
    applicable_news = applicable_volatility_news(news, case.get("period"), tick)
    return {
        "period": case.get("period"), "tick": tick, "status": "ACTIVE",
        "action": action, "reason": reason, "spot": spot,
        "ranges": ranges, "sources": sources, "vols": vols,
        "stress_low": stress_low, "stress_high": stress_high,
        "unknown_weeks": unknown_weeks, "option_rows": rows,
        "reported_option_positions": tuple(sorted(
            (security.get("ticker"), number(security.get("position")))
            for security in securities
            if identify_option(security.get("ticker")) is not None
            and number(security.get("position")) not in (None, 0)
        )),
        "straddles": straddles, "candidates": candidates,
        "selected": selected, "shortlist": shortlist,
        "max_edge": max_edge, "cutoff": cutoff,
        "recommendation": recommendation,
        "current_option_delta": current_option_delta,
        "current_rtm_position": current_rtm_position,
        "current_portfolio_delta": current_portfolio_delta,
        "rtm_tradeable": security_is_tradeable(rtm),
        "applicable_news": applicable_news,
        "existing_positions": existing_positions,
        "exit_alerts": exit_alerts,
        "warnings": news_warnings + data_warnings + position_warnings,
    }


def normalize_news_text(value):
    return " ".join(str(value or "").split()).casefold()


def volatility_news_identity(items):
    """Build an order-independent identity from parsed, eligible news only."""
    records = []
    for item in items:
        if not isinstance(item, dict) or parse_volatility_range(item.get("body")) is None:
            continue
        headline = normalize_news_text(item.get("headline"))
        initial = headline == normalize_news_text(INITIAL_HEADLINE)
        news_tick = _integer(item.get("tick"))
        if news_tick is None and initial and item.get("tick") is None:
            news_tick = 0
        if news_tick is None:
            continue
        news_id = str(item.get("news_id", "")) if item.get("news_id") is not None else ""
        records.append((
            news_id, news_tick, headline, normalize_news_text(item.get("body")),
        ))
    return tuple(sorted(
        set(records), key=lambda record: (record[1], record[0], record[2], record[3])
    ))


def build_news_identity(news, current_period, current_tick):
    """Filter by period/time and then construct the stable news identity."""
    return volatility_news_identity(
        applicable_volatility_news(news, current_period, current_tick)
    )


def option_position_state(option_rows):
    """Classify actual API positions before considering any fresh entry."""
    positions = tuple(sorted(
        (row["ticker"], row["position"])
        for row in option_rows if row["position"] != 0
    ))
    if not positions:
        return "EMPTY", positions
    pairs = {}
    for row in option_rows:
        kind, strike = identify_option(row["ticker"])
        pairs.setdefault(strike, {})[kind] = row["position"]
    for pair in pairs.values():
        call_position = pair.get("c", 0)
        put_position = pair.get("p", 0)
        if ((call_position == 0) != (put_position == 0)
                or call_position * put_position < 0):
            return "PARTIAL_FILL", positions
    return "POSITION_MANAGEMENT", positions


def find_plan_candidate(snapshot, plan):
    for row in snapshot["straddles"]:
        if row["strike"] == plan.strike:
            return direction_candidate(row, plan.direction)
    return None


def create_locked_plan(candidate, snapshot):
    latest_news = snapshot.get("applicable_news", [])
    identity = news_identity(latest_news[-1]) if latest_news else ""
    quantity = (SIMULATED_LONG_STRADDLE_QTY
                if candidate["direction"] == "LONG"
                else SIMULATED_SHORT_STRADDLE_QTY)
    return LockedPlan(
        direction=candidate["direction"], strike=candidate["strike"],
        created_tick=snapshot["tick"], created_news_identity=identity,
        lock_until_tick=snapshot["tick"] + PLAN_LOCK_TICKS,
        quantity=quantity, initial_robust_edge=candidate["robust_edge_dollars"],
    )


def update_locked_plan(snapshot, state):
    """Update execution state while leaving the current valuation untouched."""
    tick = snapshot["tick"]
    current_identity = volatility_news_identity(snapshot.get("applicable_news", []))

    # Startup accepts all currently eligible news once. Derived volatilities,
    # spot, quotes, and time-to-expiry never participate in this identity.
    if state.accepted_news_identity is None and state.pending_news_identity is None:
        state.accepted_news_identity = current_identity
    elif current_identity != state.accepted_news_identity:
        if current_identity != state.pending_news_identity:
            had_plan = state.locked_plan is not None
            state.pending_news_identity = current_identity
            state.news_detected_tick = tick
            state.locked_plan = None
            state.last_plan_executable = None
            return {"event": "INVALIDATED_NEWS" if had_plan else "NEWS_SETTLE",
                    "executable": False}
        if tick < state.news_detected_tick + NEWS_SETTLE_TICKS:
            return {"event": "SETTLING", "executable": False}
        state.accepted_news_identity = state.pending_news_identity
        state.pending_news_identity = None
        state.news_detected_tick = None
    elif state.pending_news_identity is not None:
        # The feed reverted to the already accepted set before settlement.
        state.pending_news_identity = None
        state.news_detected_tick = None

    if not snapshot.get("rtm_tradeable", True):
        event = "INVALIDATED_UNTRADEABLE" if state.locked_plan is not None else "WAIT"
        state.locked_plan = None
        state.last_plan_executable = None
        return {"event": event, "executable": False}

    if tick >= NO_NEW_ENTRY_TICK:
        event = "ENTRY_WINDOW_CLOSED" if state.locked_plan is not None else "WAIT"
        state.locked_plan = None
        state.last_plan_executable = None
        return {"event": event, "executable": False}

    if state.locked_plan is None:
        candidate = snapshot.get("selected")
        if candidate is None:
            return {"event": "WAIT", "executable": False}
        recommendation = simulated_entry(
            candidate, snapshot["current_option_delta"], snapshot["current_rtm_position"]
        )
        if recommendation["risk_block"]:
            return {"event": "RISK_BLOCK", "candidate": candidate,
                    "recommendation": recommendation, "executable": False}
        state.locked_plan = create_locked_plan(candidate, snapshot)
        state.last_plan_executable = True
        return {"event": "CREATED", "plan": state.locked_plan,
                "candidate": candidate, "recommendation": recommendation,
                "executable": True}

    plan = state.locked_plan
    candidate = find_plan_candidate(snapshot, plan)
    if candidate is None:
        state.locked_plan = None
        state.last_plan_executable = None
        return {"event": "INVALIDATED_UNTRADEABLE", "executable": False}

    passes = candidate_passes_thresholds(candidate, tick)
    if not passes:
        if plan.last_invalid_tick != tick:
            plan.consecutive_invalid_ticks = (
                plan.consecutive_invalid_ticks + 1
                if plan.last_invalid_tick is not None
                and tick == plan.last_invalid_tick + 1
                else 1
            )
            plan.last_invalid_tick = tick
        if plan.consecutive_invalid_ticks >= INVALID_EDGE_CONFIRMATIONS:
            state.locked_plan = None
            state.last_plan_executable = None
            return {"event": "INVALIDATED_EDGE", "candidate": candidate,
                    "executable": False}
    else:
        plan.consecutive_invalid_ticks = 0
        plan.last_invalid_tick = None

    recommendation = simulated_entry(
        candidate, snapshot["current_option_delta"], snapshot["current_rtm_position"]
    )
    executable = passes and not recommendation["risk_block"]

    if tick >= plan.lock_until_tick:
        challengers = [
            row for row in snapshot["candidates"]
            if (row["direction"], row["strike"]) != (plan.direction, plan.strike)
        ]
        challenger, _, _, _ = select_candidate(challengers)
        if (challenger is not None
                and challenger["robust_edge_dollars"]
                >= candidate["robust_edge_dollars"] * (1 + CHALLENGER_RELATIVE_IMPROVEMENT)
                and challenger["robust_edge_dollars"]
                >= candidate["robust_edge_dollars"] + CHALLENGER_ABSOLUTE_IMPROVEMENT):
            challenger_recommendation = simulated_entry(
                challenger, snapshot["current_option_delta"], snapshot["current_rtm_position"]
            )
            if not challenger_recommendation["risk_block"]:
                old_plan = plan
                state.locked_plan = create_locked_plan(challenger, snapshot)
                state.last_plan_executable = True
                return {"event": "REPLACED", "old_plan": old_plan,
                        "plan": state.locked_plan, "candidate": challenger,
                        "recommendation": challenger_recommendation,
                        "executable": True}

    transition = (state.last_plan_executable is not None
                  and executable != state.last_plan_executable)
    state.last_plan_executable = executable
    return {"event": "EXECUTABILITY_CHANGED" if transition else "LOCKED",
            "plan": plan, "candidate": candidate,
            "recommendation": recommendation, "executable": executable}


def actual_position_hedge(snapshot):
    target = round(-snapshot["current_option_delta"])
    trade = target - snapshot["current_rtm_position"]
    return {
        "target_rtm_position": target,
        "rtm_trade_quantity": trade,
        "current_portfolio_delta": snapshot["current_portfolio_delta"],
        "available": snapshot.get("rtm_tradeable", True),
        "hedge_required": (snapshot.get("rtm_tradeable", True)
                           and abs(snapshot["current_portfolio_delta"])
                           > DELTA_WARNING_LEVEL),
    }


def recommendation_fingerprint(snapshot):
    selected = snapshot.get("selected") or {}
    latest_news = snapshot.get("applicable_news", [])
    latest_id = news_identity(latest_news[-1]) if latest_news else ""
    return (
        snapshot.get("period"), latest_id, snapshot.get("action"),
        selected.get("strike"), snapshot.get("action") in {"LONG VOL", "SHORT VOL"},
    )


def compact_status(snapshot):
    return (f"Tick {snapshot['tick']:g} | ACTIVE | RTM {snapshot['spot']:.2f} | "
            f"EffVol {snapshot['vols'][1]:.1%} | {snapshot['action']}")


def print_entry_alert(snapshot):
    candidate = snapshot["selected"]
    recommendation = snapshot["recommendation"]
    direction = candidate["direction"]
    verb = "BUY" if direction == "LONG" else "SELL"
    price_label = "ask <=" if direction == "LONG" else "bid >="
    call_price = candidate["call_ask"] if direction == "LONG" else candidate["call_bid"]
    put_price = candidate["put_ask"] if direction == "LONG" else candidate["put_bid"]
    hedge = recommendation["rtm_trade_quantity"]
    print("\a" + "=" * 60)
    print(f"{direction} VOL OPPORTUNITY — MANUAL CONFIRMATION REQUIRED")
    print(f"Tick: {snapshot['tick']:g}")
    print(f"Volatility update: {news_brief(snapshot['applicable_news'][-1] if snapshot['applicable_news'] else None)}")
    print(f"{verb} {recommendation['quantity']} {candidate['call_ticker']} "
          f"at current {price_label} {call_price:.2f}")
    print(f"{verb} {recommendation['quantity']} {candidate['put_ticker']} "
          f"at current {price_label} {put_price:.2f}")
    if hedge:
        print(f"Then {'BUY' if hedge > 0 else 'SELL'} {abs(hedge):g} RTM "
              "based on current projected delta")
    else:
        print("No initial RTM trade suggested by the current rounded delta")
    print(f"Robust round-trip edge: ${candidate['robust_edge_dollars']:.2f} per straddle")
    print(f"Robust edge return: {candidate['robust_edge_return']:.1%}")
    print(f"Total option premium {'paid' if direction == 'LONG' else 'received'}: "
          f"${recommendation['total_option_premium']:.2f}")
    print(f"Entry commissions: ${recommendation['total_entry_commissions']:.2f}; "
          f"complete round-trip commissions: "
          f"${recommendation['estimated_round_trip_commissions']:.2f}")
    print(f"Estimated RTM opening/closing fees: "
          f"${recommendation['estimated_rtm_open_close_fees']:.2f}; "
          f"rehedge buffer: ${recommendation['rehedge_buffer']:.2f}")
    print(f"Total base model edge: ${recommendation['total_base_model_edge']:.2f}; "
          f"total stress model edge: ${recommendation['total_stress_model_edge']:.2f}")
    print(f"Projected portfolio delta before hedge: "
          f"{recommendation['projected_portfolio_delta_before_hedge']:.2f}; "
          f"after rounded hedge: {recommendation['projected_portfolio_delta_after_hedge']:.2f}")
    print("Model estimates are not guaranteed profit.")
    print("Valid for this snapshot only. Recheck quotes before submitting manually.")
    print("NO ORDERS HAVE BEEN SUBMITTED.")
    print("=" * 60, flush=True)


def print_risk_block(snapshot):
    recommendation = snapshot.get("recommendation") or {}
    print("\a" + "=" * 60)
    print("RISK BLOCK — NO NEW ENTRY")
    print(snapshot["reason"])
    projected = recommendation.get("projected_portfolio_delta_before_hedge")
    if projected is not None:
        print(f"Projected portfolio delta before hedge: {projected:.2f}")
    print("NO ORDERS HAVE BEEN SUBMITTED.")
    print("=" * 60, flush=True)


def print_exit_alert(snapshot, alert):
    position = alert["position"]
    print("\a" + "=" * 60)
    print("EXIT ALERT — MANUAL CONFIRMATION REQUIRED")
    print("Reason: " + "; ".join(alert["reasons"]))
    if position is not None:
        verb = "SELL" if position["direction"] == "LONG" else "BUY"
        quantity = position["matched_quantity"]
        print(f"{verb} {quantity:g} {position['call_ticker']} using a current executable quote")
        print(f"{verb} {quantity:g} {position['put_ticker']} using a current executable quote")
    for ticker, quantity in alert.get("manual_options", []):
        print(f"{'SELL' if quantity > 0 else 'BUY'} {abs(quantity):g} {ticker} "
              "using a current executable quote")
    print("After option legs close, recalculate and manually flatten remaining RTM exposure.")
    print("Do not assume fills. NO ORDERS HAVE BEEN SUBMITTED.")
    print("=" * 60, flush=True)


def print_verbose(snapshot):
    print("Volatility schedule:")
    print("Week | Ticks   | Low   | Mid   | High  | Source")
    for week in range(4):
        low, high = snapshot["ranges"][week]
        print(f"{week + 1:>4} | {week * 75:>3}-{week * 75 + 74:<3} | "
              f"{low:>5.1%} | {(low + high) / 2:>5.1%} | {high:>5.1%} | "
              f"{snapshot['sources'][week]}")
    print(f"Effective volatility: low={snapshot['vols'][0]:.4%} "
          f"mid={snapshot['vols'][1]:.4%} high={snapshot['vols'][2]:.4%}; "
          f"stress_low={snapshot['stress_low']:.4%} "
          f"stress_high={snapshot['stress_high']:.4%}")
    if snapshot["unknown_weeks"]:
        weeks = ", ".join(str(week + 1) for week in snapshot["unknown_weeks"])
        print(f"Unknown future weeks {weeks} use {UNKNOWN_WEEK_VOL_RANGE[0]:.0%}-"
              f"{UNKNOWN_WEEK_VOL_RANGE[1]:.0%} for sensitivity only, not forecasts.")
    print("Applicable volatility news:")
    for item in snapshot["applicable_news"]:
        print(f"[{item.get('news_id', '')}] {item.get('headline', '')}: {item.get('body', '')}")
    print("Options:")
    print("ticker | bid | ask | pos | delta | fair_low | fair_mid | fair_high | stress_low | stress_high")
    for row in snapshot["option_rows"]:
        print(f"{row['ticker']} | {row['bid']} | {row['ask']} | {row['position']} | "
              f"{row['delta']:.4f} | {row['fair_low']:.4f} | {row['fair_mid']:.4f} | "
              f"{row['fair_high']:.4f} | {row['stress_low']:.4f} | {row['stress_high']:.4f}")
    print("Straddles (round-trip model estimates):")
    print("strike | bid | ask | spread | long_base | long_stress | short_base | short_stress | delta")
    for row in sorted(snapshot["straddles"], key=lambda value: value["strike"]):
        print(f"{row['strike']:g} | {row['combined_bid']:.4f} | {row['combined_ask']:.4f} | "
              f"{row['combined_spread']:.4f} | {row['long_base_edge_dollars']:.2f} | "
              f"{row['long_stress_edge_dollars']:.2f} | {row['short_base_edge_dollars']:.2f} | "
              f"{row['short_stress_edge_dollars']:.2f} | "
              f"{row['delta_shares_per_long_straddle']:.2f}")
    for warning in snapshot["warnings"]:
        print(f"DATA WARNING: {warning}")


def print_locked_plan_block(snapshot, plan_update, title):
    candidate = plan_update["candidate"]
    recommendation = plan_update["recommendation"]
    plan = plan_update["plan"]
    direction = plan.direction
    verb = "BUY" if direction == "LONG" else "SELL"
    price_label = "ask <=" if direction == "LONG" else "bid >="
    call_price = candidate["call_ask"] if direction == "LONG" else candidate["call_bid"]
    put_price = candidate["put_ask"] if direction == "LONG" else candidate["put_bid"]
    hedge = recommendation["rtm_trade_quantity"]
    print("\a" + "=" * 60)
    print(title)
    print(f"Tick: {snapshot['tick']:g} | Locked through tick: {plan.lock_until_tick:g}")
    if plan_update["executable"]:
        print(f"{verb} {plan.quantity} {candidate['call_ticker']} at current {price_label} {call_price:.2f}")
        print(f"{verb} {plan.quantity} {candidate['put_ticker']} at current {price_label} {put_price:.2f}")
    else:
        print("NOT EXECUTABLE — do not enter this plan at the current edge.")
        print(f"Current quotes: {candidate['call_ticker']} bid/ask "
              f"{candidate['call_bid']:.2f}/{candidate['call_ask']:.2f}; "
              f"{candidate['put_ticker']} bid/ask "
              f"{candidate['put_bid']:.2f}/{candidate['put_ask']:.2f}")
    print(f"Current robust round-trip edge: ${candidate['robust_edge_dollars']:.2f}; "
          f"return: {candidate['robust_edge_return']:.1%}")
    if hedge:
        print(f"Current post-fill hedge estimate: "
              f"{'BUY' if hedge > 0 else 'SELL'} {abs(hedge):g} RTM")
    else:
        print("Current post-fill hedge estimate: no RTM trade")
    if plan_update["executable"]:
        print("After both option legs are confirmed filled, refresh positions and hedge "
              "the resulting portfolio delta.")
        print("Do not submit the RTM hedge before both option fills are confirmed.")
    print("Manual/display-only instructions. NO ORDERS HAVE BEEN SUBMITTED.")
    print("=" * 60, flush=True)


def print_plan_invalidation(snapshot, event):
    reasons = {
        "INVALIDATED_NEWS": "volatility news changed the effective model",
        "INVALIDATED_UNTRADEABLE": "the locked option pair is no longer tradeable",
        "INVALIDATED_EDGE": "the locked edge failed the threshold twice",
        "ENTRY_WINDOW_CLOSED": "the new-entry window has closed",
    }
    print("\a" + "=" * 60)
    print("LOCKED PLAN INVALIDATED — NO NEW ENTRY INSTRUCTIONS")
    print(f"Tick {snapshot['tick']:g}: {reasons.get(event, event)}.")
    print("NO ORDERS HAVE BEEN SUBMITTED.")
    print("=" * 60, flush=True)


def compact_locked_status(snapshot, plan_update):
    plan = plan_update.get("plan")
    candidate = plan_update.get("candidate")
    if plan is None or candidate is None:
        return (f"Tick {snapshot['tick']:g} | ACTIVE | RTM {snapshot['spot']:.2f} | "
                f"EffVol {snapshot['vols'][1]:.1%} | WAIT")
    state = "EXECUTABLE" if plan_update["executable"] else "NOT EXECUTABLE"
    return (f"Tick {snapshot['tick']:g} | LOCKED {plan.direction} {plan.strike:g} | "
            f"{state} | Edge ${candidate['robust_edge_dollars']:.2f} | "
            f"Delta {plan_update['recommendation']['projected_portfolio_delta_before_hedge']:.1f}")


def print_position_management(snapshot, position_state, hedge):
    print("\a" + "=" * 60)
    if position_state == "PARTIAL_FILL":
        print("PARTIAL FILL — DO NOT OPEN ANOTHER STRADDLE; manage the filled leg first.")
    else:
        print("POSITION MANAGEMENT — EXISTING OPTION POSITIONS DETECTED")
    print(f"Actual option delta: {snapshot['current_option_delta']:.2f}; "
          f"actual portfolio delta: {snapshot['current_portfolio_delta']:.2f}")
    for position in snapshot.get("existing_positions", []):
        captured = captured_fraction(position)
        current_price = (position["combined_bid"] if position["direction"] == "LONG"
                         else position["combined_ask"])
        price_label = "liquidation bid" if position["direction"] == "LONG" else "cover ask"
        captured_text = (f"; captured estimate {captured:.1%}"
                         if captured is not None else "; captured estimate unavailable")
        print(f"{position['direction']} {position['matched_quantity']:g} straddle(s) "
              f"at strike {position['strike']:g}: current {price_label} "
              f"{current_price:.4f}{captured_text}")
    if not hedge.get("available", True):
        reason = hedge.get("unavailable_reason", "RTM is not tradeable")
        print(f"DATA WARNING — {reason}; no hedge instruction generated.")
    elif hedge["hedge_required"]:
        trade = hedge["rtm_trade_quantity"]
        if trade:
            print(f"Manual RTM hedge only: {'BUY' if trade > 0 else 'SELL'} "
                  f"{abs(trade):g} RTM based on API-reported positions.")
        else:
            print("The actual RTM position already matches the rounded delta target.")
    else:
        print(f"No RTM hedge instruction: absolute delta is below {DELTA_WARNING_LEVEL:g}.")
    print("Refresh API positions after every manual fill. NO ORDERS HAVE BEEN SUBMITTED.")
    print("=" * 60, flush=True)


def process_snapshot(case, securities, news, state, log_path=ALERT_LOG_PATH):
    """Evaluate and display one already-fetched snapshot."""
    status = normalized_status(case)
    if status != "ACTIVE":
        state.locked_plan = None
        state.last_plan_executable = None
        print(INACTIVE_MESSAGE, flush=True)
        return {"action": "WAIT", "status": status}
    if valid_valuation_tick(case) is None:
        state.locked_plan = None
        state.last_plan_executable = None
        print("WAIT — invalid or expired tick; no valuation or recommendation generated.")
        return {"action": "WAIT", "status": status}
    snapshot = evaluate_active_snapshot(case, securities, news)
    if "spot" not in snapshot:
        print(f"DATA WARNING — {snapshot['reason']}")
        return snapshot

    fresh_news = state.new_news(snapshot["applicable_news"])
    for item in fresh_news:
        message = news_brief(item)
        print(f"\aVOLATILITY NEWS — {message}")
        append_event(event_record(snapshot, "NEWS", message), log_path)

    position_state, position_signature = option_position_state(snapshot["option_rows"])
    reported_positions = snapshot.get("reported_option_positions", position_signature)
    incomplete_position_data = bool(
        reported_positions and reported_positions != position_signature
    )
    if incomplete_position_data:
        position_state = "PARTIAL_FILL"
        position_signature = reported_positions
        snapshot["warnings"].append(
            "A reported option position could not be fully valued; hedge estimates may be incomplete."
        )
    position_changed = position_signature != state.last_position_signature
    state.last_position_signature = position_signature
    state.last_position_state = position_state
    if position_state != "EMPTY":
        state.locked_plan = None
        state.last_plan_executable = None
        hedge = actual_position_hedge(snapshot)
        if incomplete_position_data:
            hedge["available"] = False
            hedge["hedge_required"] = False
            hedge["unavailable_reason"] = "option position data is incomplete"
        hedge_crossed = hedge["hedge_required"] != state.last_hedge_required
        state.last_hedge_required = hedge["hedge_required"]
        snapshot["action"] = position_state
        if position_changed or hedge_crossed:
            print_position_management(snapshot, position_state, hedge)
            append_event(event_record(
                snapshot, "POSITION_STATE", position_state,
            ), log_path)
        else:
            print(f"Tick {snapshot['tick']:g} | {position_state} | RTM {snapshot['spot']:.2f} | "
                  f"Portfolio delta {snapshot['current_portfolio_delta']:.1f}")
        plan_update = None
    else:
        state.last_hedge_required = False
        plan_update = update_locked_plan(snapshot, state)
        event = plan_update["event"]
        snapshot["selected"] = plan_update.get("candidate")
        snapshot["recommendation"] = plan_update.get("recommendation")
        snapshot["action"] = (
            f"LOCKED {plan_update['plan'].direction}"
            if plan_update.get("plan") else "WAIT"
        )
        if event in {"CREATED", "REPLACED"}:
            title = ("LOCKED PLAN CREATED — MANUAL CONFIRMATION REQUIRED"
                     if event == "CREATED"
                     else "LOCKED PLAN REPLACED — MATERIAL CHALLENGER")
            print_locked_plan_block(snapshot, plan_update, title)
            append_event(event_record(
                snapshot, "ENTRY_ALERT" if event == "CREATED" else "PLAN_REPLACED",
                event, plan_update["candidate"], plan_update["recommendation"],
            ), log_path)
        elif event in {"INVALIDATED_NEWS", "INVALIDATED_UNTRADEABLE",
                       "INVALIDATED_EDGE", "ENTRY_WINDOW_CLOSED"}:
            print_plan_invalidation(snapshot, event)
            if event == "INVALIDATED_NEWS":
                print(f"Tick {snapshot['tick']:g} | NEWS SETTLE | "
                      "waiting before locking a new plan")
            append_event(event_record(snapshot, "PLAN_INVALIDATED", event), log_path)
        elif event == "EXECUTABILITY_CHANGED":
            title = ("LOCKED PLAN IS EXECUTABLE"
                     if plan_update["executable"] else "LOCKED PLAN IS NOT EXECUTABLE")
            print_locked_plan_block(snapshot, plan_update, title)
        elif event == "RISK_BLOCK":
            snapshot["reason"] = "projected pre-hedge delta exceeds hard safety level"
            snapshot["recommendation"] = plan_update["recommendation"]
            print_risk_block(snapshot)
            append_event(event_record(
                snapshot, "RISK_BLOCK", snapshot["reason"],
                plan_update["candidate"], plan_update["recommendation"],
            ), log_path)
        elif event in {"NEWS_SETTLE", "SETTLING"}:
            print(f"Tick {snapshot['tick']:g} | NEWS SETTLE | waiting before locking a new plan")
        else:
            print(compact_locked_status(snapshot, plan_update), flush=True)

    for alert in snapshot["exit_alerts"]:
        position = alert.get("position") or {}
        latest_news = snapshot.get("applicable_news", [])
        latest_news_id = news_identity(latest_news[-1]) if latest_news else ""
        exit_fingerprint = (
            snapshot.get("period"), position.get("direction"),
            position.get("strike"), latest_news_id,
            any("force-exit" in reason for reason in alert["reasons"]),
            any("captured opportunity" in reason for reason in alert["reasons"]),
            any("no longer supports" in reason for reason in alert["reasons"]),
            tuple(alert.get("manual_options", [])),
        )
        if not state.should_exit_alert(exit_fingerprint):
            continue
        print_exit_alert(snapshot, alert)
        append_event(event_record(
            snapshot, "EXIT_ALERT", "; ".join(alert["reasons"]),
            alert.get("position"), None,
        ), log_path)

    warning_delta = max(
        abs(snapshot["current_portfolio_delta"]),
        abs((snapshot.get("recommendation") or {}).get(
            "projected_portfolio_delta_before_hedge", 0
        )),
    )
    if warning_delta > DELTA_WARNING_LEVEL:
        print(f"DELTA WARNING — absolute portfolio delta is {warning_delta:.2f}.")
    if position_state == "EMPTY" and plan_update is not None \
            and plan_update["event"] in {"CREATED", "REPLACED", "EXECUTABILITY_CHANGED"}:
        state.last_status_tick = snapshot["tick"]
    if RIT_VERBOSE or not COMPACT_MODE:
        print_verbose(snapshot)
    return snapshot


def main():
    print(BANNER)
    print("All trade instructions are manual/display-only. Model estimates are not guaranteed.")
    try:
        username, password = validate_configuration()
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return
    state = RuntimeState()
    try:
        with requests.Session() as session:
            session.auth = HTTPBasicAuth(username, password)
            while True:
                started = monotonic()
                try:
                    case = api_get(session, API_ENDPOINT, "case")
                    if not isinstance(case, dict):
                        raise ValueError("Expected /case object.")
                    if (state.have_previous
                            and reset_detected(state.previous_period, state.previous_tick, case)):
                        print(RESET_MESSAGE)
                        state.clear_period_state()
                        append_event({
                            "period": case.get("period"), "tick": case.get("tick"),
                            "event_type": "RESET", "action": "WAIT",
                            "message": RESET_MESSAGE,
                        })
                    state.previous_period = case.get("period")
                    state.previous_tick = number(case.get("tick"))
                    state.have_previous = True

                    if normalized_status(case) != "ACTIVE":
                        state.locked_plan = None
                        state.last_plan_executable = None
                        print(INACTIVE_MESSAGE, flush=True)
                    elif valid_valuation_tick(case) is None:
                        state.locked_plan = None
                        state.last_plan_executable = None
                        print("WAIT — invalid or expired tick; no valuation or recommendation generated.")
                    else:
                        securities = api_get(session, API_ENDPOINT, "securities")
                        news = api_get(session, API_ENDPOINT, "news")
                        process_snapshot(case, securities, news, state)
                except AuthenticationError as exc:
                    print(exc)
                    append_event({"event_type": "API_ERROR", "message": str(exc)})
                    break
                except (ApiError, ValueError, KeyError, TypeError) as exc:
                    message = str(exc) if isinstance(exc, (ApiError, ValueError)) else "invalid data shape"
                    print(f"API/DATA WARNING: {message}")
                    append_event({"event_type": "API_ERROR", "message": message})
                sleep(max(0.0, POLL_SECONDS - (monotonic() - started)))
    except KeyboardInterrupt:
        print("\nRead-only Case 1 assistant stopped cleanly.")


if __name__ == "__main__":
    main()
