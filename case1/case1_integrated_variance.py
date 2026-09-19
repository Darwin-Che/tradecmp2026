"""Event-driven Case 1 strategy; defaults to a 7000 delta limit.

DRY_RUN prints orders against actual account positions; it does not simulate fills.
Baseline observations must precede release ticks. Starting mid-case can therefore
block entries until a subsequent release has an observed pre-news IV.
"""
import math
import time
import argparse
import json
import re
from pathlib import Path
from dataclasses import dataclass, field

import requests
from case1_trade_ledger import TradeLedger, execution_fee
from case1_simple import (api_get, parse_news,
                          news_identity, number, option_identity, valid_security,
                          black_scholes)

API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16655/v1"  # Volatility Trading case
USERNAME = "goal-2"
PASSWORD = "credit"

DRY_RUN = False
AUTO_ENTRIES = True
AUTO_SIGNAL_EXITS = True  # False makes signal exits advisory; safety exits still run.
REENTRY_COOLDOWN_TICKS = 5
ENTRY_NEWS_WINDOW_TICKS = 10
EXACT_ENTRY_NEWS_WINDOW_TICKS = 20
ENTRY_CONFIRMATION_TICKS = 2  # Distinct ticks, not duplicate polling cycles.
CURRENT_VOL_MARGIN = 0.02  # Require favorable current-week carry by two vol points.
UNKNOWN_VOL_STRESS = 0.03  # Sensitivity around pre-news baseline, not analyst bounds.
MAX_CALL_PUT_IV_GAP = 0.05
EVENT_LOG_PATH = Path(__file__).with_name('integrated_variance_events.jsonl')
PAIR_COMPLETION_ATTEMPTS = 3
PAIR_COMPLETION_SECONDS = 6.0
PAIR_ROLLBACK_ATTEMPTS = 4
ENTRY_REFRESH_ATTEMPTS = 3
POSITION_RECONCILE_ATTEMPTS = 3
POSITION_RECONCILE_DELAY_SECONDS = 0.2
TOTAL_TICKS = 300
TOTAL_WEEKS = 4
TICKS_PER_WEEK = 75
TRADING_DAYS_PER_YEAR = 240
TRADING_DAYS_PER_WEEK = 5
TICKS_PER_YEAR = TICKS_PER_WEEK * TRADING_DAYS_PER_YEAR / TRADING_DAYS_PER_WEEK
DT_YEAR = 1 / TICKS_PER_YEAR
OPTION_MULTIPLIER = 100
RISK_FREE_RATE = 0.0
OPTION_COMMISSION = 2.0  # per contract per side
RTM_COMMISSION = 0.02  # per share per side
EXIT_SPREAD_FACTOR = 1.0  # reserve a full current option spread for exit
REHEDGE_COST = 3.0  # dollars per straddle
MIN_ENTRY_EDGE = 18.0
MIN_EDGE_PER_STRADDLE = MIN_ENTRY_EDGE  # Compatibility for external analysis.
EXIT_EDGE = 0.0
EXIT_CONFIRMATION_TICKS = 2
EXIT_EDGE_PER_STRADDLE = EXIT_EDGE
EXACT_VOL_SIZE_MULTIPLIER = 1.5
HIGH_COVERAGE_SIZE_MULTIPLIER = 2.0
HIGH_COVERAGE_MIN = 0.80
MIN_COVERAGE_SIZE_FACTOR = 0.40
FORECAST_SIZE_MULTIPLIER = 1.0
INITIAL_ENTRY_FRACTION = 0.30
STAGED_ENTRY_MIN_EDGE = 40.0
ADD_EDGE_THRESHOLD = 25.0
FULL_EXIT_EDGE = -8.0
ENABLE_FORECAST_SLEEVE = False
FORECAST_SPECULATIVE_EDGE = 28.0
FORECAST_SLEEVE_FRACTION = 0.15
FORECAST_CONFIDENCE = 0.6
BASE_STRADDLES = 10
EDGE_SIZE_SCALE = 18.0
MAX_TARGET_STRADDLES = 100
BASELINE_3SIGMA_DELTA_SHOCK = 2750
MAX_3SIGMA_DELTA_SHOCK = 6000
GAMMA_DELTA_BUDGET = MAX_3SIGMA_DELTA_SHOCK
SOFT_DELTA_LIMIT = 3500
HARD_DELTA_LIMIT = 6000
OFFICIAL_DELTA_LIMIT = 7000
DEFAULT_DELTA_LIMIT = 7000
MOVE_SIGMAS = 3.0
LONG_VOL_HEDGE_BAND = 1300
SHORT_VOL_HEDGE_BAND = 800
EMERGENCY_DELTA = 3500
LEG_DELTA_BUDGET = 5000
OPTION_GROSS_LIMIT = 2500
OPTION_NET_LIMIT = 1000
RTM_LIMIT = 50000
OPTION_MAX_ORDER = 100
RTM_MAX_ORDER = 10000
PAIRED_BATCH = 30
STRONG_EDGE_PAIR_BATCH = 50  # At most one existing EXIT_BATCH to unwind promptly.
BOOSTED_TARGET_MULTIPLIER = 3
BOOSTED_CAPACITY_FRACTION = 0.60
FULL_SUPPORT_TARGET_MULTIPLIER = 4
FULL_SUPPORT_CAPACITY_FRACTION = 0.75
CAPACITY_ENTRY_DELTA_BAND = 800
EXIT_BATCH = 50
NO_NEW_ENTRY_TICK = 270
FORCE_EXIT_TICK = 275  # Leave time for multiple confirmed exit batches.
POLL_SECONDS = 0.5
ORDER_TIMEOUT_SECONDS = 2.0
ORDER_POLL_SECONDS = 0.1
API_TIMEOUT_SECONDS = 5
MAX_BASELINE_AGE_TICKS = 5
IV_MIN = 0.00001
IV_MAX = 5.0
IV_ITERATIONS = 70
ENABLE_PARITY_DETECTOR = False  # executable conversion/reversal alerts
MIN_PARITY_EDGE = 20.0
PARITY_EXIT_BUFFER = 5.0


def record_event(event, **data):
    """Local audit only; never log connection credentials or HTTP headers."""
    if EVENT_LOG_PATH is None:
        return
    try:
        with open(EVENT_LOG_PATH, 'a', encoding='utf-8') as output:
            output.write(json.dumps(dict(event=event, time=time.time(), **data),
                                    allow_nan=False) + '\n')
    except (OSError, ValueError) as exc:
        print(f'AUDIT LOG unavailable: {type(exc).__name__}')


def remaining_times(tick):
    """Year fractions; a week is five trading days, the case twenty days."""
    return tuple(max(0, w * TICKS_PER_WEEK - max(tick, (w - 1) * TICKS_PER_WEEK))
                 / TICKS_PER_YEAR for w in range(1, TOTAL_WEEKS + 1))


def bs(spot, strike, maturity, vol, kind):
    price, delta = black_scholes(spot, strike, maturity, vol, kind, RISK_FREE_RATE)
    if maturity <= 0 or vol <= 0:
        return price, delta, 0.0
    d1 = (math.log(spot / strike) + (RISK_FREE_RATE + vol * vol / 2) * maturity) / (vol * math.sqrt(maturity))
    gamma = math.exp(-d1*d1/2) / (math.sqrt(2*math.pi) * spot * vol * math.sqrt(maturity))
    return price, delta, gamma


def implied_vol(price, spot, strike, maturity, kind):
    if maturity <= 0:
        return None
    lo, hi = IV_MIN, IV_MAX
    if not bs(spot, strike, maturity, lo, kind)[0] < price < bs(spot, strike, maturity, hi, kind)[0]:
        return None
    for _ in range(IV_ITERATIONS):
        mid = (lo + hi) / 2
        if bs(spot, strike, maturity, mid, kind)[0] < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def straddle_iv(price, spot, strike, maturity):
    """Executable straddle IV, using the sum rather than averaging leg IVs."""
    if maturity <= 0:
        return None
    value = lambda vol: sum(bs(spot, strike, maturity, vol, kind)[0] for kind in 'CP')
    lo, hi = IV_MIN, IV_MAX
    if not value(lo) < price < value(hi):
        return None
    for _ in range(IV_ITERATIONS):
        mid = (lo+hi)/2
        if value(mid) < price:
            lo = mid
        else:
            hi = mid
    return (lo+hi)/2


@dataclass
class VolatilityModel:
    weeks: dict = field(default_factory=dict)
    seen: set = field(default_factory=set)
    observations: list = field(default_factory=list)
    baselines: dict = field(default_factory=dict)
    baseline: float | None = None
    latest_release: float = -1
    confidence: float = 0.0

    def update(self, news, tick, period, atm_iv):
        incoming = []
        for item in news:
            if not isinstance(item, dict):
                continue
            nt = number(item.get('tick'))
            if nt is None or not 0 <= nt <= tick:
                continue
            if 'period' in item and str(item['period']) != str(period):
                continue
            identity = news_identity(item)
            if identity not in self.seen and parse_news(item):
                incoming.append((nt, identity, item))
        for nt, identity, item in sorted(incoming):
            # Strictly earlier ticks avoid storing the first post-release quote.
            before = [(t, iv) for t, iv in self.observations
                      if t < nt and nt - t <= MAX_BASELINE_AGE_TICKS]
            baseline = before[-1][1] if before else None
            self.baselines[identity] = baseline
            if nt >= self.latest_release:
                same_release = nt == self.latest_release
                self.baseline = baseline
                self.latest_release = nt
                confidence = (1.0 if any(k == 'ACTUAL' for _, k, _, _ in parse_news(item))
                              else FORECAST_CONFIDENCE)
                self.confidence = max(self.confidence, confidence) if same_release else confidence
            for week, kind, low, high in parse_news(item):
                old = self.weeks.get(week)
                if old and old[0] > nt:
                    continue
                if kind == 'ACTUAL' or not old or old[1] != 'ACTUAL':
                    self.weeks[week] = (nt, kind, low, high)
            self.seen.add(identity)
        if atm_iv is not None:
            self.observations.append((tick, atm_iv))
            self.observations = self.observations[-2000:]
        return bool(incoming)

    def fair(self, tick):
        times = remaining_times(tick)
        total = sum(times)
        integrated = [0.0] * 3
        for week, duration in enumerate(times, 1):
            if not duration:
                continue
            info = self.weeks.get(week)
            if info:
                _, kind, low, high = info
                central = low*low if kind == 'ACTUAL' else (low*low + low*high + high*high) / 3
                variances = (low*low, central, high*high)
            elif self.baseline is not None:
                variances = (self.baseline**2,) * 3
            else:
                return None
            for i, variance in enumerate(variances):
                integrated[i] += variance * duration
        vols = tuple(math.sqrt(v / total) if total else 0.0 for v in integrated)
        return tuple(integrated), vols

    def stressed_vols(self, tick):
        fair = self.fair(tick)
        if fair is None:
            return None
        integrated, vols = fair
        times = remaining_times(tick)
        unknown_time = sum(t for w, t in enumerate(times, 1) if w not in self.weeks)
        if not unknown_time or self.baseline is None or not sum(times):
            return vols
        low = integrated[0] + (max(0, self.baseline-UNKNOWN_VOL_STRESS)**2
                                     - self.baseline**2)*unknown_time
        high = integrated[2] + ((self.baseline+UNKNOWN_VOL_STRESS)**2
                                      - self.baseline**2)*unknown_time
        return math.sqrt(max(0, low)/sum(times)), vols[1], math.sqrt(high/sum(times))


def market_snapshot(rows, tick):
    by_ticker = {r['ticker']: dict(r) for r in rows}
    rtm = by_ticker.get('RTM')
    if not rtm or not valid_security(rtm, 1):
        raise ValueError('Invalid RTM quote/contract size')
    spot = (float(rtm['bid']) + float(rtm['ask'])) / 2
    maturity = sum(remaining_times(tick))
    options, pairs = [], {}
    for row in by_ticker.values():
        ident = option_identity(row)
        if row is rtm or ident:
            pos = number(row.get('position'))
            if pos is None or pos != int(pos):
                raise ValueError('Missing/noninteger account position')
            row['position'] = int(pos)
        if not ident:
            continue
        if not valid_security(row, OPTION_MULTIPLIER):
            if row['position']:
                raise ValueError('Held option has invalid quote/size')
            continue
        strike, kind = ident
        row['iv'] = implied_vol((float(row['bid']) + float(row['ask']))/2,
                                spot, strike, maturity, kind)
        options.append(row)
        pairs.setdefault(strike, {})[kind] = row
    pairs = {k: p for k, p in pairs.items() if len(p) == 2}
    atm = min(pairs, key=lambda k: abs(k-spot)) if pairs else None
    ivs = [r['iv'] for r in pairs.get(atm, {}).values() if r['iv'] is not None]
    atm_iv = sum(ivs)/len(ivs) if ivs else None
    return dict(rows=by_ticker, rtm=rtm, spot=spot, maturity=maturity,
                options=options, pairs=pairs, atm_iv=atm_iv, tick=tick)


def greeks(snapshot, fallback=None):
    delta = gamma = abs_gamma = 0.0
    for row in snapshot['options']:
        vol = row['iv'] or snapshot['atm_iv'] or fallback
        if vol is None:
            if row['position']:
                raise ValueError('No volatility available for held-option Greeks')
            continue
        strike, kind = option_identity(row)
        _, row['delta'], row['gamma'] = bs(snapshot['spot'], strike, snapshot['maturity'], vol, kind)
        delta += OPTION_MULTIPLIER * row['position'] * row['delta']
        gamma += OPTION_MULTIPLIER * row['position'] * row['gamma']
        abs_gamma += OPTION_MULTIPLIER * abs(row['position']) * row['gamma']
    snapshot.update(option_delta=delta, option_gamma=gamma, abs_gamma=abs_gamma,
                    portfolio_delta=delta + snapshot['rtm']['position'])


def option_delta_shock(row, snapshot, move):
    strike, kind = option_identity(row)
    scenarios = [row['iv'] or snapshot['atm_iv']]
    scenarios.extend(snapshot.get('decision_vols') or [])
    shocks = []
    for vol in scenarios:
        if vol is None:
            continue
        base = bs(snapshot['spot'], strike, snapshot['maturity'], vol, kind)[1]
        for spot in (max(0.0001, snapshot['spot']-move), snapshot['spot']+move):
            shocks.append(abs(bs(spot, strike, snapshot['maturity'], vol, kind)[1]-base)*OPTION_MULTIPLIER)
    return max(shocks, default=OPTION_MULTIPLIER)


def economics(snapshot, strike, vols):
    pair = snapshot['pairs'][strike]
    if any('delta' not in row for row in pair.values()):
        return None
    fair = [sum(bs(snapshot['spot'], strike, snapshot['maturity'], v, k)[0] for k in 'CP') for v in vols]
    bid = sum(float(r['bid']) for r in pair.values())
    ask = sum(float(r['ask']) for r in pair.values())
    delta = OPTION_MULTIPLIER * sum(r['delta'] for r in pair.values())
    gamma = OPTION_MULTIPLIER * sum(r['gamma'] for r in pair.values())
    stock_spread = float(snapshot['rtm']['ask']) - float(snapshot['rtm']['bid'])
    future_cost = ((ask-bid)*OPTION_MULTIPLIER*EXIT_SPREAD_FACTOR
                   + REHEDGE_COST + abs(delta)*(stock_spread + 2*execution_fee(snapshot["rtm"], RTM_COMMISSION)[0]))
    cost = 2*sum(execution_fee(r, OPTION_COMMISSION)[0] for r in pair.values()) + future_cost
    long = (fair[0]-ask)*OPTION_MULTIPLIER-cost
    short = (bid-fair[2])*OPTION_MULTIPLIER-cost
    move = MOVE_SIGMAS * snapshot['spot'] * max(vols[2], snapshot['atm_iv'] or 0,
                 snapshot.get('current_high', 0),
                 *(r['iv'] or 0 for r in pair.values())) * math.sqrt(DT_YEAR)
    return dict(strike=strike, pair=pair, long=long, short=short, delta=delta,
                gamma=gamma, move=move,
                risk=max(gamma*move, sum(option_delta_shock(r, snapshot, move) for r in pair.values())),
                central_long=(fair[1]-ask)*OPTION_MULTIPLIER-cost,
                central_short=(bid-fair[1])*OPTION_MULTIPLIER-cost,
                bid_iv=straddle_iv(bid, snapshot['spot'], strike, snapshot['maturity']),
                ask_iv=straddle_iv(ask, snapshot['spot'], strike, snapshot['maturity']),
                # Entry commissions are sunk. Compare holding to executable liquidation.
                hold_long=(fair[0]-bid)*OPTION_MULTIPLIER-future_cost,
                hold_short=(ask-fair[2])*OPTION_MULTIPLIER-future_cost,
                hold_central_long=(fair[1]-bid)*OPTION_MULTIPLIER-future_cost,
                hold_central_short=(ask-fair[1])*OPTION_MULTIPLIER-future_cost)


def baseline_risk_size_limit(e, snapshot):
    own_tickers = {r['ticker'] for r in e['pair'].values()}
    other_risk = sum(abs(r['position'])*option_delta_shock(r, snapshot, e['move'])
                     for r in snapshot['options'] if r['ticker'] not in own_tickers)
    gamma_cap = int(max(0, BASELINE_3SIGMA_DELTA_SHOCK-other_risk)/max(e['risk'], 1e-12))
    return min(gamma_cap, MAX_TARGET_STRADDLES, OPTION_NET_LIMIT//2, OPTION_GROSS_LIMIT//2)


def offline_3sigma_max_pairs(e, snapshot, direction):
    """Static selected-strike capacity with a single RTM hedge and ±3σ delta envelope.

    This is a sizing estimate, not a fill simulation. The execution path still
    verifies every actual fill and rechecks risk at each child order.
    """
    if not e or snapshot['maturity'] <= 0:
        return 0
    cap = min(OPTION_NET_LIMIT//2, OPTION_GROSS_LIMIT//2)
    stress = max(snapshot.get('current_high',0),snapshot['atm_iv'] or 0,
                 *(snapshot.get('decision_vols') or ()),
                 *(r['iv'] or 0 for r in snapshot['options']))
    move = snapshot['spot']*stress*math.sqrt(DT_YEAR)
    pair = list(e['pair'].values())
    others = [r for r in snapshot['options']
              if r['ticker'] not in {p['ticker'] for p in pair} and r['position']]
    if any(r['iv'] is None for r in pair+others):
        return 0
    unit,other = {},{}
    for sigma in range(-3,4):
        spot = max(.0001,snapshot['spot']+sigma*move)
        unit[sigma] = direction*OPTION_MULTIPLIER*sum(
            bs(spot,e['strike'],snapshot['maturity'],r['iv'],r['ticker'][-1])[1] for r in pair)
        other[sigma] = OPTION_MULTIPLIER*sum(
            r['position']*bs(spot,option_identity(r)[0],snapshot['maturity'],
                             r['iv'],option_identity(r)[1])[1] for r in others)
    band = min(CAPACITY_ENTRY_DELTA_BAND, max(0, EMERGENCY_DELTA-1))
    scenario_limit = min(MAX_3SIGMA_DELTA_SHOCK,HARD_DELTA_LIMIT)
    if scenario_limit <= band:
        return 0
    best = 0
    for pairs in range(1,cap+1):
        current=unit[0]*pairs+other[0]
        hedge = (math.floor(band-current) if current>band else
                 math.ceil(-band-current) if current < -band else 0)
        if abs(hedge)>RTM_LIMIT:
            continue
        if all(abs(unit[z]*pairs+other[z]+hedge) <= (band if z==0 else scenario_limit)+1e-6
               for z in range(-3,4)):
            best=pairs
    return best


def risk_size_limit(e, snapshot, direction=None):
    direction = direction or (1 if e['long']>=e['short'] else -1)
    own_tickers = {r['ticker'] for r in e['pair'].values()}
    other_risk = sum(abs(r['position'])*option_delta_shock(r, snapshot, e['move'])
                     for r in snapshot['options'] if r['ticker'] not in own_tickers)
    shock_cap = int(max(0,MAX_3SIGMA_DELTA_SHOCK-other_risk)/max(e['risk'],1e-12))
    return min(shock_cap,offline_3sigma_max_pairs(e,snapshot,direction),
               OPTION_NET_LIMIT//2,OPTION_GROSS_LIMIT//2)


def baseline_target_size(e, direction, confidence, snapshot):
    edge = e['long'] if direction > 0 else e['short']
    if edge < MIN_ENTRY_EDGE:
        return 0
    multiplier = EXACT_VOL_SIZE_MULTIPLIER if confidence == 1.0 else FORECAST_SIZE_MULTIPLIER
    coverage = snapshot.get('analyst_coverage', 1.0)
    if confidence == 1.0 and coverage >= HIGH_COVERAGE_MIN:
        multiplier = HIGH_COVERAGE_SIZE_MULTIPLIER
    coverage_factor = MIN_COVERAGE_SIZE_FACTOR+(1-MIN_COVERAGE_SIZE_FACTOR)*coverage
    desired = int(BASE_STRADDLES*confidence*edge/EDGE_SIZE_SCALE*multiplier*coverage_factor)
    return min(desired,baseline_risk_size_limit(e,snapshot))


def target_size(e, direction, confidence, snapshot):
    current_target = baseline_target_size(e,direction,confidence,snapshot)
    if not current_target:
        return 0
    coverage = snapshot.get('analyst_coverage',1.0)
    full_support = confidence == 1.0 and coverage >= 1-1e-9
    boost = FULL_SUPPORT_TARGET_MULTIPLIER if full_support else BOOSTED_TARGET_MULTIPLIER
    fraction = FULL_SUPPORT_CAPACITY_FRACTION if full_support else BOOSTED_CAPACITY_FRACTION
    capacity = offline_3sigma_max_pairs(e,snapshot,direction)
    return min(current_target*boost,int(fraction*capacity),risk_size_limit(e,snapshot,direction))


def analyst_variance_coverage(model, tick):
    """Fraction of central remaining variance supported by exact/range news."""
    fair = model.fair(tick)
    if fair is None or fair[0][1] <= 0:
        return 0.0
    unknown_time = sum(t for w, t in enumerate(remaining_times(tick), 1) if w not in model.weeks)
    unknown_variance = (model.baseline or 0)**2*unknown_time
    return max(0.0, min(1.0, 1-unknown_variance/fair[0][1]))


def entry_filter(e, direction, model, tick, *, adding=False):
    if entry_sleeve(e, direction, model.confidence) is None:
        return 'edge below minimum after costs and unknown-week stress'
    window = EXACT_ENTRY_NEWS_WINDOW_TICKS if model.confidence == 1.0 else ENTRY_NEWS_WINDOW_TICKS
    if not adding and not 0 <= tick-model.latest_release <= window:
        return 'news entry window closed'
    if adding and e['long' if direction > 0 else 'short'] < ADD_EDGE_THRESHOLD:
        return 'current robust edge below addition threshold'
    if any(r['iv'] is None for r in e['pair'].values()):
        return 'invalid option IV'
    if abs(e['pair']['C']['iv']-e['pair']['P']['iv']) > MAX_CALL_PUT_IV_GAP:
        return 'call/put IV disagreement'
    return current_carry_problem(e, direction, model, tick)


def entry_sleeve(e, direction, confidence):
    if e['long' if direction > 0 else 'short'] >= MIN_ENTRY_EDGE:
        return 'ROBUST'
    if (ENABLE_FORECAST_SLEEVE and 0 < confidence < 1
            and e.get('central_long' if direction > 0 else 'central_short', -math.inf) > FORECAST_SPECULATIVE_EDGE):
        return 'FORECAST_EXPERIMENT'
    return None


def signal_direction(e, confidence):
    if max(e['long'], e['short']) >= MIN_ENTRY_EDGE or not ENABLE_FORECAST_SLEEVE or confidence == 1:
        return 1 if e['long'] >= e['short'] else -1
    return 1 if e.get('central_long', -math.inf) >= e.get('central_short', -math.inf) else -1


def remaining_edge(e, direction, campaign):
    prefix = 'hold_central_' if campaign and campaign.get('sleeve') == 'FORECAST_EXPERIMENT' else 'hold_'
    return e[prefix+('long' if direction > 0 else 'short')]


def entry_target(e, direction, confidence, snapshot):
    sleeve = entry_sleeve(e, direction, confidence)
    if sleeve == 'ROBUST':
        return target_size(e, direction, confidence, snapshot)
    if sleeve == 'FORECAST_EXPERIMENT':
        central = e['central_long' if direction > 0 else 'central_short']
        full = min(int(BASE_STRADDLES*confidence*central/EDGE_SIZE_SCALE*FORECAST_SIZE_MULTIPLIER),
                   risk_size_limit(e, snapshot))
        return int(full*FORECAST_SLEEVE_FRACTION)
    return 0


def current_carry_problem(e, direction, model, tick):
    info = model.weeks.get(min(int(tick//TICKS_PER_WEEK)+1, TOTAL_WEEKS))
    if info is None:
        return 'current-week analyst volatility unavailable'
    _, _, low, high = info
    if direction > 0 and (e['ask_iv'] is None or low < e['ask_iv']+CURRENT_VOL_MARGIN):
        return 'long lacks favorable current-week volatility carry'
    if direction < 0 and (e['bid_iv'] is None or high > e['bid_iv']-CURRENT_VOL_MARGIN):
        return 'short lacks favorable current-week volatility carry'
    return None


def effective_hedge_band(s):
    return SHORT_VOL_HEDGE_BAND if s.get('option_gamma', 0) < 0 else LONG_VOL_HEDGE_BAND


def hedge_trade(s):
    delta = s['portfolio_delta']
    if not any(r['position'] for r in s['options']):
        # Once the options are flat there is no reason to retain a small hedge.
        return max(-RTM_MAX_ORDER, min(RTM_MAX_ORDER, -s['rtm']['position']))
    if abs(delta) < min(effective_hedge_band(s), EMERGENCY_DELTA):
        return 0
    target = max(-RTM_LIMIT, min(RTM_LIMIT, round(-s['option_delta'])))
    change = target-s['rtm']['position']
    return max(-RTM_MAX_ORDER, min(RTM_MAX_ORDER, change))


def batch_plan(s, e, direction, quantity, batch_limit=None):
    """Check both intermediate portfolios, including every held option."""
    if batch_limit is None:
        edge=e['long' if direction>0 else 'short']
        batch_limit=(STRONG_EDGE_PAIR_BATCH if edge>STAGED_ENTRY_MIN_EDGE else PAIRED_BATCH)
    legs = sorted(e['pair'].values(), key=lambda r: abs(r['delta']))
    if abs(s['portfolio_delta']) >= SOFT_DELTA_LIMIT and any(r['position']*direction >= 0 for r in legs):
        return []
    positions = {r['ticker']: r['position'] for r in s['options']}
    for qty in range(min(int(quantity), batch_limit, OPTION_MAX_ORDER), 0, -1):
        trial = dict(positions)
        delta = s['portfolio_delta']
        okay = True
        for leg in legs:
            change = direction*qty
            trial[leg['ticker']] += change
            leg_delta = change*OPTION_MULTIPLIER*leg['delta']
            delta += leg_delta
            option_delta = delta-s['rtm']['position']
            if (abs(leg_delta) > LEG_DELTA_BUDGET or abs(delta) >= min(EMERGENCY_DELTA, HARD_DELTA_LIMIT)
                    or abs(option_delta) > RTM_LIMIT
                    or sum(abs(p) for p in trial.values()) > OPTION_GROSS_LIMIT
                    or abs(sum(trial.values())) > OPTION_NET_LIMIT):
                okay = False
                break
        if okay:
            return [(r['ticker'], direction*qty) for r in legs]
    return []


def parity_opportunities(s):
    """Detection only: no atomic three-leg execution facility in the RIT wrapper.

    Reserve both opening/closing fees and full current exit spreads. Financing
    uses discounted strike; no assumed free early liquidation or stock borrow.
    """
    found = []
    for strike, p in s['pairs'].items():
        c, put, stock = p['C'], p['P'], s['rtm']
        pvk = strike*math.exp(-RISK_FREE_RATE*s['maturity'])
        costs = (2*sum(execution_fee(r, OPTION_COMMISSION)[0] for r in (c, put))
                 + 2*OPTION_MULTIPLIER*execution_fee(stock, RTM_COMMISSION)[0]
                 + OPTION_MULTIPLIER*sum(float(r['ask'])-float(r['bid']) for r in (c, put, stock))
                 + PARITY_EXIT_BUFFER)
        conversion = OPTION_MULTIPLIER*(pvk-float(stock['ask'])-float(put['ask'])+float(c['bid']))-costs
        reversal = OPTION_MULTIPLIER*(float(stock['bid'])+float(put['bid'])-float(c['ask'])-pvk)-costs
        for name, edge in [('CONVERSION', conversion), ('REVERSAL', reversal)]:
            if edge >= MIN_PARITY_EDGE:
                found.append((strike, name, edge))
    return found


def trim_unmatched(s, pair):
    """Undo only excess contracts; preserve the already matched straddles."""
    if set(pair) != {'C', 'P'}:
        return []
    c, p = pair['C']['position'], pair['P']['position']
    if c*p < 0:
        return []  # Opposite-signed legs are not an interrupted straddle batch.
    matched = min(abs(c), abs(p))
    leg = pair['C'] if abs(c) > abs(p) else pair['P']
    excess = abs(leg['position'])-matched
    for qty in range(min(excess, EXIT_BATCH, OPTION_MAX_ORDER), 0, -1):
        change = -qty if leg['position'] > 0 else qty
        added_delta = change*OPTION_MULTIPLIER*leg['delta']
        projected = s['portfolio_delta']+added_delta
        if (abs(added_delta) <= LEG_DELTA_BUDGET and abs(projected) < EMERGENCY_DELTA
                and abs(s['option_delta']+added_delta) <= RTM_LIMIT):
            return [(leg['ticker'], change)]
    return []


class Strategy:
    def __init__(self):
        self.model = VolatilityModel()
        self.period = self.tick = None
        self.exiting = set()
        self.previous_held = set()
        self.cooldown_until = -1
        self.campaign = None
        self.used_events = set()
        self.confirmation = None
        self.last_audit = None
        self.exit_confirmation = {}
        self.full_exit_confirmation = {}
        self.scale_targets = {}
        self.scaled_strikes = set()

    def evaluate(self, case, rows, news):
        tick = number(case.get('tick'))
        if tick is None or not 0 <= tick < TOTAL_TICKS:
            raise ValueError('Invalid or expired tick')
        if self.tick is not None and (case.get('period') != self.period or tick < self.tick):
            self.__init__()
        bootstrap = self.tick is None
        self.tick, self.period = tick, case.get('period')
        s = market_snapshot(rows, tick)
        new = self.model.update(news, tick, self.period, s['atm_iv'])
        # Loading history after launch is not a fresh invalidation event.
        new = new and not bootstrap
        s['news_seen'] = set(self.model.seen)
        event_key = (self.model.latest_release, tuple(sorted(
            identity for identity in self.model.seen if identity[1] == self.model.latest_release)))
        fair = self.model.fair(tick)
        vols = fair[1] if fair else None
        decision_vols = self.model.stressed_vols(tick)
        s['decision_vols'] = decision_vols
        s['analyst_coverage'] = analyst_variance_coverage(self.model, tick)
        current_week = min(int(tick // TICKS_PER_WEEK) + 1, TOTAL_WEEKS)
        current_info = self.model.weeks.get(current_week)
        s['current_high'] = current_info[3] if current_info else (self.model.baseline or 0)
        greeks(s, vols[1] if vols else None)
        candidates = [economics(s, k, decision_vols) for k in s['pairs']] if decision_vols else []
        candidates = [e for e in candidates if e]
        ranked = sorted(candidates, key=lambda e: (max(e['long'], e['short'])*self.model.confidence
                       / (1+e['risk']/GAMMA_DELTA_BUDGET), -abs(e['strike']-s['spot'])), reverse=True)
        selected = next((e for e in ranked if entry_filter(e, signal_direction(e, self.model.confidence),
                                                         self.model, tick) is None),
                        ranked[0] if ranked else None)
        if self.confirmation and not self.campaign:
            (pending_event, pending_strike, pending_direction), observed_tick, _ = self.confirmation
            pending = next((e for e in candidates if e['strike'] == pending_strike), None)
            # Preserve confirmation across ranking changes, never across a lost
            # signal, a new release, or a gap in observed ticks.
            if (pending_event == event_key and 0 <= tick-observed_tick <= 1
                    and pending is not None
                    and entry_filter(pending, pending_direction, self.model, tick) is None):
                selected = pending
        target, plan, reason = 0, [], 'WAIT'
        held = [r for r in s['options'] if r['position']]
        held_strikes = {option_identity(r)[0] for r in held}
        if self.previous_held and not held_strikes:
            self.cooldown_until = tick + REENTRY_COOLDOWN_TICKS
            self.campaign = None
            self.confirmation = None
        if new:
            self.cooldown_until = -1  # A genuinely new release supersedes old cooldown.
        if self.campaign and held_strikes:
            self.used_events.add(self.campaign['event'])
        if not held_strikes and self.campaign and self.campaign['event'] != event_key:
            self.campaign = None
        self.previous_held = held_strikes
        self.exiting.intersection_update(held_strikes)
        self.exit_confirmation = {k: v for k, v in self.exit_confirmation.items() if k in held_strikes}
        self.full_exit_confirmation = {k: v for k, v in self.full_exit_confirmation.items() if k in held_strikes}
        self.scale_targets = {k: v for k, v in self.scale_targets.items() if k in held_strikes}
        self.scaled_strikes.intersection_update(held_strikes)
        # Re-evaluate held strikes; do not churn into a slightly better challenger.
        repairs = []
        missing_valuation = False
        for strike in sorted(held_strikes):
            e = next((e for e in candidates if e['strike'] == strike), None)
            pair = s['pairs'].get(strike, {})
            balanced = len(pair) == 2 and pair['C']['position'] == pair['P']['position']
            if not balanced:
                compatible = (len(pair) == 2 and pair['C']['position']*pair['P']['position'] >= 0)
                if compatible:
                    repairs.append((strike, pair))
                    matched = min(abs(pair['C']['position']), abs(pair['P']['position']))
                    if self.campaign and self.campaign['strike'] == strike:
                        self.campaign['target'] = min(self.campaign['target'], matched)
                    self.used_events.add(event_key)
                    # Signal/expiry exits are assessed once the excess is removed.
                    continue
                self.exiting.add(strike)
                continue
            if e is None:
                missing_valuation = True
                if tick >= FORCE_EXIT_TICK:
                    self.exiting.add(strike)
                continue
            position = pair['C']['position']
            direction = 1 if position > 0 else -1
            remaining = remaining_edge(e, direction, self.campaign)
            opposite = e['short'] if direction > 0 else e['long']
            edge = e['long'] if direction > 0 else e['short']
            # Use the displayed executable robust edge consistently for scale-outs.
            for threshold, counters in ((EXIT_EDGE, self.exit_confirmation),
                                        (FULL_EXIT_EDGE, self.full_exit_confirmation)):
                if edge < threshold:
                    last_tick, count = counters.get(strike, (tick, 0))
                    if count == 0 or tick-last_tick > 1:
                        counters[strike] = (tick, 1)
                    elif tick > last_tick:
                        counters[strike] = (tick, count+1)
                else:
                    counters.pop(strike, None)
            persistent_negative = self.exit_confirmation.get(strike, (tick, 0))[1] >= EXIT_CONFIRMATION_TICKS
            persistent_deep = self.full_exit_confirmation.get(strike, (tick, 0))[1] >= EXIT_CONFIRMATION_TICKS
            invalidated = new and (edge <= 0 or signal_direction(e, self.model.confidence) != direction
                                   or current_carry_problem(e, direction, self.model, tick))
            signal_exit = persistent_deep or invalidated
            if (signal_exit or persistent_negative) and not AUTO_SIGNAL_EXITS:
                print(f'MANUAL SIGNAL EXIT: K={strike:g}, robust edge=${edge:.2f}')
            if (signal_exit and AUTO_SIGNAL_EXITS) or tick >= FORCE_EXIT_TICK:
                self.exiting.add(strike)
            elif persistent_negative and AUTO_SIGNAL_EXITS and strike not in self.scaled_strikes:
                self.scaled_strikes.add(strike)
                self.scale_targets[strike] = abs(position)//2
                if self.campaign and self.campaign['strike'] == strike:
                    self.campaign['target'] = min(self.campaign['target'], self.scale_targets[strike])
                record_event('scale_out', tick=tick, strike=strike, edge=edge,
                             remaining_target=self.scale_targets[strike])
            selected = e
        if repairs:
            strike, pair = repairs[0]
            plan = trim_unmatched(s, pair)
            target = min(abs(pair['C']['position']), abs(pair['P']['position']))
            reason = 'REPAIR: trim excess leg; preserve matched straddles'
        elif self.exiting:
            reason = 'EXIT: depleted/invalidated signal, expiry, or unmatched legs'
            strike = min(self.exiting)
            e = next((e for e in candidates if e['strike'] == strike), None)
            pair = s['pairs'].get(strike, {})
            if e and len(pair) == 2 and pair['C']['position'] == pair['P']['position']:
                pos = pair['C']['position']
                plan = batch_plan(s, e, -1 if pos > 0 else 1, abs(pos), EXIT_BATCH)
            else:
                # Recover partial pairs one leg at a time, based on actual positions.
                legs = [r for r in held if option_identity(r)[0] == strike]
                for r in sorted(legs, key=lambda r: abs(r['delta'])):
                    qty = min(abs(r['position']), EXIT_BATCH, OPTION_MAX_ORDER,
                              int(LEG_DELTA_BUDGET/max(100*abs(r['delta']), 1)))
                    change = -qty if r['position'] > 0 else qty
                    projected = s['portfolio_delta']+change*100*r['delta']
                    if abs(projected) < EMERGENCY_DELTA:
                        plan = [(r['ticker'], change)]
                        break
        elif not missing_valuation and any(abs(s['pairs'][k]['C']['position']) > goal for k, goal in self.scale_targets.items()):
            strike = next(k for k, goal in self.scale_targets.items()
                          if abs(s['pairs'][k]['C']['position']) > goal)
            selected = next(e for e in candidates if e['strike'] == strike)
            pos = selected['pair']['C']['position']
            target = self.scale_targets[strike]
            plan = batch_plan(s, selected, -1 if pos > 0 else 1, abs(pos)-target, EXIT_BATCH)
            reason = 'SCALE OUT: persistent negative edge; reduce to half once'
        elif held and missing_valuation:
            reason = 'HOLD: historical baseline unavailable; hedge actual exposure, no adding'
        elif selected:
            direction = signal_direction(selected, self.model.confidence)
            if held:
                direction = 1 if selected['pair']['C']['position'] > 0 else -1
            current = selected['pair']['C']['position']
            if held and self.campaign is None:
                # Adopt existing holdings without inferring an unfilled original target.
                self.campaign = dict(event=event_key, strike=selected['strike'],
                                     direction=direction, target=abs(current))
                self.used_events.add(event_key)
            if self.campaign:
                locked = next((e for e in candidates if e['strike'] == self.campaign['strike']), None)
                if locked and (not held or len(held_strikes) == 1):
                    selected, direction = locked, self.campaign['direction']
                    current = selected['pair']['C']['position']
                # Risk reductions are permanent within this campaign: no add-back churn.
                self.campaign['target'] = min(self.campaign['target'], risk_size_limit(selected, s, direction))
                target = self.campaign['target']
            block = entry_filter(selected, direction, self.model, tick, adding=bool(held))
            if event_key in self.used_events and (not held or not self.campaign or self.campaign['event'] != event_key):
                block = 'release already traded'
            if held and self.campaign and self.campaign['event'] != event_key:
                if not block and signal_direction(selected, self.model.confidence) == direction:
                    self.campaign['event'] = event_key
                    self.used_events.add(event_key)
                else:
                    block = block or 'new release no longer supports campaign direction'
            if tick < self.cooldown_until:
                block = 'post-exit cooldown'
            if not AUTO_ENTRIES or tick >= NO_NEW_ENTRY_TICK:
                block = 'new entries disabled or expiry cutoff reached'
            if abs(s['portfolio_delta']) >= SOFT_DELTA_LIMIT:
                block = 'soft delta limit; no adding'
            if selected['strike'] in self.scaled_strikes:
                block = 'campaign scaled out; no add-back'
            key = (event_key, selected['strike'], direction)
            if block:
                self.confirmation = None
            elif not self.campaign:
                if not self.confirmation or self.confirmation[0] != key or tick-self.confirmation[1] > 1:
                    self.confirmation = (key, tick, 1)
                elif tick > self.confirmation[1]:
                    self.confirmation = (key, tick, self.confirmation[2]+1)
                robust_edge = selected['long' if direction > 0 else 'short']
                staged = self.model.confidence == 1.0 and robust_edge > STAGED_ENTRY_MIN_EDGE
                if self.confirmation[2] < ENTRY_CONFIRMATION_TICKS and not staged:
                    block = 'waiting for confirmation on distinct ticks'
                else:
                    target = entry_target(selected, direction, self.model.confidence, s)
                    self.campaign = dict(event=event_key, strike=selected['strike'],
                                         direction=direction, target=target,
                                         sleeve=entry_sleeve(selected, direction, self.model.confidence),
                                         staged=staged, stage_tick=tick, initial_edge=robust_edge,
                                         starter=max(1, math.ceil(target*(INITIAL_ENTRY_FRACTION
                                             +(1-INITIAL_ENTRY_FRACTION)*s['analyst_coverage']
                                             if self.model.confidence == 1.0 else INITIAL_ENTRY_FRACTION))),
                                         metrics=dict(target_pairs=target, maximum_matched_pairs=0,
                                             additions_blocked_by_stale_tick=0, additions_blocked_by_worsened_quote=0,
                                             additions_blocked_by_persistence_rule=0,
                                             additions_refreshed_after_tick=0, additions_repriced_after_quote=0))
            if self.campaign and self.campaign.get('staged'):
                if abs(current) > 0:
                    self.campaign['staged'] = False
                else:
                    target = min(target, self.campaign['starter'])
            if self.campaign:
                self.campaign['add_paused'] = bool(block and abs(current) < self.campaign['target'])
            if held and abs(current) > target:
                plan = batch_plan(s, selected, -direction, abs(current)-target, EXIT_BATCH)
                reason = 'REDUCE to risk limit'
            elif not block and len(held_strikes) <= 1:
                plan = batch_plan(s, selected, direction, max(0, target-abs(current)))
                reason = 'ENTER/COMPLETE FIXED TARGET' if plan else 'HOLD/WAIT'
            else:
                reason = 'HOLD/WAIT: '+str(block)
        hedge = hedge_trade(s)
        # A safe closing batch already removes exposure; hedge after its fills
        # instead of buying a hedge only to unwind it immediately. Emergency wins.
        if hedge and (abs(s['portfolio_delta']) >= EMERGENCY_DELTA or not ((self.exiting or repairs or reason.startswith('SCALE OUT')) and plan)):
            plan = [('RTM', hedge)]
            reason = 'EMERGENCY HEDGE' if abs(s['portfolio_delta']) >= EMERGENCY_DELTA else 'HEDGE'
        if abs(s['portfolio_delta']) >= EMERGENCY_DELTA and not hedge:
            plan = []
            reason = 'HEDGE CAPACITY EXHAUSTED: manual exposure reduction required'
        if self.campaign:
            metrics = self.campaign.setdefault('metrics', dict(target_pairs=self.campaign['target'],
                maximum_matched_pairs=0, additions_blocked_by_stale_tick=0,
                additions_blocked_by_worsened_quote=0, additions_blocked_by_persistence_rule=0,
                additions_refreshed_after_tick=0, additions_repriced_after_quote=0))
            pair = s['pairs'].get(self.campaign['strike'], {})
            matched = (min(abs(r['position']) for r in pair.values())
                       if len(pair) == 2 and pair['C']['position']*pair['P']['position'] >= 0 else 0)
            metrics['maximum_matched_pairs'] = max(metrics['maximum_matched_pairs'], matched)
            metrics['target_filled_pct'] = 100*metrics['maximum_matched_pairs']/max(1, metrics['target_pairs'])
        self.report(s, vols, selected, target, reason, hedge, new)
        s['valuation_model'] = self.model
        s['selected'] = selected
        s['campaign'] = dict(self.campaign) if self.campaign else None
        s['period'] = self.period
        audit_key = (tick, tuple((r['ticker'], r['position']) for r in [s['rtm']]+s['options']), event_key,
                     reason, tuple(plan), target, selected['long'] if selected else None,
                     selected['short'] if selected else None)
        if audit_key != self.last_audit:
            record_event('snapshot', case=case, securities=rows, news=news,
                         reason=reason, plan=plan, target=target,
                         baseline=self.model.baseline, fair_vols=vols, stressed_vols=decision_vols,
                         option_delta=s['option_delta'], portfolio_delta=s['portfolio_delta'],
                         option_gamma=s['option_gamma'], hedge_band=effective_hedge_band(s),
                         analyst_variance_coverage=s['analyst_coverage'],
                         deployment=dict(self.campaign['metrics']) if self.campaign else None,
                         strike=selected['strike'] if selected else None,
                         long_edge=selected['long'] if selected else None,
                         short_edge=selected['short'] if selected else None)
            self.last_audit = audit_key
        if ENABLE_PARITY_DETECTOR:
            for strike, name, edge in parity_opportunities(s):
                print(f'PARITY DETECTOR ONLY: {name} K={strike:g} net edge=${edge:.2f}; no order submitted')
        return s, plan

    def report(self, s, vols, e, target, reason, hedge, new):
        fmt = lambda v: 'unavailable' if v is None else f'{v:.2%}'
        schedule = ' | '.join(f'W{w} {self.model.weeks[w][1]} {self.model.weeks[w][2]:.1%}-{self.model.weeks[w][3]:.1%}'
                    if w in self.model.weeks else f'W{w} UNKNOWN' for w in range(1, 5))
        print(f"\nTick {s['tick']:g} RTM={s['spot']:.2f} T={s['maturity']:.6f} years | "
              f"mode={'DRY RUN' if DRY_RUN else 'LIVE'} | {reason} | news={new}")
        if self.campaign and self.campaign.get('sleeve') == 'FORECAST_EXPERIMENT':
            print('EXPERIMENTAL FORECAST SLEEVE: central-value signal; robust threshold not met.')
        elif e and max(e['long'], e['short']) < MIN_ENTRY_EDGE:
            print(f'ENTRY FILTER: best edge ${max(e["long"], e["short"]):.2f} '
                  f'< minimum ${MIN_EDGE_PER_STRADDLE:.2f}; no new entry.')
        print(schedule + f' | remaining week years={remaining_times(s["tick"])}')
        print(f'Pre-news baseline={fmt(self.model.baseline)} ATM IV={fmt(s["atm_iv"])} '
              f'fair low/central/high={" / ".join(map(fmt, vols)) if vols else "unavailable"} '
              f'confidence={self.model.confidence:.2f}')
        print(f'Entry stress low/central/high={" / ".join(map(fmt, s["decision_vols"])) if s["decision_vols"] else "unavailable"}'
              f' | news age={s["tick"]-self.model.latest_release:g} ticks'
              f' | bid/ask straddle IV={fmt(e["bid_iv"]) if e else "unavailable"}/'
              f'{fmt(e["ask_iv"]) if e else "unavailable"}')
        print(f'K={e["strike"] if e else "none"} long edge={e["long"] if e else float("nan"):.2f} '
              f'short edge={e["short"] if e else float("nan"):.2f} target={target} '
              f'positions={[(r["ticker"], r["position"]) for r in s["options"] if r["position"]]}')
        if self.campaign:
            print(f'Full campaign target={self.campaign["target"]} '
                  f'| starter pending={self.campaign.get("staged", False)} '
                  f'| additions paused={self.campaign.get("add_paused", False)} '
                  f'| sleeve={self.campaign.get("sleeve", "ROBUST")}')
        if self.campaign and self.campaign.get('metrics'):
            print(f"DEPLOYMENT: {self.campaign['metrics']}")
        print(f'Analyst-supported variance={s["analyst_coverage"]:.1%}')
        print(f'Option delta={s["option_delta"]:.1f} gamma={s["option_gamma"]:.2f} '
              f'portfolio delta={s["portfolio_delta"]:.1f} RTM position={s["rtm"]["position"]} '
              f'hedge band=±{effective_hedge_band(s):.0f} hedge recommendation={hedge:+d}', flush=True)


class Broker:
    """Reuse GET wrapper; limit orders are filled/cancelled before another leg.

    Ambiguous submissions stop the process without retrying POST. Never infer a
    fill from an acknowledgement or leave a resting first leg while sending more.
    """
    def __init__(self, session, ledger=None):
        self.session = session
        self.ledger = ledger
        self.analysis = None
        self.execution_state = 'FLAT'
        self.pending_pair = None

    def execution_notice(self, reason, **details):
        print(f'EXECUTION {self.execution_state}: {reason} | {details}', flush=True)
        record_event('execution_status', state=self.execution_state, reason=reason, **details)

    def pair_snapshot(self, case):
        current = self.get('/case')
        if (current.get('status') != 'ACTIVE' or current.get('period') != case.get('period')
                or not case['tick'] <= current.get('tick', -1) < TOTAL_TICKS):
            raise RuntimeError('CRITICAL: pending pair cannot execute in inactive/reset case; inspect account')
        snap = market_snapshot(self.get('/securities'), current['tick'])
        snap['current_high'] = getattr(self, 'current_high', 0)
        model = (self.analysis or {}).get('valuation_model')
        snap['decision_vols'] = model.stressed_vols(current['tick']) if model else (self.analysis or {}).get('decision_vols')
        greeks(snap)
        self.observe(snap)
        return snap

    def pair_status(self, snap):
        p = self.pending_pair
        values = {kind: snap['rows'][ticker]['position'] for kind, ticker in p['tickers'].items()}
        matched = min(abs(v) for v in values.values()) if values['C']*values['P'] >= 0 else 0
        self.execution_notice('PAIR STATUS', signal_target=p['signal_target'],
                              campaign_target=p['campaign_target'], target_pairs=p['quantity'],
                              filled_calls=values['C'], filled_puts=values['P'], filled_pairs=matched,
                              pending_pairs=abs(p['first_filled']-p['second_filled']),
                              repair_quantity=p.get('repair_quantity', 0),
                              unmatched_calls=max(0, abs(values['C'])-matched),
                              unmatched_puts=max(0, abs(values['P'])-matched))

    def drain_pair(self, case, s, plan, first_filled, entering):
        """Synchronous exclusive ownership: strategy never observes intentional imbalance."""
        first, second = plan
        sign = 1 if second[1] > 0 else -1
        detail = self.last_fill or {}
        first_price = detail.get('price', float(s['rows'][first[0]]['ask' if sign > 0 else 'bid']))
        campaign = s.get('campaign') or {}
        self.pending_pair = dict(tickers={option_identity(s['rows'][t])[1]: t for t, _ in plan},
                                 quantity=abs(first[1]), first_filled=first_filled, second_filled=0,
                                 signal_target=campaign.get('target', abs(first[1])),
                                 campaign_target=campaign.get('target', abs(first[1])))
        self.execution_state = 'PAIR_PENDING' if entering else 'EXIT_PENDING'
        deadline = time.monotonic()+PAIR_COMPLETION_SECONDS
        expected = {r['ticker']: r['position'] for r in [s['rtm']]+s['options']}
        expected[first[0]] += sign*first_filled
        def checked():
            snap = self.pair_snapshot(case)
            actual = {r['ticker']: r['position'] for r in [snap['rtm']]+snap['options']}
            if actual != expected:
                raise RuntimeError('CRITICAL: pending pair positions changed outside confirmed fills')
            self.pair_status(snap)
            return snap
        for attempt in range(PAIR_COMPLETION_ATTEMPTS):
            snap = checked()
            missing = first_filled-self.pending_pair['second_filled']
            if not missing:
                break
            if time.monotonic() > deadline:
                self.execution_notice('completion deadline reached')
                break
            row = snap['rows'][second[0]]
            projected = snap['portfolio_delta']+sign*missing*100*row['delta']
            if abs(projected) >= min(EMERGENCY_DELTA, HARD_DELTA_LIMIT):
                self.execution_notice('completion delta blocked', projected_delta=projected)
                break
            if entering:
                projected_positions = {r['ticker']: r['position'] for r in snap['options']}
                projected_positions[second[0]] += sign*missing
                if (sum(abs(p) for p in projected_positions.values()) > OPTION_GROSS_LIMIT
                        or abs(sum(projected_positions.values())) > OPTION_NET_LIMIT
                        or abs(snap['option_delta']+sign*missing*100*row['delta']) > RTM_LIMIT):
                    self.execution_notice('completion account capacity blocked')
                    break
                if 'news_seen' in s:
                    items = self.get('/news')
                    if any(parse_news(item) and number(item.get('tick')) is not None
                           and 0 <= number(item['tick']) <= snap['tick']
                           and ('period' not in item or str(item['period']) == str(case.get('period')))
                           and news_identity(item) not in s['news_seen'] for item in items):
                        self.execution_notice('new news during pending entry; unwind excess')
                        break
                vols = snap.get('decision_vols')
                if vols:
                    strike = option_identity(row)[0]
                    e = economics(snap, strike, vols)
                    shock = sum(abs(projected_positions[r['ticker']])*option_delta_shock(r, snap, e['move'])
                                for r in snap['options'])
                    if shock > MAX_3SIGMA_DELTA_SHOCK:
                        self.execution_notice('completion stress budget blocked', shock=shock)
                        break
                    current_first = float(snap['rows'][first[0]]['ask' if sign > 0 else 'bid'])
                    key = ('central_' if campaign.get('sleeve') == 'FORECAST_EXPERIMENT' else '')+('long' if sign > 0 else 'short')
                    combined_edge = e[key]+sign*(current_first-first_price)*100
                    minimum = FORECAST_SPECULATIVE_EDGE if key.startswith('central') else s.get('entry_minimum', MIN_ENTRY_EDGE)
                    if combined_edge < minimum:
                        self.execution_notice('combined fill edge no longer qualifies', edge=combined_edge)
                        break
            self.execution_notice('submit missing leg', ticker=second[0], quantity=missing, attempt=attempt+1)
            filled = self.confirmed_order(row, sign*missing)
            expected[second[0]] += sign*filled
            self.pending_pair['second_filled'] += filled
        snap = checked()
        excess = first_filled-self.pending_pair['second_filled']
        if excess:
            self.execution_state = 'ROLLBACK_PENDING'
            self.pending_pair['repair_quantity'] = excess
            # For exits finish reducing the remaining leg, rather than reopen risk.
            ticker, rollback_sign = (first[0], -sign) if entering else (second[0], sign)
            for attempt in range(PAIR_ROLLBACK_ATTEMPTS):
                row = snap['rows'][ticker]
                qty = self.pending_pair['repair_quantity']
                if not qty:
                    break
                projected = snap['portfolio_delta']+rollback_sign*qty*100*row['delta']
                if abs(projected) >= HARD_DELTA_LIMIT:
                    raise RuntimeError('CRITICAL: rollback delta blocked; manual intervention required')
                self.execution_notice('urgent exposure reduction at fresh executable quote', ticker=ticker, quantity=qty)
                filled = self.confirmed_order(row, rollback_sign*qty)
                expected[ticker] += rollback_sign*filled
                self.pending_pair['repair_quantity'] -= filled
                if entering:
                    self.pending_pair['first_filled'] -= filled
                else:
                    self.pending_pair['second_filled'] += filled
                snap = checked()
            if self.pending_pair['repair_quantity']:
                raise RuntimeError('CRITICAL: rollback exhausted; new entries blocked; inspect open exposure')
        self.execution_state = 'PAIR_COMPLETE'
        self.pair_status(snap)
        self.pending_pair = None
        self.hedge_after_batch(case)
        self.execution_state = 'ACTIVE_CAMPAIGN' if any(r['position'] for r in snap['options']) else 'FLAT'

    def observe(self, snapshot):
        if self.ledger is None:
            return
        if self.analysis:
            snapshot['campaign'] = self.analysis.get('campaign')
            snapshot['selected'] = self.analysis.get('selected')
            snapshot['period'] = self.analysis.get('period')
            # pair_snapshot has already recomputed volatility for its current tick.
            snapshot.setdefault('decision_vols', self.analysis.get('decision_vols'))
            e = snapshot.get('selected')
            if e and snapshot.get('decision_vols') and e['strike'] in snapshot['pairs']:
                snapshot['selected'] = economics(snapshot, e['strike'], snapshot['decision_vols'])
        self.ledger.observe(snapshot)

    def get(self, resource):
        return api_get(self.session, API_ENDPOINT.rstrip('/'), resource)

    def order(self, row, change):
        cap = RTM_MAX_ORDER if row['ticker'] == 'RTM' else OPTION_MAX_ORDER
        if not change or int(change) != change or abs(change) > cap:
            raise ValueError('Invalid order quantity')
        side = 'BUY' if change > 0 else 'SELL'
        price = float(row['ask'] if change > 0 else row['bid'])
        print(f'{"DRY RUN" if DRY_RUN else "ORDER"}: {side} {abs(change)} {row["ticker"]} LIMIT {price:.2f}', flush=True)
        if DRY_RUN:
            return 0
        response = self.session.post(API_ENDPOINT.rstrip('/')+'/orders', params=dict(
            ticker=row['ticker'], type='LIMIT', quantity=abs(change), action=side, price=price), timeout=API_TIMEOUT_SECONDS)
        response.raise_for_status()
        order_id = response.json()['order_id']
        record_event('order_submitted', order_id=order_id, ticker=row['ticker'],
                     action=side, quantity=abs(change), limit_price=price)
        deadline = time.monotonic()+ORDER_TIMEOUT_SECONDS
        while True:
            order = self.get(f'/orders/{order_id}')
            if order['status'] in ('TRANSACTED', 'CANCELLED'):
                break
            if time.monotonic() >= deadline:
                response = self.session.delete(API_ENDPOINT.rstrip('/')+f'/orders/{order_id}', timeout=API_TIMEOUT_SECONDS)
                response.raise_for_status()
                order = self.get(f'/orders/{order_id}')
                if order['status'] not in ('TRANSACTED', 'CANCELLED'):
                    raise RuntimeError('Order cancellation unresolved; stop and inspect account')
                break
            time.sleep(ORDER_POLL_SECONDS)
        filled = number(order.get('quantity_filled'))
        if filled is None or not 0 <= filled <= abs(change) or int(filled) != filled:
            raise RuntimeError('Fill status unresolved; inspect account')
        if order['status'] == 'TRANSACTED' and filled != abs(change):
            raise RuntimeError('Completed order has inconsistent fill quantity')
        print(f'CONFIRMED: order={order_id} {order["status"]}, '
              f'filled={int(filled)}/{abs(change)} {row["ticker"]}', flush=True)
        record_event('order_terminal', order=order,
                     estimated_commission=filled*execution_fee(row, RTM_COMMISSION if row['ticker'] == 'RTM' else OPTION_COMMISSION)[0])
        actual_price = next((number(order.get(key)) for key in ('vwap', 'average_price', 'avg_price')
                             if number(order.get(key)) is not None and number(order.get(key)) > 0), None)
        self.last_fill = dict(price=actual_price if actual_price is not None else price,
                              source='actual_vwap' if actual_price is not None else 'limit_estimate',
                              order_id=order_id)
        return int(filled)

    def confirmed_order(self, row, change):
        self.last_fill = None
        filled = self.order(row, change)
        expected = row['position'] + (filled if change > 0 else -filled)
        # The order endpoint can report a terminal fill before /securities catches up.
        # Retry only the account read; never submit the same order again.
        for attempt in range(POSITION_RECONCILE_ATTEMPTS):
            after = self.get('/securities')
            actual = next((r['position'] for r in after if r['ticker'] == row['ticker']), None)
            if actual == expected:
                break
            if attempt + 1 < POSITION_RECONCILE_ATTEMPTS:
                time.sleep(POSITION_RECONCILE_DELAY_SECONDS)
        else:
            record_event('position_fill_mismatch', ticker=row['ticker'],
                         expected=expected, actual=actual, filled=filled,
                         attempted_change=change)
            raise RuntimeError(f'Positions disagree with fills for {row["ticker"]}: '
                               f'expected {expected}, observed {actual}; stop and reconcile')
        if self.ledger is not None:
            detail = self.last_fill or dict(price=float(row['ask'] if change > 0 else row['bid']),
                                           source='limit_estimate', order_id=None)
            self.ledger.fill(row, change, filled, detail['price'], detail['source'], detail['order_id'], after)
        return filled

    def deployment_counter(self, snapshot, name):
        metrics = (snapshot.get('campaign') or {}).get('metrics')
        if metrics is not None:
            metrics[name] = metrics.get(name, 0)+1
        if self.ledger is not None:
            self.ledger.deployment_counts[name] = self.ledger.deployment_counts.get(name, 0)+1
        record_event('deployment_counter', counter=name, tick=snapshot['tick'],
                     value=metrics.get(name) if metrics else None)

    def execute_refreshed_entry(self, case, s, plan, expected_positions):
        """Retry only before submission. Never retry an ambiguous POST."""
        model = s['valuation_model']
        strike = option_identity(s['rows'][plan[0][0]])[0]
        direction = 1 if plan[0][1] > 0 else -1
        campaign = s.get('campaign') or {}
        for attempt in range(ENTRY_REFRESH_ATTEMPTS):
            current = self.get('/case')
            tick = number(current.get('tick'))
            if (current.get('status') != 'ACTIVE' or current.get('period') != case.get('period')
                    or tick is None or not case['tick'] <= tick < TOTAL_TICKS
                    or tick >= NO_NEW_ENTRY_TICK):
                self.execution_notice('entry refresh expired or case inactive')
                return
            fresh = market_snapshot(self.get('/securities'), tick)
            items = self.get('/news')
            if any(parse_news(item) and number(item.get('tick')) is not None
                   and 0 <= number(item['tick']) <= tick
                   and ('period' not in item or str(item['period']) == str(current.get('period')))
                   and news_identity(item) not in s['news_seen'] for item in items):
                self.execution_notice('new news before entry; return to strategy')
                return
            actual = {r['ticker']: r['position'] for r in [fresh['rtm']]+fresh['options']}
            if actual != expected_positions:
                raise RuntimeError('Account changed outside this batch; stop and reconcile')
            vols = model.stressed_vols(tick)
            if vols is None or strike not in fresh['pairs']:
                return
            info = model.weeks.get(min(int(tick//TICKS_PER_WEEK)+1, TOTAL_WEEKS))
            fresh.update(decision_vols=vols, current_high=info[3] if info else (model.baseline or 0),
                         analyst_coverage=analyst_variance_coverage(model, tick))
            greeks(fresh, vols[1])
            e = economics(fresh, strike, vols)
            adding = any(s['rows'][t]['position']*q > 0 for t, q in plan)
            block = entry_filter(e, direction, model, tick, adding=adding) if e else 'valuation unavailable'
            if e and signal_direction(e, model.confidence) != direction:
                block = 'signal direction changed'
            if block or abs(fresh['portfolio_delta']) >= SOFT_DELTA_LIMIT:
                self.execution_notice('refreshed entry rejected', reason_detail=block or 'delta limit')
                return
            if tick != case['tick'] and adding:
                self.deployment_counter(s, 'additions_refreshed_after_tick')
            side = 'ask' if direction > 0 else 'bid'
            if adding and any(direction*(float(r[side])-float(s['rows'][r['ticker']][side])) > 1e-9
                              for r in e['pair'].values()):
                self.deployment_counter(s, 'additions_repriced_after_quote')
            held = abs(e['pair']['C']['position'])
            # A campaign's approved target remains valid as edge converges, provided
            # the absolute add threshold and freshly recomputed risk cap both pass.
            target = min(campaign.get('target', held+abs(plan[0][1])), risk_size_limit(e, fresh, direction))
            quantity = min(abs(plan[0][1]), max(0, target-held))
            fresh_plan = batch_plan(fresh, e, direction, quantity)
            if not fresh_plan:
                self.execution_notice('refreshed entry has no safe additional size')
                return
            projected = {r['ticker']: r['position'] for r in fresh['options']}
            for ticker, change in fresh_plan:
                projected[ticker] += change
            stress = max(fresh['current_high'], fresh['atm_iv'] or 0, *vols,
                         *(r['iv'] or 0 for r in fresh['options'] if projected[r['ticker']]))
            move = MOVE_SIGMAS*fresh['spot']*stress*math.sqrt(DT_YEAR)
            shock = sum(abs(projected[r['ticker']])*option_delta_shock(r, fresh, move)
                        for r in fresh['options'])
            if shock > MAX_3SIGMA_DELTA_SHOCK:
                self.execution_notice('refreshed entry stress budget blocked', shock=shock)
                return
            verified = self.get('/case')
            if any(verified.get(k) != current.get(k) for k in ('tick', 'period', 'status')):
                self.execution_notice('retry entry valuation after case changed', attempt=attempt+1)
                continue
            fresh['entry_minimum'] = ADD_EDGE_THRESHOLD if adding else MIN_ENTRY_EDGE
            fresh.update(selected=e, campaign=campaign, period=current.get('period'),
                         news_seen=s['news_seen'], valuation_model=model)
            self.analysis = fresh
            self.current_high = fresh['current_high']
            self.observe(fresh)
            ticker, change = fresh_plan[0]
            filled = self.confirmed_order(fresh['rows'][ticker], change)
            if filled:
                return self.drain_pair(current, fresh, fresh_plan, filled, True)
            self.execution_notice('first leg unfilled; no pair opened')
            return
        if any(s['rows'][t]['position']*q > 0 for t, q in plan):
            self.deployment_counter(s, 'additions_blocked_by_stale_tick')
        self.execution_notice('entry refresh retries exhausted; return to strategy')

    def execute(self, case, s, plan):
        if self.pending_pair is not None:
            raise RuntimeError('CRITICAL: unresolved PAIR_PENDING; normal strategy execution blocked')
        self.analysis = s
        self.observe(s)
        self.current_high = s.get('current_high', 0)
        if not plan:
            return
        if DRY_RUN:
            for ticker, change in plan:
                self.order(s['rows'][ticker], change)
            return
        if self.get('/orders?status=OPEN'):
            raise RuntimeError('Open account orders exist; cannot safely reserve exposure')
        expected_positions = {r['ticker']: r['position'] for r in [s['rtm']] + s['options']}
        entering = any(t != 'RTM' and (s['rows'][t]['position'] == 0
                       or s['rows'][t]['position']*q > 0) for t, q in plan)
        if entering:
            if s.get('valuation_model') is None:
                raise ValueError('Entry requires a volatility model for fresh valuation')
            if (len(plan) != 2 or len({option_identity(s['rows'][t])[0] for t, _ in plan}) != 1
                    or plan[0][1] != plan[1][1] or plan[0][0] == plan[1][0]):
                raise ValueError('Entry requires an equal-size call/put pair')
            return self.execute_refreshed_entry(case, s, plan, expected_positions)
        first_fill = None
        for index, (ticker, change) in enumerate(plan):
            fresh_case = self.get('/case')
            elapsed = (number(fresh_case.get('tick')) or 0) - case['tick']
            if (fresh_case.get('status') != 'ACTIVE' or fresh_case.get('period') != case.get('period')
                    or elapsed < 0):
                self.execution_notice('snapshot tick/status changed before submission', elapsed=elapsed)
                break  # next cycle recovers any unmatched leg
            fresh = market_snapshot(self.get('/securities'), fresh_case['tick'])
            fresh['current_high'] = getattr(self, 'current_high', 0)
            fresh['decision_vols'] = s.get('decision_vols')
            greeks(fresh)
            self.observe(fresh)
            actual_positions = {r['ticker']: r['position'] for r in [fresh['rtm']] + fresh['options']}
            if actual_positions != expected_positions:
                raise RuntimeError('Account changed outside this batch; stop and reconcile')
            row = fresh['rows'][ticker]
            if index and first_fill is not None:
                change = (1 if change > 0 else -1)*first_fill
            if not change:
                break
            if ticker != 'RTM':
                projected = fresh['portfolio_delta'] + change*OPTION_MULTIPLIER*row['delta']
                positions = {r['ticker']: r['position'] for r in fresh['options']}
                positions[ticker] += change
                if (abs(projected) >= min(EMERGENCY_DELTA, HARD_DELTA_LIMIT)
                        or abs(change*OPTION_MULTIPLIER*row['delta']) > LEG_DELTA_BUDGET
                        or abs(projected-fresh['rtm']['position']) > RTM_LIMIT
                        or sum(abs(p) for p in positions.values()) > OPTION_GROSS_LIMIT
                        or abs(sum(positions.values())) > OPTION_NET_LIMIT):
                    break
            elif abs(row['position']+change) > RTM_LIMIT:
                break
            verified = self.get('/case')
            keys = ('period', 'status')
            if any(verified.get(k) != fresh_case.get(k) for k in keys):
                self.execution_notice('case changed during pre-order checks')
                break
            first_fill = self.confirmed_order(row, change)
            expected_positions[ticker] += first_fill if change > 0 else -first_fill
            if index == 0 and len(plan) == 2 and all(t != 'RTM' for t, _ in plan):
                if first_fill:
                    return self.drain_pair(case, s, plan, first_fill, entering)
                self.execution_notice('first leg unfilled; no pair opened')
                return
        # Hedge actual filled positions, including partial or failed pairs.
        self.hedge_after_batch(case)

    def hedge_after_batch(self, case):
        for attempt in range(ENTRY_REFRESH_ATTEMPTS + math.ceil(RTM_LIMIT/RTM_MAX_ORDER)):
            current = self.get('/case')
            if (current.get('status') != 'ACTIVE' or current.get('period') != case.get('period')
                    or not case['tick'] <= current.get('tick', TOTAL_TICKS) < TOTAL_TICKS):
                return
            fresh = market_snapshot(self.get('/securities'), current['tick'])
            fresh['current_high'] = getattr(self, 'current_high', 0)
            greeks(fresh)
            self.observe(fresh)
            hedge = hedge_trade(fresh)
            if not hedge:
                return
            verified = self.get('/case')
            if any(verified.get(k) != current.get(k) for k in ('tick', 'period', 'status')):
                self.execution_notice('retry hedge with fresh snapshot', attempt=attempt+1)
                continue
            filled = self.confirmed_order(fresh['rtm'], hedge)
            if not filled or any(r['position'] for r in fresh['options']):
                return
        self.execution_notice('hedge refresh retries exhausted; re-evaluate next cycle')


def replay(path):
    """Replay recorded account snapshots without a Broker or any network calls.

    This diagnoses decisions against historical positions; it is not a backtest
    of hypothetical fills and must not be reported as strategy returns.
    """
    global EVENT_LOG_PATH, DRY_RUN
    old_path, old_dry = EVENT_LOG_PATH, DRY_RUN
    EVENT_LOG_PATH, DRY_RUN = None, True
    try:
        strategy = Strategy()
        with open(path, encoding='utf-8') as source:
            for line in source:
                event = json.loads(line)
                if event.get('event') == 'snapshot':
                    strategy.evaluate(event['case'], event['securities'], event['news'])
    finally:
        EVENT_LOG_PATH, DRY_RUN = old_path, old_dry


def configure_announced_delta_limit(limit):
    """Set live risk gates from the sub-heat announcement, keeping a delta buffer."""
    global OFFICIAL_DELTA_LIMIT, HARD_DELTA_LIMIT, EMERGENCY_DELTA
    global SOFT_DELTA_LIMIT, MAX_3SIGMA_DELTA_SHOCK, GAMMA_DELTA_BUDGET
    global LONG_VOL_HEDGE_BAND, SHORT_VOL_HEDGE_BAND
    if not isinstance(limit,int) or limit <= 1000:
        raise ValueError('Provide the announced sub-heat delta limit (>1000)')
    buffer = min(1000,max(200,int(.2*limit)))
    OFFICIAL_DELTA_LIMIT = limit
    HARD_DELTA_LIMIT = min(6000,limit-buffer)
    EMERGENCY_DELTA = min(3500,int(.6*HARD_DELTA_LIMIT))
    SOFT_DELTA_LIMIT = min(3500,EMERGENCY_DELTA)
    MAX_3SIGMA_DELTA_SHOCK = HARD_DELTA_LIMIT
    GAMMA_DELTA_BUDGET = MAX_3SIGMA_DELTA_SHOCK
    LONG_VOL_HEDGE_BAND = min(1300,EMERGENCY_DELTA)
    SHORT_VOL_HEDGE_BAND = min(800,EMERGENCY_DELTA)


def announced_delta_limit_from_news(items, period=None):
    """Read the sub-heat delta announcement when it is present in case news."""
    limits=[]
    for item in items:
        if period is not None and 'period' in item and str(item['period']) != str(period):
            continue
        body=str(item.get('body') or item.get('headline') or '')
        match=re.search(r'\bdelta\s+limit\b.{0,80}?\b(?:is|of|=)\s*([\d,]+)',body,re.I)
        if match:
            limits.append(int(match.group(1).replace(',','')))
    return limits[-1] if limits else None


def choose_announced_delta_limit(published, supplied=None):
    """Use the published limit, an explicit override, or the 7000 default."""
    if published is not None and supplied is not None and published != supplied:
        raise ValueError('Configured delta limit differs from sub-heat news')
    if published is not None:
        return published
    if supplied is not None:
        return supplied
    return DEFAULT_DELTA_LIMIT


def log_configuration():
    record_event('configuration', dry_run=DRY_RUN, min_edge=MIN_EDGE_PER_STRADDLE,
                 exit_edge=EXIT_EDGE_PER_STRADDLE, max_target=MAX_TARGET_STRADDLES,
                 entry_batch=PAIRED_BATCH, strong_entry_batch=STRONG_EDGE_PAIR_BATCH,
                 entry_news_window=ENTRY_NEWS_WINDOW_TICKS,
                 entry_confirmation_ticks=ENTRY_CONFIRMATION_TICKS,
                 unknown_vol_stress=UNKNOWN_VOL_STRESS, current_vol_margin=CURRENT_VOL_MARGIN,
                 long_hedge_band=LONG_VOL_HEDGE_BAND, short_hedge_band=SHORT_VOL_HEDGE_BAND,
                 add_edge_threshold=ADD_EDGE_THRESHOLD, full_exit_edge=FULL_EXIT_EDGE,
                 emergency_delta=EMERGENCY_DELTA, hard_delta=HARD_DELTA_LIMIT,
                 announced_delta_limit=OFFICIAL_DELTA_LIMIT,
                 exact_size_multiplier=EXACT_VOL_SIZE_MULTIPLIER, max_delta_shock=MAX_3SIGMA_DELTA_SHOCK,
                 option_commission=OPTION_COMMISSION, stock_commission=RTM_COMMISSION)


def main(announced_delta_limit=None):
    if announced_delta_limit is not None:
        if not isinstance(announced_delta_limit,int) or announced_delta_limit <= 1000:
            raise ValueError('Provide the announced sub-heat delta limit (>1000)')
    print(f'Integrated-variance strategy | DRY_RUN={DRY_RUN} | '
          f'AUTO_ENTRIES={AUTO_ENTRIES} | AUTO_SIGNAL_EXITS={AUTO_SIGNAL_EXITS} | '
          f'DELTA_LIMIT_DEFAULT={DEFAULT_DELTA_LIMIT}')
    strategy = Strategy()
    with requests.Session() as session:
        session.auth = (USERNAME, PASSWORD)
        ledger = TradeLedger(record_event, OPTION_COMMISSION, RTM_COMMISSION)
        broker = Broker(session, ledger)
        previous_active = None
        configured_period = None
        configured_limit = None
        try:
            while True:
                case = broker.get('/case')
                if case.get('status') == 'ACTIVE':
                    current = (case.get('period'), case.get('tick'))
                    if previous_active and (current[0] != previous_active[0] or current[1] < previous_active[1]):
                        ledger.summary()
                        ledger = TradeLedger(record_event, OPTION_COMMISSION, RTM_COMMISSION)
                        broker.ledger = ledger
                        configured_period = None
                        configured_limit = None
                        announced_delta_limit = None  # Never carry an old sub-heat's manual value forward.
                    previous_active = current
                    if case.get('tick', 0) >= TOTAL_TICKS:
                        ledger.summary()
                        time.sleep(POLL_SECONDS)
                        continue
                    news = broker.get('/news')
                    published_limit=announced_delta_limit_from_news(news,case.get('period'))
                    if configured_period != case.get('period'):
                        supplied = announced_delta_limit if configured_period is None else None
                        limit=choose_announced_delta_limit(published_limit,supplied)
                        configure_announced_delta_limit(limit)
                        configured_period=case.get('period')
                        configured_limit=limit
                        print(f'Configured sub-heat delta limit={limit}; '
                              f'hard={HARD_DELTA_LIMIT}, emergency={EMERGENCY_DELTA}',flush=True)
                        log_configuration()
                    elif published_limit is not None and published_limit != configured_limit:
                        raise RuntimeError('Announced delta limit changed; stop and reconcile before more orders')
                    rows = broker.get('/securities')
                    verified = broker.get('/case')
                    if any(verified.get(k) != case.get(k) for k in ('tick', 'period', 'status')):
                        continue
                    s, plan = strategy.evaluate(case, rows, news)
                    broker.execute(case, s, plan)
                else:
                    if ledger.context is not None and case.get('status') == 'STOPPED':
                        ledger.summary()
                    print(f'Case {case.get("status")}; waiting', flush=True)
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            ledger.summary()
            print('Stopped; account positions are unchanged by shutdown.')
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
            ledger.summary()
            # No automatic retry after an uncertain order submission.
            print(f'STOPPED: {type(exc).__name__}: {exc}. Inspect account before restarting.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', metavar='JSONL', help='offline decision replay, no orders')
    parser.add_argument('--announced-delta-limit', type=int,
                        help='optional explicit sub-heat value; otherwise use news or default to 7000')
    args = parser.parse_args()
    if args.replay:
        replay(args.replay)
    else:
        main(args.announced_delta_limit)
