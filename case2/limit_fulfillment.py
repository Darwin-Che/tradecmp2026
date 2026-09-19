"""Passive-first execution with reconciled, marketable-limit hedging."""

from api import ApiException
from completion_log import log_completed_bundles
from event_log import order_detail
from execution import submit_limit_order
from fulfillment import (
    _acceptable_quantity,
    _apply_inventory_closes,
    _bundle_members,
    _bundle_started,
    _prepare_bundles,
)
from state import estimate_fill


TERMINAL_ORDER_STATUSES = {"TRANSACTED", "FILLED", "CANCELLED", "REJECTED", "EXPIRED"}


def _record_fill(state, intent, quantity):
    if quantity <= 0:
        return
    intent.record_fill(quantity)
    state.positions[intent.ticker] += quantity if intent.action == "BUY" else -quantity
    order_detail(
        f"INTENT PROGRESS | id={intent.intent_id} "
        f"filled={intent.filled_quantity}/{abs(intent.quantity)} "
        f"remaining={intent.remaining:+d}"
    )


def _reconcile(client, state, intent):
    """Apply only newly confirmed fills; retain uncertain orders as live."""
    order_id = intent.live_order_id
    if order_id is None:
        return True
    snapshot = client.get_order(order_id)
    if not isinstance(snapshot, dict):
        return False
    order = state.orders[order_id]
    filled = int(snapshot.get("quantity_filled", order.filled))
    if filled < order.filled or filled > order.quantity:
        raise ValueError(f"invalid fill quantity for order {order_id}: {filled}")
    delta = filled - order.filled
    if delta:
        _record_fill(state, intent, delta)
        if filled < order.quantity:
            print(
                f"ORDER PARTIAL | order={order_id} intent={intent.intent_id} "
                f"filled={filled}/{order.quantity}"
            )
    order.filled = filled
    if snapshot.get("vwap") is not None:
        order.vwap = float(snapshot["vwap"])
    order.status = str(snapshot.get("status", order.status)).upper()
    if order.status in TERMINAL_ORDER_STATUSES or filled == order.quantity:
        intent.live_order_id = None
        intent.live_order_tick = None
        intent.live_order_mode = None
        return True
    return False


def _cancel_and_reconcile(client, state, intent):
    """Never place a replacement until cancellation and final fills are known."""
    if intent.live_order_id is None:
        return True
    result = client.cancel_order(intent.live_order_id)
    if not result or not result.get("success", False):
        _reconcile(client, state, intent)
        return intent.live_order_id is None
    return _reconcile(client, state, intent)


def _post_limit(client, state, intent, quantity, price, mode, current_tick):
    try:
        response = submit_limit_order(
            client,
            state,
            intent.ticker,
            intent.action,
            quantity,
            price,
            reason=intent.reason,
            context=f"intent={intent.intent_id} {intent.context}".strip(),
        )
    except ApiException as exc:
        # A failed POST can still have reached the server. Stop rather than
        # automatically submitting a duplicate quantity next cycle.
        raise RuntimeError(
            f"limit order submission outcome is uncertain: {intent.intent_id}"
        ) from exc
    if not response:
        raise ValueError(
            f"limit order response is absent; submission outcome is unknown: "
            f"{intent.intent_id}"
        )
    order_id = response.get("order_id")
    if order_id is None:
        # The outcome of the POST is ambiguous. Do not submit a replacement.
        raise ValueError(f"limit order response has no order_id: {intent.intent_id}")
    order_id = int(order_id)
    intent.order_ids.append(order_id)
    order = state.orders[order_id]
    immediate_fill = order.filled
    if immediate_fill:
        _record_fill(state, intent, immediate_fill)
        if immediate_fill < order.quantity:
            print(
                f"ORDER PARTIAL | order={order_id} intent={intent.intent_id} "
                f"filled={immediate_fill}/{order.quantity}"
            )
    order.status = str(order.status).upper()
    if order.status not in TERMINAL_ORDER_STATUSES:
        intent.live_order_id = order_id
        intent.live_order_tick = current_tick
        intent.live_order_mode = mode
    return True


def _passive_price(intent, book):
    if book.best_bid is None or book.best_ask is None:
        return None
    if book.best_bid >= book.best_ask:
        return None
    price = book.best_bid if intent.action == "BUY" else book.best_ask
    if intent.limit_price is not None:
        if intent.action == "BUY" and price > intent.limit_price:
            return None
        if intent.action == "SELL" and price < intent.limit_price:
            return None
    return price


def _marketable_price(intent, book, quantity):
    fill = estimate_fill(book, intent.action, quantity)
    return fill.worst_price if fill.complete else None


def cancel_live_orders(client, state):
    """Cancel this strategy's working limit orders on shutdown."""
    for intent in state.intents.values():
        if intent.live_order_id is not None:
            _cancel_and_reconcile(client, state, intent)


def fulfill_limit_intents(
    client,
    state,
    max_order_size=10_000,
    passive_wait_ticks=1,
):
    """Work one passive arb leg, then use marketable limits to finish."""
    current_tick = state.case_tick if state.case_tick is not None else 0
    submitted = False

    # Reconcile every outstanding order before cancellation, expiration, or
    # replacement. This also discovers partial fills between polling cycles.
    for intent in state.intents.values():
        if intent.live_order_id is None:
            continue
        _reconcile(client, state, intent)
        if intent.live_order_id is None:
            continue
        bundle = state.bundles.get(intent.bundle_id)
        members = _bundle_members(state, bundle) if bundle else []
        hedge_started = bool(bundle and _bundle_started(members))
        placed_tick = (
            current_tick if intent.live_order_tick is None
            else intent.live_order_tick
        )
        age = current_tick - placed_tick
        should_cancel = (
            hedge_started
            or intent.is_expired(current_tick)
            or age >= passive_wait_ticks
            or intent.live_order_mode == "AGGRESSIVE"
        )
        if should_cancel:
            _cancel_and_reconcile(client, state, intent)

    ready_bundles = _prepare_bundles(state, current_tick)
    blocked_bundles = set()
    for intent in state.active_intents():
        if intent.live_order_id is not None:
            continue
        bundle = state.bundles.get(intent.bundle_id)
        if bundle and bundle.bundle_id in blocked_bundles:
            continue
        members = _bundle_members(state, bundle) if bundle else []
        hedge_started = bool(bundle and _bundle_started(members))
        if bundle and not hedge_started and bundle.bundle_id not in ready_bundles:
            continue
        if not bundle and intent.is_expired(current_tick):
            intent.status = "EXPIRED"
            continue
        book = state.current_books.get(intent.ticker)
        if book is None:
            continue

        # A tender has already created exposure. An arb bundle gets one passive
        # attempt before any fill; its other legs wait until that attempt ends.
        fresh_arb = bool(bundle and bundle.reason.startswith("ETF_ARB_")
                         and not hedge_started)
        if fresh_arb and not any(member.order_ids for member in members):
            if intent is not members[0]:
                continue
            price = _passive_price(intent, book)
            if price is not None:
                quantity = min(abs(intent.remaining), max_order_size)
                submitted |= _post_limit(
                    client, state, intent, quantity, price, "PASSIVE", current_tick,
                )
                blocked_bundles.add(bundle.bundle_id)
                continue
        if fresh_arb and any(member.live_order_id for member in members):
            continue

        available = _acceptable_quantity(intent, book, ignore_limit=hedge_started)
        quantity = min(abs(intent.remaining), available, max_order_size)
        if quantity <= 0:
            continue
        price = _marketable_price(intent, book, quantity)
        if price is None:
            continue
        submitted |= _post_limit(
            client, state, intent, quantity, price, "AGGRESSIVE", current_tick,
        )

    for bundle in state.bundles.values():
        members = _bundle_members(state, bundle)
        if members and all(item.status == "FILLED" for item in members):
            bundle.status = "FILLED"
        elif _bundle_started(members) and any(item.is_active for item in members):
            bundle.status = "HEDGING"
        elif any(item.is_active for item in members):
            bundle.status = "PLANNED"
        elif members and bundle.status != "CANCELLED":
            bundle.status = "INCOMPLETE"
    _apply_inventory_closes(state)
    log_completed_bundles(state)
    return submitted
