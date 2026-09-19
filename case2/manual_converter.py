"""Optional, human-executed converter route for balanced ETF inventory.

This module never calls a converter API. It only compares a manual conversion
with an executable market close and recognizes the resulting position change.
"""

from dataclasses import dataclass
from time import monotonic, time

from arbitrage import ArbitragePlan, PlannedLeg
from state import estimate_fill


BLOCK_SIZE = 10_000
FEE_USD_PER_BLOCK = 1_500.0


@dataclass(frozen=True)
class ConverterProposal:
    action: str
    direction: str
    blocks: int
    market_close_cad: float
    converter_cost_cad: float
    advantage_cad: float

    @property
    def quantity(self):
        return self.blocks * BLOCK_SIZE


@dataclass
class ConverterWindow:
    proposal: ConverterProposal
    baseline: tuple[int, int, int]
    deadline_at: float
    confirmed_blocks: int = 0


def equity_signature(positions):
    return tuple(int(positions.get(ticker, 0))
                 for ticker in ("BULL", "BEAR", "RITC"))


def balanced_converter_direction(positions):
    bull, bear, ritc = equity_signature(positions)
    if bull > 0 and bear > 0 and ritc < 0:
        return "CREATE", "BUY_ETF", min(bull, bear, -ritc) // BLOCK_SIZE
    if bull < 0 and bear < 0 and ritc > 0:
        return "REDEEM", "SELL_ETF", min(-bull, -bear, ritc) // BLOCK_SIZE
    return None


def market_close_plan(state, direction, quantity, market_fee):
    """Require executable depth for every share of a full-block close."""
    books = {ticker: state.current_books.get(ticker)
             for ticker in ("BULL", "BEAR", "RITC", "USD")}
    if not all(books.values()):
        return None
    usd = books["USD"]
    if usd.best_bid is None or usd.best_ask is None:
        return None
    if direction == "BUY_ETF":
        actions = (("BULL", "SELL", -1), ("BEAR", "SELL", -1),
                   ("RITC", "BUY", 1))
    elif direction == "SELL_ETF":
        actions = (("BULL", "BUY", 1), ("BEAR", "BUY", 1),
                   ("RITC", "SELL", -1))
    else:
        raise ValueError("direction must be BUY_ETF or SELL_ETF")
    fills = [estimate_fill(books[ticker], action, quantity)
             for ticker, action, _ in actions]
    if not all(fill.complete for fill in fills):
        return None
    bull_fill, bear_fill, ritc_fill = fills
    if direction == "BUY_ETF":
        gross_profit = (
            bull_fill.notional + bear_fill.notional
            - ritc_fill.notional * usd.best_ask
        )
    else:
        gross_profit = (
            ritc_fill.notional * usd.best_bid
            - bull_fill.notional - bear_fill.notional
        )
    fees = quantity * market_fee * (2 + usd.best_ask)
    legs = tuple(
        PlannedLeg(ticker, sign * quantity, fill.worst_price)
        for (ticker, _, sign), fill in zip(actions, fills)
    )
    projected = dict(state.positions)
    for leg in legs:
        projected[leg.ticker] = projected.get(leg.ticker, 0) + leg.signed_quantity
    projected_gross = (
        abs(projected.get("BULL", 0)) + abs(projected.get("BEAR", 0))
        + 2 * abs(projected.get("RITC", 0))
    )
    projected_net = (
        projected.get("BULL", 0) + projected.get("BEAR", 0)
        + 2 * projected.get("RITC", 0)
    )
    expected = gross_profit - fees
    return ArbitragePlan(
        reason=f"ETF_ARB_{direction}", quantity=quantity,
        gross_profit_cad=gross_profit, fees_cad=fees,
        expected_profit_cad=expected,
        edge_per_share_cad=expected / quantity,
        projected_gross=projected_gross,
        projected_net=projected_net,
        legs=legs,
    )


def propose_conversion(
    state, *, max_blocks, market_fee, delay_buffer_cad_per_share,
    min_advantage_cad, max_fallback_loss_cad_per_block,
    max_book_age_seconds=1.5, now=None,
):
    """Propose only when conversion beats a bounded, executable fallback."""
    direction = balanced_converter_direction(state.positions)
    if direction is None:
        return None
    action, close_direction, available_blocks = direction
    usd = state.current_books.get("USD")
    books = [state.current_books.get(ticker)
             for ticker in ("BULL", "BEAR", "RITC", "USD")]
    if not all(books) or usd.best_ask is None:
        return None
    observed_at = time() if now is None else now
    if observed_at - min(book.timestamp for book in books) > max_book_age_seconds:
        return None
    best = None
    for blocks in range(1, min(max_blocks, available_blocks) + 1):
        quantity = blocks * BLOCK_SIZE
        market = market_close_plan(state, close_direction, quantity, market_fee)
        if market is None:
            continue
        if market.expected_profit_cad < -max_fallback_loss_cad_per_block * blocks:
            continue
        converter_cost_cad = (
            FEE_USD_PER_BLOCK * blocks * usd.best_ask
            + delay_buffer_cad_per_share * quantity
        )
        advantage = -converter_cost_cad - market.expected_profit_cad
        if advantage < min_advantage_cad:
            continue
        proposal = ConverterProposal(
            action=action, direction=close_direction, blocks=blocks,
            market_close_cad=market.expected_profit_cad,
            converter_cost_cad=converter_cost_cad,
            advantage_cad=advantage,
        )
        if best is None or proposal.advantage_cad > best.advantage_cad:
            best = proposal
    return best


def converted_blocks(window, positions):
    """Return confirmed blocks, or None for an unexpected position change."""
    bull, bear, ritc = equity_signature(positions)
    old_bull, old_bear, old_ritc = window.baseline
    if window.proposal.action == "CREATE":
        deltas = (old_bull - bull, old_bear - bear, ritc - old_ritc)
    else:
        deltas = (bull - old_bull, bear - old_bear, old_ritc - ritc)
    if (deltas[0] != deltas[1] or deltas[1] != deltas[2]
            or deltas[0] < 0 or deltas[0] % BLOCK_SIZE
            or deltas[0] > window.proposal.quantity):
        return None
    return deltas[0] // BLOCK_SIZE


def apply_conversion_to_open_lots(state, action, quantity):
    """Keep convergence lots consistent with externally converted inventory."""
    opening_reason = (
        "ETF_ARB_SELL_ETF" if action == "CREATE" else "ETF_ARB_BUY_ETF"
    )
    remaining = quantity
    for bundle in state.bundles.values():
        if (bundle.status != "FILLED" or bundle.reason != opening_reason
                or bundle.open_quantity <= 0):
            continue
        closed = min(remaining, bundle.open_quantity)
        bundle.open_quantity -= closed
        remaining -= closed
        if not remaining:
            break
    return quantity - remaining


def start_window(proposal, positions, wait_seconds, now=None):
    started_at = monotonic() if now is None else now
    return ConverterWindow(
        proposal=proposal, baseline=equity_signature(positions),
        deadline_at=started_at + wait_seconds,
    )
