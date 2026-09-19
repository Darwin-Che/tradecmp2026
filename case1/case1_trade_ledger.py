"""Fill-based attribution for one active volatility campaign at a time.

No inferred entry basis for inherited positions. Spread is an execution-quality
estimate already embedded in fill cash flows, and is never deducted twice.
"""
from dataclasses import dataclass, field
from uuid import uuid4
import math


def execution_fee(row, fallback):
    """Server fee per traded unit; configured rate is only a fallback."""
    try:
        fee = float(row.get("trading_fee"))
    except (TypeError, ValueError):
        return fallback, "configured fallback"
    if not math.isfinite(fee) or fee < 0:
        return fallback, "configured fallback"
    return fee, "security trading_fee"


@dataclass
class Trade:
    trade_id: int
    direction: str
    strike: float
    entry_tick: float
    entry_edge: float | None
    sleeve: str
    maximum_edge: float | None = None
    exit_edge: float | None = None
    exit_tick: float | None = None
    max_position: int = 0
    option_pnl: float = 0.0
    stock_pnl: float = 0.0
    option_commissions: float = 0.0
    stock_commissions: float = 0.0
    spread_cost: float = 0.0
    turnover: int = 0
    max_delta: float = 0.0
    max_gamma: float = 0.0
    estimated_fills: int = 0
    fills: list = field(default_factory=list)
    entry_target_pairs: int = 0
    unmatched_events: int = 0
    max_unmatched_contracts: int = 0
    unmatched_ticks: float = 0
    unmatched_since: float | None = None
    additions_blocked_by_stale_tick: int = 0
    additions_blocked_by_worsened_quote: int = 0
    additions_blocked_by_persistence_rule: int = 0
    additions_refreshed_after_tick: int = 0
    additions_repriced_after_quote: int = 0

    @property
    def net(self):
        return self.option_pnl+self.stock_pnl-self.option_commissions-self.stock_commissions

    def result(self):
        return dict(self.__dict__, net_pnl=self.net,
                    target_pairs=self.entry_target_pairs, maximum_matched_pairs=self.max_position,
                    target_filled_pct=100*self.max_position/max(1, self.entry_target_pairs),
                    campaign_outcome='COMPLETED_STRADDLE' if self.max_position > 0 else 'ABORTED_ENTRY',
                    holding_ticks=None if self.exit_tick is None else self.exit_tick-self.entry_tick,
                    commission_source='per-fill security fee or configured fallback', spread_deducted_from_net=False)


class TradeLedger:
    def __init__(self, record, option_fee, stock_fee):
        self.run_id = str(uuid4())
        self.record = lambda event, **data: record(event, run_id=self.run_id, **data)
        self.option_fee, self.stock_fee = option_fee, stock_fee
        self.active = None
        self.closed = []
        self.context = None
        self.inherited = False
        self.max_delta = 0.0
        self.summarized = False
        self.expected = {}
        self.abandoned = 0
        self.deployment_counts = dict(additions_blocked_by_stale_tick=0,
            additions_blocked_by_worsened_quote=0, additions_blocked_by_persistence_rule=0,
            additions_refreshed_after_tick=0, additions_repriced_after_quote=0)

    @staticmethod
    def positions(rows):
        return {r['ticker']: float(r.get('position', 0)) for r in rows
                if r.get('ticker', '').startswith('RTM') and float(r.get('position', 0)) != 0}

    def abandon(self):
        self.record('trade_attribution_abandoned', trade_id=self.active.trade_id,
                    reason='positions changed outside tracked fills; complete entry basis unavailable')
        self.abandoned += 1
        self.active = None

    @staticmethod
    def flat(rows):
        return all(float(r.get('position', 0)) == 0 for r in rows
                   if r.get('ticker', '').startswith('RTM'))

    def observe(self, s):
        self.context = s
        self.max_delta = max(self.max_delta, abs(s['portfolio_delta']))
        if self.active and self.positions(s['rows'].values()) != self.expected:
            self.abandon()
        if self.active is None:
            self.inherited = not self.flat(s['rows'].values())
            return
        t = self.active
        self.sync_deployment(s)
        t.max_delta = max(t.max_delta, abs(s['portfolio_delta']))
        t.max_gamma = max(t.max_gamma, abs(s['option_gamma']))
        e = s.get('selected')
        if e and e['strike'] == t.strike:
            edge = e['long' if t.direction == 'LONG' else 'short']
            t.exit_edge = edge
            t.maximum_edge = max(t.maximum_edge if t.maximum_edge is not None else edge, edge)

    def sync_deployment(self, snapshot):
        campaign = snapshot.get('campaign') or {}
        metrics = campaign.get('metrics') or {}
        if self.active:
            self.active.entry_target_pairs = max(self.active.entry_target_pairs,
                metrics.get('target_pairs', campaign.get('target', 0)))
            for name in self.deployment_counts:
                setattr(self.active, name, metrics.get(name, getattr(self.active, name)))

    def fill(self, row, change, quantity, price, source, order_id, after):
        if not quantity or self.context is None:
            return
        s = self.context
        if self.active is None:
            if self.inherited or row['ticker'] == 'RTM':
                self.record('unattributed_fill', ticker=row['ticker'], quantity=quantity,
                            reason='inherited exposure or standalone stock; entry basis unavailable')
                return
            e = s.get('selected')
            strike = float(row['ticker'][3:-1])
            direction = 'LONG' if change > 0 else 'SHORT'
            edge = e['long' if change > 0 else 'short'] if e and e['strike'] == strike else None
            self.active = Trade(len(self.closed)+self.abandoned+1, direction, strike, s['tick'], edge,
                                (s.get('campaign') or {}).get('sleeve', 'ROBUST'), maximum_edge=edge)
            self.active.entry_target_pairs = (s.get('campaign') or {}).get('target', quantity)
            self.expected = self.positions(s['rows'].values())
        expected = dict(self.expected)
        expected[row['ticker']] = expected.get(row['ticker'], 0)+(quantity if change > 0 else -quantity)
        expected = {ticker: pos for ticker, pos in expected.items() if pos}
        if expected != self.positions(after):
            self.abandon()
            self.inherited = not self.flat(after)
            return
        self.expected = expected
        t = self.active
        self.sync_deployment(s)
        stock = row['ticker'] == 'RTM'
        multiplier = 1 if stock else 100
        sign = 1 if change > 0 else -1
        cash = -sign*quantity*price*multiplier
        fee, fee_source = execution_fee(row, self.stock_fee if stock else self.option_fee)
        commission = quantity*fee
        mid = (float(row['bid'])+float(row['ask']))/2
        spread = sign*(price-mid)*quantity*multiplier
        if stock:
            t.stock_pnl += cash
            t.stock_commissions += commission
            t.turnover += quantity
        else:
            t.option_pnl += cash
            t.option_commissions += commission
        t.spread_cost += spread
        t.estimated_fills += source != 'actual_vwap'
        t.max_delta = max(t.max_delta, abs(s['portfolio_delta']))
        t.max_gamma = max(t.max_gamma, abs(s['option_gamma']))
        pair = [r for r in after if r.get('ticker') in
                (f'RTM{t.strike:g}C', f'RTM{t.strike:g}P')]
        if len(pair) == 2 and float(pair[0]['position'])*float(pair[1]['position']) >= 0:
            t.max_position = max(t.max_position, int(min(abs(float(r['position'])) for r in pair)))
        metrics = (s.get('campaign') or {}).get('metrics')
        if metrics is not None:
            metrics['maximum_matched_pairs'] = max(metrics.get('maximum_matched_pairs', 0), t.max_position)
            metrics['target_filled_pct'] = 100*metrics['maximum_matched_pairs']/max(1, metrics['target_pairs'])
        positions = [float(r['position']) for r in pair]
        unmatched = abs(positions[0]-positions[1]) if len(positions) == 2 else sum(abs(p) for p in positions)
        if unmatched and t.unmatched_since is None:
            t.unmatched_events += 1
            t.unmatched_since = s['tick']
        t.max_unmatched_contracts = max(t.max_unmatched_contracts, int(unmatched))
        if not unmatched and t.unmatched_since is not None:
            t.unmatched_ticks += max(0, s['tick']-t.unmatched_since)
            t.unmatched_since = None
        fill = dict(order_id=order_id, tick=s['tick'], ticker=row['ticker'], quantity=quantity,
                    side='BUY' if change > 0 else 'SELL', price=price, price_source=source,
                    commission=commission, commission_source=fee_source, spread_estimate=spread)
        t.fills.append(fill)
        self.record('attributed_fill', trade_id=t.trade_id, **fill)
        if self.flat(after):
            t.exit_tick = s['tick']
            self.closed.append(t)
            self.active = None
            self.inherited = False
            self.record('trade_closed', **t.result())
            counts = {f'{kind}_{side.lower()}': sum(f['quantity'] for f in t.fills
                       if f['ticker'].endswith(kind) and f['side'] == side)
                      for kind in ('C', 'P') for side in ('BUY', 'SELL')}
            print(f'PAIRED ORDER AUDIT: target={t.entry_target_pairs}, max matched={t.max_position}, '
                  f'contracts={counts}, unmatched events={t.unmatched_events}, '
                  f'max unmatched={t.max_unmatched_contracts}, unmatched ticks={t.unmatched_ticks}; '
                  f'outcome={"COMPLETED_STRADDLE" if t.max_position else "ABORTED_ENTRY"}', flush=True)
            print(f'DEPLOYMENT AUDIT: target pairs={t.entry_target_pairs}, maximum matched pairs={t.max_position}, '
                  f'target filled={100*t.max_position/max(1, t.entry_target_pairs):.1f}%; '
                  f'additions blocked: stale tick={t.additions_blocked_by_stale_tick}, '
                  f'worsened quote={t.additions_blocked_by_worsened_quote}, '
                  f'persistence={t.additions_blocked_by_persistence_rule}', flush=True)
            fmt = lambda value: 'unavailable' if value is None else f'${value:.2f}'
            print(f'\nTRADE #{t.trade_id}: {t.direction} VOL K={t.strike:g} ({t.sleeve})\n'
                  f'Entry/exit ticks: {t.entry_tick:g}/{t.exit_tick:g}; max straddles: {t.max_position}; '
                  f'holding: {t.exit_tick-t.entry_tick:g} ticks\n'
                  f'Entry/max/exit robust edge: {fmt(t.entry_edge)} / {fmt(t.maximum_edge)} / {fmt(t.exit_edge)}\n'
                  f'Option realized P&L: ${t.option_pnl:+.2f}; RTM hedge P&L: ${t.stock_pnl:+.2f}\n'
                  f'Option commissions: -${t.option_commissions:.2f}; RTM commissions: -${t.stock_commissions:.2f}\n'
                  f'Estimated spread cost: ${t.spread_cost:.2f} (already included in fill P&L)\n'
                  f'NET TRADE P&L: ${t.net:+.2f}; commissions use security fees with fallback; '
                  f'limit-price fallback fills: {t.estimated_fills}\n'
                  f'RTM turnover: {t.turnover}; max sampled abs delta: {t.max_delta:.1f}; '
                  f'max sampled abs gamma: {t.max_gamma:.2f}', flush=True)

    def summary(self):
        if self.summarized:
            return
        self.summarized = True
        n = len(self.closed)
        total = lambda name: sum(getattr(t, name) for t in self.closed)
        edges = [t.entry_edge for t in self.closed if t.entry_edge is not None]
        report = dict(trades=n, net_pnl=sum(t.net for t in self.closed),
                      option_pnl=total('option_pnl'), stock_pnl=total('stock_pnl'),
                      option_commissions=total('option_commissions'), stock_commissions=total('stock_commissions'),
                      estimated_spread_cost=total('spread_cost'), turnover=total('turnover'),
                      win_rate=sum(t.net > 0 for t in self.closed)/n if n else None,
                      average_entry_edge=sum(edges)/len(edges) if edges else None,
                      average_holding_ticks=sum(t.exit_tick-t.entry_tick for t in self.closed)/n if n else None,
                      max_sampled_delta=self.max_delta, open_trade=self.active is not None,
                      inherited_exposure=self.inherited, limit_price_fallbacks=total('estimated_fills'))
        report.update(self.deployment_counts)
        report['target_pairs'] = total('entry_target_pairs')
        report['maximum_matched_pairs'] = total('max_position')
        report['target_filled_pct'] = 100*report['maximum_matched_pairs']/max(1, report['target_pairs'])
        report['unattributed_campaigns'] = self.abandoned
        completed = [t for t in self.closed if t.max_position > 0]
        report['completed_straddles'] = len(completed)
        report['aborted_entries'] = n-len(completed)
        report['completed_straddle_win_rate'] = sum(t.net > 0 for t in completed)/len(completed) if completed else None
        report['average_pnl'] = report['net_pnl']/n if n else None
        self.record('case_summary', **report)
        print('\nEND-OF-CASE ATTRIBUTION (closed tracked trades only; security fees with fallback)\n'
              + '\n'.join(f'{key}: {value}' for key, value in report.items()), flush=True)
