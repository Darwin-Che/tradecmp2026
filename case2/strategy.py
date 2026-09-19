"""ETF arbitrage and tender strategy decisions."""

from dataclasses import replace
from math import ceil
import sys

from api import ApiException
from arbitrage import best_arbitrage, evaluate_arbitrage
from convergence import find_convergence_exit, market_residual, update_convergence
from fulfillment import fulfill_intents
from intentions import OrderIntent, TradeBundle
from state import TenderState, TradingState
from tender_strategy import evaluate_fixed_tender
from valuation import liquidation_value_cad

STATE = TradingState(history_limit=300)
ACCEPTED_TENDER_IDS = set()


# Tickers
CAD  = "CAD"    # currency instrument quoted in CAD
USD  = "USD"    # price of 1 USD in CAD (i.e., USD/CAD)
BULL = "BULL"   # stock in CAD
BEAR = "BEAR"   # stock in CAD
RITC = "RITC"   # ETF quoted in USD

# Per problem statement
FEE_MKT = 0.02          # $/share for market orders
MAX_SIZE_EQUITY = 10000 # per order for BULL/BEAR/RITC

# Basic risk guardrails (adjust as needed)
MAX_LONG_NET  = 25000
MAX_SHORT_NET = -25000
MAX_GROSS     = 300000
ORDER_QTY     = 5000    # child order size for arb legs
RISK_LIMITS_LOADED = False

ARB_MIN_NET_EDGE_CAD = 0.05
ARB_MIN_PROFIT_CAD = 150.0
INVENTORY_MAX_CLOSE_COST_CAD = 0.08
CONVERGENCE_EXIT_THRESHOLD = 0.75
CONVERGENCE_MIN_ROUND_TRIP_CAD = 100.0
PNL_THRESHOLDS_CAD = (10_000.0, 30_000.0, 60_000.0)
PNL_GROSS_PROFILES = (
    (0.80, 0.65),
    (0.65, 0.50),
    (0.50, 0.30),
    (0.25, 0.10),
)
PNL_MAX_GIVEBACK_FRACTION = 0.20
TENDER_BUFFER_USD = 0.03
TENDER_MIN_PROFIT_CAD = 50.0
TENDER_MAX_BOOK_AGE = 1.5


def load_risk_limits(client):
    """Load the server's stock-category limits once for this process."""
    global MAX_GROSS, MAX_LONG_NET, MAX_SHORT_NET, RISK_LIMITS_LOADED
    if RISK_LIMITS_LOADED:
        return
    try:
        payload = client.get_limits()
        rows = payload if isinstance(payload, list) else payload.get("limits", ())
        stock = next(
            (row for row in rows if "STOCK" in str(row.get("name", "")).upper()),
            rows[0] if rows else None,
        )
        if stock:
            gross_limit = int(float(stock.get("gross_limit", MAX_GROSS)))
            net_limit = int(float(stock.get("net_limit", MAX_LONG_NET)))
            if gross_limit > 0:
                MAX_GROSS = gross_limit
            if net_limit > 0:
                MAX_LONG_NET = net_limit
                MAX_SHORT_NET = -net_limit
    except (ApiException, AttributeError, TypeError, ValueError) as exc:
        print(f"RISK LIMITS | using safe fallback: {exc}", file=sys.stderr)
    RISK_LIMITS_LOADED = True
    print(
        f"RISK LIMITS | gross={MAX_GROSS} "
        f"net=[{MAX_SHORT_NET},{MAX_LONG_NET}]"
    )

def get_tick_status(client):
    # Gets simulation status (active or stopped) for the tick
    j = client.get_case()
    if j is None:
        return None, None
    return j["tick"], j["status"]

def get_order_book(client, ticker):
    # Aggregate individual API orders into immutable price levels.
    data = client.get_order_book(ticker)
    if data is None:
        return STATE.record_book(ticker, (), ())

    def available_orders(orders):
        for order in orders:
            quantity = int(order.get("quantity", 0))
            filled = int(order.get("quantity_filled", 0))
            remaining = max(0, quantity - filled)
            if remaining:
                yield float(order["price"]), remaining

    return STATE.record_book(
        ticker,
        bids=available_orders(data.get("bids", ())),
        asks=available_orders(data.get("asks", ())),
    )

def positions_map(client):
    """Return current simulator positions, including currency cash balances."""
    data = client.get_securities()
    if data is None:
        return {k: 0 for k in (BULL, BEAR, RITC, USD, CAD)}
    out = {p["ticker"]: int(p.get("position", 0)) for p in data}
    for k in (BULL, BEAR, RITC, USD, CAD):
        out.setdefault(k, 0)
    return out

def accept_active_tender_offers(client):
    # Re-evaluate rejected tenders while they remain live.
    offers = client.get_tenders()
    if not offers:
        return False

    candidates = []
    for offer in offers:
        tender_id = offer["tender_id"]
        if tender_id in ACCEPTED_TENDER_IDS:
            continue
        evaluation = evaluate_fixed_tender(
            offer,
            STATE,
            market_fee_usd=FEE_MKT,
            stock_market_fee_cad=FEE_MKT,
            safety_buffer_per_share_usd=TENDER_BUFFER_USD,
            minimum_profit_cad=TENDER_MIN_PROFIT_CAD,
            max_book_age_seconds=TENDER_MAX_BOOK_AGE,
            max_gross=MAX_GROSS,
            min_net=MAX_SHORT_NET,
            max_net=MAX_LONG_NET,
        )
        print(evaluation.log_line())
        if evaluation.should_accept:
            candidates.append(evaluation)
    if not candidates:
        return False

    evaluation = max(candidates, key=lambda item: item.expected_profit_cad)
    tender_id = evaluation.tender_id

    try:
        response = client.accept_tender(tender_id, evaluation.tender_price)
    except ApiException as exc:
        print(f"Tender {tender_id} failed: {exc}", file=sys.stderr)
        return False

    accepted = bool(response and response.get("success", True))
    if not accepted:
        print(f"Tender {tender_id} was not accepted by the API", file=sys.stderr)
        return False

    ACCEPTED_TENDER_IDS.add(tender_id)

    tick = STATE.case_tick or 0
    bundle_id = f"tender-{tender_id}-exit"
    tender = TenderState(
        tender_id=tender_id,
        action=evaluation.action,
        price=evaluation.tender_price,
        quantity=evaluation.quantity,
        accepted=True,
        route=evaluation.route,
        direct_quantity=evaluation.direct_quantity,
        basket_quantity=evaluation.basket_quantity,
    )
    STATE.tenders[tender_id] = tender
    STATE.strategy_status = "UNWINDING_TENDER"

    tender_delta = (
        evaluation.quantity if evaluation.action == "BUY" else -evaluation.quantity
    )
    STATE.positions[RITC] += tender_delta
    bundle = STATE.add_bundle(TradeBundle(
        bundle_id=bundle_id,
        reason=f"TENDER_{evaluation.route}",
        created_tick=tick,
        expected_profit_cad=evaluation.expected_profit_cad,
        max_unhedged_ticks=2,
        quantity=evaluation.quantity,
        projected_gross=evaluation.projected_gross,
        projected_net=evaluation.projected_net,
        context=f"tender={tender_id} route={evaluation.route}",
    ))
    intent_ids = []
    for leg in evaluation.exit_legs:
        intent = STATE.add_intent(OrderIntent(
            intent_id=f"{bundle_id}-{leg.ticker}",
            ticker=leg.ticker,
            quantity=leg.signed_quantity,
            reason=f"TENDER_{leg.role}_EXIT",
            created_tick=tick,
            deadline_tick=tick + 300,
            limit_price=leg.limit_price,
            urgency=1.0,
            priority=100,
            bundle_id=bundle_id,
            context=bundle.context,
        ))
        bundle.intent_ids.append(intent.intent_id)
        intent_ids.append(intent.intent_id)
        print(
            f"INTENT ADD | id={intent.intent_id} reason={intent.reason} "
            f"{intent.ticker} {intent.action} qty={abs(intent.quantity)} "
            f"limit={intent.limit_price} deadline={intent.deadline_tick}"
        )
    tender.intent_ids = tuple(intent_ids)
    return True


def sync_tender_unwinds():
    """Reflect direct and basket hedge progress in the dashboard."""
    pending = {}
    for tender_id, tender in STATE.tenders.items():
        legs = [STATE.intents[item] for item in tender.intent_ids]
        if not legs:
            continue
        direct = next((leg for leg in legs if leg.ticker == RITC), None)
        bull = next((leg for leg in legs if leg.ticker == BULL), None)
        bear = next((leg for leg in legs if leg.ticker == BEAR), None)
        direct_done = direct.filled_quantity if direct else 0
        basket_done = min(
            bull.filled_quantity if bull else 0,
            bear.filled_quantity if bear else 0,
        )
        tender.quantity_unwound = direct_done + basket_done
        for leg in legs:
            if leg.remaining:
                pending[leg.ticker] = pending.get(leg.ticker, 0) + leg.remaining
    STATE.hedge_remaining = {
        ticker: quantity for ticker, quantity in pending.items() if quantity
    }
    if not pending and STATE.strategy_status == "UNWINDING_TENDER":
        STATE.strategy_status = "IDLE"


def equity_gross(positions):
    return (
        abs(positions.get(BULL, 0))
        + abs(positions.get(BEAR, 0))
        + 2 * abs(positions.get(RITC, 0))
    )


def pnl_risk_profile():
    """Return gross start/target fractions and drawdown state."""
    pnl = STATE.pnl_cad
    profile_index = sum(pnl >= threshold for threshold in PNL_THRESHOLDS_CAD)
    drawdown_active = (
        STATE.pnl_high_water_cad >= PNL_THRESHOLDS_CAD[0]
        and pnl <= STATE.pnl_high_water_cad * (1 - PNL_MAX_GIVEBACK_FRACTION)
    )
    if drawdown_active:
        profile_index = len(PNL_GROSS_PROFILES) - 1
    start, target = PNL_GROSS_PROFILES[profile_index]
    return start, target, drawdown_active


def inventory_reduction_plan():
    """Plan a controlled reverse bundle when inventory consumes capacity."""
    gross = equity_gross(STATE.positions)
    start_fraction, target_fraction, _ = pnl_risk_profile()
    target_gross = int(MAX_GROSS * target_fraction)
    reducing = STATE.strategy_status == "REDUCING_INVENTORY"
    if reducing and gross <= target_gross:
        STATE.strategy_status = "IDLE"
        return None
    if not reducing and gross < MAX_GROSS * start_fraction:
        return None

    bull = STATE.positions.get(BULL, 0)
    bear = STATE.positions.get(BEAR, 0)
    ritc = STATE.positions.get(RITC, 0)
    if bull > 0 and bear > 0 and ritc < 0:
        direction = "BUY_ETF"
        balanced_quantity = min(bull, bear, -ritc)
    elif bull < 0 and bear < 0 and ritc > 0:
        direction = "SELL_ETF"
        balanced_quantity = min(-bull, -bear, ritc)
    else:
        return None

    quantity_to_target = max(1, ceil((gross - target_gross) / 4))
    max_quantity = min(ORDER_QTY, balanced_quantity, quantity_to_target)
    if max_quantity <= 0:
        return None

    plan = evaluate_arbitrage(
        STATE,
        direction,
        max_quantity=max_quantity,
        market_fee=FEE_MKT,
        minimum_net_edge_cad=-INVENTORY_MAX_CLOSE_COST_CAD,
        minimum_profit_cad=float("-inf"),
        max_gross=MAX_GROSS,
        min_net=MAX_SHORT_NET,
        max_net=MAX_LONG_NET,
        prefer_larger=True,
    )
    if plan is None:
        return None
    STATE.strategy_status = "REDUCING_INVENTORY"
    return replace(plan, reason=f"INVENTORY_REDUCE_{direction}")


def enqueue_arb(plan, closes_bundle_id=None):
    """Create intentions from a depth, cost, and risk checked plan."""
    tick = STATE.case_tick or 0
    bundle_id = f"arb-{tick}-{len(STATE.bundles) + 1}"
    is_opening = plan.reason.startswith("ETF_ARB_")
    closes_reason = None
    source = STATE.bundles.get(closes_bundle_id)
    if source is not None:
        closes_reason = source.reason
    elif plan.reason.endswith("BUY_ETF") and not is_opening:
        closes_reason = "ETF_ARB_SELL_ETF"
    elif plan.reason.endswith("SELL_ETF") and not is_opening:
        closes_reason = "ETF_ARB_BUY_ETF"
    bundle = STATE.add_bundle(TradeBundle(
        bundle_id=bundle_id,
        reason=plan.reason,
        created_tick=tick,
        expected_profit_cad=plan.expected_profit_cad,
        max_unhedged_ticks=2,
        quantity=plan.quantity,
        open_quantity=plan.quantity if is_opening else 0,
        closes_reason=closes_reason,
        closes_bundle_id=closes_bundle_id,
        gross_profit_cad=plan.gross_profit_cad,
        fees_cad=plan.fees_cad,
        edge_per_share_cad=plan.edge_per_share_cad,
        projected_gross=plan.projected_gross,
        projected_net=plan.projected_net,
        entry_residual_cad=(
            market_residual(STATE)
            if is_opening else None
        ),
        context=(
            f"tick={tick} net_edge={plan.edge_per_share_cad:.4f}CAD "
            f"planned_qty={plan.quantity}"
        ),
    ))
    for leg in plan.legs:
        intent = STATE.add_intent(OrderIntent(
            intent_id=f"{bundle_id}-{leg.ticker}",
            ticker=leg.ticker,
            quantity=leg.signed_quantity,
            reason=plan.reason,
            created_tick=tick,
            deadline_tick=tick + bundle.max_unhedged_ticks,
            limit_price=leg.limit_price,
            urgency=1.0,
            priority=50,
            bundle_id=bundle_id,
            context=bundle.context,
        ))
        bundle.intent_ids.append(intent.intent_id)
    print(
        f"BUNDLE ADD | id={bundle_id} reason={plan.reason} qty={plan.quantity} "
        f"gross={plan.gross_profit_cad:+.2f}CAD fees={plan.fees_cad:.2f}CAD "
        f"net={plan.expected_profit_cad:+.2f}CAD "
        f"edge={plan.edge_per_share_cad:+.4f}CAD "
        f"risk_gross={plan.projected_gross} risk_net={plan.projected_net}"
    )
    return bundle

# --------- CORE LOGIC ----------
def step_once(client):
    load_risk_limits(client)
    # Get executable prices
    bull_book = get_order_book(client, BULL)
    bear_book = get_order_book(client, BEAR)
    ritc_book = get_order_book(client, RITC)
    usd_book = get_order_book(client, USD)   # USD quoted in CAD (USD/CAD)

    bull_bid, bull_ask = bull_book.best_bid, bull_book.best_ask
    bear_bid, bear_ask = bear_book.best_bid, bear_book.best_ask
    ritc_bid_usd, ritc_ask_usd = ritc_book.best_bid, ritc_book.best_ask
    usd_bid, usd_ask = usd_book.best_bid, usd_book.best_ask

    pos = positions_map(client)
    STATE.update_positions(pos)
    STATE.update_portfolio_value(
        liquidation_value_cad(STATE.positions, STATE.current_books)
    )
    risk_start, risk_target, drawdown = pnl_risk_profile()
    previous_profile = (
        STATE.gross_start_fraction,
        STATE.gross_target_fraction,
        STATE.pnl_drawdown_active,
    )
    STATE.update_risk_profile(risk_start, risk_target, drawdown)
    if previous_profile != (risk_start, risk_target, drawdown):
        print(
            f"RISK PROFILE | pnl={STATE.pnl_cad:+.2f}CAD "
            f"high_water={STATE.pnl_high_water_cad:+.2f}CAD "
            f"gross={risk_start:.0%}->{risk_target:.0%} "
            f"drawdown={drawdown}"
        )
    unfinished_bundle = any(
        bundle.status in ("HEDGING", "INCOMPLETE")
        for bundle in STATE.bundles.values()
    )
    busy = bool(STATE.active_intents()) or unfinished_bundle
    quotes = (
        bull_bid, bull_ask, bear_bid, bear_ask,
        ritc_bid_usd, ritc_ask_usd, usd_bid, usd_ask,
    )
    if any(value is None for value in quotes):
        submitted = fulfill_intents(
            client, STATE, max_order_size=MAX_SIZE_EQUITY,
            fee_per_share=FEE_MKT,
        )
        sync_tender_unwinds()
        return busy or submitted, None, None, {
            "bull_bid": bull_bid, "bull_ask": bull_ask,
            "bear_bid": bear_bid, "bear_ask": bear_ask,
            "ritc_bid_usd": ritc_bid_usd, "ritc_ask_usd": ritc_ask_usd,
            "usd_bid": usd_bid, "usd_ask": usd_ask,
            "ritc_bid_cad": None, "ritc_ask_cad": None,
        }

    # Convert RITC to CAD using USD book
    ritc_bid_cad = ritc_bid_usd * usd_bid
    ritc_ask_cad = ritc_ask_usd * usd_ask

    # Basket executable values in CAD
    basket_sell_value = bull_bid + bear_bid      # what we get if we SELL basket now
    basket_buy_cost   = bull_ask + bear_ask      # what we pay if we BUY basket now

    # Direction 1: Basket rich vs ETF
    # SELL basket (hit bids), BUY RITC in USD (lift ask) -> compare in CAD
    gross_buy_etf_edge = basket_sell_value - ritc_ask_cad

    # Direction 2: ETF rich vs Basket
    # SELL RITC (hit bid in USD), BUY basket (lift asks) -> compare in CAD
    gross_sell_etf_edge = ritc_bid_cad - basket_buy_cost
    fee_per_share_cad = FEE_MKT * (2 + usd_ask)
    buy_etf_edge = gross_buy_etf_edge - fee_per_share_cad
    sell_etf_edge = gross_sell_etf_edge - fee_per_share_cad
    STATE.record_edges(buy_etf_edge, sell_etf_edge)
    update_convergence(STATE, market_fee=FEE_MKT)

    created = False
    if not busy:
        created = accept_active_tender_offers(client)

    if not busy and not created:
        gross = equity_gross(STATE.positions)
        closes_bundle_id = None
        exit_choice = find_convergence_exit(
            STATE,
            minimum_convergence=CONVERGENCE_EXIT_THRESHOLD,
            minimum_round_trip_cad=CONVERGENCE_MIN_ROUND_TRIP_CAD,
            market_fee=FEE_MKT,
            max_gross=MAX_GROSS,
            min_net=MAX_SHORT_NET,
            max_net=MAX_LONG_NET,
        )
        plan = None
        if exit_choice is not None:
            opening, plan, round_trip = exit_choice
            closes_bundle_id = opening.bundle_id
            print(
                f"CONVERGENCE EXIT | source={opening.bundle_id} "
                f"convergence={opening.convergence:.1%} "
                f"qty={plan.quantity} round_trip={round_trip:+.2f}CAD"
            )
        if plan is None:
            plan = inventory_reduction_plan()
        reducing = STATE.strategy_status == "REDUCING_INVENTORY"
        if plan is None:
            plan = best_arbitrage(
                STATE,
                max_quantity=min(ORDER_QTY, MAX_SIZE_EQUITY),
                market_fee=FEE_MKT,
                minimum_net_edge_cad=ARB_MIN_NET_EDGE_CAD,
                minimum_profit_cad=ARB_MIN_PROFIT_CAD,
                max_gross=MAX_GROSS,
                min_net=MAX_SHORT_NET,
                max_net=MAX_LONG_NET,
            )
            inventory_guard = (
                reducing or gross >= MAX_GROSS * pnl_risk_profile()[0]
            )
            if plan and inventory_guard and plan.projected_gross >= gross:
                plan = None
        if plan is not None:
            enqueue_arb(plan, closes_bundle_id=closes_bundle_id)
            created = True

    submitted = fulfill_intents(
        client, STATE, max_order_size=MAX_SIZE_EQUITY,
        fee_per_share=FEE_MKT,
    )
    sync_tender_unwinds()

    return busy or created or submitted, buy_etf_edge, sell_etf_edge, {
        "bull_bid": bull_bid, "bull_ask": bull_ask,
        "bear_bid": bear_bid, "bear_ask": bear_ask,
        "ritc_bid_usd": ritc_bid_usd, "ritc_ask_usd": ritc_ask_usd,
        "usd_bid": usd_bid, "usd_ask": usd_ask,
        "ritc_bid_cad": ritc_bid_cad, "ritc_ask_cad": ritc_ask_cad
    }
