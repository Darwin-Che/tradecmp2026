"""Centralized order submission, state recording, and execution logging."""

import sys

from state import OrderState


def submit_market_order(
    client,
    state,
    ticker,
    action,
    quantity,
    reason,
    context="",
    fee_per_share=0.02,
):
    """Submit one market order and log both its intent and API result."""
    quantity = int(quantity)
    context_text = f" {context}" if context else ""
    print(
        f"ORDER SUBMIT | reason={reason}{context_text} | "
        f"{ticker} {action} qty={quantity} type=MARKET"
    )

    response = client.place_order(
        ticker,
        action,
        quantity,
        order_type="MARKET",
    )
    if not response:
        print(
            f"ORDER RESULT | reason={reason}{context_text} | "
            f"{ticker} {action} qty={quantity} no API response",
            file=sys.stderr,
        )
        return response

    order_id = response.get("order_id")
    filled = int(response.get("quantity_filled", 0))
    status = response.get("status", "UNKNOWN")
    vwap = response.get("vwap")
    currency = "USD" if ticker == "RITC" else "CAD"
    commission = filled * fee_per_share
    vwap_text = "-" if vwap is None else f"{float(vwap):.4f}"

    if order_id is not None:
        state.orders[int(order_id)] = OrderState(
            order_id=int(order_id),
            ticker=ticker,
            action=action,
            quantity=quantity,
            order_type="MARKET",
            filled=filled,
            status=status,
            reason=reason,
            context=context,
        )

    print(
        f"ORDER FILL | order={order_id} reason={reason}{context_text} | "
        f"{ticker} {action} requested={quantity} filled={filled} "
        f"vwap={vwap_text} status={status} "
        f"commission={commission:.2f}{currency}"
    )
    return response


def submit_limit_order(
    client,
    state,
    ticker,
    action,
    quantity,
    price,
    reason,
    context="",
):
    """Submit a limit order; fills remain subject to later reconciliation."""
    quantity = int(quantity)
    print(
        f"ORDER SUBMIT | reason={reason} {context} | "
        f"{ticker} {action} qty={quantity} type=LIMIT price={price:.4f}"
    )
    response = client.place_order(
        ticker, action, quantity, order_type="LIMIT", price=price,
    )
    if not response:
        print(
            f"ORDER RESULT | reason={reason} {context} | no API response",
            file=sys.stderr,
        )
        return response
    order_id = response.get("order_id")
    if order_id is not None:
        state.orders[int(order_id)] = OrderState(
            order_id=int(order_id),
            ticker=ticker,
            action=action,
            quantity=quantity,
            order_type="LIMIT",
            price=price,
            filled=int(response.get("quantity_filled", 0)),
            status=response.get("status", "UNKNOWN"),
            reason=reason,
            context=context,
        )
    return response
