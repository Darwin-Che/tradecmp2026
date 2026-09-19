"""Standalone, read-only manual volatility assistant. Run with Python 3."""

import argparse
import json
import math
import re
import time

import requests

API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16595/v1"  # Volatility Trading case
USERNAME = "goal-2"
PASSWORD = "credit"

TOTAL_TICKS = 300
TICKS_PER_WEEK = 75
TOTAL_WEEKS = 4
ANNUALIZATION_TICKS = 3600
UNKNOWN_WEEK_VOL = 0.25
OPTION_COMMISSION_PER_CONTRACT_PER_SIDE = 2.00
RTM_FEE_PER_SHARE_PER_SIDE = 0.02
MODEL_BUFFER_PER_STRADDLE = 2.00
MIN_LONG_EDGE_PER_STRADDLE = 10.00
MIN_SHORT_EDGE_PER_STRADDLE = 15.00
ENTRY_QUANTITY = 100  # Desired ceiling; actual quantity is risk-limited below.
DELTA_HEDGE_TRIGGER = 250
NO_NEW_ENTRY_TICK = 285
POLL_SECONDS = 0.5
OPTION_MULTIPLIER = 100
API_TIMEOUT_SECONDS = 5

# Published case limits; confirm these against the competition configuration.
OPTION_GROSS_LIMIT = 2500
OPTION_NET_LIMIT = 1000
OPTION_MAX_TRADE = 100
RTM_POSITION_LIMIT = 50000
RTM_MAX_TRADE = 10000
CASE_DELTA_LIMIT = 7000
DELTA_FINE_PER_SHARE_SECOND = 0.10
# Strategy choices, not case rules or empirically optimized parameters.
MANUAL_UNHEDGED_DELTA_BUDGET = 1000
MAX_LONG_PREMIUM_DOLLARS = 1000.00
EXIT_REVIEW_TICK = 290
EXIT_REMAINING_ADVANTAGE = 5.00
REHEDGE_ALLOWANCE_PER_STRADDLE = 2.00
RISK_FREE_RATE = 0.0


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def normalize(value):
    return ' '.join(str(value or '').lower().split())


def news_identity(item):
    return (str(item.get('news_id', item.get('id', ''))),
            number(item.get('tick')), normalize(item.get('headline')),
            normalize(item.get('body')))


def parse_news(item):
    """Return (human week, kind, low, high) for each volatility statement."""
    tick = number(item.get('tick'))
    if tick is None or tick < 0:
        return []
    source_week = int(tick // TICKS_PER_WEEK) + 1
    results = []
    # Sentence boundaries exclude decimal points. Rate clauses are cut off so
    # a risk-free percentage cannot become the volatility value.
    text = normalize(item.get('body')).replace('–', '-').replace('—', '-')
    for sentence in re.split(r'(?<=[.!?])\s+|[;\n]', text):
        for clause in re.split(r'\b(?:and\s+)?(?:the\s+)?risk[- ]free\b', sentence):
            marker = re.search(r'\bvolatility\b', clause)
            if not marker:
                continue
            description = clause[marker.end():]
            digits = r'(\d+(?:\.\d+)?)'
            bounds = re.search(digits + r'\s*%?\s*(?:-|to|and)\s*'
                               + digits + r'\s*%', description)
            if bounds:
                low, high = (float(value) / 100 for value in bounds.groups())
                kind = 'FORECAST'
            else:
                # An incomplete range must never become an exact observation.
                if re.search(r'\b(?:between|from|to|and)\b|[\d%]\s*-', description):
                    continue
                exact = re.search(digits + r'\s*%', description)
                if not exact:
                    continue
                low = high = float(exact.group(1)) / 100
                kind = 'ACTUAL'
            if low > high:
                continue
            week = source_week + (1 if 'next week' in clause else 0)
            if not ('this week' in clause or 'next week' in clause or
                    'current annualized' in clause):
                continue
            if 1 <= week <= TOTAL_WEEKS:
                results.append((week, kind, low, high))
    return results


def weekly_schedule(news, tick, period=None):
    schedule = {w: ('UNKNOWN', UNKNOWN_WEEK_VOL, UNKNOWN_WEEK_VOL)
                for w in range(1, TOTAL_WEEKS + 1)}
    applicable = {}
    for item in news:
        if not isinstance(item, dict):
            continue
        nt = number(item.get('tick'))
        if nt is None or nt < 0 or nt > tick:
            continue
        if 'period' in item and str(item['period']) != str(period):
            continue
        if parse_news(item):
            applicable[news_identity(item)] = item
    for identity in sorted(applicable, key=lambda key: (key[1], key)):
        for week, kind, low, high in parse_news(applicable[identity]):
            if kind == 'ACTUAL' or schedule[week][0] != 'ACTUAL':
                schedule[week] = (kind, low, high)
    return schedule, applicable


def effective_volatility(schedule, tick):
    remaining = max(TOTAL_TICKS - tick, 0)
    if not remaining:
        return (0.0, 0.0, 0.0)
    variance = [0.0, 0.0, 0.0]
    for week, (_, low, high) in schedule.items():
        duration = max(0, min(week * TICKS_PER_WEEK, TOTAL_TICKS)
                       - max(tick, (week - 1) * TICKS_PER_WEEK))
        for i, vol in enumerate((low, (low + high) / 2, high)):
            variance[i] += vol * vol * duration
    return tuple(math.sqrt(v / remaining) for v in variance)


def black_scholes(spot, strike, maturity, vol, kind, rate=0.0):
    """Return price and delta, including deterministic and expiry limits."""
    discount = math.exp(-rate * max(maturity, 0))
    forward_intrinsic = spot - strike * discount
    if maturity <= 1e-12 or vol <= 1e-12:
        call_delta = 1.0 if forward_intrinsic > 0 else 0.0 if forward_intrinsic < 0 else 0.5
        if kind == 'C':
            return max(forward_intrinsic, 0), call_delta
        return max(-forward_intrinsic, 0), call_delta - 1
    sigma_t = vol * math.sqrt(maturity)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * maturity) / sigma_t
    d2 = d1 - sigma_t
    cdf = lambda x: (1 + math.erf(x / math.sqrt(2))) / 2
    if kind == 'C':
        return spot * cdf(d1) - strike * discount * cdf(d2), cdf(d1)
    return strike * discount * cdf(-d2) - spot * cdf(-d1), cdf(d1) - 1


def option_identity(row):
    match = re.fullmatch(r'RTM(\d+(?:\.\d+)?)([CP])', str(row.get('ticker', '')))
    if match and float(match.group(1)) > 0:
        return float(match.group(1)), match.group(2)
    return None


def contract_size(row):
    """RIT uses size; also accept contract_size from compatible snapshots."""
    values = [number(row[key]) for key in ('size', 'contract_size') if key in row]
    if not values or any(value is None or value != values[0] for value in values):
        return None
    return values[0]


def security_problem(row, multiplier):
    bid, ask = number(row.get('bid')), number(row.get('ask'))
    if bid is None or ask is None or not 0 < bid <= ask:
        return 'missing, nonpositive, or crossed bid/ask'
    if contract_size(row) != multiplier:
        return f'missing, conflicting, or unexpected size (expected {multiplier})'
    return None


def valid_security(row, multiplier):
    return security_problem(row, multiplier) is None


def select_strike(options, spot):
    pairs = {}
    for row in options:
        identity = option_identity(row)
        if identity and valid_security(row, OPTION_MULTIPLIER):
            strike, kind = identity
            pairs.setdefault(strike, {})[kind] = row
    common = {k: v for k, v in pairs.items() if set(v) == {'C', 'P'}}
    if not common:
        return None
    return min(common, key=lambda k: (abs(k - spot),
               sum(float(r['ask']) - float(r['bid']) for r in common[k].values()), k))


def straddle_edges(call, put, spot, strike, maturity, vols):
    fairs = [sum(black_scholes(spot, strike, maturity, vol, kind, RISK_FREE_RATE)[0]
                 for kind in ('C', 'P')) for vol in vols]
    delta = sum(black_scholes(spot, strike, maturity, vols[1], kind, RISK_FREE_RATE)[1]
                for kind in ('C', 'P'))
    cost = (4 * OPTION_COMMISSION_PER_CONTRACT_PER_SIDE
            + abs(round(OPTION_MULTIPLIER * delta)) * RTM_FEE_PER_SHARE_PER_SIDE * 2
            + MODEL_BUFFER_PER_STRADDLE)
    long = (fairs[0] - float(call['ask']) - float(put['ask'])) * OPTION_MULTIPLIER - cost
    short = (float(call['bid']) + float(put['bid']) - fairs[2]) * OPTION_MULTIPLIER - cost
    signal = 'WAIT'
    if long >= MIN_LONG_EDGE_PER_STRADDLE and long > short:
        signal = 'LONG'
    elif short >= MIN_SHORT_EDGE_PER_STRADDLE and short > long:
        signal = 'SHORT'
    return long, short, signal


def portfolio_hedge(held, spot, maturity, vol, rtm_position):
    delta = 0.0
    for row in held:
        if not valid_security(row, OPTION_MULTIPLIER) or number(row.get('position')) is None:
            raise ValueError('Invalid held option data; hedge unavailable.')
        strike, kind = option_identity(row)
        delta += float(row['position']) * contract_size(row) * black_scholes(
            spot, strike, maturity, vol, kind, RISK_FREE_RATE)[1]
    target = round(-delta)
    return delta, delta + rtm_position, target, target - rtm_position


def entry_quantity(call=None, put=None, direction=None):
    """Flat-account size cap including one-leg exposure and eventual hedging."""
    quantity = max(0, min(int(ENTRY_QUANTITY), OPTION_MAX_TRADE,
                      OPTION_GROSS_LIMIT // 2, OPTION_NET_LIMIT // 2,
                      RTM_POSITION_LIMIT // OPTION_MULTIPLIER,
                      int(min(MANUAL_UNHEDGED_DELTA_BUDGET, CASE_DELTA_LIMIT)
                          // OPTION_MULTIPLIER)))
    if direction == 'LONG' and call is not None and put is not None:
        debit = (float(call['ask']) + float(put['ask'])) * OPTION_MULTIPLIER
        debit += 2 * OPTION_COMMISSION_PER_CONTRACT_PER_SIDE
        quantity = min(quantity, max(0, int(MAX_LONG_PREMIUM_DOLLARS // debit)))
    return quantity


def close_instruction(row):
    position = number(row.get('position'))
    multiplier = OPTION_MULTIPLIER if option_identity(row) else 1
    if position is None or not valid_security(row, multiplier):
        return f'{row.get("ticker")}: closing quote/position unavailable; inspect client.'
    side, quote = ('SELL', 'bid') if position > 0 else ('BUY', 'ask')
    cap = OPTION_MAX_TRADE if multiplier == OPTION_MULTIPLIER else RTM_MAX_TRADE
    quantity = min(abs(position), cap)
    suffix = ' (first chunk; refresh positions before the next)' if abs(position) > cap else ''
    return f'{side} {quantity:g} {row["ticker"]} at current {quote} {float(row[quote]):.2f}{suffix}'


def position_review(held, rtm, spot, maturity, vols, tick):
    """Executable marks and a forward-looking exit test; never reconstruct fills."""
    lines = []
    active = held + ([rtm] if number(rtm.get('position')) != 0 else [])
    pnl = 0.0
    pnl_available = True
    for row in active:
        position = number(row.get('position'))
        multiplier = OPTION_MULTIPLIER if option_identity(row) else 1
        if position is None or not valid_security(row, multiplier):
            return ['REVIEW DATA: position valuation unavailable; inspect client.']
        quote = float(row['bid'] if position > 0 else row['ask'])
        cash = position * multiplier * quote
        fee = abs(position) * (OPTION_COMMISSION_PER_CONTRACT_PER_SIDE
                               if multiplier == OPTION_MULTIPLIER else RTM_FEE_PER_SHARE_PER_SIDE)
        lines.append(f'{row["ticker"]}: close {"receive" if cash >= 0 else "pay"} '
                     f'${abs(cash):.2f}, closing fee ${fee:.2f}')
        basis = number(row.get('vwap'))
        if basis is None or basis <= 0:
            pnl_available = False
        else:
            pnl += position * multiplier * (quote - basis) - fee
    lines.append(f'Open-position mark-to-close P&L: ${pnl:.2f}' if pnl_available else
                 'Open-position mark-to-close P&L: unavailable (API vwap missing).')
    lines.append('P&L excludes entry fees, realized trades, prior hedge costs and fines; check client total P&L.')
    groups = {}
    for row in held:
        strike, kind = option_identity(row)
        groups.setdefault(strike, {})[kind] = row
    balanced = bool(groups) and all(set(pair) == {'C', 'P'} and
                 number(pair['C'].get('position')) == number(pair['P'].get('position'))
                 for pair in groups.values())
    gross = sum(abs(float(row['position'])) for row in held)
    net = sum(float(row['position']) for row in held)
    rtm_position = float(rtm['position'])
    breach = (gross > OPTION_GROSS_LIMIT or abs(net) > OPTION_NET_LIMIT
              or abs(rtm_position) > RTM_POSITION_LIMIT)
    lines.append(f'Limits: option gross {gross:g}/{OPTION_GROSS_LIMIT}, '
                 f'net {net:g}/±{OPTION_NET_LIMIT}; RTM {rtm_position:g}/±{RTM_POSITION_LIMIT}')
    reasons = []
    if breach:
        reasons.append('position limit exceeded')
    if tick >= EXIT_REVIEW_TICK:
        reasons.append('planned expiry review time reached')
    if held and not balanced:
        reasons.append('partial or unbalanced position; inspect fills before acting')
    for strike, pair in groups.items():
        if set(pair) != {'C', 'P'}:
            continue
        position = float(pair['C']['position'])
        if position != float(pair['P']['position']):
            continue
        # Compare current liquidation with conservative fair value. Entry costs
        # are sunk; this is not another entry-edge calculation.
        vol = vols[0] if position > 0 else vols[2]
        fair = sum(black_scholes(spot, strike, maturity, vol, kind, RISK_FREE_RATE)[0]
                   for kind in ('C', 'P'))
        market = sum(float(row['bid' if position > 0 else 'ask']) for row in pair.values())
        advantage = (fair - market) * OPTION_MULTIPLIER * (1 if position > 0 else -1)
        advantage -= REHEDGE_ALLOWANCE_PER_STRADDLE + MODEL_BUFFER_PER_STRADDLE
        lines.append(f'Held K={strike:g} {"LONG" if position > 0 else "SHORT"}: '
                     f'remaining model advantage ${advantage:.2f}/straddle')
        if advantage <= EXIT_REMAINING_ADVANTAGE:
            reasons.append(f'K={strike:g} remaining advantage ≤ ${EXIT_REMAINING_ADVANTAGE:.2f}')
    if not held:
        lines.append('RTM ONLY: option exposure is gone; target stock position is zero.')
        lines.append('MANUAL FLATTEN: ' + close_instruction(rtm))
    elif reasons:
        lines.append('REVIEW EXIT: ' + '; '.join(reasons))
        lines.append('If closing, use these current option quotes; confirm fills in the client:')
        lines.extend(close_instruction(row) for row in held)
        lines.append('After option closes fill, refresh actual positions and flatten remaining RTM. '
                     'Do not execute a pre-close hedge and a flatten instruction together.')
    else:
        lines.append('HOLD: model advantage remains; monitor news, delta and closing value.')
    return lines


class Assistant:
    def __init__(self):
        self.period = None
        self.tick = None
        self.last_printed_tick = None
        self.seen_news = set()
        self.cached_news = {}
        self.selected_strike = None
        self.last_signal = 'WAIT'
        self.last_status = object()
        self.last_positions = None

    def update(self, case, securities, news):
        """Process every snapshot; suppress unchanged intra-tick output."""
        tick = number(case.get('tick'))
        period = case.get('period')
        reset = self.tick is not None and (period != self.period or
                                          (tick is not None and tick < self.tick))
        if reset:
            self.__init__()
        self.period, self.tick = period, tick
        status = case.get('status')
        if status != 'ACTIVE':
            self.last_signal = 'WAIT'
            if status == self.last_status and not reset:
                return ''
            self.last_status = status
            return f'Case {status or "status missing"}; waiting for ACTIVE.'
        self.last_status = status
        if tick is None or not 0 <= tick <= TOTAL_TICKS:
            return 'Invalid case tick; waiting for valid data.'
        same_tick = tick == self.last_printed_tick
        self.last_printed_tick = tick
        lines = ['Case reset; cleared news and selected strike.'] if reset else []
        if tick >= NO_NEW_ENTRY_TICK:
            lines.append('Expiry approaching; no new entries.')
        rows = [r for r in securities if isinstance(r, dict)]
        rtm = next((r for r in rows if r.get('ticker') == 'RTM'), None)
        if rtm is None or not valid_security(rtm, 1):
            self.last_signal = 'WAIT'
            if same_tick:
                return ''
            reason = 'ticker missing' if rtm is None else security_problem(rtm, 1)
            return '\n'.join(lines + [f'Tick {tick:g} | RTM unavailable: {reason}; WAIT'])
        spot = (float(rtm['bid']) + float(rtm['ask'])) / 2
        _, incoming = weekly_schedule(news, tick, period)
        self.cached_news.update(incoming)
        schedule, applicable = weekly_schedule(list(self.cached_news.values()), tick, period)
        new = set(applicable) - self.seen_news
        if not same_tick:
            self.seen_news.update(new)
        options = [r for r in rows if option_identity(r)]
        old_strike = self.selected_strike
        if self.selected_strike is None or (new and not same_tick):
            self.selected_strike = select_strike(options, spot)
        if new and not same_tick:
            lines.append('NEW VOL NEWS')
            for identity in sorted(new, key=lambda key: (key[1], key)):
                for week, kind, low, high in parse_news(applicable[identity]):
                    value = f'{low:.1%}' if low == high else f'{low:.1%}-{high:.1%}'
                    lines.append(f'News {identity[0] or "?"}: week {week} {kind.lower()} volatility {value}')
            lines.append(f'Selected ATM strike: {self.selected_strike}')
        vols = effective_volatility(schedule, tick)
        maturity = max((TOTAL_TICKS - tick) / ANNUALIZATION_TICKS, 1e-12)
        unknown = [str(w) for w, value in schedule.items()
                   if value[0] == 'UNKNOWN' and w * TICKS_PER_WEEK > tick]
        if not same_tick:
            lines.append(f'Unknown weeks at {UNKNOWN_WEEK_VOL:.0%}: ' + (', '.join(unknown) or 'none')
                         + '; edges are model estimates, not realized profit.')
        prefix = f'Tick {tick:g} | RTM {spot:.2f} | K={self.selected_strike} | EffVol {vols[1]:.1%}'
        # Unknown position data must not be mistaken for a flat portfolio.
        held = [r for r in options if number(r.get('position')) != 0]
        rtm_position = number(rtm.get('position'))
        positions = tuple(sorted((r['ticker'], str(number(r.get('position'))))
                                 for r in [rtm] + options))
        changed = self.last_positions is not None and positions != self.last_positions
        self.last_positions = positions
        if same_tick and changed:
            lines.append('POSITION CHANGE')
        if held or rtm_position != 0:
            self.last_signal = 'WAIT'
            held_strikes = ','.join(f'{k:g}' for k in sorted({option_identity(r)[0] for r in held})) or 'none'
            management_prefix = f'Tick {tick:g} | RTM {spot:.2f} | Held K={held_strikes} | EffVol {vols[1]:.1%}'
            lines.extend([management_prefix + ' | POSITION MANAGEMENT',
                          'Options: ' + (', '.join(f'{r["ticker"]}={r.get("position", "unknown")}' for r in held) or 'none')])
            balances = {}
            for r in held:
                strike, kind = option_identity(r)
                balances.setdefault(strike, {})[kind] = number(r.get('position'))
            if any(set(v) != {'C', 'P'} or v.get('C') != v.get('P') for v in balances.values()):
                lines.extend(['PARTIAL OR UNBALANCED OPTION POSITION',
                              'Do not open another straddle.',
                              'Review the filled leg and hedge the actual portfolio delta.'])
            lines.append(f'Current RTM position: {rtm_position}')
            review = position_review(held, rtm, spot, maturity, vols, tick)
            reviewing_exit = any(line.startswith(('REVIEW EXIT:', 'RTM ONLY:')) for line in review)
            try:
                if rtm_position is None:
                    raise ValueError('RTM position missing; hedge unavailable.')
                delta, total, target, required = portfolio_hedge(held, spot, maturity, vols[1], rtm_position)
                lines.append(f'Option delta: {delta:.2f} | Total portfolio delta: {total:.2f}')
                lines.append(f'Target RTM position: {target} | Required RTM trade: {required:+g}')
                if abs(total) > CASE_DELTA_LIMIT:
                    fine = (abs(total) - CASE_DELTA_LIMIT) * DELTA_FINE_PER_SHARE_SECOND
                    lines.append(f'URGENT DELTA LIMIT: estimated excess fine ${fine:.2f}/second.')
                if abs(target) > RTM_POSITION_LIMIT:
                    lines.append('HEDGE BLOCKED: target exceeds RTM limit; reduce option exposure manually.')
                elif held and abs(total) >= DELTA_HEDGE_TRIGGER and (not reviewing_exit or abs(total) > CASE_DELTA_LIMIT):
                    quantity = min(abs(required), RTM_MAX_TRADE)
                    lines.append(f'MANUAL HEDGE: {"BUY" if required > 0 else "SELL"} {quantity:g} RTM shares.')
                    if abs(required) > RTM_MAX_TRADE:
                        lines.append('First hedge chunk only; refresh fills before another trade.')
                elif abs(total) < DELTA_HEDGE_TRIGGER:
                    lines.append('Hedge threshold not reached.')
                elif reviewing_exit:
                    lines.append('Exit review takes priority over a routine hedge; if retaining options, reassess delta.')
            except ValueError as exc:
                lines.append(str(exc))
            lines.extend(review)
            return '\n'.join(lines) if not same_tick or changed else ''
        if same_tick:
            return '\n'.join(lines + ['Portfolio flat; entry review resumes next tick.']) if changed else ''
        pair = {option_identity(r)[1]: r for r in options
                if option_identity(r)[0] == self.selected_strike and valid_security(r, OPTION_MULTIPLIER)}
        if set(pair) != {'C', 'P'}:
            self.last_signal = 'WAIT'
            return '\n'.join(lines + [prefix + ' | Call/put pair unavailable; WAIT'])
        long, short, signal = straddle_edges(pair['C'], pair['P'], spot,
                                           self.selected_strike, maturity, vols)
        # Additional stock spread and rehedging allowance beyond the base formula.
        straddle_delta = sum(black_scholes(spot, self.selected_strike, maturity,
                             vols[1], kind, RISK_FREE_RATE)[1] for kind in ('C', 'P'))
        extra = abs(round(OPTION_MULTIPLIER * straddle_delta)) * (float(rtm['ask']) - float(rtm['bid']))
        extra += REHEDGE_ALLOWANCE_PER_STRADDLE
        long, short = long - extra, short - extra
        signal = ('LONG' if long >= MIN_LONG_EDGE_PER_STRADDLE and long > short else
                  'SHORT' if short >= MIN_SHORT_EDGE_PER_STRADDLE and short > long else 'WAIT')
        quantity = entry_quantity(pair['C'], pair['P'], signal)
        if tick >= NO_NEW_ENTRY_TICK or quantity == 0:
            signal = 'WAIT'
        if quantity == 0:
            lines.append('ENTRY BLOCKED: configured size/risk limits leave no capacity.')
        lines.append(prefix + f' | LONG edge ${long:.2f} | SHORT edge ${short:.2f} | {signal}'
                     + (f' | Entry size {quantity} straddles' if signal != 'WAIT' else ''))
        if signal != 'WAIT' and (signal != self.last_signal or new or old_strike != self.selected_strike):
            side, quote = ('BUY', 'ask') if signal == 'LONG' else ('SELL', 'bid')
            edge = long if signal == 'LONG' else short
            lines.extend([f'{signal} VOL — MANUAL TRADE', f'Tick: {tick:g}',
                          f'Selected strike: {self.selected_strike:g}'])
            for kind in ('C', 'P'):
                row = pair[kind]
                lines.append(f'{side} {quantity} {row["ticker"]} at current {quote} {float(row[quote]):.2f}')
            lines.extend([f'Conservative edge: ${edge:.2f} per straddle',
                          f'Estimated total edge for {quantity}: ${edge * quantity:.2f}',
                          f'Size: {quantity} straddles = {2 * quantity} contracts; one batch only, no adding.',
                          f'Sizing limits: unhedged delta budget {MANUAL_UNHEDGED_DELTA_BUDGET:g}; long premium cap ${MAX_LONG_PREMIUM_DOLLARS:.2f}.',
                          'Short premium received is not maximum loss or capital at risk.',
                          'Confirm no pending manual trades in the client; this program cannot see them.',
                          'Do not submit the RTM hedge yet.',
                          'After both option legs are confirmed filled, let the next API poll read actual positions and calculate the portfolio hedge.',
                          'NO ORDERS HAVE BEEN SUBMITTED.'])
        self.last_signal = signal
        return '\n'.join(lines)


def api_get(session, endpoint, resource):
    response = session.get(endpoint + resource, timeout=API_TIMEOUT_SECONDS)
    if response.status_code in (401, 403):
        raise ValueError('Authentication failed; check credentials at the top of the file.')
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError('Malformed API JSON; waiting for valid data.') from exc


def main(record_path=None):
    endpoint = API_ENDPOINT.rstrip('/')
    assistant = Assistant()
    last_error = None
    try:
        with requests.Session() as session:
            session.auth = (USERNAME, PASSWORD)
            while True:
                try:
                    case = api_get(session, endpoint, '/case')
                    if not isinstance(case, dict):
                        raise ValueError('Missing case data.')
                    securities, news = [], []
                    if case.get('status') == 'ACTIVE':
                        securities = api_get(session, endpoint, '/securities')
                        news = api_get(session, endpoint, '/news')
                        if not isinstance(securities, list) or not securities:
                            raise ValueError('Missing securities; waiting for valid data.')
                        if not isinstance(news, list):
                            raise ValueError('Missing news; waiting for valid data.')
                    # Recheck case after sequential GETs: do not mix a reset with old quotes.
                    if case.get('status') == 'ACTIVE':
                        verified = api_get(session, endpoint, '/case')
                        if (not isinstance(verified, dict) or verified.get('status') != 'ACTIVE'
                                or verified.get('period') != case.get('period')
                                or verified.get('tick') != case.get('tick')):
                            message = 'Snapshot changed during API reads; waiting for a consistent ACTIVE tick.'
                            if last_error != message:
                                print(message, flush=True)
                            last_error = message
                            time.sleep(POLL_SECONDS)
                            continue
                    if record_path:
                        with open(record_path, 'a', encoding='utf-8') as log:
                            log.write(json.dumps(dict(case=case, securities=securities, news=news)) + '\n')
                    output = assistant.update(case, securities, news)
                    if output:
                        print(output, flush=True)
                    last_error = None
                except (requests.RequestException, ValueError, OSError) as exc:
                    message = str(exc) if isinstance(exc, ValueError) else f'API {type(exc).__name__}; retrying.'
                    if message != last_error:
                        print(message, flush=True)
                    last_error = message
                    assistant.last_signal = 'WAIT'
                time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print('Read-only Case 1 assistant stopped cleanly.')


def replay(path):
    """Replay captured observations; this is not a fill simulator or backtest."""
    assistant = Assistant()
    with open(path, encoding='utf-8') as source:
        for line_number, line in enumerate(source, 1):
            try:
                snapshot = json.loads(line)
                output = assistant.update(snapshot['case'], snapshot['securities'], snapshot['news'])
                if output:
                    print(output)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f'Invalid replay snapshot at line {line_number}') from exc


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--record', metavar='JSONL', help='append API snapshots for offline review')
    mode.add_argument('--replay', metavar='JSONL', help='offline output replay; no API connection')
    args = parser.parse_args()
    if args.replay:
        replay(args.replay)
    else:
        main(args.record)
