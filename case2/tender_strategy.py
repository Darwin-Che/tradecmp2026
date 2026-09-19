"""Pure fixed-tender profitability and exit-route evaluation."""

from dataclasses import dataclass
from itertools import product
from time import time
from typing import Optional, Tuple

from state import estimate_fill


@dataclass(frozen=True)
class TenderExitLeg:
    """One market leg used to neutralize an accepted tender."""

    ticker: str
    signed_quantity: int
    limit_price: float
    role: str


@dataclass(frozen=True)
class TenderEvaluation:
    tender_id: int
    should_accept: bool
    reason: str
    action: str
    exit_action: str
    quantity: int
    tender_price: Optional[float]
    route: str = "NONE"
    direct_quantity: int = 0
    basket_quantity: int = 0
    exit_legs: Tuple[TenderExitLeg, ...] = ()
    visible_quantity: int = 0
    exit_average_price: Optional[float] = None
    exit_worst_price: Optional[float] = None
    gross_profit_usd: float = 0.0
    fees_usd: float = 0.0
    buffer_usd: float = 0.0
    expected_profit_usd: float = 0.0
    basket_profit_cad: float = 0.0
    expected_profit_cad: float = 0.0
    edge_per_share_usd: float = 0.0
    projected_gross: int = 0
    projected_net: int = 0

    def log_line(self):
        verdict = "ACCEPT" if self.should_accept else "REJECT"
        price = "-" if self.tender_price is None else f"{self.tender_price:.4f}"
        average = (
            "-" if self.exit_average_price is None
            else f"{self.exit_average_price:.4f}"
        )
        economics = (
            f"direct={self.direct_quantity} basket={self.basket_quantity} "
            f"direct_net={self.expected_profit_usd:+.2f}USD "
            f"basket_net={self.basket_profit_cad:+.2f}CAD | "
            f"net={self.expected_profit_cad:+.2f}CAD "
            f"edge={self.expected_profit_cad / self.quantity:+.4f}CAD"
            if self.quantity > 0 and self.route != "NONE"
            else "economics=not evaluated"
        )
        return (
            f"TENDER {self.tender_id} {verdict} | {self.action} "
            f"{self.quantity} RITC @{price} | route={self.route} "
            f"direct {self.exit_action} avg={average} "
            f"depth={self.visible_quantity}/{self.direct_quantity} | "
            f"{economics} | "
            f"risk gross={self.projected_gross} net={self.projected_net} | "
            f"reason={self.reason}"
        )


def _portfolio_risk(positions):
    gross = (
        abs(positions.get("BULL", 0))
        + abs(positions.get("BEAR", 0))
        + 2 * abs(positions.get("RITC", 0))
    )
    net = (
        positions.get("BULL", 0)
        + positions.get("BEAR", 0)
        + 2 * positions.get("RITC", 0)
    )
    return gross, net


def _route_is_safe(
    positions,
    tender_sign,
    quantity,
    direct_quantity,
    basket_quantity,
    max_gross,
    min_net,
    max_net,
):
    """Check every corner of the possible asynchronous fill path."""
    base = dict(positions)
    base["RITC"] = base.get("RITC", 0) + tender_sign * quantity

    final = None
    for direct_done, bull_done, bear_done in product((0, 1), repeat=3):
        projected = dict(base)
        projected["RITC"] -= tender_sign * direct_quantity * direct_done
        projected["BULL"] = (
            projected.get("BULL", 0)
            - tender_sign * basket_quantity * bull_done
        )
        projected["BEAR"] = (
            projected.get("BEAR", 0)
            - tender_sign * basket_quantity * bear_done
        )
        gross, net = _portfolio_risk(projected)
        if gross > max_gross or not min_net <= net <= max_net:
            return False, gross, net
        if direct_done and bull_done and bear_done:
            final = (gross, net)
    return True, *final


def _candidate_basket_quantities(quantity, books):
    """Return quantities containing price-depth breakpoints plus a fine grid."""
    candidates = {0, quantity}
    step = 100 if quantity <= 200_000 else 500
    candidates.update(range(0, quantity + 1, step))

    bull, bear, ritc = books
    for book in (bull, bear):
        for levels in (book.bids, book.asks):
            cumulative = 0
            for level in levels:
                cumulative += level.quantity
                candidates.add(min(quantity, cumulative))
    for levels in (ritc.bids, ritc.asks):
        cumulative = 0
        for level in levels:
            cumulative += level.quantity
            candidates.add(max(0, quantity - cumulative))
    return sorted(item for item in candidates if 0 <= item <= quantity)


def evaluate_fixed_tender(
    offer,
    state,
    market_fee_usd=0.02,
    stock_market_fee_cad=0.02,
    safety_buffer_per_share_usd=0.03,
    minimum_profit_cad=50.0,
    max_book_age_seconds=1.5,
    max_gross=500_000,
    min_net=-25_000,
    max_net=25_000,
    now=None,
):
    """Choose the best feasible direct, basket, or hybrid tender exit."""
    tender_id = int(offer.get("tender_id", -1))
    action = str(offer.get("action", "")).upper()
    quantity = int(offer.get("quantity", 0))
    tender_price = offer.get("price")
    ticker = offer.get("ticker", "RITC")
    exit_action = "SELL" if action == "BUY" else "BUY"

    def reject(reason, **values):
        return TenderEvaluation(
            tender_id=tender_id,
            should_accept=False,
            reason=reason,
            action=action or "?",
            exit_action=exit_action,
            quantity=quantity,
            tender_price=None if tender_price is None else float(tender_price),
            **values,
        )

    if not offer.get("is_fixed_bid"):
        return reject("competitive tender")
    if ticker != "RITC":
        return reject(f"unsupported ticker {ticker}")
    if action not in ("BUY", "SELL"):
        return reject(f"unsupported action {action or '?'}")
    if quantity <= 0:
        return reject("quantity must be positive")
    if tender_price is None:
        return reject("missing tender price")
    tender_price = float(tender_price)

    ritc = state.current_books.get("RITC")
    usd = state.current_books.get("USD")
    bull = state.current_books.get("BULL")
    bear = state.current_books.get("BEAR")
    if not all((ritc, usd, bull, bear)):
        return reject("RITC, USD, BULL, or BEAR book unavailable")

    observed_at = time() if now is None else now
    oldest_book = min(book.timestamp for book in (ritc, usd, bull, bear))
    if observed_at - oldest_book > max_book_age_seconds:
        return reject("market data is stale")
    if usd.best_bid is None or usd.best_ask is None:
        return reject("USD conversion quote unavailable")

    tender_sign = 1 if action == "BUY" else -1
    after_tender = dict(state.positions)
    after_tender["RITC"] = (
        after_tender.get("RITC", 0) + tender_sign * quantity
    )
    acceptance_gross, acceptance_net = _portfolio_risk(after_tender)
    acceptance_risk = {
        "projected_gross": acceptance_gross,
        "projected_net": acceptance_net,
    }
    if acceptance_gross > max_gross:
        return reject("tender acceptance would exceed gross limit", **acceptance_risk)
    if not min_net <= acceptance_net <= max_net:
        return reject("tender acceptance would exceed net limit", **acceptance_risk)

    best = None
    for basket_quantity in _candidate_basket_quantities(
        quantity, (bull, bear, ritc),
    ):
        direct_quantity = quantity - basket_quantity
        safe, final_gross, final_net = _route_is_safe(
            state.positions,
            tender_sign,
            quantity,
            direct_quantity,
            basket_quantity,
            max_gross,
            min_net,
            max_net,
        )
        if not safe:
            continue

        direct_fill = estimate_fill(ritc, exit_action, direct_quantity)
        basket_action = "SELL" if action == "BUY" else "BUY"
        bull_fill = estimate_fill(bull, basket_action, basket_quantity)
        bear_fill = estimate_fill(bear, basket_action, basket_quantity)
        if not all((direct_fill.complete, bull_fill.complete, bear_fill.complete)):
            continue

        if action == "BUY":
            direct_gross_usd = (
                direct_fill.notional - tender_price * direct_quantity
            )
            basket_gross_cad = (
                bull_fill.notional
                + bear_fill.notional
                - tender_price * basket_quantity * usd.best_ask
            )
        else:
            direct_gross_usd = (
                tender_price * direct_quantity - direct_fill.notional
            )
            basket_gross_cad = (
                tender_price * basket_quantity * usd.best_bid
                - bull_fill.notional
                - bear_fill.notional
            )

        direct_fees_usd = market_fee_usd * direct_quantity
        direct_buffer_usd = safety_buffer_per_share_usd * direct_quantity
        direct_net_usd = (
            direct_gross_usd - direct_fees_usd - direct_buffer_usd
        )
        direct_fx = usd.best_bid if direct_net_usd >= 0 else usd.best_ask
        direct_net_cad = direct_net_usd * direct_fx

        basket_fees_cad = 2 * stock_market_fee_cad * basket_quantity
        basket_buffer_cad = (
            safety_buffer_per_share_usd * basket_quantity * usd.best_ask
        )
        basket_net_cad = (
            basket_gross_cad - basket_fees_cad - basket_buffer_cad
        )
        expected_profit_cad = direct_net_cad + basket_net_cad

        legs = []
        if direct_quantity:
            legs.append(TenderExitLeg(
                ticker="RITC",
                signed_quantity=-tender_sign * direct_quantity,
                limit_price=direct_fill.worst_price,
                role="DIRECT",
            ))
        if basket_quantity:
            basket_sign = -tender_sign
            legs.extend((
                TenderExitLeg(
                    ticker="BULL",
                    signed_quantity=basket_sign * basket_quantity,
                    limit_price=bull_fill.worst_price,
                    role="BASKET",
                ),
                TenderExitLeg(
                    ticker="BEAR",
                    signed_quantity=basket_sign * basket_quantity,
                    limit_price=bear_fill.worst_price,
                    role="BASKET",
                ),
            ))

        route = (
            "DIRECT" if basket_quantity == 0
            else "BASKET" if direct_quantity == 0
            else "HYBRID"
        )
        candidate = TenderEvaluation(
            tender_id=tender_id,
            should_accept=expected_profit_cad >= minimum_profit_cad,
            reason=(
                "profitable after costs"
                if expected_profit_cad >= minimum_profit_cad
                else "profit below minimum"
            ),
            action=action,
            exit_action=exit_action,
            quantity=quantity,
            tender_price=tender_price,
            route=route,
            direct_quantity=direct_quantity,
            basket_quantity=basket_quantity,
            exit_legs=tuple(legs),
            visible_quantity=direct_fill.filled_quantity,
            exit_average_price=direct_fill.average_price,
            exit_worst_price=direct_fill.worst_price,
            gross_profit_usd=direct_gross_usd,
            fees_usd=direct_fees_usd,
            buffer_usd=direct_buffer_usd,
            expected_profit_usd=direct_net_usd,
            basket_profit_cad=basket_net_cad,
            expected_profit_cad=expected_profit_cad,
            edge_per_share_usd=(
                direct_net_usd / direct_quantity if direct_quantity else 0.0
            ),
            projected_gross=final_gross,
            projected_net=final_net,
        )
        if best is None or candidate.expected_profit_cad > best.expected_profit_cad:
            best = candidate

    if best is None:
        return reject(
            "insufficient executable depth or route capacity",
            **acceptance_risk,
        )
    return best
