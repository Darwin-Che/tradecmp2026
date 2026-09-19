import math
from unittest.mock import Mock

import pytest
import case1_integrated_variance as s


@pytest.fixture(autouse=True)
def disable_audit_files(monkeypatch):
    monkeypatch.setattr(s, 'EVENT_LOG_PATH', None)


def news(tick=10, body='Volatility this week is 20%', ident=1):
    return dict(tick=tick, news_id=ident, body=body)


def rows(vol=.3, tick=10, position=0):
    result = [dict(ticker='RTM', bid=99.99, ask=100.01, size=1, position=0)]
    for strike in (95, 100, 105):
        for kind in 'CP':
            price = s.bs(100, strike, sum(s.remaining_times(tick)), vol, kind)[0]
            result.append(dict(ticker=f'RTM{strike}{kind}', bid=price-.01,
                               ask=price+.01, size=100,
                               position=position if strike == 100 else 0))
    return result


def execution_snapshot(account, tick=10):
    snap = s.market_snapshot(account, tick)
    model = s.VolatilityModel(weeks={w: (tick, 'ACTUAL', .9, .9) for w in range(1, 5)},
                              latest_release=tick, confidence=1)
    snap.update(valuation_model=model, news_seen=set(), decision_vols=model.stressed_vols(tick),
                current_high=.9, analyst_coverage=1)
    s.greeks(snap)
    return snap


def test_time_and_expiry():
    assert sum(s.remaining_times(0)) == pytest.approx(20/240)
    assert s.remaining_times(75) == pytest.approx((0, 5/240, 5/240, 5/240))
    assert s.remaining_times(300) == (0, 0, 0, 0)
    assert s.DT_YEAR == 1/3600


def test_news_baseline_and_uniform_variance():
    m = s.VolatilityModel()
    m.update([], 9, 1, .33)
    items = [news(), news(body='Volatility next week is between 20% and 40%', ident=2)]
    m.update(items, 10, 1, .6)
    assert m.baseline == .33
    integrated, vols = m.fair(10)
    t = s.remaining_times(10)
    central = .2**2*t[0] + (.2**2+.2*.4+.4**2)/3*t[1] + .33**2*(t[2]+t[3])
    assert integrated[1] == pytest.approx(central)
    assert vols[1] == pytest.approx(math.sqrt(central/sum(t)))
    m.update(items, 11, 1, .7)
    assert m.baseline == .33
    assert m.fair(75)[0][1] == pytest.approx(central-.2**2*t[0])


def test_startup_and_same_tick_are_not_pre_news():
    m = s.VolatilityModel()
    m.update([], 10, 1, .2)
    m.update([news()], 10, 1, .4)
    assert m.baseline is None
    assert m.fair(10) is None
    m.update([news(11, ident=2)], 11, 1, .5)
    assert m.baseline == .4


def test_exact_over_forecast_and_old_release():
    m = s.VolatilityModel()
    m.update([], 9, 1, .3)
    m.update([news()], 10, 1, .4)
    m.update([news(11, 'Volatility this week is between 40% and 50%', 2)], 11, 1, .5)
    assert m.weeks[1][1:] == ('ACTUAL', .2, .2)
    assert m.baseline == .4


@pytest.mark.parametrize('kind', ['C', 'P'])
def test_iv_and_gamma(kind):
    p, d, g = s.bs(100, 105, .05, .37, kind)
    assert s.implied_vol(p, 100, 105, .05, kind) == pytest.approx(.37)
    eps = .001
    numerical = (s.bs(100+eps, 105, .05, .37, kind)[1]-s.bs(100-eps, 105, .05, .37, kind)[1])/(2*eps)
    assert g == pytest.approx(numerical, rel=1e-6)
    assert s.implied_vol(200, 100, 105, .05, kind) is None


def test_robust_edges_and_costs():
    snap = s.market_snapshot(rows(), 10)
    s.greeks(snap)
    e = s.economics(snap, 100, (.2, .3, .4))
    assert e['long'] < 0 and e['short'] < 0
    cheap = s.economics(snap, 100, (.5, .55, .6))
    assert cheap['long'] > s.MIN_EDGE_PER_STRADDLE
    size = s.target_size(cheap, 1, 1, snap)
    assert size*cheap['risk'] <= s.GAMMA_DELTA_BUDGET
    assert s.target_size(cheap, 1, .6, snap) <= size
    plan = s.batch_plan(snap, cheap, 1, size)
    assert len(plan) == 2
    assert all(q <= s.OPTION_MAX_ORDER for _, q in plan)
    first = snap['rows'][plan[0][0]]
    second = snap['rows'][plan[1][0]]
    assert abs(first['delta']) <= abs(second['delta'])


def test_limits_and_hedge_band():
    snap = s.market_snapshot(rows(position=500), 10)
    s.greeks(snap)
    e = s.economics(snap, 100, (.4, .4, .4))
    assert not s.batch_plan(snap, e, 1, 10)
    snap['portfolio_delta'] = s.effective_hedge_band(snap)-1
    assert s.hedge_trade(snap) == 0
    snap['portfolio_delta'] = 6000
    snap['option_delta'] = 6000
    assert s.hedge_trade(snap) == -6000


def test_dry_run_never_mutates(monkeypatch):
    monkeypatch.setattr(s, 'DRY_RUN', True)
    session = Mock()
    broker = s.Broker(session)
    snap = s.market_snapshot(rows(), 10)
    broker.execute(dict(tick=10, period=1), snap, [('RTM100C', 10), ('RTM100P', 10)])
    session.post.assert_not_called()
    session.delete.assert_not_called()


def test_reset_and_reversal_exit(capsys):
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    snap, plan = strategy.evaluate(dict(tick=10, period=1), rows(position=10),
                                  [news(body='Volatility this week is 1%')])
    assert 100 in strategy.exiting
    assert plan and all(q < 0 for _, q in plan)
    strategy.evaluate(dict(tick=0, period=2), rows(tick=0), [])
    assert strategy.model.baseline is None
    assert not strategy.exiting


def test_parity_fees():
    snap = s.market_snapshot(rows(), 10)
    assert s.parity_opportunities(snap) == []
    snap['pairs'][100]['C']['bid'] += 2
    snap['pairs'][100]['C']['ask'] += 2
    found = s.parity_opportunities(snap)
    assert any(k == 100 and kind == 'CONVERSION' and edge > 20 for k, kind, edge in found)


def test_live_pair_uses_confirmed_partial_fill(monkeypatch):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    broker = s.Broker(Mock())
    case = dict(tick=10, period=1, status='ACTIVE')
    account = rows()
    snapshot = execution_snapshot(account)
    sent = []

    def get(resource):
        if resource == '/case':
            return case
        if resource == '/news': return []
        if resource == '/orders?status=OPEN':
            return []
        if resource == '/securities':
            return [dict(r) for r in account]
        raise AssertionError(resource)

    def order(row, change):
        sent.append((row['ticker'], change))
        filled = 3 if len(sent) == 1 else abs(change)
        actual = next(r for r in account if r['ticker'] == row['ticker'])
        actual['position'] += filled if change > 0 else -filled
        return filled

    broker.get = get
    broker.order = order
    broker.execute(case, snapshot, [('RTM100P', 10), ('RTM100C', 10)])
    assert sent == [('RTM100P', 10), ('RTM100C', 3)]


def test_terminal_fill_waits_for_position_snapshot(monkeypatch):
    monkeypatch.setattr(s.time, 'sleep', lambda _: None)
    broker = s.Broker(Mock())
    row = rows()[1]
    broker.order = Mock(return_value=2)
    broker.get = Mock(side_effect=[
        [dict(row)],
        [dict(row, position=2)],
    ])

    assert broker.confirmed_order(row, 2) == 2
    broker.order.assert_called_once_with(row, 2)
    assert broker.get.call_count == 2


def test_persistent_position_mismatch_stops_without_resubmitting(monkeypatch):
    monkeypatch.setattr(s.time, 'sleep', lambda _: None)
    broker = s.Broker(Mock())
    row = rows()[1]
    broker.order = Mock(return_value=2)
    broker.get = Mock(return_value=[dict(row, position=1)])

    with pytest.raises(RuntimeError, match='expected 2, observed 1'):
        broker.confirmed_order(row, 2)
    broker.order.assert_called_once_with(row, 2)
    assert broker.get.call_count == s.POSITION_RECONCILE_ATTEMPTS


def test_uncertain_post_is_not_retried(monkeypatch):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    session = Mock()
    session.post.side_effect = s.requests.Timeout('uncertain acknowledgement')
    broker = s.Broker(session)
    with pytest.raises(s.requests.Timeout):
        broker.order(rows()[1], 5)
    assert session.post.call_count == 1


def test_cancel_waits_for_terminal_status(monkeypatch):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    monkeypatch.setattr(s, 'ORDER_TIMEOUT_SECONDS', 0)
    session = Mock()
    session.post.return_value.json.return_value = {'order_id': 123}
    broker = s.Broker(session)
    broker.get = Mock(side_effect=[dict(status='OPEN', quantity_filled=2),
                                  dict(status='CANCELLED', quantity_filled=3)])
    assert broker.order(rows()[1], 5) == 3
    session.delete.assert_called_once()


def test_exit_latched_until_all_legs_flat(capsys):
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    strategy.evaluate(dict(tick=10, period=1), rows(position=10),
                      [news(body='Volatility this week is 1%')])
    assert 100 in strategy.exiting
    strategy.evaluate(dict(tick=11, period=1), rows(tick=11, position=5),
                      [news(11, 'Volatility this week is 90%', 2)])
    assert 100 in strategy.exiting


def test_exact_release_confidence_with_simultaneous_forecast():
    m = s.VolatilityModel()
    m.update([], 9, 1, .3)
    m.update([news(), news(10, 'Volatility next week is between 20% and 40%', 2)], 10, 1, .4)
    assert m.confidence == 1.0


def test_flat_options_clear_small_stock_hedge():
    snapshot = s.market_snapshot(rows(), 10)
    snapshot['rtm']['position'] = 400
    s.greeks(snapshot)
    assert s.hedge_trade(snapshot) == -400


def test_manual_signal_exit_keeps_expiry_automatic(monkeypatch, capsys):
    monkeypatch.setattr(s, 'AUTO_SIGNAL_EXITS', False)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    strategy.evaluate(dict(tick=10, period=1), rows(position=5),
                      [news(body='Volatility this week is 1%')])
    assert not strategy.exiting
    assert 'MANUAL SIGNAL EXIT' in capsys.readouterr().out
    strategy.evaluate(dict(tick=s.FORCE_EXIT_TICK, period=1),
                      rows(tick=s.FORCE_EXIT_TICK, position=5), [])
    assert 100 in strategy.exiting


def test_reentry_cooldown_and_entry_switch(monkeypatch, capsys):
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(position=2), items)
    _, plan = strategy.evaluate(dict(tick=11, period=1), rows(tick=11), items)
    assert not plan
    assert strategy.cooldown_until == 11+s.REENTRY_COOLDOWN_TICKS
    monkeypatch.setattr(s, 'AUTO_ENTRIES', False)
    _, plan = strategy.evaluate(dict(tick=20, period=1), rows(tick=20), items)
    assert not plan


def test_automated_entry_exit_and_residual_hedge(monkeypatch, capsys):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    monkeypatch.setattr(s, 'BASE_STRADDLES', 2)
    monkeypatch.setattr(s, 'ENTRY_CONFIRMATION_TICKS', 1)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    case = dict(tick=10, period=1, status='ACTIVE')
    items = [news(body='Volatility this week is 90%')]
    account = rows()
    broker = s.Broker(Mock())
    orders = []

    def get(resource):
        if resource == '/case':
            return dict(case)
        if resource == '/securities':
            return [dict(r) for r in account]
        if resource == '/news':
            return items
        if resource == '/orders?status=OPEN':
            return []
        raise AssertionError(resource)

    def order(row, change):
        orders.append((row['ticker'], change))
        next(r for r in account if r['ticker'] == row['ticker'])['position'] += change
        return abs(change)

    broker.get, broker.order = get, order
    snapshot, plan = strategy.evaluate(case, account, items)
    assert len(plan) == 2 and all(q > 0 for _, q in plan)
    broker.execute(case, snapshot, plan)
    assert sum(abs(r['position']) for r in account[1:]) > 0
    case['tick'] = 11
    items.append(news(11, 'Volatility this week is 1%', 2))
    # A leftover RTM hedge below the routine band must disappear after closing.
    account[0]['position'] = 50
    snapshot, plan = strategy.evaluate(case, account, items)
    assert len(plan) == 2 and all(q < 0 for _, q in plan)
    broker.execute(case, snapshot, plan)
    assert all(r['position'] == 0 for r in account)
    assert orders[-1] == ('RTM', -50)


def test_entry_without_revaluable_model_is_rejected(monkeypatch):
    broker = s.Broker(Mock())
    broker.get = lambda resource: []
    broker.order = Mock()
    snap = s.market_snapshot(rows(), 10)
    with pytest.raises(ValueError, match='volatility model'):
        broker.execute(dict(tick=10, period=1, status='ACTIVE'), snap,
                       [('RTM100P', 5), ('RTM100C', 5)])
    broker.order.assert_not_called()


def test_outside_position_change_blocks_execution(monkeypatch):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    case = dict(tick=10, period=1, status='ACTIVE')
    account = rows()
    snapshot = execution_snapshot(account)
    account[0]['position'] = 1
    broker = s.Broker(Mock())
    broker.get = lambda resource: (case if resource == '/case' else
                                  [] if resource == '/orders?status=OPEN' else account)
    broker.order = Mock()
    with pytest.raises(RuntimeError, match='outside this batch'):
        broker.execute(case, snapshot, [('RTM100P', 5), ('RTM100C', 5)])
    broker.order.assert_not_called()


def test_future_forecast_does_not_justify_negative_current_carry():
    model = s.VolatilityModel()
    model.update([], 58, 1, .25)
    items = [news(59, 'Volatility this week is 50%'),
             news(59, 'Volatility next week is between 5% and 10%', 2)]
    model.update(items, 60, 1, .35)
    snapshot = s.market_snapshot(rows(vol=.35, tick=60), 60)
    s.greeks(snapshot)
    e = s.economics(snapshot, 100, model.stressed_vols(60))
    assert e['short'] > s.MIN_EDGE_PER_STRADDLE
    assert s.entry_filter(e, -1, model, 60) == 'short lacks favorable current-week volatility carry'


def test_unknown_stress_preserves_central_integrated_variance():
    model = s.VolatilityModel()
    model.update([], 9, 1, .3)
    model.update([news()], 10, 1, .4)
    original = model.fair(10)
    low, central, high = model.stressed_vols(10)
    assert low < original[1][0]
    assert central == original[1][1]
    assert high > original[1][2]
    assert model.baseline == .3


def test_confirmation_counts_distinct_ticks(capsys, monkeypatch):
    monkeypatch.setattr(s, "STAGED_ENTRY_MIN_EDGE", float("inf"))
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    for _ in range(3):
        _, plan = strategy.evaluate(dict(tick=10, period=1), rows(), items)
        assert not plan
    _, plan = strategy.evaluate(dict(tick=11, period=1), rows(tick=11), items)
    assert len(plan) == 2


def test_fixed_target_does_not_follow_edge_oscillations(monkeypatch, capsys):
    monkeypatch.setattr(s, 'ENTRY_CONFIRMATION_TICKS', 1)
    monkeypatch.setattr(s, 'BASE_STRADDLES', 1)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(), items)
    original_target = strategy.campaign['target']
    assert original_target > 0
    for tick, vol in [(11, .4), (12, .33), (13, .4)]:
        account = rows(vol=vol, tick=tick, position=original_target)
        snapshot, plan = strategy.evaluate(dict(tick=tick, period=1), account, items)
        assert strategy.campaign['target'] == original_target
        assert all(ticker == 'RTM' for ticker, _ in plan)
        e = s.economics(snapshot, 100, strategy.model.stressed_vols(tick))
        if vol == .4:
            assert s.target_size(e, 1, 1, snapshot) < original_target


def test_release_cannot_be_retraded_after_closing(monkeypatch, capsys):
    monkeypatch.setattr(s, 'ENTRY_CONFIRMATION_TICKS', 1)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(), items)
    strategy.evaluate(dict(tick=11, period=1), rows(tick=11, position=2), items)
    strategy.evaluate(dict(tick=12, period=1), rows(tick=12), items)
    _, plan = strategy.evaluate(dict(tick=18, period=1), rows(tick=18), items)
    assert not plan
    assert 'release already traded' in capsys.readouterr().out


def test_old_news_cannot_open_first_position(monkeypatch, capsys):
    monkeypatch.setattr(s, 'ENTRY_CONFIRMATION_TICKS', 1)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    _, plan = strategy.evaluate(dict(tick=31, period=1), rows(tick=31), items)
    assert not plan
    assert 'news entry window closed' in capsys.readouterr().out


def test_short_vol_band_applies_before_and_after_final_week():
    snapshot = s.market_snapshot(rows(position=-12), 10)
    s.greeks(snapshot)
    for tick in (10, 225):
        snapshot['tick'] = tick
        snapshot['option_delta'] = snapshot['portfolio_delta'] = -799
        assert s.effective_hedge_band(snapshot) == 800
        assert s.hedge_trade(snapshot) == 0
        snapshot['option_delta'] = snapshot['portfolio_delta'] = -800
        assert s.hedge_trade(snapshot) == 800


def test_executable_straddle_iv_roundtrip():
    premium = sum(s.bs(52.37, 51, .05, .33, kind)[0] for kind in 'CP')
    assert s.straddle_iv(premium, 52.37, 51, .05) == pytest.approx(.33)


def test_recorded_replay_never_connects_or_changes_live_setting(tmp_path, monkeypatch, capsys):
    import json
    path = tmp_path/'recording.jsonl'
    path.write_text(json.dumps(dict(event='snapshot', case=dict(tick=9, period=1),
                                    securities=rows(tick=9), news=[]))+'\n')
    session = Mock(side_effect=AssertionError('Replay connected to server'))
    monkeypatch.setattr(s.requests, 'Session', session)
    monkeypatch.setattr(s, 'DRY_RUN', False)
    s.replay(path)
    session.assert_not_called()
    assert s.DRY_RUN is False
    assert 'mode=DRY RUN' in capsys.readouterr().out


def test_audit_records_decision_and_deduplicates_ticks(tmp_path, monkeypatch, capsys):
    import json
    path = tmp_path/'audit.jsonl'
    monkeypatch.setattr(s, 'EVENT_LOG_PATH', path)
    strategy = s.Strategy()
    for _ in range(3):
        strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]['event'] == 'snapshot'
    assert 'portfolio_delta' in records[0] and 'hedge_band' in records[0]
    assert s.PASSWORD not in path.read_text()


@pytest.mark.parametrize('direction', [-1, 1])
def test_partial_add_trims_only_excess_and_does_not_retry(direction, capsys):
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%' if direction > 0 else 'Volatility this week is 1%')]
    account = rows(position=20*direction)
    next(r for r in account if r['ticker'] == 'RTM100C')['position'] = 40*direction
    snapshot, plan = strategy.evaluate(dict(tick=10, period=1), account, items)
    assert plan == [('RTM100C', -20*direction)]
    assert 100 not in strategy.exiting
    assert 'preserve matched straddles' in capsys.readouterr().out


def test_partial_repair_ratchets_campaign_target(capsys):
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(), items)
    strategy.evaluate(dict(tick=11, period=1), rows(tick=11), items)
    strategy.campaign['target'] = 40
    account = rows(tick=12, position=20)
    next(r for r in account if r['ticker'] == 'RTM100C')['position'] = 40
    _, plan = strategy.evaluate(dict(tick=12, period=1), account, items)
    assert plan == [('RTM100C', -20)]
    assert strategy.campaign['target'] == 20
    _, plan = strategy.evaluate(dict(tick=13, period=1), rows(tick=13, position=20), items)
    assert all(ticker == 'RTM' for ticker, _ in plan)


def test_bootstrap_historical_news_does_not_invalidate_hold(capsys):
    # All remaining weeks have information, so startup needs no unknown baseline.
    strategy = s.Strategy()
    items = [news(150, 'Volatility this week is 35%', 1),
             news(187, 'Volatility next week is between 8% and 13%', 2)]
    _, plan = strategy.evaluate(dict(tick=221, period=1), rows(vol=.35, tick=221, position=-20), items)
    assert not strategy.exiting
    assert all(ticker == 'RTM' for ticker, _ in plan)
    assert 'news=False' in capsys.readouterr().out


def test_restart_missing_baseline_manages_existing_options(capsys):
    strategy = s.Strategy()
    _, plan = strategy.evaluate(dict(tick=50, period=1), rows(tick=50, position=20), [news()])
    assert strategy.model.baseline is None
    assert not strategy.exiting
    assert all(ticker == 'RTM' for ticker, _ in plan)
    assert 'historical baseline unavailable' in capsys.readouterr().out


def test_new_release_supersedes_cooldown(capsys):
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    strategy.cooldown_until = 20
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(), items)
    assert strategy.cooldown_until == -1
    _, plan = strategy.evaluate(dict(tick=11, period=1), rows(tick=11), items)
    assert len(plan) == 2


def test_larger_target_still_obeys_gamma_and_batch_limits():
    snapshot = s.market_snapshot(rows(), 10)
    s.greeks(snapshot)
    e = s.economics(snapshot, 100, (.7, .7, .7))
    quantity = s.target_size(e, 1, 1, snapshot)
    assert 100 < quantity <= s.OPTION_NET_LIMIT//2
    assert quantity*e['risk'] <= s.GAMMA_DELTA_BUDGET
    plan = s.batch_plan(snapshot, e, 1, quantity)
    assert all(abs(q) <= s.STRONG_EDGE_PAIR_BATCH for _, q in plan)


@pytest.mark.parametrize('invalidate', [False, True])
def test_provisional_strike_survives_ranking_change_only_while_eligible(monkeypatch, capsys, invalidate):
    monkeypatch.setattr(s, "STAGED_ENTRY_MIN_EDGE", float("inf"))
    original = s.economics

    def ranked_economics(snapshot, strike, vols):
        e = original(snapshot, strike, vols)
        e['short'] = -100
        e['long'] = 20
        if strike == 100:
            e['long'] = 100 if snapshot['tick'] == 10 else (0 if invalidate else 80)
        if strike == 105 and snapshot['tick'] >= 11:
            e['long'] = 200
        return e

    monkeypatch.setattr(s, 'economics', ranked_economics)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    _, plan = strategy.evaluate(dict(tick=10, period=1), rows(), items)
    assert not plan
    assert strategy.confirmation[0][1] == 100
    _, plan = strategy.evaluate(dict(tick=11, period=1), rows(tick=11), items)
    if invalidate:
        assert not plan
        assert strategy.confirmation[0][1] == 105
        _, plan = strategy.evaluate(dict(tick=12, period=1), rows(tick=12), items)
        assert plan and all(ticker.startswith('RTM105') for ticker, _ in plan)
    else:
        assert plan and all(ticker.startswith('RTM100') for ticker, _ in plan)
        assert strategy.campaign['strike'] == 100


def test_new_release_restarts_provisional_confirmation(capsys, monkeypatch):
    monkeypatch.setattr(s, "STAGED_ENTRY_MIN_EDGE", float("inf"))
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(), items)
    items.append(news(11, 'Volatility this week is 85%', 2))
    _, plan = strategy.evaluate(dict(tick=11, period=1), rows(tick=11), items)
    assert not plan
    assert strategy.confirmation[2] == 1
    _, plan = strategy.evaluate(dict(tick=12, period=1), rows(tick=12), items)
    assert len(plan) == 2


def test_strong_edge_batches_shrink_for_temporary_delta():
    snapshot = s.market_snapshot(rows(), 10)
    s.greeks(snapshot)
    e = s.economics(snapshot, 100, (.5, .5, .5))
    plan = s.batch_plan(snapshot, e, 1, 100)
    assert [q for _, q in plan] == [50, 50]
    weak = dict(e, long=27)
    assert [q for _, q in s.batch_plan(snapshot,weak,1,100)] == [30,30]
    snapshot['rtm']['position'] = s.EMERGENCY_DELTA-10
    s.greeks(snapshot)
    plan = s.batch_plan(snapshot, e, 1, 100)
    assert plan and max(q for _, q in plan) < s.STRONG_EDGE_PAIR_BATCH
    delta = snapshot['portfolio_delta']
    for ticker, qty in plan:
        leg_delta = qty*s.OPTION_MULTIPLIER*snapshot['rows'][ticker]['delta']
        delta += leg_delta
        assert abs(leg_delta) <= s.LEG_DELTA_BUDGET
        assert abs(delta) < s.EMERGENCY_DELTA


@pytest.mark.parametrize('delta,expected', [(100, 0), (-200, 0), (1299, 0), (1300, -1300),
                                           (-1300, 1300), (3500, -3500)])
def test_requested_hedge_boundaries(delta, expected):
    snapshot = s.market_snapshot(rows(position=2), 10)
    s.greeks(snapshot)
    snapshot['portfolio_delta'] = snapshot['option_delta'] = delta
    assert s.hedge_trade(snapshot) == expected


def test_exact_size_multiplier_and_forecast_sleeve(monkeypatch):
    snap = s.market_snapshot(rows(), 10)
    s.greeks(snap)
    e = s.economics(snap, 100, (.5, .5, .5))
    e.update(long=60, short=-100, risk=1)
    assert s.baseline_target_size(e, 1, 1, snap) == 66
    assert s.target_size(e, 1, 1, snap) == 264
    assert s.baseline_target_size(e, 1, .6, snap) == 20
    assert s.target_size(e, 1, .6, snap) == 60
    e.update(long=10, central_long=50)
    assert s.entry_target(e, 1, .6, snap) == 0
    monkeypatch.setattr(s, 'ENABLE_FORECAST_SLEEVE', True)
    assert s.entry_target(e, 1, .6, snap) == 2
    assert s.entry_target(e, 1, 1, snap) == 0
    e['long'] = 60
    assert s.entry_target(e, 1, .6, snap) == s.target_size(e, 1, .6, snap)


@pytest.mark.parametrize('second_edge,adds', [(79, True), (27, True), (25, True), (24.99, False), (18, False)])
def test_additions_use_absolute_current_edge(monkeypatch, capsys, second_edge, adds):
    original = s.economics
    edge = {'value': 100}
    def economics(snapshot, strike, vols):
        e = original(snapshot, strike, vols)
        e['long'] = edge['value'] if strike == 100 else 0
        e['short'] = -100
        return e
    monkeypatch.setattr(s, 'economics', economics)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    snap, plan = strategy.evaluate(dict(tick=10, period=1), rows(), items)
    starter = plan[0][1]
    original_target = strategy.campaign['target']
    assert 0 < starter < original_target
    edge['value'] = second_edge
    # No tick delay or recovery near the original edge is necessary.
    snap, next_plan = strategy.evaluate(dict(tick=10, period=1), rows(position=starter), items)
    assert bool(next_plan) == adds
    if adds:
        assert all(q > 0 for _, q in next_plan)
        assert strategy.campaign['target'] == original_target
        batch = s.STRONG_EDGE_PAIR_BATCH if second_edge > s.STAGED_ENTRY_MIN_EDGE else s.PAIRED_BATCH
        assert max(q for _, q in next_plan) == min(batch, original_target-starter)
    else:
        assert strategy.campaign['add_paused']
    assert snap['campaign']['metrics']['additions_blocked_by_persistence_rule'] == 0


def test_scale_out_needs_distinct_ticks_and_does_not_repeat(monkeypatch, capsys):
    original = s.economics
    edge = {'value': 30}
    def economics(snapshot, strike, vols):
        e = original(snapshot, strike, vols)
        e.update(long=edge['value'], short=-100)
        return e
    monkeypatch.setattr(s, 'economics', economics)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(position=10), items)
    edge['value'] = -1
    for _ in range(3):
        _, plan = strategy.evaluate(dict(tick=11, period=1), rows(tick=11, position=10), items)
        assert not plan and not strategy.scaled_strikes
    edge['value'] = 5
    strategy.evaluate(dict(tick=12, period=1), rows(tick=12, position=10), items)
    edge['value'] = -1
    strategy.evaluate(dict(tick=13, period=1), rows(tick=13, position=10), items)
    _, plan = strategy.evaluate(dict(tick=14, period=1), rows(tick=14, position=10), items)
    assert len(plan) == 2 and all(q == -5 for _, q in plan)
    assert not strategy.exiting
    for tick in (14, 15, 16):
        _, plan = strategy.evaluate(dict(tick=tick, period=1), rows(tick=tick, position=5), items)
        assert not plan  # No repeated halvings or add-back.
    edge['value'] = -9
    for _ in range(3):
        _, plan = strategy.evaluate(dict(tick=17, period=1), rows(tick=17, position=5), items)
        assert not plan and not strategy.exiting
    _, plan = strategy.evaluate(dict(tick=18, period=1), rows(tick=18, position=5), items)
    assert strategy.exiting == {100}
    assert len(plan) == 2 and all(q == -5 for _, q in plan)


def test_repriced_gamma_shock_limits_target_under_fair_vol_convergence():
    snap = s.market_snapshot(rows(vol=.5, tick=230), 230)
    snap['decision_vols'] = (.09, .09, .09)
    s.greeks(snap)
    e = s.economics(snap, 100, snap['decision_vols'])
    quantity = s.target_size(e, -1, 1, snap)
    assert e['risk'] > e['gamma']*e['move']
    assert quantity*e['risk'] <= s.MAX_3SIGMA_DELTA_SHOCK
    assert (quantity+1)*e['risk'] > s.MAX_3SIGMA_DELTA_SHOCK or quantity == s.MAX_TARGET_STRADDLES


def test_trade_ledger_fill_cashflow_no_double_spread_deduction(capsys):
    from case1_trade_ledger import TradeLedger
    events = []
    ledger = TradeLedger(lambda name, **data: events.append((name, data)), 2, .02)
    account = rows()
    snap = s.market_snapshot(account, 10)
    s.greeks(snap)
    snap['selected'] = dict(strike=100, long=-30, short=50)
    ledger.observe(snap)
    def fill(ticker, quantity, price, source='actual_vwap'):
        row = next(r for r in account if r['ticker'] == ticker)
        before = dict(row, bid=price-.01, ask=price+.01)
        row['position'] += quantity
        ledger.fill(before, quantity, abs(quantity), price, source, len(events)+1, account)
    fill('RTM100C', -2, 4)
    fill('RTM100P', -2, 3)
    fill('RTM', 10, 50)
    fill('RTM100C', 2, 3)
    fill('RTM100P', 2, 2)
    assert not ledger.closed  # Stock hedge has not been flattened yet.
    fill('RTM', -10, 51)
    t = ledger.closed[0]
    assert t.option_pnl == 400
    assert t.stock_pnl == 10
    assert t.option_commissions == 16
    assert t.stock_commissions == pytest.approx(.4)
    assert t.net == pytest.approx(393.6)
    assert t.turnover == 20 and t.max_position == 2
    assert t.result()['target_pairs'] == 2
    assert t.result()['maximum_matched_pairs'] == 2
    assert t.result()['target_filled_pct'] == 100
    ledger.summary()
    ledger.summary()
    assert sum(name == 'case_summary' for name, _ in events) == 1
    summary = next(data for name, data in events if name == 'case_summary')
    assert summary['target_pairs'] == summary['maximum_matched_pairs'] == 2
    assert summary['target_filled_pct'] == 100
    assert summary['additions_blocked_by_stale_tick'] == 0
    assert summary['additions_blocked_by_worsened_quote'] == 0
    assert summary['additions_blocked_by_persistence_rule'] == 0


def test_ledger_flags_inherited_positions_instead_of_fabricating_profit(capsys):
    from case1_trade_ledger import TradeLedger
    events = []
    ledger = TradeLedger(lambda name, **data: events.append((name, data)), 2, .02)
    account = rows(position=2)
    snap = s.market_snapshot(account, 10)
    s.greeks(snap)
    ledger.observe(snap)
    row = next(r for r in account if r['ticker'] == 'RTM100C')
    ledger.fill(row, -2, 2, 1, 'actual_vwap', 1, account)
    assert not ledger.closed and ledger.active is None
    assert events[0][0] == 'unattributed_fill'


def test_broker_records_actual_vwap(monkeypatch):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    session = Mock()
    session.post.return_value.json.return_value = {'order_id': 123}
    broker = s.Broker(session)
    broker.get = Mock(return_value=dict(status='TRANSACTED', quantity_filled=3, vwap=.88))
    assert broker.order(rows()[1], 3) == 3
    assert broker.last_fill == dict(price=.88, source='actual_vwap', order_id=123)


def test_additions_resume_on_first_qualifying_quote(monkeypatch, capsys):
    original = s.economics
    def economics(snapshot, strike, vols):
        e = original(snapshot, strike, vols)
        e['long'] = ({10: 100, 11: 20}.get(snapshot['tick'], 27)) if strike == 100 else 0
        e['short'] = -100
        return e
    monkeypatch.setattr(s, 'economics', economics)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    _, plan = strategy.evaluate(dict(tick=10, period=1), rows(), items)
    starter = plan[0][1]
    _, plan = strategy.evaluate(dict(tick=11, period=1), rows(tick=11, position=starter), items)
    assert not plan and strategy.campaign['add_paused']
    _, plan = strategy.evaluate(dict(tick=12, period=1), rows(tick=12, position=starter), items)
    assert len(plan) == 2 and all(q > 0 for _, q in plan)
    assert not strategy.campaign['add_paused']


def test_coverage_sizes_unknown_weeks_conservatively():
    model = s.VolatilityModel()
    model.update([], 9, 1, .30)
    model.update([news()], 10, 1, .25)
    coverage = s.analyst_variance_coverage(model, 10)
    times = s.remaining_times(10)
    assert coverage == pytest.approx(.2**2*times[0]/(.2**2*times[0]+.3**2*sum(times[1:])))
    snap = s.market_snapshot(rows(), 10)
    s.greeks(snap)
    e = s.economics(snap, 100, (.5, .5, .5))
    e.update(long=70, risk=1)
    snap['analyst_coverage'] = coverage
    low = s.target_size(e, 1, 1, snap)
    snap['analyst_coverage'] = 1
    high = s.target_size(e, 1, 1, snap)
    assert low < high <= s.OPTION_NET_LIMIT//2
    assert s.baseline_target_size(e,1,1,snap) == int(s.BASE_STRADDLES*70/s.EDGE_SIZE_SCALE*2)
    assert high == min(4*s.baseline_target_size(e,1,1,snap),
                       int(.75*s.offline_3sigma_max_pairs(e,snap,1)),
                       s.risk_size_limit(e,snap,1))


@pytest.mark.parametrize('missing_fills', [[2, 3], [0, 0, 0]])
def test_exclusive_pair_completion_or_rollback(monkeypatch, missing_fills):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    case = dict(tick=10, period=1, status='ACTIVE')
    account = rows()
    snap = execution_snapshot(account)
    s.greeks(snap)
    broker = s.Broker(Mock())
    calls = []
    attempt = iter(missing_fills)
    def get(resource):
        if resource == '/case': return dict(case)
        if resource == '/news': return []
        if resource == '/orders?status=OPEN': return []
        if resource == '/securities': return [dict(r) for r in account]
        raise AssertionError(resource)
    def order(row, change):
        calls.append((row['ticker'], change, broker.execution_state))
        filled = next(attempt) if row['ticker'] == 'RTM100C' else abs(change)
        next(r for r in account if r['ticker'] == row['ticker'])['position'] += filled if change > 0 else -filled
        if len(calls) == 1:
            case['tick'] = 11  # A tick change must not abandon matching leg.
        return filled
    broker.get, broker.order = get, order
    broker.execute(dict(tick=10, period=1, status='ACTIVE'), snap,
                   [('RTM100P', 5), ('RTM100C', 5)])
    assert broker.pending_pair is None
    pair = [r['position'] for r in account if r['ticker'] in ('RTM100C', 'RTM100P')]
    assert pair == ([5, 5] if sum(missing_fills) else [0, 0])
    assert calls[1][2] == 'PAIR_PENDING'
    if not sum(missing_fills):
        assert calls[-1] == ('RTM100P', -5, 'ROLLBACK_PENDING')


def test_unresolved_rollback_blocks_further_orders(monkeypatch):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    case = dict(tick=10, period=1, status='ACTIVE')
    account = rows()
    snap = execution_snapshot(account)
    s.greeks(snap)
    broker = s.Broker(Mock())
    broker.get = lambda resource: (case if resource == '/case' else [] if resource == '/orders?status=OPEN' else [dict(r) for r in account])
    count = 0
    def order(row, change):
        nonlocal count
        count += 1
        filled = abs(change) if count == 1 else 0
        next(r for r in account if r['ticker'] == row['ticker'])['position'] += filled
        return filled
    broker.order = order
    with pytest.raises(RuntimeError, match='rollback exhausted'):
        broker.execute(case, snap, [('RTM100P', 5), ('RTM100C', 5)])
    assert broker.pending_pair is not None
    with pytest.raises(RuntimeError, match='unresolved PAIR_PENDING'):
        broker.execute(case, snap, [])


def refreshed_campaign(monkeypatch, *, edge=27, worsen=True):
    """A real Strategy snapshot and Broker with an in-memory exchange, no HTTP."""
    original = s.economics
    def economics(snapshot, strike, vols):
        e = original(snapshot, strike, vols)
        e.update(long=(44 if snapshot['tick'] == 10 else edge) if strike == 100 else -100, short=-100)
        return e
    monkeypatch.setattr(s, 'economics', economics)
    strategy = s.Strategy()
    strategy.evaluate(dict(tick=9, period=1), rows(tick=9), [])
    items = [news(body='Volatility this week is 90%')]
    strategy.evaluate(dict(tick=10, period=1), rows(), items)
    strike = strategy.campaign['strike']
    assert strike == 100
    strategy.campaign['target'] = 36
    strategy.campaign['metrics']['target_pairs'] = 36
    snapshot, plan = strategy.evaluate(dict(tick=10, period=1), rows(position=11), items)
    account = rows(tick=11, position=11)
    if worsen:
        for row in account[1:]:
            row['bid'] += .07
            row['ask'] += .07
    broker = s.Broker(Mock())
    state = dict(tick=11, period=1, status='ACTIVE')
    orders = []
    def get(resource):
        if resource == '/case':
            return dict(state)
        if resource == '/news':
            return list(items)
        if resource == '/orders?status=OPEN':
            return []
        if resource == '/securities':
            return [dict(r) for r in account]
        raise AssertionError(resource)
    def order(row, change):
        orders.append((row['ticker'], change, broker.execution_state))
        next(r for r in account if r['ticker'] == row['ticker'])['position'] += change
        broker.last_fill = dict(price=row['ask'] if change > 0 else row['bid'],
                                source='actual_vwap', order_id=len(orders))
        return abs(change)
    broker.get = get
    broker.order = order
    return broker, snapshot, plan, account, state, items, orders


def test_refreshed_addition_accepts_changed_tick_worse_quote_and_edge_44_to_27(monkeypatch, capsys):
    broker, snapshot, plan, account, state, items, orders = refreshed_campaign(monkeypatch)
    broker.execute(dict(tick=10, period=1, status='ACTIVE'), snapshot, plan)
    assert [(t, q) for t, q, _ in orders] == [(t, 25) for t, _ in plan]
    assert orders[1][2] == 'PAIR_PENDING'
    assert broker.pending_pair is None
    assert broker.execution_state == 'ACTIVE_CAMPAIGN'
    assert all(r['position'] == 36 for r in account if r['ticker'] in ('RTM100C', 'RTM100P'))
    metrics = snapshot['campaign']['metrics']
    assert metrics['additions_refreshed_after_tick'] == 1
    assert metrics['additions_repriced_after_quote'] == 1
    assert metrics['additions_blocked_by_stale_tick'] == 0
    assert metrics['additions_blocked_by_worsened_quote'] == 0
    assert metrics['additions_blocked_by_persistence_rule'] == 0


@pytest.mark.parametrize('failure', ['edge', 'news', 'inactive', 'reset', 'positions', 'risk', 'direction'])
def test_refreshed_addition_rejects_invalid_thesis_or_limits(monkeypatch, capsys, failure):
    broker, snapshot, plan, account, state, items, orders = refreshed_campaign(
        monkeypatch, edge=24.99 if failure == 'edge' else 27)
    if failure == 'news':
        items.append(news(11, 'Volatility this week is 10%', 2))
    if failure == 'inactive':
        state['status'] = 'STOPPED'
    if failure == 'reset':
        state['tick'] = 0
    if failure == 'positions':
        account[0]['position'] = 1
    if failure == 'risk':
        monkeypatch.setattr(s, 'MAX_3SIGMA_DELTA_SHOCK', 1)
    if failure == 'direction':
        original = s.economics
        def reverse(snapshot, strike, vols):
            e = original(snapshot, strike, vols)
            e['short'] = 100
            return e
        monkeypatch.setattr(s, 'economics', reverse)
    if failure == 'positions':
        with pytest.raises(RuntimeError, match='Account changed'):
            broker.execute(dict(tick=10, period=1, status='ACTIVE'), snapshot, plan)
    else:
        broker.execute(dict(tick=10, period=1, status='ACTIVE'), snapshot, plan)
    assert not orders


@pytest.mark.parametrize('always_changes', [False, True])
def test_entry_refresh_retries_without_posting_stale_quotes(monkeypatch, capsys, always_changes):
    broker, snapshot, plan, account, state, items, orders = refreshed_campaign(monkeypatch)
    original = broker.get
    calls = 0
    def get(resource):
        nonlocal calls
        if resource == '/case':
            calls += 1
            if (always_changes and calls % 2 == 0) or calls == 2:
                state['tick'] += 1
        return original(resource)
    broker.get = get
    broker.execute(dict(tick=10, period=1, status='ACTIVE'), snapshot, plan)
    metrics = snapshot['campaign']['metrics']
    if always_changes:
        assert not orders
        assert metrics['additions_blocked_by_stale_tick'] == 1
    else:
        assert len(orders) == 2
        assert metrics['additions_blocked_by_stale_tick'] == 0
        assert calls >= 4


@pytest.mark.parametrize('band', [1200, 1300, 1500, 700, 800, 900])
def test_wider_hedge_band_boundaries_and_emergency(monkeypatch, band):
    long = band >= 1200
    monkeypatch.setattr(s, 'LONG_VOL_HEDGE_BAND' if long else 'SHORT_VOL_HEDGE_BAND', band)
    snap = s.market_snapshot(rows(position=10 if long else -10), 10)
    s.greeks(snap)
    for sign in (1, -1):
        snap['option_delta'] = snap['portfolio_delta'] = sign*(band-1)
        assert s.hedge_trade(snap) == 0
        snap['option_delta'] = snap['portfolio_delta'] = sign*band
        assert s.hedge_trade(snap) == -sign*band
        snap['option_delta'] = snap['portfolio_delta'] = sign*s.EMERGENCY_DELTA
        assert s.hedge_trade(snap) == -sign*s.EMERGENCY_DELTA
    assert s.EMERGENCY_DELTA == 3500 < s.HARD_DELTA_LIMIT == 6000 < s.OFFICIAL_DELTA_LIMIT == 7000


def test_full_supported_exact_campaign_has_full_initial_target(monkeypatch, capsys):
    original = s.economics
    def economics(snapshot, strike, vols):
        e = original(snapshot, strike, vols)
        e.update(short=44 if strike == 100 else -100, long=-100)
        return e
    monkeypatch.setattr(s, 'economics', economics)
    strategy = s.Strategy()
    snap, plan = strategy.evaluate(dict(tick=225, period=1), rows(vol=.40, tick=225),
                                  [news(225, 'Volatility this week is 25%')])
    assert snap['analyst_coverage'] == pytest.approx(1)
    assert strategy.campaign['starter'] == strategy.campaign['target']
    assert all(q < 0 for _, q in plan)
    assert abs(plan[0][1]) == min(s.STRONG_EDGE_PAIR_BATCH,strategy.campaign['target'])


def test_announced_delta_limit_configures_live_gates():
    try:
        s.configure_announced_delta_limit(5000)
        assert s.OFFICIAL_DELTA_LIMIT == 5000
        assert s.HARD_DELTA_LIMIT == 4000
        assert s.EMERGENCY_DELTA == s.SOFT_DELTA_LIMIT == 2400
        assert s.MAX_3SIGMA_DELTA_SHOCK == 4000
        with pytest.raises(ValueError,match='announced'):
            s.configure_announced_delta_limit(1000)
    finally:
        s.configure_announced_delta_limit(7000)


def test_announced_delta_limit_is_read_from_matching_news_period():
    items=[dict(period=1,body='The delta limit for this sub-heat is 10,000 and penalty 1%'),
           dict(period=2,body='The delta limit for this sub-heat is 5,000 and penalty 0.5%')]
    assert s.announced_delta_limit_from_news(items,2)==5000
    assert s.announced_delta_limit_from_news(items,1)==10000
    assert s.announced_delta_limit_from_news([],2) is None


def test_delta_limit_selection_defaults_to_7000_unless_news_or_override():
    assert s.choose_announced_delta_limit(None) == 7000
    assert s.choose_announced_delta_limit(5000)==5000
    assert s.choose_announced_delta_limit(None,supplied=6000)==6000
    with pytest.raises(ValueError,match='differs'):
        s.choose_announced_delta_limit(5000,supplied=7000)


def test_known_campaign_can_add_after_initial_news_window(monkeypatch, capsys):
    broker, snapshot, plan, account, state, items, orders = refreshed_campaign(monkeypatch)
    state['tick'] = 35  # Past 20-tick entry window; current-week thesis still valid.
    broker.execute(dict(tick=10, period=1, status='ACTIVE'), snapshot, plan)
    assert len(orders) == 2


def test_hedge_refresh_and_multi_batch_flatten(monkeypatch, capsys):
    account = rows()
    account[0]['position'] = 15000
    case = dict(tick=11, period=1, status='ACTIVE')
    broker = s.Broker(Mock())
    calls, orders = 0, []
    def get(resource):
        nonlocal calls
        if resource == '/case':
            calls += 1
            if calls == 2:
                case['tick'] = 12
            return dict(case)
        if resource == '/securities':
            return [dict(r) for r in account]
        raise AssertionError(resource)
    def order(row, change):
        orders.append(change)
        account[0]['position'] += change
        return abs(change)
    broker.get, broker.order = get, order
    broker.hedge_after_batch(dict(tick=10, period=1))
    assert orders == [-10000, -5000]
    assert account[0]['position'] == 0


@pytest.mark.parametrize('value,expected', [(1, 1), (0, 0), (None, 2), (-1, 2), (float('nan'), 2), ('bad', 2)])
def test_security_fee_validation(value, expected):
    assert s.execution_fee(dict(trading_fee=value), 2)[0] == expected


def test_economics_uses_security_fees():
    snap = s.market_snapshot(rows(), 10)
    s.greeks(snap)
    before = s.economics(snap, 100, (.5, .5, .5))
    for row in snap['pairs'][100].values():
        row['trading_fee'] = 1
    snap['rtm']['trading_fee'] = .01
    after = s.economics(snap, 100, (.5, .5, .5))
    saving = 4+abs(before['delta'])*.02
    assert after['long']-before['long'] == pytest.approx(saving)
    assert after['short']-before['short'] == pytest.approx(saving)


def test_pending_completion_uses_combined_edge_after_large_quote_change(monkeypatch, capsys):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    account = rows()
    snap = execution_snapshot(account)
    broker = s.Broker(Mock())
    case = dict(tick=10, period=1, status='ACTIVE')
    orders = []
    def get(resource):
        if resource == '/case': return dict(case)
        if resource == '/orders?status=OPEN': return []
        if resource == '/news': return []
        if resource == '/securities': return [dict(r) for r in account]
        raise AssertionError(resource)
    def order(row, change):
        orders.append((row['ticker'], change, broker.execution_state))
        next(r for r in account if r['ticker'] == row['ticker'])['position'] += change
        if len(orders) == 1:
            other = next(r for r in account if r['ticker'] == 'RTM100C')
            other['bid'] += .12
            other['ask'] += .12
        return abs(change)
    broker.get, broker.order = get, order
    broker.execute(case, snap, [('RTM100P', 5), ('RTM100C', 5)])
    assert orders == [('RTM100P', 5, 'FLAT'), ('RTM100C', 5, 'PAIR_PENDING')]
    assert broker.pending_pair is None
    assert all(r['position'] == 5 for r in account if r['ticker'] in ('RTM100C', 'RTM100P'))


def test_refreshed_short_addition_accepts_current_edge_after_quote_worsens(monkeypatch, capsys):
    monkeypatch.setattr(s, 'DRY_RUN', False)
    model = s.VolatilityModel(weeks={w: (225, 'ACTUAL', .25, .25) for w in range(1, 5)},
                              latest_release=225, confidence=1)
    original = rows(vol=.40, tick=225, position=-11)
    snap = s.market_snapshot(original, 225)
    snap.update(valuation_model=model, decision_vols=model.stressed_vols(225),
                current_high=.25, news_seen=set(),
                campaign=dict(strike=100, direction=-1, target=36,
                              metrics=dict(target_pairs=36, maximum_matched_pairs=11)))
    s.greeks(snap)
    account = rows(vol=.40, tick=226, position=-11)
    for r in account[1:]:
        r['bid'] -= .03
        r['ask'] -= .03
    broker = s.Broker(Mock())
    case = dict(tick=226, period=1, status='ACTIVE')
    orders = []
    def get(resource):
        if resource == '/case': return dict(case)
        if resource == '/orders?status=OPEN': return []
        if resource == '/news': return []
        if resource == '/securities': return [dict(r) for r in account]
        raise AssertionError(resource)
    def order(row, change):
        orders.append((row['ticker'], change, broker.execution_state))
        next(r for r in account if r['ticker'] == row['ticker'])['position'] += change
        return abs(change)
    broker.get, broker.order = get, order
    refreshed = s.market_snapshot(account, 226)
    refreshed['decision_vols'] = model.stressed_vols(226)
    s.greeks(refreshed)
    assert s.economics(refreshed, 100, refreshed['decision_vols'])['short'] >= s.ADD_EDGE_THRESHOLD
    broker.execute(dict(tick=225, period=1, status='ACTIVE'), snap,
                   [('RTM100P', -25), ('RTM100C', -25)])
    assert len(orders) == 2
    assert all(q < 0 for _, q, _ in orders)
    assert orders[1][2] == 'PAIR_PENDING'
    assert snap['campaign']['metrics']['additions_refreshed_after_tick'] == 1
    assert snap['campaign']['metrics']['additions_repriced_after_quote'] == 1
