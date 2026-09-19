"""In-memory state for the ETF arbitrage algorithm."""

from collections import deque
from dataclasses import dataclass, field
from time import time
from typing import Deque, Dict, Iterable, Optional, Tuple

from intentions import OrderIntent, TradeBundle, active_intents


INSTRUMENTS = ("BULL", "BEAR", "RITC", "USD", "CAD")
MARKET_TICKERS = ("BULL", "BEAR", "RITC", "USD")


@dataclass(frozen=True)
class PriceLevel:
    """Total available quantity at one price."""

    price: float
    quantity: int


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Immutable, aggregated order-book depth observed at one time."""

    ticker: str
    timestamp: float
    bids: Tuple[PriceLevel, ...]
    asks: Tuple[PriceLevel, ...]

    @property
    def best_bid(self):
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self):
        return self.asks[0].price if self.asks else None

    @property
    def spread(self):
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass(frozen=True)
class FillEstimate:
    """Estimated result of immediately crossing an order book."""

    requested_quantity: int
    filled_quantity: int
    notional: float
    average_price: Optional[float]
    worst_price: Optional[float]

    @property
    def complete(self):
        return self.filled_quantity == self.requested_quantity


def aggregate_levels(entries: Iterable[Tuple[float, int]], descending=False):
    """Combine individual orders sharing a price into sorted levels."""
    quantities = {}
    for price, quantity in entries:
        price = float(price)
        quantity = int(quantity)
        if quantity > 0:
            quantities[price] = quantities.get(price, 0) + quantity
    return tuple(
        PriceLevel(price, quantities[price])
        for price in sorted(quantities, reverse=descending)
    )


def estimate_fill(book, action, quantity):
    """Estimate a marketable BUY or SELL against visible depth."""
    action = action.upper()
    if action not in ("BUY", "SELL"):
        raise ValueError("action must be BUY or SELL")
    if quantity < 0:
        raise ValueError("quantity cannot be negative")

    levels = book.asks if action == "BUY" else book.bids
    remaining = int(quantity)
    filled = 0
    notional = 0.0
    worst_price = None

    for level in levels:
        level_fill = min(remaining, level.quantity)
        filled += level_fill
        remaining -= level_fill
        notional += level_fill * level.price
        if level_fill:
            worst_price = level.price
        if remaining == 0:
            break

    return FillEstimate(
        requested_quantity=int(quantity),
        filled_quantity=filled,
        notional=notional,
        average_price=notional / filled if filled else None,
        worst_price=worst_price,
    )


@dataclass(frozen=True)
class EdgeSnapshot:
    """Executable arbitrage edges calculated for one market cycle."""

    timestamp: float
    buy_etf_edge_cad: float
    sell_etf_edge_cad: float


@dataclass
class OrderState:
    """Local view of an order submitted to the RIT API."""

    order_id: int
    ticker: str
    action: str
    quantity: int
    order_type: str
    price: Optional[float] = None
    filled: int = 0
    status: str = "OPEN"
    reason: str = ""
    context: str = ""
    vwap: Optional[float] = None
    fee_per_share: Optional[float] = None

    @property
    def remaining(self):
        return max(0, self.quantity - self.filled)


@dataclass
class TenderState:
    """Local view of a private tender and its unwind progress."""

    tender_id: int
    action: str
    price: float
    quantity: int
    accepted: bool = False
    quantity_unwound: int = 0
    route: str = "DIRECT"
    direct_quantity: int = 0
    basket_quantity: int = 0
    intent_ids: Tuple[str, ...] = ()

    @property
    def remaining_to_unwind(self):
        return max(0, self.quantity - self.quantity_unwound)


@dataclass
class TradingState:
    """All mutable information used by the strategy during one heat."""

    history_limit: int = 300
    current_books: Dict[str, OrderBookSnapshot] = field(default_factory=dict)
    book_history: Dict[str, Deque[OrderBookSnapshot]] = field(init=False)
    edge_history: Deque[EdgeSnapshot] = field(init=False)
    positions: Dict[str, int] = field(
        default_factory=lambda: {ticker: 0 for ticker in INSTRUMENTS}
    )
    orders: Dict[int, OrderState] = field(default_factory=dict)
    tenders: Dict[int, TenderState] = field(default_factory=dict)
    intents: Dict[str, OrderIntent] = field(default_factory=dict)
    bundles: Dict[str, TradeBundle] = field(default_factory=dict)
    hedge_remaining: Dict[str, int] = field(default_factory=dict)
    strategy_status: str = "IDLE"
    case_tick: Optional[int] = None
    case_status: Optional[str] = None
    pnl_cad: float = 0.0
    pnl_high_water_cad: float = 0.0
    portfolio_value_cad: Optional[float] = None
    portfolio_baseline_cad: Optional[float] = None
    gross_start_fraction: float = 0.80
    gross_target_fraction: float = 0.65
    pnl_drawdown_active: bool = False

    def __post_init__(self):
        if self.history_limit <= 0:
            raise ValueError("history_limit must be positive")
        self.book_history = {
            ticker: deque(maxlen=self.history_limit)
            for ticker in MARKET_TICKERS
        }
        self.edge_history = deque(maxlen=self.history_limit)

    def record_book(self, ticker, bids, asks, timestamp=None):
        if ticker not in self.book_history:
            raise KeyError(f"Unsupported market ticker: {ticker}")
        snapshot = OrderBookSnapshot(
            ticker=ticker,
            timestamp=time() if timestamp is None else timestamp,
            bids=aggregate_levels(bids, descending=True),
            asks=aggregate_levels(asks),
        )
        self.current_books[ticker] = snapshot
        self.book_history[ticker].append(snapshot)
        return snapshot

    def record_edges(self, buy_etf_edge_cad, sell_etf_edge_cad, timestamp=None):
        snapshot = EdgeSnapshot(
            timestamp=time() if timestamp is None else timestamp,
            buy_etf_edge_cad=float(buy_etf_edge_cad),
            sell_etf_edge_cad=float(sell_etf_edge_cad),
        )
        self.edge_history.append(snapshot)
        return snapshot

    def update_positions(self, positions):
        for ticker in INSTRUMENTS:
            self.positions[ticker] = int(positions.get(ticker, 0))

    def update_case(self, tick, status):
        self.case_tick = tick
        self.case_status = status

    def update_pnl(self, pnl_cad):
        self.pnl_cad = float(pnl_cad)
        self.pnl_high_water_cad = max(self.pnl_high_water_cad, self.pnl_cad)

    def update_portfolio_value(self, value_cad):
        if value_cad is None:
            return
        self.portfolio_value_cad = float(value_cad)
        if self.portfolio_baseline_cad is None:
            self.portfolio_baseline_cad = self.portfolio_value_cad
        self.update_pnl(self.portfolio_value_cad - self.portfolio_baseline_cad)

    def update_risk_profile(self, start, target, drawdown_active=False):
        self.gross_start_fraction = float(start)
        self.gross_target_fraction = float(target)
        self.pnl_drawdown_active = bool(drawdown_active)

    def add_intent(self, intent):
        if intent.intent_id in self.intents:
            raise ValueError(f"duplicate intent_id: {intent.intent_id}")
        self.intents[intent.intent_id] = intent
        return intent

    def add_bundle(self, bundle):
        if bundle.bundle_id in self.bundles:
            raise ValueError(f"duplicate bundle_id: {bundle.bundle_id}")
        self.bundles[bundle.bundle_id] = bundle
        return bundle

    def active_intents(self):
        return active_intents(self.intents)

    def format_state(self, detail=1):
        """Return a readable state report at compact, summary, or full detail."""
        levels = {"compact": 0, "summary": 1, "full": 2}
        if isinstance(detail, str):
            try:
                detail = levels[detail.lower()]
            except KeyError as exc:
                raise ValueError("detail must be compact, summary, or full") from exc
        if detail not in (0, 1, 2):
            raise ValueError("detail must be 0, 1, or 2")

        positions = " ".join(
            f"{ticker}={self.positions[ticker]:+d}" for ticker in INSTRUMENTS
        )
        latest_edge = self.edge_history[-1] if self.edge_history else None
        edge_text = (
            "edges=unavailable"
            if latest_edge is None
            else (
                f"net_buy_etf={latest_edge.buy_etf_edge_cad:+.4f}CAD "
                f"net_sell_etf={latest_edge.sell_etf_edge_cad:+.4f}CAD"
            )
        )
        header = (
            f"tick={self.case_tick} case={self.case_status} "
            f"strategy={self.strategy_status} | {positions} | "
            f"pnl={self.pnl_cad:+.2f}CAD "
            f"high={self.pnl_high_water_cad:+.2f}CAD "
            f"risk={self.gross_start_fraction:.0%}->{self.gross_target_fraction:.0%} "
            f"drawdown={self.pnl_drawdown_active} | {edge_text}"
        )
        if detail == 0:
            return header

        lines = [header, "Market:"]
        for ticker in MARKET_TICKERS:
            book = self.current_books.get(ticker)
            if book is None:
                lines.append(f"  {ticker}: unavailable")
                continue
            bid = book.bids[0] if book.bids else None
            ask = book.asks[0] if book.asks else None
            bid_text = "-" if bid is None else f"{bid.price:.4f} x {bid.quantity}"
            ask_text = "-" if ask is None else f"{ask.price:.4f} x {ask.quantity}"
            lines.append(
                f"  {ticker}: bid {bid_text} | ask {ask_text} "
                f"| snapshots={len(self.book_history[ticker])}"
            )

        lines.append(
            f"Execution: orders={len(self.orders)} tenders={len(self.tenders)} "
            f"intents={len(self.active_intents())} bundles={len(self.bundles)} "
            f"hedge_remaining={self.hedge_remaining or '{}'}"
        )
        open_lots = [
            bundle for bundle in self.bundles.values()
            if bundle.status == "FILLED" and bundle.open_quantity > 0
        ]
        if open_lots:
            latest = open_lots[-1]
            convergence = (
                "-" if latest.convergence is None
                else f"{latest.convergence:.1%}"
            )
            round_trip = (
                "-" if latest.estimated_round_trip_cad is None
                else f"{latest.estimated_round_trip_cad:+.2f}CAD"
            )
            lines.append(
                f"Convergence: open_lots={len(open_lots)} "
                f"open_qty={sum(item.open_quantity for item in open_lots)} "
                f"latest={convergence} round_trip={round_trip}"
            )
        else:
            lines.append("Convergence: no open arbitrage lots")
        if detail == 1:
            return "\n".join(lines)

        lines.append("Current depth:")
        for ticker in MARKET_TICKERS:
            book = self.current_books.get(ticker)
            if book is None:
                continue
            bids = ", ".join(
                f"{level.price:.4f}x{level.quantity}" for level in book.bids
            ) or "-"
            asks = ", ".join(
                f"{level.price:.4f}x{level.quantity}" for level in book.asks
            ) or "-"
            lines.extend((f"  {ticker} bids: {bids}", f"  {ticker} asks: {asks}"))

        lines.append("Orders:")
        if self.orders:
            lines.extend(f"  {order}" for order in self.orders.values())
        else:
            lines.append("  none")

        lines.append("Tenders:")
        if self.tenders:
            lines.extend(f"  {tender}" for tender in self.tenders.values())
        else:
            lines.append("  none")

        lines.append("Intentions:")
        if self.intents:
            lines.extend(f"  {intent}" for intent in self.intents.values())
        else:
            lines.append("  none")

        lines.append("Trade bundles:")
        if self.bundles:
            lines.extend(f"  {bundle}" for bundle in self.bundles.values())
        else:
            lines.append("  none")

        lines.append("Recent edges:")
        if self.edge_history:
            for edge in list(self.edge_history)[-5:]:
                lines.append(
                    f"  t={edge.timestamp:.3f} "
                    f"net_buy_etf={edge.buy_etf_edge_cad:+.4f}CAD "
                    f"net_sell_etf={edge.sell_etf_edge_cad:+.4f}CAD"
                )
        else:
            lines.append("  none")
        return "\n".join(lines)

    def print_state(self, detail=1, file=None):
        """Print ``format_state`` output to stdout or another text stream."""
        print(self.format_state(detail), file=file)
