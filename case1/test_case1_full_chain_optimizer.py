"""Offline checks for the full-chain capacity study."""
import copy
from pathlib import Path

import pytest

import case1_full_chain_optimizer as audit
import case1_integrated_variance as strategy

BASELINE_RUN_ID='ce5be58f-a5e3-4ec0-8239-15622a04e516'
RECENT_RUN_ID='86a1c432-2aff-43f6-8881-af6187de2802'


@pytest.fixture(scope='module')
def recorded():
    return audit.historical_snapshots(
        Path(__file__).with_name('integrated_variance_events.jsonl'),
        run_id=BASELINE_RUN_ID)[0]


def test_hedge_rounding_respects_band():
    for delta in (-800.2, -800.0, 0, 800.0, 800.2, 971.99):
        hedge=audit.cheapest_hedge(delta)
        assert abs(delta+hedge)<=audit.ENTRY_DELTA_BAND+1e-9
        if abs(delta)<=audit.ENTRY_DELTA_BAND:
            assert hedge==0


def test_contract_edges_charge_costs_and_keep_ordered_stress(recorded):
    snapshot,vols,_=recorded[151]
    contracts=audit.contract_edges(snapshot,vols)
    assert len(contracts)==len(snapshot['options'])
    for c in contracts:
        assert c.fair_low<=c.fair_central<=c.fair_high
        assert c.vega_point>0 and c.gamma>0
        assert c.buy_edge<(c.fair_low-c.ask)*100
        assert c.sell_edge<(c.bid-c.fair_high)*100
        assert c.buy_edge+c.sell_edge<0


@pytest.mark.parametrize('tick',[76,151,227])
def test_optimizer_beats_pair_at_same_scenario_limits(recorded,tick):
    snapshot,vols,confidence=recorded[tick]
    contracts=audit.contract_edges(snapshot,vols)
    old,e=audit.old_strategy_plan(snapshot,vols,confidence,contracts)
    pair=audit.best_pair_plan(snapshot,contracts,vols)
    optimized=audit.optimize(snapshot,contracts,vols)
    assert optimized['expected_edge']>=pair['expected_edge']-100
    assert pair['expected_edge']>old['expected_edge']*3
    assert optimized['gross_contracts']<=audit.OPTION_GROSS_LIMIT
    assert abs(optimized['net_contracts'])<=audit.OPTION_NET_LIMIT
    assert abs(optimized['hedge'])<=audit.STOCK_LIMIT
    assert abs(optimized['portfolio_delta'])<=audit.ENTRY_DELTA_BAND
    assert all(abs(optimized['scenario'][z])<=audit.SCENARIO_DELTA_LIMIT+1e-6
               for z in (-3,-2,-1,1,2,3))
    chunks=audit.split_orders(optimized['quantities'])
    assert all(0<abs(quantity)<=audit.OPTION_ORDER_LIMIT for _,quantity in chunks)
    assert sum(abs(q) for _,q in chunks)==optimized['gross_contracts']
    stock_chunks=audit.split_stock_orders(optimized['hedge'])
    assert sum(stock_chunks)==optimized['hedge']
    assert all(0<abs(q)<=audit.STOCK_ORDER_LIMIT for q in stock_chunks)
    assert pair['pairs']<=500
    assert audit.pair_capacity(snapshot,e,vols)>=old['pairs']


def test_full_support_is_reported_and_large_static_sizes_flag_risk(recorded):
    snapshot,vols,confidence=recorded[227]
    assert snapshot['analyst_coverage']==pytest.approx(1)
    contracts=audit.contract_edges(snapshot,vols)
    _,e=audit.old_strategy_plan(snapshot,vols,confidence,contracts)
    sizes=audit.counterfactual_sizes(snapshot,e,vols)
    assert sizes[-1]['pairs']==500
    assert sizes[-1]['official_initial_ok']
    assert not sizes[-1]['official_three_sigma_ok']
    assert audit.split_stock_orders(24765)==[10000,10000,4765]


def test_requires_flat_account_and_never_submits_orders(recorded,monkeypatch):
    snapshot,vols,_=recorded[76]
    contracts=audit.contract_edges(snapshot,vols)
    occupied=copy.deepcopy(snapshot)
    occupied['rtm']['position']=1
    with pytest.raises(ValueError,match='flat'):
        audit.optimize(occupied,contracts,vols)
    assert audit.DRY_RUN is True
    assert not hasattr(audit,'Broker')


def test_report_reconciles_recorded_fill_count(recorded):
    snapshots,trades=audit.historical_snapshots(
        Path(__file__).with_name('integrated_variance_events.jsonl'),
        run_id=BASELINE_RUN_ID)
    report=audit.render_report(snapshots,trades)
    assert '19 / 19' in report
    assert '38 / 12' in report
    assert '36 / 11' in report
    assert 'RTM49C' in report


@pytest.mark.parametrize('tick,baseline,capacity,boosted',[
    (76,21,247,63),(151,41,164,98),(227,53,184,138)])
def test_boosted_best_strike_targets_match_offline_capacity(recorded,tick,baseline,capacity,boosted):
    snapshot,vols,confidence=recorded[tick]
    contracts=audit.contract_edges(snapshot,vols)
    old,e=audit.old_strategy_plan(snapshot,vols,confidence,contracts)
    direction=1 if old['direction']=='LONG' else -1
    assert strategy.baseline_target_size(e,direction,confidence,snapshot)==baseline
    assert strategy.offline_3sigma_max_pairs(e,snapshot,direction)==capacity
    assert strategy.target_size(e,direction,confidence,snapshot)==boosted
    assert boosted<=strategy.OPTION_NET_LIMIT//2


def test_fast_fading_recent_signal_can_open_larger_verified_pair_batch():
    snapshots,_=audit.historical_snapshots(
        Path(__file__).with_name('integrated_variance_events.jsonl'),
        run_id=RECENT_RUN_ID)
    snapshot,vols,_=snapshots[225]
    e=strategy.economics(snapshot,50,vols)
    assert e['short']>strategy.STAGED_ENTRY_MIN_EDGE
    plan=strategy.batch_plan(snapshot,e,-1,106)
    assert [abs(q) for _,q in plan]==[50,50]
    assert all(abs(q)<=strategy.OPTION_MAX_ORDER for _,q in plan)
