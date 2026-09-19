"""Tests for the final read-only Case 1 assistant."""

import csv
import inspect

import pytest

import case1_final as final


def option_row(ticker, bid=1.0, ask=1.1, fair_mid=1.5, stress_low=1.6,
               stress_high=1.7, delta=0.6, position=0, vwap=1.0,
               tradeable=True):
    return {
        "ticker": ticker, "bid": bid, "ask": ask,
        "fair_low": fair_mid - 0.1, "fair_mid": fair_mid,
        "fair_high": fair_mid + 0.1, "stress_low": stress_low,
        "stress_high": stress_high, "delta": delta,
        "multiplier": 100, "position": position, "vwap": vwap,
        "tradeable": tradeable,
    }


def long_straddle():
    call = option_row("RTM50C", delta=0.6)
    put = option_row("RTM50P", delta=-0.4)
    return final.straddle_economics(call, put, 50.0)


def short_straddle():
    call = option_row("RTM50C", fair_mid=0.5, stress_low=0.4,
                      stress_high=0.4, delta=0.6)
    put = option_row("RTM50P", fair_mid=0.5, stress_low=0.4,
                     stress_high=0.4, delta=-0.4)
    return final.straddle_economics(call, put, 50.0)


def candidate(direction="LONG", strike=50, edge=40, distance=0.1,
              delta=20, edge_return=0.20, base=50, spread=0.2):
    return {
        "direction": direction, "strike": strike,
        "robust_edge_dollars": edge, "base_edge_dollars": base,
        "robust_edge_return": edge_return, "distance_from_spot": distance,
        "delta_shares_per_straddle": delta,
        "delta_shares_per_long_straddle": delta,
        "combined_spread": spread, "combined_ask": 2.0, "combined_bid": 1.8,
        "multiplier": 100, "call_ticker": f"RTM{strike}C",
        "put_ticker": f"RTM{strike}P", "call_bid": 0.8, "call_ask": 0.9,
        "put_bid": 1.0, "put_ask": 1.1,
    }


def test_complete_round_trip_long_edge_formula():
    row = long_straddle()
    expected_exit = row["fair_mid"] - row["combined_spread"] / 2
    expected = ((expected_exit - row["combined_ask"]) * 100
                - row["total_execution_cost_buffer"])
    assert row["long_base_edge_dollars"] == pytest.approx(expected)


def test_complete_round_trip_short_edge_formula():
    row = short_straddle()
    expected_cover = row["fair_mid"] + row["combined_spread"] / 2
    expected = ((row["combined_bid"] - expected_cover) * 100
                - row["total_execution_cost_buffer"])
    assert row["short_base_edge_dollars"] == pytest.approx(expected)


def test_four_option_commissions_per_round_trip():
    assert long_straddle()["option_round_trip_commissions"] == 8.0


def test_rtm_open_and_close_hedge_costs():
    row = long_straddle()
    assert row["rounded_hedge_shares"] == 20
    assert row["estimated_rtm_open_close_cost"] == pytest.approx(0.8)


def test_rehedge_cost_buffer():
    row = long_straddle()
    assert row["rehedge_cost_buffer"] == 1.0
    assert row["total_execution_cost_buffer"] == pytest.approx(9.8)


def test_long_vol_candidate_threshold():
    row = long_straddle()
    candidates = final.entry_candidates([row], 100)
    assert any(item["direction"] == "LONG" for item in candidates)


def test_short_vol_candidate_threshold():
    row = short_straddle()
    candidates = final.entry_candidates([row], 100)
    assert any(item["direction"] == "SHORT" for item in candidates)


def test_long_versus_short_direction_selection():
    long = candidate("LONG", edge=60)
    short = candidate("SHORT", edge=30)
    selected, _, _, _ = final.select_candidate([long, short])
    assert selected["direction"] == "LONG"


def test_95_percent_robust_shortlist():
    first = candidate(strike=48, edge=100, distance=1)
    second = candidate(strike=49, edge=96, distance=0.2)
    third = candidate(strike=50, edge=94, distance=0.1)
    _, shortlist, maximum, cutoff = final.select_candidate([first, second, third])
    assert maximum == 100
    assert cutoff == 95
    assert {row["strike"] for row in shortlist} == {48, 49}


def test_atm_preference_within_shortlist():
    far = candidate(strike=48, edge=100, distance=0.91, delta=22)
    near = candidate(strike=49, edge=96, distance=0.09, delta=1)
    selected, _, _, _ = final.select_candidate([far, near])
    assert selected["strike"] == 49


def test_portfolio_aware_hedge_for_long_straddle(monkeypatch):
    monkeypatch.setattr(final, "SIMULATED_LONG_STRADDLE_QTY", 10)
    result = final.simulated_entry(candidate(delta=20), 100, 50)
    assert result["projected_option_delta"] == 300
    assert result["target_rtm_position"] == -300
    assert result["rtm_trade_quantity"] == -350


def test_portfolio_aware_hedge_for_short_straddle(monkeypatch):
    monkeypatch.setattr(final, "SIMULATED_SHORT_STRADDLE_QTY", 3)
    result = final.simulated_entry(candidate("SHORT", delta=20), 100, 50)
    assert result["projected_option_delta"] == 40
    assert result["target_rtm_position"] == -40


def test_existing_rtm_position_is_included_in_hedge_trade():
    result = final.simulated_entry(candidate(delta=0), 100, 75)
    assert result["target_rtm_position"] == -100
    assert result["rtm_trade_quantity"] == -175


def test_delta_risk_block(monkeypatch):
    monkeypatch.setattr(final, "DELTA_HARD_SAFETY_LEVEL", 100)
    result = final.simulated_entry(candidate(delta=20), 0, 0)
    assert result["risk_block"] is True


def test_no_new_entry_at_tick_285():
    assert final.entry_candidates([long_straddle()], 285) == []


def test_forced_exit_alert_at_tick_290():
    position = {**long_straddle(), "direction": "LONG", "call_vwap": 1.0,
                "put_vwap": 1.0}
    reasons, _ = final.exit_reasons(position, 290)
    assert any("force-exit" in reason for reason in reasons)


def test_existing_long_straddle_detection():
    row = {**long_straddle(), "call_position": 5, "put_position": 4}
    positions, _ = final.detect_existing_positions([row])
    assert positions[0]["direction"] == "LONG"
    assert positions[0]["matched_quantity"] == 4


def test_existing_short_straddle_detection():
    row = {**short_straddle(), "call_position": -3, "put_position": -5}
    positions, _ = final.detect_existing_positions([row])
    assert positions[0]["direction"] == "SHORT"
    assert positions[0]["matched_quantity"] == 3


def test_matched_versus_unmatched_quantities_warn():
    row = {**long_straddle(), "call_position": 5, "put_position": 2}
    positions, warnings = final.detect_existing_positions([row])
    assert positions[0]["matched_quantity"] == 2
    assert "unmatched" in warnings[0]


@pytest.mark.parametrize(
    "direction,entry,current,target,expected",
    [("LONG", 2.0, 2.6, 3.0, 0.6), ("SHORT", 3.0, 2.4, 2.0, 0.6)],
)
def test_captured_fraction_using_vwap(direction, entry, current, target, expected):
    row = {
        "direction": direction, "call_vwap": entry / 2, "put_vwap": entry / 2,
        "combined_bid": current, "combined_ask": current,
        "estimated_long_exit_bid_base": target,
        "estimated_short_cover_ask_base": target,
    }
    assert final.captured_fraction(row) == pytest.approx(expected)


def test_stopped_case_has_no_actionable_output(capsys):
    state = final.RuntimeState()
    result = final.process_snapshot(
        {"tick": 0, "period": 1, "status": "STOPPED"},
        [{"ticker": "stale"}], [{"body": "stale"}], state,
    )
    output = capsys.readouterr().out
    assert result["action"] == "WAIT"
    assert "OPPORTUNITY" not in output and "SIMULATED" not in output


def test_future_news_is_ignored():
    news = [{"body": "volatility this week will be 31%", "tick": 75}]
    ranges, _, _ = final.build_weekly_vol_ranges(news, 1, 0)
    assert ranges == final.BASELINE_WEEKLY_VOL_RANGES


def test_wrong_period_news_is_ignored():
    news = [{"body": "volatility this week will be 31%", "tick": 0, "period": 2}]
    ranges, _, _ = final.build_weekly_vol_ranges(news, 1, 0)
    assert ranges == final.BASELINE_WEEKLY_VOL_RANGES


def test_nontradeable_leg_cannot_form_candidate():
    call = option_row("RTM50C", tradeable=False)
    put = option_row("RTM50P", delta=-0.4)
    assert final.straddle_economics(call, put, 50) is None


def test_alert_fingerprint_is_not_repeated():
    state = final.RuntimeState()
    fingerprint = (1, "7", "LONG VOL", 49, True)
    assert state.should_alert(fingerprint) is True
    assert state.should_alert(fingerprint) is False


def test_changed_news_or_strike_produces_new_alert():
    state = final.RuntimeState()
    assert state.should_alert((1, "7", "LONG VOL", 49, True))
    assert state.should_alert((1, "8", "LONG VOL", 49, True))
    assert state.should_alert((1, "8", "LONG VOL", 50, True))


def test_csv_logging_contains_no_credentials(tmp_path):
    path = tmp_path / "alerts.csv"
    final.append_event({
        "event_type": "TEST", "message": "safe",
        "username": "user-secret", "password": "password-secret",
        "authorization": "header-secret",
    }, path)
    text = path.read_text()
    assert "user-secret" not in text
    assert "password-secret" not in text
    assert "header-secret" not in text
    with path.open(newline="") as handle:
        assert next(csv.DictReader(handle))["message"] == "safe"


def test_final_source_is_read_only():
    source = inspect.getsource(final).casefold()
    forbidden_resource = "/" + "ord" + "ers"
    forbidden_method = "." + "po" + "st(" 
    assert forbidden_resource not in source
    assert forbidden_method not in source


def test_reset_clears_runtime_event_state():
    state = final.RuntimeState(seen_news_ids={"1"}, last_fingerprint=(1,),
                               last_status_tick=100)
    state.clear_period_state()
    assert state.seen_news_ids == set()
    assert state.last_fingerprint is None
    assert state.last_status_tick is None


def test_news_time_and_period_filtering_for_identity_list():
    news = [
        {"news_id": 1, "tick": 0, "period": 1,
         "body": "volatility this week will be 20%"},
        {"news_id": 2, "tick": 10, "period": 2,
         "body": "volatility this week will be 30%"},
        {"news_id": 3, "tick": 20, "period": 1,
         "body": "volatility this week will be 40%"},
    ]
    result = final.applicable_volatility_news(news, 1, 10)
    assert [item["news_id"] for item in result] == [1]


def plan_straddle(strike, edge, distance, delta=1.0, edge_return=0.25):
    return {
        "strike": strike, "distance_from_spot": distance,
        "call_ticker": f"RTM{strike:g}C", "put_ticker": f"RTM{strike:g}P",
        "call_bid": 0.9, "call_ask": 1.0, "put_bid": 0.9, "put_ask": 1.0,
        "combined_bid": 1.8, "combined_ask": 2.0, "combined_spread": 0.2,
        "multiplier": 100, "delta_shares_per_long_straddle": delta,
        "long_stress_edge_dollars": edge, "long_base_edge_dollars": edge + 10,
        "long_edge_return": edge_return,
        "short_stress_edge_dollars": -100, "short_base_edge_dollars": -90,
        "short_edge_return": -0.5,
    }


def plan_snapshot(tick, rows, vols=(0.2, 0.25, 0.3), news=None,
                  option_rows=None, option_delta=0, rtm_position=0):
    candidates = final.entry_candidates(rows, tick)
    selected, shortlist, maximum, cutoff = final.select_candidate(candidates)
    return {
        "period": 1, "tick": tick, "status": "ACTIVE", "spot": 49.0,
        "vols": vols, "stress_low": vols[0], "stress_high": vols[2],
        "applicable_news": news or [], "straddles": rows,
        "candidates": candidates, "selected": selected, "shortlist": shortlist,
        "max_edge": maximum, "cutoff": cutoff,
        "current_option_delta": option_delta,
        "current_rtm_position": rtm_position,
        "current_portfolio_delta": option_delta + rtm_position,
        "option_rows": option_rows or [], "exit_alerts": [], "warnings": [],
        "ranges": final.BASELINE_WEEKLY_VOL_RANGES,
        "sources": {week: "BASELINE" for week in range(4)},
        "unknown_weeks": [],
    }


def test_locked_strike_ignores_small_ranking_changes():
    state = final.RuntimeState()
    first = plan_snapshot(10, [
        plan_straddle(48, 100, 0.9, 20),
        plan_straddle(49, 96, 0.1, 2),
    ])
    assert final.update_locked_plan(first, state)["event"] == "CREATED"
    assert state.locked_plan.strike == 49
    second = plan_snapshot(11, [
        plan_straddle(48, 105, 0.1, 1),
        plan_straddle(49, 96, 0.9, 20),
    ])
    update = final.update_locked_plan(second, state)
    assert update["event"] == "LOCKED"
    assert state.locked_plan.strike == 49


def test_material_challenger_can_replace_after_lock():
    state = final.RuntimeState()
    first = plan_snapshot(10, [plan_straddle(49, 50, 0.1)])
    final.update_locked_plan(first, state)
    second = plan_snapshot(15, [
        plan_straddle(49, 50, 0.2), plan_straddle(48, 80, 0.1),
    ])
    update = final.update_locked_plan(second, state)
    assert update["event"] == "REPLACED"
    assert state.locked_plan.strike == 48


def test_new_volatility_model_invalidates_plan():
    state = final.RuntimeState()
    final.update_locked_plan(plan_snapshot(10, [plan_straddle(49, 50, 0.1)]), state)
    news = [{"news_id": 8, "tick": 11, "body": "volatility this week will be 30%"}]
    changed = plan_snapshot(11, [plan_straddle(49, 50, 0.1)],
                            vols=(0.3, 0.3, 0.3), news=news)
    update = final.update_locked_plan(changed, state)
    assert update["event"] == "INVALIDATED_NEWS"
    assert state.locked_plan is None
    assert state.pending_news_identity is not None
    assert state.news_detected_tick == 11


def test_two_distinct_invalid_ticks_required_to_unlock():
    state = final.RuntimeState()
    final.update_locked_plan(plan_snapshot(10, [plan_straddle(49, 50, 0.1)]), state)
    invalid_one = plan_snapshot(11, [plan_straddle(49, 5, 0.1, edge_return=0.01)])
    first = final.update_locked_plan(invalid_one, state)
    assert first["event"] == "EXECUTABILITY_CHANGED"
    assert state.locked_plan is not None
    same_tick = final.update_locked_plan(invalid_one, state)
    assert state.locked_plan is not None
    invalid_two = plan_snapshot(12, [plan_straddle(49, 5, 0.1, edge_return=0.01)])
    second = final.update_locked_plan(invalid_two, state)
    assert second["event"] == "INVALIDATED_EDGE"
    assert state.locked_plan is None


def test_option_positions_stop_new_entry(monkeypatch, tmp_path, capsys):
    row = option_row("RTM49C", position=1)
    snapshot = plan_snapshot(20, [plan_straddle(49, 50, 0.1)], option_rows=[row],
                             option_delta=40)
    monkeypatch.setattr(final, "evaluate_active_snapshot", lambda *args: snapshot)
    state = final.RuntimeState(locked_plan=final.create_locked_plan(
        final.direction_candidate(snapshot["straddles"][0], "LONG"), snapshot
    ))
    result = final.process_snapshot(
        {"tick": 20, "period": 1, "status": "ACTIVE"}, [], [], state,
        tmp_path / "events.csv",
    )
    output = capsys.readouterr().out
    assert result["action"] == "PARTIAL_FILL"
    assert state.locked_plan is None
    assert "DO NOT OPEN ANOTHER STRADDLE" in output


def test_partial_fill_state_warning():
    rows = [option_row("RTM49C", position=2), option_row("RTM49P", position=0)]
    state, positions = final.option_position_state(rows)
    assert state == "PARTIAL_FILL"
    assert positions == (("RTM49C", 2),)


def test_stopped_and_reset_clear_locked_plan(capsys):
    snapshot = plan_snapshot(10, [plan_straddle(49, 50, 0.1)])
    state = final.RuntimeState()
    final.update_locked_plan(snapshot, state)
    final.process_snapshot(
        {"tick": 11, "period": 1, "status": "STOPPED"}, [], [], state
    )
    assert state.locked_plan is None
    state.locked_plan = final.create_locked_plan(
        final.direction_candidate(snapshot["straddles"][0], "LONG"), snapshot
    )
    state.clear_period_state()
    assert state.locked_plan is None


def test_actual_positions_drive_rtm_hedge(monkeypatch):
    monkeypatch.setattr(final, "DELTA_WARNING_LEVEL", 100)
    snapshot = plan_snapshot(20, [], option_delta=350, rtm_position=-50)
    hedge = final.actual_position_hedge(snapshot)
    assert hedge["target_rtm_position"] == -350
    assert hedge["rtm_trade_quantity"] == -300
    assert hedge["hedge_required"] is True


def volatility_item(news_id, tick, value="30%"):
    return {
        "news_id": news_id, "tick": tick, "period": 1,
        "headline": f"Announcement {news_id}",
        "body": f"The realized volatility of RTM this week will be {value}",
    }


def test_identical_news_ticks_36_through_44_invalidates_at_most_once():
    state = final.RuntimeState()
    news = [volatility_item(1, 30)]
    invalidations = 0
    for tick in range(36, 45):
        changing_vols = (0.20 + tick / 10000, 0.25 + tick / 10000, 0.30 + tick / 10000)
        snapshot = plan_snapshot(
            tick, [plan_straddle(49, 50, 0.1)], vols=changing_vols, news=news
        )
        event = final.update_locked_plan(snapshot, state)["event"]
        invalidations += event == "INVALIDATED_NEWS"
    assert invalidations <= 1
    assert state.pending_news_identity is None


def test_effective_volatility_changes_do_not_start_news_settle():
    state = final.RuntimeState()
    news = [volatility_item(1, 30)]
    first = plan_snapshot(36, [plan_straddle(49, 50, 0.1)],
                          vols=(0.20, 0.25, 0.30), news=news)
    final.update_locked_plan(first, state)
    second = plan_snapshot(37, [plan_straddle(49, 50, 0.1)],
                           vols=(0.201, 0.251, 0.301), news=news)
    update = final.update_locked_plan(second, state)
    assert update["event"] not in {"NEWS_SETTLE", "SETTLING", "INVALIDATED_NEWS"}


def test_quote_and_spot_changes_do_not_start_news_settle():
    state = final.RuntimeState()
    news = [volatility_item(1, 30)]
    first = plan_snapshot(36, [plan_straddle(49, 50, 0.1)], news=news)
    final.update_locked_plan(first, state)
    second = plan_snapshot(37, [plan_straddle(49, 48, 0.4)], news=news)
    second["spot"] = 50.25
    update = final.update_locked_plan(second, state)
    assert update["event"] not in {"NEWS_SETTLE", "SETTLING", "INVALIDATED_NEWS"}


def test_reordered_news_has_same_identity():
    first = volatility_item(1, 10, "20%")
    second = volatility_item(2, 20, "30%")
    assert final.build_news_identity([first, second], 1, 25) == \
        final.build_news_identity([second, first], 1, 25)


def test_genuinely_new_announcement_invalidates_exactly_once():
    state = final.RuntimeState()
    old_news = [volatility_item(1, 10, "20%")]
    final.update_locked_plan(
        plan_snapshot(20, [plan_straddle(49, 50, 0.1)], news=old_news), state
    )
    new_news = old_news + [volatility_item(2, 21, "30%")]
    events = [
        final.update_locked_plan(
            plan_snapshot(21, [plan_straddle(49, 50, 0.1)], news=new_news), state
        )["event"]
        for _ in range(3)
    ]
    assert events.count("INVALIDATED_NEWS") == 1
    assert state.news_detected_tick == 21


def test_plan_locks_after_news_settlement_finishes():
    state = final.RuntimeState()
    old_news = [volatility_item(1, 10, "20%")]
    final.update_locked_plan(
        plan_snapshot(20, [plan_straddle(49, 50, 0.1)], news=old_news), state
    )
    new_news = old_news + [volatility_item(2, 21, "30%")]
    final.update_locked_plan(
        plan_snapshot(21, [plan_straddle(49, 50, 0.1)], news=new_news), state
    )
    update = final.update_locked_plan(
        plan_snapshot(22, [plan_straddle(49, 50, 0.1)], news=new_news), state
    )
    assert update["event"] == "CREATED"
    assert state.locked_plan.strike == 49
    assert state.pending_news_identity is None


def test_same_pending_news_does_not_restart_timer():
    state = final.RuntimeState()
    old_news = [volatility_item(1, 10, "20%")]
    final.update_locked_plan(
        plan_snapshot(20, [plan_straddle(49, 50, 0.1)], news=old_news), state
    )
    new_news = old_news + [volatility_item(2, 21, "30%")]
    final.update_locked_plan(
        plan_snapshot(21, [plan_straddle(49, 50, 0.1)], news=new_news), state
    )
    for _ in range(4):
        final.update_locked_plan(
            plan_snapshot(21, [plan_straddle(49, 50, 0.1)], news=new_news), state
        )
    assert state.news_detected_tick == 21


def test_future_news_is_excluded_from_stable_identity():
    current = volatility_item(1, 10, "20%")
    future = volatility_item(2, 75, "30%")
    assert final.build_news_identity([current, future], 1, 36) == \
        final.build_news_identity([current], 1, 36)


def test_reset_clears_all_locked_news_state():
    state = final.RuntimeState(
        accepted_news_identity=(("1", 10, "h", "b"),),
        pending_news_identity=(("2", 20, "h2", "b2"),),
        news_detected_tick=20,
        locked_plan=final.LockedPlan("LONG", 49, 10, "1", 15, 10, 50),
    )
    state.clear_period_state()
    assert state.accepted_news_identity is None
    assert state.pending_news_identity is None
    assert state.news_detected_tick is None
    assert state.locked_plan is None
