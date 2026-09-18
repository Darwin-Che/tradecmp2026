"""Convert active order intentions into bounded market orders."""

import sys

from execution import submit_market_order


def _acceptable_quantity(intent, book, ignore_limit=False):
    levels = book.asks if intent.action == "BUY" else book.bids
    if ignore_limit or intent.limit_price is None:
        return sum(level.quantity for level in levels)
    if intent.action == "BUY":
        return sum(
            level.quantity for level in levels
            if level.price <= intent.limit_price
        )
    return sum(
        level.quantity for level in levels
        if level.price >= intent.limit_price
    )


def _bundle_members(state, bundle):
    return [state.intents[item] for item in bundle.intent_ids]


def _bundle_started(members):
    return any(intent.filled_quantity > 0 for intent in members)


def _ready_to_start(state, members):
    """Require the full planned quantity of every leg before the first fill."""
    for intent in members:
        if not intent.is_active:
            continue
        book = state.current_books.get(intent.ticker)
        if book is None:
            return False
        if _acceptable_quantity(intent, book) < abs(intent.remaining):
            return False
    return True


def _prepare_bundles(state, current_tick):
    ready = set()
    for bundle in state.bundles.values():
        members = _bundle_members(state, bundle)
        if not members or all(item.status == "FILLED" for item in members):
            bundle.status = "FILLED" if members else "CANCELLED"
            continue
        if _bundle_started(members):
            bundle.status = "HEDGING"
            continue
        if any(item.is_expired(current_tick) for item in members):
            for intent in members:
                if intent.is_active:
                    intent.status = "CANCELLED"
            bundle.status = "CANCELLED"
            print(
                f"BUNDLE CANCEL | id={bundle.bundle_id} reason=deadline "
                "no legs filled"
            )
            continue
        if _ready_to_start(state, members):
            ready.add(bundle.bundle_id)
        bundle.status = "PLANNED"
    return ready


def _apply_inventory_closes(state):
    """Allocate completed closing bundles against oldest open entry lots."""
    for closing in state.bundles.values():
        if (
            closing.status != "FILLED"
            or not closing.closes_reason
            or closing.inventory_applied
        ):
            continue
        remaining = closing.quantity
        if closing.closes_bundle_id:
            target = state.bundles.get(closing.closes_bundle_id)
            openings = [target] if target is not None else []
        else:
            openings = state.bundles.values()
        for opening in openings:
            if remaining <= 0:
                break
            if opening.reason != closing.closes_reason or opening.status != "FILLED":
                continue
            closed = min(remaining, opening.open_quantity)
            opening.open_quantity -= closed
            remaining -= closed
        closing.inventory_applied = True
        print(
            f"BUNDLE INVENTORY | id={closing.bundle_id} "
            f"closed={closing.quantity - remaining}/{closing.quantity}"
        )


def fulfill_intents(client, state, max_order_size=10_000, fee_per_share=0.02):
    """Execute intentions while preserving bundle hedge obligations."""
    current_tick = state.case_tick if state.case_tick is not None else 0
    ready_bundles = _prepare_bundles(state, current_tick)
    blocked_bundles = set()
    submitted = False

    for intent in state.active_intents():
        bundle = state.bundles.get(intent.bundle_id)
        if bundle and bundle.bundle_id in blocked_bundles:
            continue
        members = _bundle_members(state, bundle) if bundle else []
        hedge_obligation = bool(bundle and _bundle_started(members))

        if bundle and not hedge_obligation and bundle.bundle_id not in ready_bundles:
            continue
        if not bundle and intent.is_expired(current_tick):
            intent.status = "EXPIRED"
            print(
                f"INTENT EXPIRED | id={intent.intent_id} "
                f"remaining={intent.remaining:+d}",
                file=sys.stderr,
            )
            continue

        book = state.current_books.get(intent.ticker)
        if book is None:
            continue
        available = _acceptable_quantity(
            intent, book, ignore_limit=hedge_obligation,
        )
        quantity = min(abs(intent.remaining), available, max_order_size)
        if quantity <= 0:
            continue

        response = submit_market_order(
            client,
            state,
            intent.ticker,
            intent.action,
            quantity,
            reason=intent.reason,
            context=f"intent={intent.intent_id} {intent.context}".strip(),
            fee_per_share=fee_per_share,
        )
        submitted = True
        if not response:
            if bundle and not _bundle_started(members):
                blocked_bundles.add(bundle.bundle_id)
            continue

        order_id = response.get("order_id")
        if order_id is not None:
            intent.order_ids.append(int(order_id))
        filled = min(quantity, int(response.get("quantity_filled", 0)))
        if filled:
            intent.record_fill(filled)
            position_delta = filled if intent.action == "BUY" else -filled
            state.positions[intent.ticker] += position_delta
        elif bundle and not _bundle_started(members):
            blocked_bundles.add(bundle.bundle_id)
        print(
            f"INTENT PROGRESS | id={intent.intent_id} status={intent.status} "
            f"filled={intent.filled_quantity}/{abs(intent.quantity)} "
            f"remaining={intent.remaining:+d}"
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
    return submitted
