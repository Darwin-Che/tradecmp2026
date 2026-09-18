"""Depth-aware, fee-aware ETF arbitrage planning."""

from dataclasses import dataclass
from typing import Optional, Tuple

from state import estimate_fill


@dataclass(frozen=True)
class PlannedLeg:
    ticker: str
    signed_quantity: int
    limit_price: float


@dataclass(frozen=True)
class ArbitragePlan:
    reason: str
    quantity: int
    gross_profit_cad: float
    fees_cad: float
    expected_profit_cad: float
    edge_per_share_cad: float
    projected_gross: int
    projected_net: int
    legs: Tuple[PlannedLeg, ...]


def _risk_after(positions, legs):
    projected = dict(positions)
    for leg in legs:
        projected[leg.ticker] = projected.get(leg.ticker, 0) + leg.signed_quantity
    gross = (
        abs(projected.get("BULL", 0))
        + abs(projected.get("BEAR", 0))
        + 2 * abs(projected.get("RITC", 0))
    )
    net = (
        projected.get("BULL", 0)
        + projected.get("BEAR", 0)
        + 2 * projected.get("RITC", 0)
    )
    return gross, net


def evaluate_arbitrage(
    state,
    direction,
    max_quantity=5_000,
    market_fee=0.02,
    minimum_net_edge_cad=0.02,
    minimum_profit_cad=25.0,
    max_gross=500_000,
    min_net=-25_000,
    max_net=25_000,
    prefer_larger=False,
) -> Optional[ArbitragePlan]:
    """Return the most profitable executable quantity for one direction."""
    bull = state.current_books.get("BULL")
    bear = state.current_books.get("BEAR")
    ritc = state.current_books.get("RITC")
    usd = state.current_books.get("USD")
    if not all((bull, bear, ritc, usd)):
        return None
    if usd.best_bid is None or usd.best_ask is None:
        return None

    if direction == "BUY_ETF":
        reason = "ETF_ARB_BUY_ETF"
        actions = (("BULL", bull, "SELL"), ("BEAR", bear, "SELL"),
                   ("RITC", ritc, "BUY"))
    elif direction == "SELL_ETF":
        reason = "ETF_ARB_SELL_ETF"
        actions = (("BULL", bull, "BUY"), ("BEAR", bear, "BUY"),
                   ("RITC", ritc, "SELL"))
    else:
        raise ValueError("direction must be BUY_ETF or SELL_ETF")

    depths = []
    for _, book, action in actions:
        levels = book.asks if action == "BUY" else book.bids
        depths.append(sum(level.quantity for level in levels))
    common_depth = min([int(max_quantity)] + depths)
    if common_depth <= 0:
        return None

    best = None
    for quantity in range(1, common_depth + 1):
        fills = [estimate_fill(book, action, quantity)
                 for _, book, action in actions]
        if not all(fill.complete for fill in fills):
            continue

        bull_fill, bear_fill, ritc_fill = fills
        if direction == "BUY_ETF":
            gross = (
                bull_fill.notional + bear_fill.notional
                - ritc_fill.notional * usd.best_ask
            )
            signs = (-1, -1, 1)
        else:
            gross = (
                ritc_fill.notional * usd.best_bid
                - bull_fill.notional - bear_fill.notional
            )
            signs = (1, 1, -1)

        fees = quantity * market_fee * (2 + usd.best_ask)
        expected = gross - fees
        if expected < minimum_profit_cad:
            continue
        if expected / quantity < minimum_net_edge_cad:
            continue

        legs = tuple(
            PlannedLeg(ticker, sign * quantity, fill.worst_price)
            for (ticker, _, _), sign, fill in zip(actions, signs, fills)
        )
        projected_gross, projected_net = _risk_after(state.positions, legs)
        if projected_gross > max_gross:
            continue
        if not min_net <= projected_net <= max_net:
            continue

        plan = ArbitragePlan(
            reason=reason,
            quantity=quantity,
            gross_profit_cad=gross,
            fees_cad=fees,
            expected_profit_cad=expected,
            edge_per_share_cad=expected / quantity,
            projected_gross=projected_gross,
            projected_net=projected_net,
            legs=legs,
        )
        if best is None:
            best = plan
        elif prefer_larger and plan.quantity > best.quantity:
            best = plan
        elif not prefer_larger and plan.expected_profit_cad > best.expected_profit_cad:
            best = plan
    return best


def best_arbitrage(state, **kwargs):
    """Return the better of the two executable arbitrage directions."""
    plans = (
        evaluate_arbitrage(state, "BUY_ETF", **kwargs),
        evaluate_arbitrage(state, "SELL_ETF", **kwargs),
    )
    available = [plan for plan in plans if plan is not None]
    return max(available, key=lambda plan: plan.expected_profit_cad, default=None)
