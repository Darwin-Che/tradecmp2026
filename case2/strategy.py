"""ETF arbitrage and tender strategy decisions."""

from dataclasses import replace
from math import ceil
import os
import sys
from time import monotonic

from api import ApiException
from arbitrage import best_arbitrage, evaluate_arbitrage
from convergence import find_convergence_exit, market_residual, update_convergence
from fulfillment import fulfill_intents
from intentions import OrderIntent, TradeBundle
from manual_converter import (
    BLOCK_SIZE, apply_conversion_to_open_lots, converted_blocks,
    equity_signature, market_close_plan, propose_conversion, start_window,
)
from state import TenderState, TradingState
from tender_strategy import evaluate_fixed_tender
from valuation import liquidation_components_cad, liquidation_value_cad

STATE = TradingState(history_limit=300)
ACCEPTED_TENDER_IDS = set()
LOGGED_TENDER_REJECTIONS = set()
CONVERTER_WINDOW = None
CONVERTER_SUPPRESSED_POSITIONS = None


def reset_for_new_heat():
    """Discard per-heat positions, orders, and tender IDs before trading again."""
    global STATE, RISK_LIMITS_LOADED, MAX_GROSS, MAX_LONG_NET, MAX_SHORT_NET
    global CONVERTER_WINDOW, CONVERTER_SUPPRESSED_POSITIONS
    STATE = TradingState(history_limit=STATE.history_limit)
    ACCEPTED_TENDER_IDS.clear()
    LOGGED_TENDER_REJECTIONS.clear()
    CONVERTER_WINDOW = None
    CONVERTER_SUPPRESSED_POSITIONS = None
    RISK_LIMITS_LOADED = False
    MAX_GROSS = DEFAULT_MAX_GROSS
    MAX_LONG_NET = DEFAULT_MAX_LONG_NET
    MAX_SHORT_NET = DEFAULT_MAX_SHORT_NET


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
DEFAULT_MAX_LONG_NET = 25000
DEFAULT_MAX_SHORT_NET = -25000
DEFAULT_MAX_GROSS = 300000
MAX_LONG_NET  = DEFAULT_MAX_LONG_NET
MAX_SHORT_NET = DEFAULT_MAX_SHORT_NET
MAX_GROSS     = DEFAULT_MAX_GROSS
ORDER_QTY     = 5000    # child order size for arb legs
RISK_LIMITS_LOADED = False

# Tick-based intent settings. The intent deadline controls how long an
# unstarted arbitrage bundle waits; max_unhedged_ticks is recorded separately.
ARB_INTENT_DEADLINE_TICKS = int(os.getenv("RIT_ARB_INTENT_DEADLINE_TICKS", "2"))
MAX_UNHEDGED_TICKS = int(os.getenv("RIT_MAX_UNHEDGED_TICKS", "2"))
if ARB_INTENT_DEADLINE_TICKS < 0 or MAX_UNHEDGED_TICKS < 0:
    raise ValueError("intent deadline and max unhedged ticks must be non-negative")
EXECUTION_POLICY = os.getenv("RIT_EXECUTION_POLICY", "market").lower()
if EXECUTION_POLICY not in ("market", "adaptive_limit"):
    raise ValueError("RIT_EXECUTION_POLICY must be market or adaptive_limit")
PASSIVE_WAIT_TICKS = int(os.getenv("RIT_PASSIVE_WAIT_TICKS", "1"))
if PASSIVE_WAIT_TICKS < 0:
    raise ValueError("RIT_PASSIVE_WAIT_TICKS must be non-negative")

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
PNL_RECOVERY_GIVEBACK_FRACTION = 0.10
PNL_DRAWDOWN_CONFIRM_TICKS = 2
PNL_DRAWDOWN_RECOVERY_TICKS = int(
    os.getenv("RIT_DRAWDOWN_RECOVERY_TICKS", "5")
)
PNL_DRAWDOWN_MIN_HOLD_TICKS = int(
    os.getenv("RIT_DRAWDOWN_MIN_HOLD_TICKS", "20")
)
if PNL_DRAWDOWN_RECOVERY_TICKS < 1 or PNL_DRAWDOWN_MIN_HOLD_TICKS < 0:
    raise ValueError("drawdown recovery ticks must be positive and hold ticks non-negative")
TENDER_MAX_CARRY_GROSS_FRACTION = float(
    os.getenv("RIT_TENDER_MAX_CARRY_GROSS_FRACTION", "0.90")
)
TENDER_LATE_TICK = int(os.getenv("RIT_TENDER_LATE_TICK", "200"))
TENDER_LATE_MAX_CARRY_GROSS_FRACTION = float(
    os.getenv("RIT_TENDER_LATE_MAX_CARRY_GROSS_FRACTION", "0.60")
)
ENDGAME_HOLD_TICK = int(os.getenv("RIT_ENDGAME_HOLD_TICK", "270"))
LOSS_GROWTH_BLOCK_CAD = float(
    os.getenv("RIT_LOSS_GROWTH_BLOCK_CAD", "5000")
)
TENDER_MIN_BASKET_EDGE_CAD = float(
    os.getenv("RIT_TENDER_MIN_BASKET_EDGE_CAD", "0.03")
)
if not (0 < TENDER_LATE_MAX_CARRY_GROSS_FRACTION
        <= TENDER_MAX_CARRY_GROSS_FRACTION <= 1):
    raise ValueError(
        "tender carry gross fractions must satisfy 0 < late <= normal <= 1"
    )
if TENDER_LATE_TICK < 0 or ENDGAME_HOLD_TICK < 0:
    raise ValueError("late and endgame ticks must be non-negative")
if LOSS_GROWTH_BLOCK_CAD < 0 or TENDER_MIN_BASKET_EDGE_CAD < 0:
    raise ValueError("loss guard and tender basket edge must be non-negative")
TENDER_BUFFER_USD = 0.03
TENDER_MIN_PROFIT_CAD = 50.0
TENDER_MAX_BOOK_AGE = 1.5
MANUAL_CONVERTER_ENABLED = os.getenv(
    "RIT_MANUAL_CONVERTER", "0"
).lower() in ("1", "true", "yes")
MANUAL_CONVERTER_WAIT_SECONDS = float(os.getenv(
    "RIT_CONVERTER_WAIT_SECONDS", "5"
))
MANUAL_CONVERTER_MAX_BLOCKS = int(os.getenv(
    "RIT_CONVERTER_MAX_BLOCKS", "1"
))
MANUAL_CONVERTER_DELAY_BUFFER_CAD = float(os.getenv(
    "RIT_CONVERTER_DELAY_BUFFER_CAD_PER_SHARE", "0.03"
))
MANUAL_CONVERTER_MIN_ADVANTAGE_CAD = float(os.getenv(
    "RIT_CONVERTER_MIN_ADVANTAGE_CAD", "100"
))
MANUAL_CONVERTER_MAX_FALLBACK_LOSS_CAD = float(os.getenv(
    "RIT_CONVERTER_MAX_FALLBACK_LOSS_CAD_PER_BLOCK", "2500"
))
if (MANUAL_CONVERTER_WAIT_SECONDS <= 0
        or MANUAL_CONVERTER_MAX_BLOCKS < 1
        or MANUAL_CONVERTER_DELAY_BUFFER_CAD < 0
        or MANUAL_CONVERTER_MIN_ADVANTAGE_CAD < 0
        or MANUAL_CONVERTER_MAX_FALLBACK_LOSS_CAD < 0):
    raise ValueError("manual converter settings must be positive or non-negative")


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
    if MANUAL_CONVERTER_ENABLED:
        print(
            "MANUAL CONVERTER | armed for human use only "
            f"wait={MANUAL_CONVERTER_WAIT_SECONDS:.1f}s "
            f"max_blocks={MANUAL_CONVERTER_MAX_BLOCKS}"
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
        return None
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
    carry_fraction = (
        TENDER_LATE_MAX_CARRY_GROSS_FRACTION
        if STATE.case_tick is not None and STATE.case_tick >= TENDER_LATE_TICK
        else TENDER_MAX_CARRY_GROSS_FRACTION
    )
    carry_cap = max(
        equity_gross(STATE.positions), int(MAX_GROSS * carry_fraction)
    )
    if STATE.loss_growth_guard_active:
        carry_cap = equity_gross(STATE.positions)
    if STATE.pnl_drawdown_active:
        current_gross = equity_gross(STATE.positions)
        drawdown_cap = int(MAX_GROSS * STATE.gross_target_fraction)
        carry_cap = min(carry_cap, max(current_gross, drawdown_cap))
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
            max_residual_gross=carry_cap,
            minimum_basket_edge_cad_per_share=TENDER_MIN_BASKET_EDGE_CAD,
        )
        decision_line = evaluation.log_line()
        if evaluation.should_accept:
            print(decision_line)
        elif (os.getenv("RIT_VERBOSE_REJECTIONS", "0").lower()
              in ("1", "true", "yes")
              or decision_line not in LOGGED_TENDER_REJECTIONS):
            print(decision_line)
            LOGGED_TENDER_REJECTIONS.add(decision_line)
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
    tender_direction = "BUY_ETF" if evaluation.action == "BUY" else "SELL_ETF"
    tender_offset = min(
        evaluation.basket_quantity,
        opposing_inventory_quantity(STATE.positions, tender_direction),
    )
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
        max_unhedged_ticks=MAX_UNHEDGED_TICKS,
        quantity=evaluation.quantity,
        offset_quantity=tender_offset,
        closes_reason=(
            "ETF_ARB_SELL_ETF" if tender_direction == "BUY_ETF"
            else "ETF_ARB_BUY_ETF"
        ) if tender_offset else None,
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


def opposing_inventory_quantity(positions, direction):
    """Balanced shares that a three-leg trade would reduce, not open."""
    bull = positions.get(BULL, 0)
    bear = positions.get(BEAR, 0)
    ritc = positions.get(RITC, 0)
    if direction == "SELL_ETF":
        return max(0, min(-bull, -bear, ritc))
    if direction == "BUY_ETF":
        return max(0, min(bull, bear, -ritc))
    raise ValueError("direction must be BUY_ETF or SELL_ETF")


def pnl_risk_profile():
    """Return gross start/target fractions and drawdown state."""
    pnl = STATE.risk_pnl_cad if STATE.risk_pnl_cad is not None else STATE.pnl_cad
    profile_index = sum(pnl >= threshold for threshold in PNL_THRESHOLDS_CAD)
    drawdown_active = STATE.pnl_drawdown_active
    if drawdown_active:
        profile_index = len(PNL_GROSS_PROFILES) - 1
    start, target = PNL_GROSS_PROFILES[profile_index]
    return start, target, drawdown_active


def loss_growth_guard_active():
    """Pause new gross exposure after a sustained loss from the heat baseline."""
    if LOSS_GROWTH_BLOCK_CAD == 0:
        return False
    threshold = (
        LOSS_GROWTH_BLOCK_CAD / 2
        if STATE.loss_growth_guard_active else LOSS_GROWTH_BLOCK_CAD
    )
    return (
        STATE.risk_pnl_cad is not None
        and STATE.risk_pnl_cad <= -threshold
    )


def arb_plan_allowed(plan, gross, *, reducing=False):
    """Allow profitable inventory offsets, but block growth under risk stress."""
    if plan is None:
        return False
    if STATE.loss_growth_guard_active and plan.projected_gross > gross:
        return False
    if STATE.pnl_drawdown_active:
        cap = int(MAX_GROSS * STATE.gross_target_fraction)
        if gross > cap:
            if plan.projected_gross >= gross:
                return False
        elif plan.projected_gross > cap:
            return False
    inventory_guard = reducing or gross >= MAX_GROSS * pnl_risk_profile()[0]
    return not (inventory_guard and plan.projected_gross >= gross)


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
    if (STATE.case_tick is not None and STATE.case_tick >= ENDGAME_HOLD_TICK
            and plan.expected_profit_cad < 0):
        # Positions settle automatically; do not pay a spread merely to be flat.
        if STATE.strategy_status != "HOLDING_TO_SETTLEMENT":
            print(
                f"ENDGAME HOLD | tick={STATE.case_tick} "
                f"gross={gross} negative_close={plan.expected_profit_cad:+.2f}CAD"
            )
        STATE.strategy_status = "HOLDING_TO_SETTLEMENT"
        return None
    STATE.strategy_status = "REDUCING_INVENTORY"
    return replace(plan, reason=f"INVENTORY_REDUCE_{direction}")


def start_manual_converter_if_better():
    """Offer a manual conversion only when it beats bounded market fallback."""
    global CONVERTER_WINDOW
    if (not MANUAL_CONVERTER_ENABLED or CONVERTER_WINDOW is not None
            or STATE.case_tick is None
            or STATE.case_tick >= ENDGAME_HOLD_TICK
            or equity_signature(STATE.positions) == CONVERTER_SUPPRESSED_POSITIONS):
        return False
    gross = equity_gross(STATE.positions)
    start_fraction, target_fraction, _ = pnl_risk_profile()
    if (gross <= MAX_GROSS * target_fraction
            or (gross < MAX_GROSS * start_fraction
                and STATE.strategy_status != "REDUCING_INVENTORY")):
        return False
    if (STATE.active_intents()
            or any(bundle.status in ("PLANNED", "HEDGING", "INCOMPLETE")
                   for bundle in STATE.bundles.values())):
        return False
    proposal = propose_conversion(
        STATE, max_blocks=MANUAL_CONVERTER_MAX_BLOCKS,
        market_fee=FEE_MKT,
        delay_buffer_cad_per_share=MANUAL_CONVERTER_DELAY_BUFFER_CAD,
        min_advantage_cad=MANUAL_CONVERTER_MIN_ADVANTAGE_CAD,
        max_fallback_loss_cad_per_block=MANUAL_CONVERTER_MAX_FALLBACK_LOSS_CAD,
        max_book_age_seconds=TENDER_MAX_BOOK_AGE,
    )
    if proposal is None:
        return False
    CONVERTER_WINDOW = start_window(
        proposal, STATE.positions, MANUAL_CONVERTER_WAIT_SECONDS,
    )
    STATE.strategy_status = "WAITING_MANUAL_CONVERTER"
    STATE.manual_converter_instruction = (
        f"MANUAL {proposal.action} {proposal.blocks} x {BLOCK_SIZE:,} "
        f"in Assets; {MANUAL_CONVERTER_WAIT_SECONDS:.1f}s deadline"
    )
    print(
        f"CONVERTER ACTION | {STATE.manual_converter_instruction} | "
        f"market_close={proposal.market_close_cad:+.2f}CAD "
        f"converter_cost={proposal.converter_cost_cad:.2f}CAD "
        f"advantage={proposal.advantage_cad:+.2f}CAD | "
        "new orders paused until confirmation or fallback"
    )
    return True


def progress_manual_converter(now=None):
    """Confirm observed conversion, or return a market fallback after timeout."""
    global CONVERTER_WINDOW, CONVERTER_SUPPRESSED_POSITIONS
    window = CONVERTER_WINDOW
    if window is None:
        return False, None
    observed = converted_blocks(window, STATE.positions)
    if observed is None or observed < window.confirmed_blocks:
        print(
            "CONVERTER ABORT | unexpected position change; "
            "no fallback order submitted automatically"
        )
        CONVERTER_SUPPRESSED_POSITIONS = equity_signature(STATE.positions)
        CONVERTER_WINDOW = None
        STATE.manual_converter_instruction = ""
        STATE.strategy_status = "IDLE"
        return True, None
    if observed > window.confirmed_blocks:
        quantity = (observed - window.confirmed_blocks) * BLOCK_SIZE
        matched = apply_conversion_to_open_lots(
            STATE, window.proposal.action, quantity,
        )
        window.confirmed_blocks = observed
        print(
            f"CONVERTER CONFIRMED | action={window.proposal.action} "
            f"blocks={observed}/{window.proposal.blocks} "
            f"matched_arb_lots={matched}/{quantity} "
            f"positions={equity_signature(STATE.positions)}"
        )
    if observed == window.proposal.blocks:
        CONVERTER_WINDOW = None
        STATE.manual_converter_instruction = ""
        STATE.strategy_status = "IDLE"
        return True, None
    current = monotonic() if now is None else now
    if current < window.deadline_at:
        remaining = window.proposal.blocks - observed
        STATE.manual_converter_instruction = (
            f"MANUAL {window.proposal.action} {remaining} x {BLOCK_SIZE:,} "
            f"in Assets; {window.deadline_at - current:.1f}s left"
        )
        return True, None

    remaining = window.proposal.blocks - observed
    quantity = remaining * BLOCK_SIZE
    plan = market_close_plan(
        STATE, window.proposal.direction, quantity, FEE_MKT,
    )
    CONVERTER_SUPPRESSED_POSITIONS = equity_signature(STATE.positions)
    CONVERTER_WINDOW = None
    STATE.manual_converter_instruction = ""
    STATE.strategy_status = "IDLE"
    if (plan is None or plan.expected_profit_cad
            < -MANUAL_CONVERTER_MAX_FALLBACK_LOSS_CAD * remaining):
        print(
            f"CONVERTER FALLBACK UNAVAILABLE | remaining={quantity} "
            "market depth or loss bound failed; normal strategy resumes"
        )
        return True, None
    print(
        f"CONVERTER FALLBACK | remaining={quantity} "
        f"market_close={plan.expected_profit_cad:+.2f}CAD"
    )
    return True, replace(
        plan, reason=f"CONVERTER_FALLBACK_{window.proposal.direction}",
    )


def enqueue_arb(plan, closes_bundle_id=None):
    """Create intentions from a depth, cost, and risk checked plan."""
    tick = STATE.case_tick or 0
    bundle_id = f"arb-{tick}-{len(STATE.bundles) + 1}"
    is_opening = plan.reason.startswith("ETF_ARB_")
    offset_quantity = 0
    if is_opening:
        direction = plan.reason.removeprefix("ETF_ARB_")
        offset_quantity = min(
            plan.quantity,
            opposing_inventory_quantity(STATE.positions, direction),
        )
    closes_reason = None
    source = STATE.bundles.get(closes_bundle_id)
    if source is not None:
        closes_reason = source.reason
    elif is_opening and offset_quantity:
        closes_reason = (
            "ETF_ARB_BUY_ETF" if direction == "SELL_ETF"
            else "ETF_ARB_SELL_ETF"
        )
    elif plan.reason.endswith("BUY_ETF") and not is_opening:
        closes_reason = "ETF_ARB_SELL_ETF"
    elif plan.reason.endswith("SELL_ETF") and not is_opening:
        closes_reason = "ETF_ARB_BUY_ETF"
    bundle = STATE.add_bundle(TradeBundle(
        bundle_id=bundle_id,
        reason=plan.reason,
        created_tick=tick,
        expected_profit_cad=plan.expected_profit_cad,
        max_unhedged_ticks=MAX_UNHEDGED_TICKS,
        quantity=plan.quantity,
        open_quantity=plan.quantity - offset_quantity if is_opening else 0,
        offset_quantity=offset_quantity if is_opening else plan.quantity,
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
            deadline_tick=tick + ARB_INTENT_DEADLINE_TICKS,
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
        f"offset={bundle.offset_quantity} opening={bundle.open_quantity} "
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
    if pos is None:
        print("POSITIONS UNAVAILABLE | skipping decisions and manual confirmation")
        return False, None, None, {}
    positions_reconciled = (
        equity_signature(pos) == equity_signature(STATE.positions)
    )
    STATE.update_positions(pos)
    previous_profile = (
        STATE.gross_start_fraction,
        STATE.gross_target_fraction,
        STATE.pnl_drawdown_active,
    )
    portfolio_mark = liquidation_value_cad(STATE.positions, STATE.current_books)
    STATE.update_portfolio_value(portfolio_mark)
    risk_mark_eligible = (
        portfolio_mark is not None
        and not STATE.active_intents()
        and not any(bundle.status in ("HEDGING", "INCOMPLETE")
                    for bundle in STATE.bundles.values())
    )
    STATE.update_risk_mark(
        STATE.case_tick, eligible=risk_mark_eligible,
        giveback_fraction=PNL_MAX_GIVEBACK_FRACTION,
        recovery_fraction=PNL_RECOVERY_GIVEBACK_FRACTION,
        confirmation_ticks=PNL_DRAWDOWN_CONFIRM_TICKS,
        recovery_confirmation_ticks=PNL_DRAWDOWN_RECOVERY_TICKS,
        minimum_drawdown_ticks=PNL_DRAWDOWN_MIN_HOLD_TICKS,
        minimum_high_water=PNL_THRESHOLDS_CAD[0],
    )
    loss_guard = loss_growth_guard_active()
    if loss_guard != STATE.loss_growth_guard_active:
        print(
            f"LOSS GROWTH GUARD | tick={STATE.case_tick} "
            f"active={loss_guard} risk_pnl={STATE.risk_pnl_cad:+.2f}CAD "
            f"threshold={-LOSS_GROWTH_BLOCK_CAD:+.2f}CAD"
        )
        STATE.loss_growth_guard_active = loss_guard
    risk_start, risk_target, drawdown = pnl_risk_profile()
    STATE.update_risk_profile(risk_start, risk_target, drawdown)
    if previous_profile != (risk_start, risk_target, drawdown):
        mark = liquidation_components_cad(STATE.positions, STATE.current_books)
        mark_text = (
            "unavailable" if mark is None else
            f"cash={mark['cash_cad']:+.2f} bull={mark['bull']:+.2f} "
            f"bear={mark['bear']:+.2f} usd_block={mark['usd_block']:+.2f}"
        )
        print(
            f"RISK PROFILE | tick={STATE.case_tick} mark_pnl={STATE.pnl_cad:+.2f}CAD "
            f"risk_pnl={STATE.risk_pnl_cad if STATE.risk_pnl_cad is not None else 0:+.2f}CAD "
            f"risk_high={STATE.risk_high_water_cad:+.2f}CAD "
            f"giveback_trigger={STATE.risk_high_water_cad * (1 - PNL_MAX_GIVEBACK_FRACTION):+.2f}CAD "
            f"stable_mark={risk_mark_eligible} "
            f"gross_now={equity_gross(STATE.positions)}/{MAX_GROSS} "
            f"gross={risk_start:.0%}->{risk_target:.0%} "
            f"gross_band={int(MAX_GROSS * risk_start)}->{int(MAX_GROSS * risk_target)} "
            f"drawdown={drawdown} mark=[{mark_text}]"
        )
    manual_blocked, converter_fallback = progress_manual_converter()
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
            policy=EXECUTION_POLICY,
            passive_wait_ticks=PASSIVE_WAIT_TICKS,
        )
        sync_tender_unwinds()
        return busy or manual_blocked or submitted, None, None, {
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
    if converter_fallback is not None:
        enqueue_arb(converter_fallback)
        created = True
    if not busy and not manual_blocked and not created:
        created = accept_active_tender_offers(client)

    if not busy and not created and not manual_blocked and positions_reconciled:
        manual_blocked = start_manual_converter_if_better()

    if not busy and not created and not manual_blocked:
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
            minimum_close_profit_cad=(
                0.0 if STATE.case_tick is not None
                and STATE.case_tick >= ENDGAME_HOLD_TICK
                else float("-inf")
            ),
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
        reducing = STATE.strategy_status in (
            "REDUCING_INVENTORY", "HOLDING_TO_SETTLEMENT"
        )
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
            if not arb_plan_allowed(plan, gross, reducing=reducing):
                plan = None
        if plan is not None:
            enqueue_arb(plan, closes_bundle_id=closes_bundle_id)
            created = True

    submitted = fulfill_intents(
        client, STATE, max_order_size=MAX_SIZE_EQUITY,
        fee_per_share=FEE_MKT,
        policy=EXECUTION_POLICY,
        passive_wait_ticks=PASSIVE_WAIT_TICKS,
    )
    sync_tender_unwinds()

    return busy or manual_blocked or created or submitted, buy_etf_edge, sell_etf_edge, {
        "bull_bid": bull_bid, "bull_ask": bull_ask,
        "bear_bid": bear_bid, "bear_ask": bear_ask,
        "ritc_bid_usd": ritc_bid_usd, "ritc_ask_usd": ritc_ask_usd,
        "usd_bid": usd_bid, "usd_ask": usd_ask,
        "ritc_bid_cad": ritc_bid_cad, "ritc_ask_cad": ritc_ask_cad
    }
