"""Focused offline tests for the standalone assistant."""
import inspect
import math
from unittest.mock import Mock

import pytest
import requests

import case1_simple as s


def news(body='The realized volatility of RTM this week will be 20%', tick=0, id=1):
    return {'news_id': id, 'tick': tick, 'headline': 'Volatility update', 'body': body}


def rows(spot=50, strikes=(50, 52), position=0, premium=0.1):
    result = [dict(ticker='RTM', bid=spot-.01, ask=spot+.01, contract_size=1, position=0)]
    for k in strikes:
        for kind in 'CP':
            result.append(dict(ticker=f'RTM{k}{kind}', bid=premium, ask=premium+.01,
                               contract_size=100, position=position))
    return result


def update(a, tick=0, securities=None, items=None, status='ACTIVE', period=1):
    return a.update(dict(tick=tick, period=period, status=status),
                    rows() if securities is None else securities, [] if items is None else items)


def test_actual_and_initial():
    assert s.parse_news(news()) == [(1, 'ACTUAL', .2, .2)]
    item = news('The risk-free rate is 0%. Its current annualized realized volatility is 25%.')
    assert s.parse_news(item) == [(1, 'ACTUAL', .25, .25)]


def test_forecast_and_source_week():
    item = news('The realized volatility of RTM next week will be between 26% and 31%', 75)
    assert s.parse_news(item) == [(3, 'FORECAST', .26, .31)]
    assert s.parse_news(news(tick=150))[0][0] == 3


def test_rate_is_not_volatility():
    assert s.parse_news(news('The risk-free rate this week is 0%.')) == []
    assert s.parse_news(news('Current annualized realized volatility is unknown and the risk-free rate is 0%.')) == []


def test_future_filter_and_actual_precedence():
    forecast = news('Volatility this week is between 26% and 31%', 1, 2)
    schedule, items = s.weekly_schedule([news(), forecast, news(tick=80, id=3)], 30)
    assert schedule[1] == ('ACTUAL', .2, .2)
    assert len(items) == 2
    assert schedule[2] == ('UNKNOWN', .25, .25)


def test_effective_variance_and_identity():
    schedule, items = s.weekly_schedule([news()], 0)
    assert s.effective_volatility(schedule, 0)[1] == pytest.approx(math.sqrt((75*.2**2 + 225*.25**2)/300))
    assert s.effective_volatility(schedule, 1) != s.effective_volatility(schedule, 0)
    assert s.weekly_schedule([news()], 1)[1] == items
    assert s.effective_volatility(schedule, 300) == (0, 0, 0)


def test_identity_order_and_normalization():
    first, second = news(), news(tick=1, id=2)
    assert s.weekly_schedule([first, second], 2) == s.weekly_schedule([second, first], 2)
    assert s.news_identity(first) == s.news_identity(dict(first, body='  '+first['body'].upper()+'  '))


def test_atm_and_tiebreak():
    securities = rows(strikes=(49, 51))
    assert s.select_strike(securities, 50) == 49
    securities[1]['ask'] = .15
    assert s.select_strike(securities, 50) == 51
    assert s.select_strike(securities, 49.1) == 49


def test_stable_strike_and_one_news_reselection():
    a = s.Assistant()
    update(a)
    update(a, 1, rows(52))
    assert a.selected_strike == 50
    output = update(a, 2, rows(52), [news(tick=2)])
    assert a.selected_strike == 52
    assert output.count('NEW VOL NEWS') == 1
    assert 'NEW VOL NEWS' not in update(a, 3, rows(50), [news(tick=2)])
    assert a.selected_strike == 52


def test_edges():
    call, put = rows()[1:3]
    vols, maturity = (.20, .25, .30), .05
    long, short, _ = s.straddle_edges(call, put, 50, 50, maturity, vols)
    fair = lambda v: sum(s.black_scholes(50, 50, maturity, v, k)[0] for k in 'CP')
    delta = sum(s.black_scholes(50, 50, maturity, .25, k)[1] for k in 'CP')
    cost = 8 + abs(round(100*delta))*.04 + 2
    assert long == pytest.approx((fair(.2)-.22)*100-cost)
    assert short == pytest.approx((.2-fair(.3))*100-cost)


def test_duplicate_poll_and_block_suppression():
    a = s.Assistant()
    assert 'MANUAL TRADE' in update(a)
    assert update(a) == ''
    out = update(a, 1)
    assert 'LONG edge' in out and 'SHORT edge' in out
    assert 'MANUAL TRADE' not in out


@pytest.mark.parametrize('status', ['STOPPED', 'PAUSED', None])
def test_inactive(status):
    a = s.Assistant()
    out = update(a, status=status)
    assert 'waiting for ACTIVE' in out
    assert 'edge' not in out and 'MANUAL TRADE' not in out
    assert update(a, status=status) == ''


def test_positions_block_entry_and_partial_warning():
    securities = rows()
    securities[1]['position'] = 3
    out = update(s.Assistant(), securities=securities)
    assert 'POSITION MANAGEMENT' in out
    assert 'PARTIAL OR UNBALANCED OPTION POSITION' in out
    assert 'MANUAL TRADE' not in out


def test_actual_portfolio_hedge():
    held = rows(strikes=(50,), position=3)[1:]
    delta, total, target, trade = s.portfolio_hedge(held, 50, .05, .25, 17)
    expected = 300*sum(s.black_scholes(50, 50, .05, .25, k)[1] for k in 'CP')
    assert delta == pytest.approx(expected)
    assert total == pytest.approx(expected+17)
    assert target == round(-expected)
    assert trade == target-17


def test_hedge_threshold_and_expiry_management():
    securities = rows(strikes=(50,), position=1)
    securities[0]['position'] = 500
    out = update(s.Assistant(), 285, securities)
    assert 'MANUAL HEDGE: SELL' in out
    assert 'Expiry approaching' in out
    assert 'MANUAL TRADE' not in out
    securities[0]['position'] = 0
    assert 'MANUAL HEDGE' not in update(s.Assistant(), 285, securities)


def test_no_entry_at_285():
    out = update(s.Assistant(), 285)
    assert 'Expiry approaching' in out and 'WAIT' in out
    assert 'LONG edge' in out and 'MANUAL TRADE' not in out


@pytest.mark.parametrize('field,value', [('bid', None), ('bid', 0), ('ask', -1),
                                        ('ask', .01), ('contract_size', None), ('contract_size', 1)])
def test_bad_security_skipped(field, value):
    securities = rows(strikes=(50,))
    securities[1][field] = value
    assert s.select_strike(securities, 50) is None
    securities[1]['position'] = 1
    out = update(s.Assistant(), securities=securities)
    assert 'POSITION MANAGEMENT' in out and 'hedge unavailable' in out
    assert 'MANUAL TRADE' not in out


def test_reset_and_malformed_rows():
    a = s.Assistant()
    update(a, 10, items=[news()])
    out = update(a, 0, rows(52)+[None, {}, 'bad'])
    assert 'Case reset' in out and a.selected_strike == 52 and not a.seen_news
    assert 'Case reset' in update(a, 0, period=2)


def test_missing_pair_and_rtm():
    assert 'pair unavailable' in update(s.Assistant(), securities=rows(strikes=()))
    assert 'RTM unavailable' in update(s.Assistant(), securities=[])


def test_black_scholes_limits_and_parity():
    call, cd = s.black_scholes(51, 50, .1, .25, 'C')
    put, pd = s.black_scholes(51, 50, .1, .25, 'P')
    assert call-put == pytest.approx(1)
    assert cd-pd == pytest.approx(1)
    assert s.black_scholes(51, 50, 0, .25, 'C') == (1, 1)
    assert s.black_scholes(49, 50, .1, 0, 'P') == (1, -1)


def test_get_errors():
    session = Mock()
    session.get.side_effect = requests.Timeout()
    with pytest.raises(requests.Timeout):
        s.api_get(session, 'http://example', '/case')
    session.get.side_effect = None
    response = session.get.return_value
    response.status_code = 401
    with pytest.raises(ValueError, match='Authentication'):
        s.api_get(session, 'http://example', '/case')
    response.status_code = 200
    response.json.side_effect = ValueError()
    with pytest.raises(ValueError, match='Malformed'):
        s.api_get(session, 'http://example', '/case')


def test_source_read_only_and_standalone():
    source = inspect.getsource(s)
    for forbidden in ('/orders', '.post(', '.put(', '.patch(', '.delete(',
                      'NEWS SETTLE', 'case1_final', 'case1_strategy', 'py_vollib'):
        assert forbidden not in source


def test_signal_thresholds_and_direction_change():
    a = s.Assistant()
    assert 'LONG VOL — MANUAL TRADE' in update(a)
    assert 'SHORT VOL — MANUAL TRADE' in update(a, 1, rows(premium=10))
    assert 'MANUAL TRADE' not in update(a, 2, rows(premium=10))
    fair = sum(s.black_scholes(50, 50, 300/3600, .25, k)[0] for k in 'CP')
    output = update(s.Assistant(), securities=rows(premium=fair/2))
    assert 'WAIT' in output and 'MANUAL TRADE' not in output
    assert s.MIN_LONG_EDGE_PER_STRADDLE == 10
    assert s.MIN_SHORT_EDGE_PER_STRADDLE == 15


def test_main_clean_interrupt(monkeypatch, capsys):
    session = Mock()
    session.get.side_effect = KeyboardInterrupt
    context = Mock()
    context.__enter__ = Mock(return_value=session)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(s.requests, 'Session', lambda: context)
    s.main()
    assert 'Read-only Case 1 assistant stopped cleanly.' in capsys.readouterr().out
    assert session.auth == (s.USERNAME, s.PASSWORD)
    session.get.assert_called_once_with(s.API_ENDPOINT + '/case', timeout=s.API_TIMEOUT_SECONDS)


def test_native_rit_size_schema_entry_and_hedge():
    securities = rows(strikes=(50,))
    for row in securities:
        row['size'] = row.pop('contract_size')
    out = update(s.Assistant(), securities=securities)
    assert 'LONG edge' in out and 'RTM unavailable' not in out
    securities[1]['position'] = 3
    out = update(s.Assistant(), securities=securities)
    assert 'Option delta:' in out and 'hedge unavailable' not in out
    delta, _, _, _ = s.portfolio_hedge(securities[1:2], 50, .05, .25, 0)
    assert delta == pytest.approx(300 * s.black_scholes(50, 50, .05, .25, 'C')[1])


@pytest.mark.parametrize('fields', [{}, {'size': None}, {'size': 0},
                                   {'size': 100}, {'size': 1, 'contract_size': 100}])
def test_invalid_rtm_size_reports_reason(fields):
    securities = rows()
    securities[0].pop('contract_size')
    securities[0].update(fields)
    out = update(s.Assistant(), securities=securities)
    assert 'RTM unavailable' in out and 'size (expected 1)' in out
    assert 'MANUAL TRADE' not in out


@pytest.mark.parametrize('value', ['between 27-30%', '27%–30%',
                                   'between 27% and 30%', '27 to 30%',
                                   'between 27.5% and 30.5%'])
def test_shared_percent_ranges(value):
    parsed = s.parse_news(news(f'Volatility next week will be {value}'))
    low, high = (.275, .305) if '27.5' in value else (.27, .30)
    assert parsed == [(2, 'FORECAST', low, high)]


@pytest.mark.parametrize('value', ['between 27%', 'between unknown and 30%',
                                   '27%-', '27% to unknown', '30-27%'])
def test_incomplete_or_reversed_ranges_rejected(value):
    assert s.parse_news(news(f'Volatility next week will be {value}')) == []


@pytest.mark.parametrize('tick,week', [(74, 1), (75, 2), (76, 2), (149, 2), (150, 3)])
def test_original_week_boundary_convention(tick, week):
    assert s.parse_news(news(tick=tick))[0][0] == week


def test_full_position_lifecycle_within_one_tick():
    a = s.Assistant()
    securities = rows(strikes=(50,))
    assert 'MANUAL TRADE' in update(a, securities=securities)
    securities[1]['position'] = 3
    out = update(a, securities=securities)
    assert 'POSITION CHANGE' in out and 'PARTIAL OR UNBALANCED' in out
    assert 'MANUAL TRADE' not in out and 'LONG edge' not in out
    assert update(a, securities=securities) == ''
    securities[2]['position'] = 3
    out = update(a, securities=securities)
    assert 'POSITION MANAGEMENT' in out and 'PARTIAL OR UNBALANCED' not in out
    securities[0]['position'] = -17
    assert 'Current RTM position: -17' in update(a, securities=securities)
    securities[1]['position'] = securities[2]['position'] = 0
    out = update(a, securities=securities)
    assert 'Options: none' in out and 'Option delta: 0.00' in out
    assert 'Target RTM position: 0 | Required RTM trade: +17' in out
    assert 'MANUAL TRADE' not in out and 'MANUAL HEDGE' not in out
    securities[0]['position'] = 0
    assert 'Portfolio flat' in update(a, securities=securities)
    assert update(a, securities=securities) == ''
    assert 'MANUAL TRADE' in update(a, 1, securities)


@pytest.mark.parametrize('position,side', [(300, 'SELL'), (-300, 'BUY')])
def test_rtm_only_hedge(position, side):
    securities = rows()
    securities[0]['position'] = position
    out = update(s.Assistant(), securities=securities)
    assert f'MANUAL FLATTEN: {side} 300 RTM' in out
    assert 'MANUAL TRADE' not in out


def test_unchanged_tick_still_calculates_hedge(monkeypatch):
    a = s.Assistant()
    securities = rows(position=3)
    real = s.portfolio_hedge
    spy = Mock(wraps=real)
    monkeypatch.setattr(s, 'portfolio_hedge', spy)
    update(a, securities=securities)
    assert update(a, securities=securities) == ''
    assert spy.call_count == 2


def test_missing_rtm_position_blocks_entry():
    securities = rows()
    securities[0].pop('position')
    out = update(s.Assistant(), securities=securities)
    assert 'RTM position missing' in out and 'MANUAL TRADE' not in out


def test_sizing_maximizes_quantity_within_configured_caps(monkeypatch):
    monkeypatch.setattr(s, 'ENTRY_QUANTITY', 1000)
    assert s.entry_quantity() == 10
    call, put = rows(premium=2)[1:3]
    assert s.entry_quantity(call, put, 'LONG') == 2
    assert s.entry_quantity(call, put, 'SHORT') == 10
    monkeypatch.setattr(s, 'MANUAL_UNHEDGED_DELTA_BUDGET', 100000)
    assert s.entry_quantity() == 70  # Case delta limit during a single-leg fill.
    monkeypatch.setattr(s, 'OPTION_NET_LIMIT', 8)
    assert s.entry_quantity() == 4
    monkeypatch.setattr(s, 'ENTRY_QUANTITY', 0)
    assert s.entry_quantity() == 0


def test_long_premium_budget_blocks_unaffordable_entry(monkeypatch):
    monkeypatch.setattr(s, 'MAX_LONG_PREMIUM_DOLLARS', 1)
    out = update(s.Assistant())
    assert 'ENTRY BLOCKED' in out and 'MANUAL TRADE' not in out


def test_mark_to_close_signed_pnl_and_missing_basis():
    securities = rows(strikes=(50,), position=3, premium=1)
    for row in securities:
        row['vwap'] = 0.8
    result = '\n'.join(s.position_review(securities[1:], securities[0], 50, .05, (.25,)*3, 100))
    assert 'P&L: $108.00' in result  # 600 shares * .20 less $12 closing fees.
    for row in securities[1:]:
        row['position'] = -3
        row['vwap'] = 1.2
    result = '\n'.join(s.position_review(securities[1:], securities[0], 50, .05, (.25,)*3, 100))
    assert 'P&L: $102.00' in result
    securities[1].pop('vwap')
    assert 'P&L: unavailable' in '\n'.join(s.position_review(securities[1:], securities[0], 50, .05, (.25,)*3, 100))


def test_hold_convergence_exit_and_expiry_exit():
    securities = rows(strikes=(50,), position=3, premium=.1)
    out = update(s.Assistant(), 100, securities)
    assert 'HOLD: model advantage remains' in out
    assert 'Held K=50' in out
    securities = rows(strikes=(50,), position=3, premium=10)
    out = update(s.Assistant(), 100, securities)
    assert 'REVIEW EXIT:' in out and 'SELL 3 RTM50C' in out
    securities = rows(strikes=(50,), position=-3, premium=.1)
    out = update(s.Assistant(), 100, securities)
    assert 'REVIEW EXIT:' in out and 'BUY 3 RTM50C' in out
    securities = rows(strikes=(50,), position=3, premium=.01)
    assert 'planned expiry review time' in update(s.Assistant(), 290, securities)


def test_held_strike_does_not_become_new_entry_strike():
    a = s.Assistant()
    securities = rows(spot=52, position=0)
    securities[1]['position'] = securities[2]['position'] = 3
    out = update(a, 100, securities, [news(tick=100)])
    assert a.selected_strike == 52
    assert '| Held K=50 |' in out


def test_partial_exit_and_stock_residual():
    securities = rows(strikes=(50,), position=0)
    securities[1]['position'] = 3
    out = update(s.Assistant(), 100, securities)
    assert 'REVIEW EXIT: partial or unbalanced' in out
    securities[1]['position'] = 0
    securities[0]['position'] = -20
    out = update(s.Assistant(), 100, securities)
    assert 'MANUAL FLATTEN: BUY 20 RTM' in out
    assert 'MANUAL TRADE' not in out


def test_order_chunking_and_delta_fine():
    row = rows(strikes=(50,), position=250)[1]
    assert 'SELL 100 RTM50C' in s.close_instruction(row)
    securities = rows(strikes=(50,), position=0)
    securities[0]['position'] = 20000
    out = update(s.Assistant(), 100, securities)
    assert 'MANUAL HEDGE:' not in out
    assert 'estimated excess fine $1300.00/second' in out
    assert 'MANUAL FLATTEN: SELL 10000' in out


def test_hedge_target_limit_blocks_impossible_stock_trade():
    securities = rows(spot=100, strikes=(50,), position=0)
    securities[1]['position'] = 600
    out = update(s.Assistant(), 100, securities)
    assert 'HEDGE BLOCKED' in out and 'MANUAL HEDGE:' not in out


def test_offline_replay_never_connects(tmp_path, monkeypatch, capsys):
    import json
    path = tmp_path / 'practice.jsonl'
    snapshot = dict(case=dict(status='ACTIVE', period=1, tick=100), securities=rows(), news=[])
    path.write_text(json.dumps(snapshot)+'\n'+json.dumps(snapshot)+'\n')
    monkeypatch.setattr(s.requests, 'Session', Mock(side_effect=AssertionError('network forbidden')))
    s.replay(path)
    output = capsys.readouterr().out
    assert output.count('LONG edge') == 1
    path.write_text('bad json\n')
    with pytest.raises(ValueError, match='line 1'):
        s.replay(path)


@pytest.mark.parametrize('changed', [False, True])
def test_main_records_only_consistent_snapshots(tmp_path, monkeypatch, capsys, changed):
    import json
    path = tmp_path / 'snapshots.jsonl'
    case = dict(status='ACTIVE', tick=10, period=1)
    verified = dict(case, tick=11) if changed else case
    getter = Mock(side_effect=[case, rows(), [], verified, KeyboardInterrupt()])
    monkeypatch.setattr(s, 'api_get', getter)
    monkeypatch.setattr(s.time, 'sleep', lambda _: None)
    s.main(str(path))
    output = capsys.readouterr().out
    if changed:
        assert not path.exists()
        assert 'Snapshot changed' in output and 'MANUAL TRADE' not in output
    else:
        snapshot = json.loads(path.read_text())
        assert snapshot['case'] == case and snapshot['securities'] == rows()
        assert set(snapshot) == {'case', 'securities', 'news'}
        assert 'MANUAL TRADE' in output


def test_exit_review_avoids_conflicting_routine_hedge():
    securities = rows(strikes=(50,), position=3, premium=10)
    securities[0]['position'] = 500
    out = update(s.Assistant(), 100, securities)
    assert 'REVIEW EXIT' in out and 'MANUAL HEDGE:' not in out
    assert 'Exit review takes priority' in out


def test_unknown_basis_and_invalid_positions_do_not_crash_review():
    securities = rows(strikes=(50,), position=3)
    securities[1]['position'] = None
    out = update(s.Assistant(), 100, securities)
    assert 'REVIEW DATA' in out and 'MANUAL TRADE' not in out


def test_displayed_edges_include_stock_spread_and_rehedge_costs():
    securities = rows(spot=51, strikes=(50,))
    call, put = securities[1:]
    maturity = 300 / 3600
    long, short, _ = s.straddle_edges(call, put, 51, 50, maturity, (.25,)*3)
    delta = sum(s.black_scholes(51, 50, maturity, .25, k)[1] for k in 'CP')
    extra = abs(round(100*delta)) * .02 + s.REHEDGE_ALLOWANCE_PER_STRADDLE
    out = update(s.Assistant(), securities=securities)
    assert f'LONG edge ${long-extra:.2f}' in out
    assert f'SHORT edge ${short-extra:.2f}' in out
