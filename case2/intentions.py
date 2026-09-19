"""State models for asynchronous trade intentions and multi-leg bundles."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


ACTIVE_INTENT_STATUSES = ("ACTIVE", "WORKING")


@dataclass
class OrderIntent:
    """A desired position change that may be executed over several ticks.

    ``quantity`` and ``remaining`` are signed: positive means BUY and
    negative means SELL. ``limit_price`` is the worst acceptable execution
    price: the maximum for a buy or the minimum for a sell.
    """

    intent_id: str
    ticker: str
    quantity: int
    reason: str
    created_tick: int
    deadline_tick: int
    limit_price: Optional[float] = None
    urgency: float = 0.0
    priority: int = 0
    bundle_id: Optional[str] = None
    context: str = ""
    remaining: Optional[int] = None
    status: str = "ACTIVE"
    order_ids: List[int] = field(default_factory=list)
    live_order_id: Optional[int] = None
    live_order_tick: Optional[int] = None
    live_order_mode: Optional[str] = None

    def __post_init__(self):
        self.quantity = int(self.quantity)
        if self.quantity == 0:
            raise ValueError("intent quantity cannot be zero")
        if self.deadline_tick < self.created_tick:
            raise ValueError("deadline_tick cannot precede created_tick")
        if not 0.0 <= self.urgency <= 1.0:
            raise ValueError("urgency must be between 0 and 1")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.remaining is None:
            self.remaining = self.quantity
        self.remaining = int(self.remaining)
        if self.remaining and (self.remaining > 0) != (self.quantity > 0):
            raise ValueError("remaining must have the same side as quantity")
        if abs(self.remaining) > abs(self.quantity):
            raise ValueError("remaining cannot exceed quantity")

    @property
    def action(self):
        return "BUY" if self.quantity > 0 else "SELL"

    @property
    def filled_quantity(self):
        return abs(self.quantity) - abs(self.remaining)

    @property
    def is_active(self):
        return self.status in ACTIVE_INTENT_STATUSES

    def is_expired(self, current_tick):
        """Return true after the final tick on which execution is allowed."""
        return self.is_active and current_tick > self.deadline_tick

    def record_fill(self, quantity):
        """Apply an unsigned fill quantity to this intent."""
        quantity = int(quantity)
        if not self.is_active:
            raise ValueError("cannot fill an inactive intent")
        if quantity <= 0 or quantity > abs(self.remaining):
            raise ValueError("fill must be positive and no greater than remaining")
        direction = 1 if self.remaining > 0 else -1
        self.remaining -= direction * quantity
        self.status = "FILLED" if self.remaining == 0 else "WORKING"


@dataclass
class TradeBundle:
    """A group of intentions that must be risk-managed together."""

    bundle_id: str
    reason: str
    created_tick: int
    expected_profit_cad: float
    max_unhedged_ticks: int
    quantity: int = 0
    open_quantity: int = 0
    offset_quantity: int = 0
    closes_reason: Optional[str] = None
    closes_bundle_id: Optional[str] = None
    inventory_applied: bool = False
    gross_profit_cad: float = 0.0
    fees_cad: float = 0.0
    edge_per_share_cad: float = 0.0
    projected_gross: int = 0
    projected_net: int = 0
    entry_residual_cad: Optional[float] = None
    current_residual_cad: Optional[float] = None
    convergence: Optional[float] = None
    estimated_close_cad: Optional[float] = None
    estimated_close_quantity: int = 0
    estimated_round_trip_cad: Optional[float] = None
    intent_ids: List[str] = field(default_factory=list)
    status: str = "PLANNED"
    context: str = ""
    completion_logged: bool = False

    def __post_init__(self):
        if self.max_unhedged_ticks < 0:
            raise ValueError("max_unhedged_ticks cannot be negative")


def active_intents(intents: Dict[str, OrderIntent]):
    """Return active intentions in execution priority order."""
    return sorted(
        (intent for intent in intents.values() if intent.is_active),
        key=lambda intent: (-intent.priority, -intent.urgency, intent.created_tick),
    )
