"""Pure fixed-tender profitability evaluation."""

from dataclasses import dataclass
from time import time
from typing import Optional

from state import estimate_fill


@dataclass(frozen=True)
class TenderEvaluation:
    tender_id: int
    should_accept: bool
    reason: str
    action: str
    exit_action: str
    quantity: int
    tender_price: Optional[float]
    visible_quantity: int = 0
    exit_average_price: Optional[float] = None
    exit_worst_price: Optional[float] = None
    gross_profit_usd: float = 0.0
    fees_usd: float = 0.0
    buffer_usd: float = 0.0
    expected_profit_usd: float = 0.0
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
        return (
            f"TENDER {self.tender_id} {verdict} | {self.action} "
            f"{self.quantity} RITC @{price} | exit {self.exit_action} "
            f"avg={average} depth={self.visible_quantity}/{self.quantity} | "
            f"gross={self.gross_profit_usd:+.2f}USD "
            f"fee={self.fees_usd:.2f} buffer={self.buffer_usd:.2f} | "
            f"net={self.expected_profit_usd:+.2f}USD/"
            f"{self.expected_profit_cad:+.2f}CAD "
            f"edge={self.edge_per_share_usd:+.4f}USD | "
            f"risk gross={self.projected_gross} net={self.projected_net} | "
            f"reason={self.reason}"
        )


def evaluate_fixed_tender(
    offer,
    state,
    market_fee_usd=0.02,
    safety_buffer_per_share_usd=0.03,
    minimum_profit_cad=50.0,
    max_book_age_seconds=1.5,
    max_gross=500_000,
    min_net=-25_000,
    max_net=25_000,
    now=None,
):
    """Evaluate accepting and immediately unwinding one fixed RITC tender."""
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

    ritc_book = state.current_books.get("RITC")
    usd_book = state.current_books.get("USD")
    if ritc_book is None or usd_book is None:
        return reject("RITC or USD book unavailable")

    observed_at = time() if now is None else now
    oldest_book = min(ritc_book.timestamp, usd_book.timestamp)
    if observed_at - oldest_book > max_book_age_seconds:
        return reject("market data is stale")

    position_delta = quantity if action == "BUY" else -quantity
    projected_ritc = state.positions.get("RITC", 0) + position_delta
    projected_gross = (
        abs(state.positions.get("BULL", 0))
        + abs(state.positions.get("BEAR", 0))
        + 2 * abs(projected_ritc)
    )
    projected_net = (
        state.positions.get("BULL", 0)
        + state.positions.get("BEAR", 0)
        + 2 * projected_ritc
    )
    risk = {"projected_gross": projected_gross, "projected_net": projected_net}
    if projected_gross > max_gross:
        return reject("projected gross limit exceeded", **risk)
    if not min_net <= projected_net <= max_net:
        return reject("projected net limit exceeded", **risk)

    fill = estimate_fill(ritc_book, exit_action, quantity)
    fill_values = {
        "visible_quantity": fill.filled_quantity,
        "exit_average_price": fill.average_price,
        "exit_worst_price": fill.worst_price,
        **risk,
    }
    if not fill.complete:
        return reject("insufficient visible RITC depth", **fill_values)

    tender_notional = tender_price * quantity
    if action == "BUY":
        gross_profit_usd = fill.notional - tender_notional
    else:
        gross_profit_usd = tender_notional - fill.notional
    fees_usd = market_fee_usd * quantity
    buffer_usd = safety_buffer_per_share_usd * quantity
    expected_profit_usd = gross_profit_usd - fees_usd - buffer_usd
    fx_rate = usd_book.best_bid if expected_profit_usd >= 0 else usd_book.best_ask
    if fx_rate is None:
        return reject("USD conversion quote unavailable", **fill_values)
    expected_profit_cad = expected_profit_usd * fx_rate
    edge_per_share_usd = expected_profit_usd / quantity
    profitable = expected_profit_cad >= minimum_profit_cad
    reason = "profitable after costs" if profitable else "profit below minimum"

    return TenderEvaluation(
        tender_id=tender_id,
        should_accept=profitable,
        reason=reason,
        action=action,
        exit_action=exit_action,
        quantity=quantity,
        tender_price=tender_price,
        gross_profit_usd=gross_profit_usd,
        fees_usd=fees_usd,
        buffer_usd=buffer_usd,
        expected_profit_usd=expected_profit_usd,
        expected_profit_cad=expected_profit_cad,
        edge_per_share_usd=edge_per_share_usd,
        **fill_values,
    )
