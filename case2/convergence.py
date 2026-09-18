"""Convergence and estimated round-trip metrics for open arbitrage lots."""

from dataclasses import replace

from arbitrage import evaluate_arbitrage


OPENING_REASONS = ("ETF_ARB_BUY_ETF", "ETF_ARB_SELL_ETF")


def market_residual(state):
    """Return midpoint ETF value minus midpoint basket value in CAD."""
    books = [state.current_books.get(item) for item in ("BULL", "BEAR", "RITC", "USD")]
    if not all(books):
        return None
    mids = []
    for book in books:
        if book.best_bid is None or book.best_ask is None:
            return None
        mids.append((book.best_bid + book.best_ask) / 2)
    bull_mid, bear_mid, ritc_mid, usd_mid = mids
    return ritc_mid * usd_mid - bull_mid - bear_mid


def update_convergence(state, market_fee=0.02):
    """Update every open lot without making an exit decision."""
    residual = market_residual(state)
    if residual is None:
        return []

    updated = []
    close_cache = {}
    for bundle in state.bundles.values():
        if (
            bundle.reason not in OPENING_REASONS
            or bundle.status != "FILLED"
            or bundle.open_quantity <= 0
        ):
            continue
        bundle.current_residual_cad = residual
        if bundle.entry_residual_cad not in (None, 0):
            bundle.convergence = 1 - residual / bundle.entry_residual_cad

        direction = (
            "SELL_ETF" if bundle.reason == "ETF_ARB_BUY_ETF" else "BUY_ETF"
        )
        cache_key = (direction, bundle.open_quantity)
        if cache_key not in close_cache:
            close_cache[cache_key] = evaluate_arbitrage(
                state,
                direction,
                max_quantity=bundle.open_quantity,
                market_fee=market_fee,
                minimum_net_edge_cad=float("-inf"),
                minimum_profit_cad=float("-inf"),
                max_gross=10**18,
                min_net=-(10**18),
                max_net=10**18,
                prefer_larger=True,
            )
        close = close_cache[cache_key]
        if close is None:
            bundle.estimated_close_cad = None
            bundle.estimated_close_quantity = 0
            bundle.estimated_round_trip_cad = None
        else:
            entry_share = bundle.expected_profit_cad / bundle.quantity
            bundle.estimated_close_cad = close.expected_profit_cad
            bundle.estimated_close_quantity = close.quantity
            bundle.estimated_round_trip_cad = (
                entry_share * close.quantity + close.expected_profit_cad
            )
        updated.append(bundle)
    return updated


def find_convergence_exit(
    state,
    minimum_convergence=0.75,
    minimum_round_trip_cad=0.0,
    market_fee=0.02,
    max_gross=300_000,
    min_net=-25_000,
    max_net=25_000,
):
    """Return an executable close plan and its exact opening lot."""
    for opening in state.bundles.values():
        if (
            opening.reason not in OPENING_REASONS
            or opening.status != "FILLED"
            or opening.open_quantity <= 0
            or opening.convergence is None
            or opening.convergence < minimum_convergence
        ):
            continue
        direction = (
            "SELL_ETF" if opening.reason == "ETF_ARB_BUY_ETF" else "BUY_ETF"
        )
        plan = evaluate_arbitrage(
            state,
            direction,
            max_quantity=opening.open_quantity,
            market_fee=market_fee,
            minimum_net_edge_cad=float("-inf"),
            minimum_profit_cad=float("-inf"),
            max_gross=max_gross,
            min_net=min_net,
            max_net=max_net,
            prefer_larger=True,
        )
        if plan is None:
            continue
        entry_profit = (
            opening.expected_profit_cad
            * plan.quantity / opening.quantity
        )
        round_trip = entry_profit + plan.expected_profit_cad
        if round_trip < minimum_round_trip_cad:
            continue
        reason = f"CONVERGENCE_EXIT_{direction}"
        return opening, replace(plan, reason=reason), round_trip
    return None
