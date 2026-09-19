"""Offline full-chain option optimizer. No broker, network, or order submission.

Run: python case1/case1_full_chain_optimizer.py --events case1/integrated_variance_events.jsonl
The objective is stressed executable *entry* value, not forecast realized P&L.
"""
import argparse
import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

import case1_integrated_variance as strategy
from case1_trade_ledger import execution_fee
from case1_simple import option_identity

DRY_RUN = True
ENTRY_DELTA_BAND = 800
SCENARIO_DELTA_LIMIT = 6000
SCENARIO_SIGMAS = 3
STOCK_LIMIT = 50000
OPTION_GROSS_LIMIT = 2500
OPTION_NET_LIMIT = 1000
OPTION_ORDER_LIMIT = 100
STOCK_ORDER_LIMIT = 10000
HEDGE_ROUND_TRIP_RESERVE = 0.0  # Stock spread and fees are charged separately below.


@dataclass(frozen=True)
class OptionEdge:
    ticker: str
    strike: float
    kind: str
    bid: float
    ask: float
    fair_low: float
    fair_central: float
    fair_high: float
    buy_edge: float
    sell_edge: float
    delta: float  # Shares per contract.
    gamma: float  # Delta shares per $1 RTM move.
    vega_point: float  # Dollars per contract for +1 vol percentage point.
    market_vol: float


def contract_edges(snapshot, decision_vols):
    """Round-trip stressed edges per contract, using executable entry quotes.

    Option fees and an exit spread are charged per leg. The $3 straddle rehedge
    reserve is split equally across two legs. Stock hedge cost is portfolio-wide.
    """
    result = []
    spot, maturity = snapshot['spot'], snapshot['maturity']
    for row in snapshot['options']:
        ident = option_identity(row)
        if ident is None or row['iv'] is None:
            continue
        strike, kind = ident
        prices = [strategy.bs(spot, strike, maturity, v, kind)[0] for v in decision_vols]
        bid, ask = float(row['bid']), float(row['ask'])
        fee = execution_fee(row, strategy.OPTION_COMMISSION)[0]
        reserve = (2*fee + (ask-bid)*strategy.OPTION_MULTIPLIER*strategy.EXIT_SPREAD_FACTOR
                   + strategy.REHEDGE_COST/2)
        _, delta, gamma = strategy.bs(spot, strike, maturity, row['iv'], kind)
        d1 = ((math.log(spot/strike) + (strategy.RISK_FREE_RATE+row['iv']**2/2)*maturity)
              / (row['iv']*math.sqrt(maturity)))
        vega_point = (spot*math.exp(-d1*d1/2)/math.sqrt(2*math.pi)*math.sqrt(maturity)
                      *strategy.OPTION_MULTIPLIER*.01)
        result.append(OptionEdge(row['ticker'], strike, kind, bid, ask, *prices,
                                 (prices[0]-ask)*strategy.OPTION_MULTIPLIER-reserve,
                                 (bid-prices[2])*strategy.OPTION_MULTIPLIER-reserve,
                                 delta*strategy.OPTION_MULTIPLIER,
                                 gamma*strategy.OPTION_MULTIPLIER,
                                 vega_point, row['iv']))
    return sorted(result, key=lambda x:(x.strike,x.kind))


def sigma_move(snapshot, decision_vols):
    vol = max(decision_vols[2], snapshot['atm_iv'] or 0,
              snapshot.get('current_high',0),
              *(r['iv'] or 0 for r in snapshot['options']))
    return snapshot['spot']*vol*math.sqrt(strategy.DT_YEAR)


def scenario_deltas(contracts, spot, maturity, move):
    """Share deltas at ±1/2/3 one-tick sigma moves, with today's IV held fixed."""
    return {sigma: np.array([
        strategy.bs(max(.0001,spot+sigma*move), c.strike, maturity,
                    c.market_vol, c.kind)[1]*strategy.OPTION_MULTIPLIER
        for c in contracts], dtype=float) for sigma in (-3,-2,-1,0,1,2,3)}


def hedge_cost_per_share(snapshot):
    rtm = snapshot['rtm']
    spread = float(rtm['ask'])-float(rtm['bid'])
    fee = execution_fee(rtm, strategy.RTM_COMMISSION)[0]
    return spread+2*fee+HEDGE_ROUND_TRIP_RESERVE


def score(contracts, quantities, hedge, stock_cost):
    return (sum((c.buy_edge*q if q>0 else c.sell_edge*(-q))
                for c,q in zip(contracts,quantities))
            - abs(hedge)*stock_cost)


def metrics(snapshot, contracts, quantities, hedge, decision_vols):
    move = sigma_move(snapshot,decision_vols)
    deltas = scenario_deltas(contracts,snapshot['spot'],snapshot['maturity'],move)
    q = np.asarray(quantities,dtype=float)
    return dict(gross_contracts=int(sum(abs(int(x)) for x in quantities)),
                net_contracts=int(sum(int(x) for x in quantities)),
                option_delta=float(deltas[0]@q),
                hedge=int(hedge),
                portfolio_delta=float(deltas[0]@q+hedge),
                gamma=float(sum(c.gamma*x for c,x in zip(contracts,quantities))),
                scenario={k:float(v@q+hedge) for k,v in deltas.items()},
                move=move,
                expected_edge=score(contracts,quantities,hedge,hedge_cost_per_share(snapshot)))


def cheapest_hedge(option_delta, band=ENTRY_DELTA_BAND):
    """Least expensive whole-share hedge satisfying the entry delta band."""
    if option_delta>band:
        return max(-STOCK_LIMIT,math.floor(band-option_delta))
    if option_delta< -band:
        return min(STOCK_LIMIT,math.ceil(-band-option_delta))
    return 0


def optimize(snapshot, contracts, decision_vols,
             entry_band=ENTRY_DELTA_BAND, scenario_limit=SCENARIO_DELTA_LIMIT,
             scenario_sigmas=SCENARIO_SIGMAS, gross_limit=OPTION_GROSS_LIMIT,
             net_limit=OPTION_NET_LIMIT):
    """Integer contract targets, one integer stock hedge, and no live orders.

    Hard scenario bounds cover ±1..scenario_sigmas. All ±3-sigma deltas are
    reported, including those outside the configured hard scenario range.
    """
    if any(r['position'] for r in snapshot['options']) or snapshot['rtm']['position']:
        raise ValueError('Optimizer comparison requires a flat account snapshot')
    if not contracts:
        raise ValueError('No valid option contracts')
    if not 0<=entry_band<=scenario_limit or not 1<=scenario_sigmas<=3:
        raise ValueError('Invalid delta constraints')
    n=len(contracts); count=2*n+2
    buy=slice(0,n); sell=slice(n,2*n); h=2*n; h_abs=2*n+1
    obj=np.zeros(count)
    obj[buy]=[-c.buy_edge for c in contracts]
    obj[sell]=[-c.sell_edge for c in contracts]
    obj[h_abs]=hedge_cost_per_share(snapshot)
    lower=np.zeros(count); upper=np.full(count,float(net_limit))
    lower[h]=-STOCK_LIMIT;upper[h]=STOCK_LIMIT;upper[h_abs]=STOCK_LIMIT
    bounds=Bounds(lower,upper)
    integrality=np.ones(count,dtype=int)
    integrality[h_abs]=0
    rows=[];lo=[];hi=[]
    def add(coeff,l=-np.inf,u=np.inf):
        rows.append(coeff);lo.append(l);hi.append(u)
    gross=np.zeros(count);gross[:2*n]=1
    add(gross,u=gross_limit)
    net=np.zeros(count);net[buy]=1;net[sell]=-1
    add(net,l=-net_limit,u=net_limit)
    abs_h=np.zeros(count);abs_h[h]=1;abs_h[h_abs]=-1
    add(abs_h,u=0)
    abs_h=np.zeros(count);abs_h[h]=-1;abs_h[h_abs]=-1
    add(abs_h,u=0)
    deltas=scenario_deltas(contracts,snapshot['spot'],snapshot['maturity'],
                           sigma_move(snapshot,decision_vols))
    for sigma in range(-scenario_sigmas,scenario_sigmas+1):
        d=np.zeros(count);d[buy]=deltas[sigma];d[sell]=-deltas[sigma];d[h]=1
        limit=entry_band if sigma==0 else scenario_limit
        add(d,l=-limit,u=limit)
    result=milp(obj,integrality=integrality,bounds=bounds,
                constraints=LinearConstraint(np.asarray(rows),lo,hi),
                options=dict(time_limit=20,mip_rel_gap=.005))
    if result.x is None or result.status not in (0,1):
        raise RuntimeError(f'MILP failed: {result.message}')
    x=np.rint(result.x).astype(int)
    quantities=(x[buy]-x[sell]).tolist()
    hedge=int(x[h])
    output=metrics(snapshot,contracts,quantities,hedge,decision_vols)
    output.update(quantities={c.ticker:int(q) for c,q in zip(contracts,quantities) if q},
                  solver_status=result.message,objective_gap=getattr(result,'mip_gap',None),
                  max_order_size=OPTION_ORDER_LIMIT)
    if (output['gross_contracts']>gross_limit or abs(output['net_contracts'])>net_limit
            or abs(hedge)>STOCK_LIMIT or abs(output['portfolio_delta'])>entry_band+1e-6
            or any(abs(output['scenario'][z])>scenario_limit+1e-6
                   for z in range(-scenario_sigmas,scenario_sigmas+1) if z)):
        raise RuntimeError('Solver result failed independent risk validation')
    return output


def split_orders(quantities, max_order=OPTION_ORDER_LIMIT):
    """Advisory order sizes only; never submits or simulates a fill."""
    orders=[]
    for ticker, quantity in sorted(quantities.items()):
        remaining=abs(quantity)
        while remaining:
            chunk=min(remaining,max_order)
            orders.append((ticker,chunk if quantity>0 else -chunk))
            remaining-=chunk
    return orders


def split_stock_orders(hedge, max_order=STOCK_ORDER_LIMIT):
    """Advisory RTM order sizes; no order submission or fill assumption."""
    remaining=abs(hedge)
    orders=[]
    while remaining:
        chunk=min(remaining,max_order)
        orders.append(chunk if hedge>0 else -chunk)
        remaining-=chunk
    return orders


def historical_snapshots(path,ticks=None,run_id=None):
    """Build pre-trade snapshots chronologically from one completed run."""
    records=[json.loads(line) for line in open(path,encoding='utf-8')]
    summaries=[r for r in records if r.get('event')=='case_summary' and
               (r.get('run_id')==run_id if run_id else r.get('completed_straddles')==3)]
    if not summaries:raise ValueError('No matching completed run in log')
    summary=summaries[-1]
    trades=[r for r in records if r.get('event')=='trade_closed' and
            r.get('run_id')==summary['run_id']]
    if ticks is None:
        ticks=tuple(int(t['entry_tick']) for t in trades)
    start=max(r['time'] for r in records if r.get('event')=='configuration' and
              r['time']<min(t['time'] for t in trades))
    records=sorted((r for r in records if r.get('event')=='snapshot' and
                    start<r['time']<summary['time']),key=lambda r:r['time'])
    model=strategy.VolatilityModel();seen=set();outputs={}
    for record in records:
        case=record['case'];tick=case['tick']
        if tick in seen:continue
        seen.add(tick)
        snapshot=strategy.market_snapshot(record['securities'],tick)
        model.update(record['news'],tick,case.get('period'),snapshot['atm_iv'])
        if tick not in ticks:continue
        vols=model.stressed_vols(tick)
        if vols is None:raise ValueError(f'Missing fair volatility at tick {tick}')
        snapshot['decision_vols']=vols
        snapshot['audit_model']=copy.deepcopy(model)
        snapshot['analyst_coverage']=strategy.analyst_variance_coverage(model,tick)
        current_week=min(int(tick//strategy.TICKS_PER_WEEK)+1,strategy.TOTAL_WEEKS)
        info=model.weeks.get(current_week)
        snapshot['current_high']=info[3] if info else (model.baseline or 0)
        strategy.greeks(snapshot,vols[1])
        outputs[tick]=(snapshot,vols,model.confidence)
    missing=set(ticks)-set(outputs)
    if missing:raise ValueError(f'Missing snapshots: {sorted(missing)}')
    return outputs,trades


def old_strategy_plan(snapshot,vols,confidence,contracts):
    candidates=[strategy.economics(snapshot,k,vols) for k in snapshot['pairs']]
    eligible=[]
    model=snapshot['audit_model']
    for e in candidates:
        direction=strategy.signal_direction(e,confidence)
        if strategy.entry_filter(e,direction,model,snapshot['tick']) is None:
            eligible.append((e,direction))
    if not eligible:raise ValueError('Historical signal no longer qualifies')
    e,direction=max(eligible,key=lambda pair:
        max(pair[0]['long'],pair[0]['short'])*confidence/
        (1+pair[0]['risk']/strategy.GAMMA_DELTA_BUDGET))
    target=strategy.baseline_target_size(e,direction,confidence,snapshot)
    q={c.ticker:(direction*target if c.strike==e['strike'] else 0) for c in contracts}
    option_delta=sum(c.delta*q[c.ticker] for c in contracts)
    hedge=cheapest_hedge(option_delta)
    detail=metrics(snapshot,contracts,[q[c.ticker] for c in contracts],hedge,vols)
    detail.update(strike=e['strike'],direction='LONG' if direction>0 else 'SHORT',
                  pairs=target,initial_batch=min(target,strategy.PAIRED_BATCH),
                  robust_pair_edge=e['long' if direction>0 else 'short'],
                  risk_per_pair=e['risk'],current_risk_cap=strategy.baseline_risk_size_limit(e,snapshot),
                  coverage=snapshot['analyst_coverage'])
    return detail,e


def pair_capacity(snapshot,e,vols,entry_band=ENTRY_DELTA_BAND,
                  scenario_limit=SCENARIO_DELTA_LIMIT,scenario_sigmas=SCENARIO_SIGMAS):
    """Pair-only capacity under stock, option, and scenario delta limits."""
    contracts=contract_edges(snapshot,vols)
    pair=[c for c in contracts if c.strike==e['strike']]
    direction=1 if e['long']>=e['short'] else -1
    option_cap=min(OPTION_NET_LIMIT//2,OPTION_GROSS_LIMIT//2)
    allowed=[]
    for size in range(option_cap+1):
        q=[direction*size]*2
        opt_delta=sum(c.delta*v for c,v in zip(pair,q))
        hedge=cheapest_hedge(opt_delta,entry_band)
        if abs(hedge)>STOCK_LIMIT:continue
        data=metrics(snapshot,pair,q,hedge,vols)
        if (abs(data['portfolio_delta'])<=entry_band+1e-6 and
                all(abs(data['scenario'][z])<=scenario_limit+1e-6
                    for z in range(-scenario_sigmas,scenario_sigmas+1) if z)):
            allowed.append(size)
    return max(allowed,default=0)


def best_pair_plan(snapshot,contracts,vols,entry_band=ENTRY_DELTA_BAND,
                   scenario_limit=SCENARIO_DELTA_LIMIT,scenario_sigmas=SCENARIO_SIGMAS):
    """Best single-strike equal-call/put plan under the optimizer's same limits."""
    by_strike={}
    for c in contracts:
        by_strike.setdefault(c.strike,{})[c.kind]=c
    deltas=scenario_deltas(contracts,snapshot['spot'],snapshot['maturity'],
                           sigma_move(snapshot,vols))
    index={c.ticker:i for i,c in enumerate(contracts)}
    cap=min(OPTION_NET_LIMIT//2,OPTION_GROSS_LIMIT//2)
    stock_cost=hedge_cost_per_share(snapshot)
    best=None
    for strike,legs in by_strike.items():
        if set(legs)!={'C','P'}:continue
        ids=[index[legs[k].ticker] for k in ('C','P')]
        for direction in (-1,1):
            edge=sum(legs[k].buy_edge if direction>0 else legs[k].sell_edge
                     for k in ('C','P'))
            if edge<=0:continue
            unit={z:direction*float(deltas[z][ids].sum())
                  for z in range(-scenario_sigmas,scenario_sigmas+1)}
            for pairs in range(1,cap+1):
                hedge=cheapest_hedge(unit[0]*pairs,entry_band)
                if abs(hedge)>STOCK_LIMIT:continue
                if any(abs(unit[z]*pairs+hedge)>(entry_band if z==0 else scenario_limit)+1e-6
                       for z in unit):continue
                value=edge*pairs-abs(hedge)*stock_cost
                if best is None or value>best['expected_edge']:
                    q=[direction*pairs if i in ids else 0 for i in range(len(contracts))]
                    best=metrics(snapshot,contracts,q,hedge,vols)
                    best.update(strike=strike,direction='LONG' if direction>0 else 'SHORT',
                                pairs=pairs,quantities={contracts[i].ticker:q[i] for i in ids})
    if best is None:raise ValueError('No feasible positive-edge pair')
    return best


def sizing_stages(snapshot,e,confidence):
    edge=e['long'] if e['long']>=e['short'] else e['short']
    base=strategy.BASE_STRADDLES*edge/strategy.EDGE_SIZE_SCALE
    conf=base*confidence
    coverage=snapshot['analyst_coverage']
    multiplier=(strategy.HIGH_COVERAGE_SIZE_MULTIPLIER if confidence==1 and
                coverage>=strategy.HIGH_COVERAGE_MIN else
                strategy.EXACT_VOL_SIZE_MULTIPLIER if confidence==1 else
                strategy.FORECAST_SIZE_MULTIPLIER)
    scaled=conf*multiplier
    after_coverage=int(scaled*(strategy.MIN_COVERAGE_SIZE_FACTOR+
                                (1-strategy.MIN_COVERAGE_SIZE_FACTOR)*coverage))
    shock_cap=math.floor(strategy.BASELINE_3SIGMA_DELTA_SHOCK/e['risk'])
    target=min(after_coverage,shock_cap,strategy.MAX_TARGET_STRADDLES,
               strategy.OPTION_NET_LIMIT//2,strategy.OPTION_GROSS_LIMIT//2)
    starter=(math.ceil(target*(strategy.INITIAL_ENTRY_FRACTION+
                 (1-strategy.INITIAL_ENTRY_FRACTION)*coverage))
             if confidence==1 and edge>strategy.STAGED_ENTRY_MIN_EDGE else target)
    return dict(raw_base=base,after_confidence=conf,after_signal_multiplier=scaled,
                after_coverage=after_coverage,shock_cap=shock_cap,
                campaign_cap=strategy.MAX_TARGET_STRADDLES,
                option_net_cap=strategy.OPTION_NET_LIMIT//2,
                option_gross_cap=strategy.OPTION_GROSS_LIMIT//2,
                target=target,starter=starter,first_batch=min(starter,strategy.PAIRED_BATCH),
                scenario_risk_per_pair=e['risk'])


def counterfactual_sizes(snapshot,e,vols,sizes=(25,50,100,200,300,400,500)):
    pair=[c for c in contract_edges(snapshot,vols) if c.strike==e['strike']]
    direction=1 if e['long']>=e['short'] else -1
    output=[]
    for n in sizes:
        q=[direction*n]*2
        opt_delta=sum(c.delta*size for c,size in zip(pair,q))
        hedge=cheapest_hedge(opt_delta)
        data=metrics(snapshot,pair,q,hedge,vols)
        data.update(pairs=n,initial_edge=n*e['long' if direction>0 else 'short'],
                    one_tick_delta_shock=max(abs(data['scenario'][z]-data['scenario'][0]) for z in (-1,1)),
                    official_initial_ok=(2*n<=OPTION_GROSS_LIMIT and 2*n<=OPTION_NET_LIMIT
                       and abs(hedge)<=STOCK_LIMIT and abs(data['portfolio_delta'])<=strategy.OFFICIAL_DELTA_LIMIT),
                    official_three_sigma_ok=max(abs(data['scenario'][z]) for z in (-3,3))<=strategy.OFFICIAL_DELTA_LIMIT)
        output.append(data)
    return output


def render_report(snapshots,trades):
    """Reproducible Markdown audit of the recorded, flat pre-entry snapshots."""
    lines=['# Case 1 full-chain capacity audit', '',
           'Offline DRY RUN on the latest completed three-trade run in `integrated_variance_events.jsonl`. '
           'Values are stressed **entry-value estimates**, not simulated realized P&L. '
           'All comparisons use the same recorded bid/ask quotes and flat pre-entry account.', '',
           'The optimizer holds current delta within ±800 shares and every ±1, ±2, and ±3 '
           'one-tick-sigma scenario within ±6000 shares. The latter leaves a 1000-share '
           'buffer to the approximate ±7000 official limit; all scenario positions are '
           'marked using fixed quoted IV, with no full-path repricing or path-dependent hedge costs. '
           'The objective does include a simple rehedge reserve.', '',
           '## Observed trades and capacity', '',
           '| Entry tick | Direction / strike | Robust edge / pair | Historical target / maximum filled | '
           'Pre-boost target | Pre-boost starter / first batch | Selected-strike pair capacity | '
           'Unused pairs vs filled | Official option pair cap | Historical net P&L |',
           '|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    research=[]
    trade_by_tick={t['entry_tick']:t for t in trades}
    for tick,(snap,vols,conf) in snapshots.items():
        contracts=contract_edges(snap,vols)
        old,e=old_strategy_plan(snap,vols,conf,contracts)
        stages=sizing_stages(snap,e,conf)
        cap=pair_capacity(snap,e,vols)
        pair=best_pair_plan(snap,contracts,vols)
        opt=optimize(snap,contracts,vols)
        t=trade_by_tick[tick]
        research.append((tick,snap,vols,contracts,old,e,stages,cap,pair,opt,t))
        lines.append(f'| {tick} | {old["direction"]} {old["strike"]:g} | '
                     f'${old["robust_pair_edge"]:.2f} | '
                     f'{t["entry_target_pairs"]} / {t["max_position"]} | {old["pairs"]} | '
                     f'{stages["starter"]} / {stages["first_batch"]} | {cap} | '
                     f'{cap-t["max_position"]} | 500 | '
                     f'${t["net_pnl"]:,.0f} |')
    lines+=['', 'The latest run filled only 19, 12, and 11 matched pairs. Its targets (19, 38, 36) '
            'differ from the pre-boost baseline replay (21, 41, 53) because code and fee '
            'metadata changed after that run. This capacity is a static comparison, '
            'not a replay of the actual order path.', '',
            'The official net option limit of 1000 contracts caps a same-direction straddle at '
            '500 pairs; gross 2500 would allow 1250. The recorded snapshot has five strikes; '
            'the optimizer evaluates every available call and put, including individual legs. '
            'The official delta limit is announced by news for each sub-heat; ±7000 is the '
            'user-supplied working assumption for this audit, not a fixed competition rule. '
            'Costs use the recorded snapshot metadata or the current strategy fallbacks; '
            'verify actual fee settings before comparing with a different sub-heat.', '',
            '## Sizing bottleneck by signal', '',
            '| Tick | Coverage | Base from edge | After exact/forecast multiplier | '
            'After coverage factor | 3σ shock cap | Campaign cap | Net / gross pair caps | '
            'Final target |',
            '|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for tick,snap,vols,contracts,old,e,s,cap,pair,opt,t in research:
        lines.append(f'| {tick} | {old["coverage"]:.1%} | {s["raw_base"]:.1f} | '
                     f'{s["after_signal_multiplier"]:.1f} | {s["after_coverage"]} | '
                     f'{s["shock_cap"]} | {s["campaign_cap"]} | '
                     f'{s["option_net_cap"]} / {s["option_gross_cap"]} | {s["target"]} |')
    lines+=['', 'The edge-scaled base and analyst coverage are the binding target restrictions '
            'at all three entries. The shock cap is 124, 78, and 69 pairs; it does not bind '
            'these pre-boost targets, but constrains expansion. The soft 3500-share and hard '
            '6000-share delta limits are execution gates, not direct inputs to `target_size`. '
            'The starter rule and 30-pair batch cap further slow deployment. The 100%-supported '
            'tick-227 signal received the 2× exact-signal multiplier, yet its baseline target '
            'was only 53 pairs and its first batch 30.', '',
            '## Boosted best-strike sizing (current code)', '',
            'This keeps the selected straddle and paired execution. A 3×/60% rule applies '
            'normally; fully analyst-supported exact signals use 4×/75%. The existing '
            'live stress gate may impose an additional cap.', '',
            '| Tick | Pre-boost target | Offline ±3σ pair capacity | Fraction cap | Boosted target |',
            '|---:|---:|---:|---:|---:|']
    for tick,snap,vols,contracts,old,e,s,cap,pair,opt,t in research:
        direction=1 if old['direction']=='LONG' else -1
        fraction=.75 if old['coverage']>=1-1e-9 else .60
        boosted=strategy.target_size(e,direction,snap['audit_model'].confidence,snap)
        lines.append(f'| {tick} | {old["pairs"]} | {cap} | {int(fraction*cap)} | {boosted} |')
    lines+=['',
            '## Same-snapshot portfolio comparison', '',
            '| Tick | Construction | Positions | Gross / net options | RTM hedge | Current delta | '
            'Robust entry edge | Multiple of old |',
            '|---:|---|---|---:|---:|---:|---:|---:|']
    for tick,snap,vols,contracts,old,e,s,cap,pair,opt,t in research:
        for name,p in [('Pre-boost target',old),('Best equal pair',pair),('Full chain',opt)]:
            positions=(f'{p["direction"]} {p["pairs"]}×{p["strike"]:g} C+P'
                       if name!='Full chain' else ', '.join(f'{k} {v:+d}' for k,v in p['quantities'].items()))
            lines.append(f'| {tick} | {name} | {positions} | '
                         f'{p["gross_contracts"]} / {p["net_contracts"]:+d} | '
                         f'{p["hedge"]:+d} | {p["portfolio_delta"]:+.0f} | '
                         f'${p["expected_edge"]:,.0f} | '
                         f'{p["expected_edge"]/old["expected_edge"]:.2f}× |')
    lines+=['', 'At equal risk bounds, most of the improvement comes from deploying more size. '
            'Across these snapshots, the best equal-call/put single-strike plan captures '
            'roughly 90–93% of the full-chain estimated edge. Strike choice and unequal legs '
            'add the remainder. The tick-76 best equal pair moves from strike 48 to 50; '
            'the full-chain plan at every tick uses unequal call and put quantities.', '',
            '## Full-chain executable edge table', '',
            'Fair is the central Black–Scholes model mark. Buy uses the low stressed fair '
            'value minus ask; sell uses bid minus high stressed fair value. Both deduct '
            'round-trip option commissions, an exit-spread reserve, and half of the '
            'per-straddle rehedge reserve. Values are dollars per contract.', '']
    for tick,snap,vols,contracts,old,e,s,cap,pair,opt,t in research:
        lines += [f'### Tick {tick}: coverage {old["coverage"]:.1%}', '',
                  '| Ticker | Bid | Ask | Fair | Buy edge | Sell edge | Delta shares | '
                  'Gamma shares/$ | Vega $/vol pt |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for c in contracts:
            lines.append(f'| {c.ticker} | {c.bid:.2f} | {c.ask:.2f} | {c.fair_central:.2f} | '
                         f'{c.buy_edge:+.2f} | {c.sell_edge:+.2f} | {c.delta:+.1f} | '
                         f'{c.gamma:.2f} | {c.vega_point:.2f} |')
        lines.append('')
    lines+=['## Delta scenario audit', '',
            'Scenario deltas include the chosen static RTM hedge. A one-tick sigma move uses '
            'the largest available stressed or quoted IV and `DT_YEAR`; it is a stress '
            'coordinate, not a calibrated tail probability.', '',
            '| Tick | Plan | -3σ | -2σ | -1σ | Now | +1σ | +2σ | +3σ |',
            '|---:|---|---:|---:|---:|---:|---:|---:|---:|']
    for tick,snap,vols,contracts,old,e,s,cap,pair,opt,t in research:
        for name,p in [('Current',old),('Best pair',pair),('Full chain',opt)]:
            lines.append(f'| {tick} | {name} | '+' | '.join(
                f'{p["scenario"][z]:+.0f}' for z in (-3,-2,-1,0,1,2,3))+' |')
    lines+=['', 'Relaxing the hard envelope to ±2σ raises modeled edge, but the resulting '
            '±3σ deltas exceed the approximate official limit. This is a sensitivity '
            'comparison, not a recommended live profile.', '',
            '| Tick | ±2σ-constrained edge | Worst ±3σ delta | ±3σ-constrained edge |',
            '|---:|---:|---:|---:|']
    for tick,snap,vols,contracts,old,e,s,cap,pair,opt,t in research:
        loose=optimize(snap,contracts,vols,scenario_sigmas=2)
        worst=max(abs(loose['scenario'][z]) for z in (-3,3))
        lines.append(f'| {tick} | ${loose["expected_edge"]:,.0f} | '
                     f'{worst:,.0f} | ${opt["expected_edge"]:,.0f} |')
    lines+=['', 'The old 2750-share projected 3σ shock budget is far inside the approximate '
            'official ±7000 delta boundary. The proposed offline ±6000 scenario envelope '
            'uses materially more of it while retaining 1000 shares of buffer. Scenario '
            'hedging or a tighter limit may be needed for fast moves, stale marks, and '
            'multi-tick gaps; none is modeled here.', '',
            '## Equal-pair size counterfactual at the old selected strike', '',
            'Initial edge below is the existing robust pair-edge estimate times size. '
            'Gamma and shock are model approximations. “Initial OK” checks recorded '
            'option/stock/current-delta limits; “3σ OK” separately checks the approximate '
            'official ±7000 delta limit after a static hedge.', '',
            '| Tick | Pairs | Initial edge | Option contracts | Initial delta | Gamma | '
            'Max ±1σ delta change | Initial OK | 3σ OK |',
            '|---:|---:|---:|---:|---:|---:|---:|---|---|']
    for tick,snap,vols,contracts,old,e,s,cap,pair,opt,t in research:
        for row in counterfactual_sizes(snap,e,vols):
            lines.append(f'| {tick} | {row["pairs"]} | ${row["initial_edge"]:,.0f} | '
                         f'{row["gross_contracts"]} | {row["portfolio_delta"]:+.0f} | '
                         f'{row["gamma"]:+.0f} | {row["one_tick_delta_shock"]:.0f} | '
                         f'{"yes" if row["official_initial_ok"] else "no"} | '
                         f'{"yes" if row["official_three_sigma_ok"] else "no"} |')
    lines+=['', 'At 500 pairs on each of the three chosen strikes, summed initial robust edge '
            'is about $84k, but all three static portfolios breach ±7000 under at least '
            'one ±3σ move. At the proposed ±6000/3σ bound, the full-chain optimizer’s '
            'summed estimated entry edge is about $40k. Neither figure predicts realized '
            'case profit: future volatility, edge decay, timing, hedge P&L, and order '
            'execution determine the result.', '',
            '## Recommendation and limits', '',
            'Keep the live paired execution, actual-fill reconciliation, news invalidation, '
            'and ledger unchanged. Test staged larger campaigns offline with a current-delta '
            'target of ±800, hard current and ±1/2/3σ scenario envelope of ±6000, '
            'official option/stock constraints, per-order 100-contract and 10,000-share '
            'splits, and fresh '
            'quote/news checks before every child order. Recompute scenario risk after '
            'every actual fill and hedge; avoid sending a full target as if fills were atomic.', '',
            'This audit supports under-deployment as a major source of foregone modeled '
            'edge. It does not establish that the volatility forecast is calibrated or that '
            'a larger portfolio would have made $100k per sub-heat. The historical three '
            'trades yielded only about $942 total net, including a first-trade loss and '
            'large stock-hedge losses; scaling those realized paths could amplify losses. '
            'The offline optimizer makes no live API calls or orders.', '']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events',type=Path,default=Path(__file__).with_name('integrated_variance_events.jsonl'))
    parser.add_argument('--ticks',type=int,nargs='+',help='Override snapshot ticks')
    parser.add_argument('--run-id',help='Select a completed run; default is latest three-trade run')
    parser.add_argument('--report',type=Path,help='Write a Markdown audit report')
    args=parser.parse_args()
    snapshots,trades=historical_snapshots(args.events,args.ticks,args.run_id)
    if args.report:
        args.report.write_text(render_report(snapshots,trades),encoding='utf-8')
    print('DRY RUN ONLY; no broker or network. Edge is estimated at recorded executable quotes.')
    for tick,(snap,vols,confidence) in snapshots.items():
        contracts=contract_edges(snap,vols)
        old,e=old_strategy_plan(snap,vols,confidence,contracts)
        optimized=optimize(snap,contracts,vols)
        print(f'\nTICK {tick} coverage={old["coverage"]:.1%} fair-vol={vols}')
        for c in contracts:
            print(f'{c.ticker:8} bid={c.bid:.2f} ask={c.ask:.2f} fair={c.fair_central:.2f} '
                  f'buy={c.buy_edge:+.2f} sell={c.sell_edge:+.2f} '
                  f'delta={c.delta:+.1f} gamma={c.gamma:.2f} vega/pt={c.vega_point:.2f}')
        for label,plan in [('OLD STRATEGY',old),('FULL-CHAIN OPTIMIZER',optimized)]:
            print(label,plan)
        print('expected-edge ratio',optimized['expected_edge']/old['expected_edge'] if old['expected_edge']>0 else None)
        print('pair-only scenario capacity',pair_capacity(snap,e,vols))
        print('sizing stages',sizing_stages(snap,e,confidence))
        for item in counterfactual_sizes(snap,e,vols):
            print('sizing counterfactual',item['pairs'],item['initial_edge'],item['gross_contracts'],
                  item['option_delta'],item['gamma'],item['one_tick_delta_shock'],
                  item['official_initial_ok'],item['official_three_sigma_ok'])
        print('DRY RUN ORDER CHUNKS',split_orders(optimized['quantities']))
        print('DRY RUN RTM CHUNKS',split_stock_orders(optimized['hedge']))


if __name__=='__main__':
    main()
