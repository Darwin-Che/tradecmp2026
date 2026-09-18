"""
RIT Market Simulator Algorithmic ETF Arbitrage Case - Support File
Rotman BMO Finance Research and Trading Lab, University of Toronto (C)
All rights reserved.
"""

import sys

from api import ApiException
from execution import submit_market_order
from state import TenderState, TradingState
from tender_strategy import evaluate_fixed_tender

'''
If you are not familiar with Python or feeling a little bit rusty, highly recommend you to go through the following link:
    https://github.com/trekhleb/learn-python

If you have any question about DMA APIs and outputs of code please read:
    https://realpython.com/api-integration-in-python/#http-methods
    https://rit.306w.ca/RIT-DMA-API/1.0.5/

So bascially：
The core of this case is to design algorithmic trading strategies that exploit arbitrage opportunities between the ETF (RITC)
and its underlying stocks (BULL and BEAR), while effectively using tender offers and conversion tools to avoid speculative risk
and maximize returns.
'''

STATE = TradingState(history_limit=300)
PROCESSED_TENDER_IDS = set()


# Tickers
CAD  = "CAD"    # currency instrument quoted in CAD
USD  = "USD"    # price of 1 USD in CAD (i.e., USD/CAD)
BULL = "BULL"   # stock in CAD
BEAR = "BEAR"   # stock in CAD
RITC = "RITC"   # ETF quoted in USD

# Per problem statement
FEE_MKT = 0.02           # $/share (market)
REBATE_LMT = 0.01        # $/share (passive) - not used in this baseline
MAX_SIZE_EQUITY = 10000 # per order for BULL/BEAR/RITC
MAX_SIZE_FX = 2500000  # per order for CAD/USD

# Basic risk guardrails (adjust as needed)
MAX_LONG_NET  = 25000
MAX_SHORT_NET = -25000
MAX_GROSS     = 500000
ORDER_QTY     = 5000    # child order size for arb legs

# Cushion to beat fees & slippage.
# 3 legs with market orders => ~0.06 CAD/sh cost; add a bit more for safety.
ARB_THRESHOLD_CAD = 0.07
TENDER_BUFFER_USD = 0.03
TENDER_MIN_PROFIT_CAD = 50.0
TENDER_MAX_BOOK_AGE = 1.5

# --------- HELPERS ----------
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
    # Tracks current positions (number of shares currently hold for a ticker/instrument), to help risk management
    data = client.get_securities()
    if data is None:
        return {k: 0 for k in (BULL, BEAR, RITC, USD, CAD)}
    out = {p["ticker"]: int(p.get("position", 0)) for p in data}
    for k in (BULL, BEAR, RITC, USD, CAD):
        out.setdefault(k, 0)
    return out

def place_mkt(client, ticker, action, qty, reason, context=""):
    resp = submit_market_order(
        client,
        STATE,
        ticker,
        action,
        qty,
        reason=reason,
        context=context,
        fee_per_share=FEE_MKT,
    )
    return resp is not None

def within_limits(pos):
    # Simple gross/net guard using equity legs only.
    # NOTE: positions are fetched ONCE per cycle and passed in, rather than
    # re-queried on every check. On the DMA API every avoidable request is a
    # step closer to an HTTP 429.
    gross = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC])
    net   = pos[BULL] + pos[BEAR] + pos[RITC]  # simple net; refine as desired
    return (gross < MAX_GROSS) and (MAX_SHORT_NET < net < MAX_LONG_NET)

def accept_active_tender_offers(client):
    # Evaluate each tender once; accept only profitable fixed-price offers.
    offers = client.get_tenders()
    if not offers:
        return False

    offer = next(
        (item for item in offers if item["tender_id"] not in PROCESSED_TENDER_IDS),
        None,
    )
    if offer is None:
        return False

    tender_id = offer["tender_id"]
    evaluation = evaluate_fixed_tender(
        offer,
        STATE,
        market_fee_usd=FEE_MKT,
        safety_buffer_per_share_usd=TENDER_BUFFER_USD,
        minimum_profit_cad=TENDER_MIN_PROFIT_CAD,
        max_book_age_seconds=TENDER_MAX_BOOK_AGE,
        max_gross=MAX_GROSS,
        min_net=MAX_SHORT_NET,
        max_net=MAX_LONG_NET,
    )
    PROCESSED_TENDER_IDS.add(tender_id)
    print(evaluation.log_line())
    if not evaluation.should_accept:
        return False

    try:
        response = client.accept_tender(tender_id, evaluation.tender_price)
    except ApiException as exc:
        print(f"Tender {tender_id} failed: {exc}", file=sys.stderr)
        return False

    accepted = bool(response and response.get("success", True))
    if not accepted:
        print(f"Tender {tender_id} was not accepted by the API", file=sys.stderr)
        return False

    tender = TenderState(
        tender_id=tender_id,
        action=evaluation.action,
        price=evaluation.tender_price,
        quantity=evaluation.quantity,
        accepted=True,
    )
    STATE.tenders[tender_id] = tender
    STATE.strategy_status = "UNWINDING_TENDER"

    tender_delta = evaluation.quantity if evaluation.action == "BUY" else -evaluation.quantity
    exit_sign = 1 if evaluation.exit_action == "BUY" else -1
    STATE.positions[RITC] += tender_delta
    STATE.hedge_remaining[RITC] = exit_sign * evaluation.quantity

    remaining = evaluation.quantity
    while remaining > 0:
        chunk = min(remaining, MAX_SIZE_EQUITY)
        order = submit_market_order(
            client,
            STATE,
            RITC,
            evaluation.exit_action,
            chunk,
            reason="TENDER_UNWIND",
            context=f"tender={tender_id}",
            fee_per_share=FEE_MKT,
        )
        if not order:
            break
        filled = int(order.get("quantity_filled", 0))
        tender.quantity_unwound += filled
        remaining -= filled
        STATE.positions[RITC] += exit_sign * filled
        STATE.hedge_remaining[RITC] = exit_sign * remaining
        if filled < chunk:
            break

    if remaining:
        STATE.hedge_remaining[RITC] = exit_sign * remaining
    else:
        STATE.hedge_remaining.pop(RITC, None)
        STATE.strategy_status = "IDLE"
    print(
        f"TENDER {tender_id} UNWIND | {evaluation.exit_action} "
        f"filled={tender.quantity_unwound}/{evaluation.quantity} "
        f"remaining={remaining}"
    )
    return True


def finish_tender_unwind(client):
    """Complete a previously partial RITC tender unwind before new trades."""
    signed_remaining = STATE.hedge_remaining.get(RITC, 0)
    if signed_remaining == 0:
        return False

    action = "BUY" if signed_remaining > 0 else "SELL"
    quantity = min(abs(signed_remaining), MAX_SIZE_EQUITY)
    active_tender = next(
        (
            tender for tender in reversed(list(STATE.tenders.values()))
            if tender.remaining_to_unwind > 0
        ),
        None,
    )
    context = (
        f"tender={active_tender.tender_id}"
        if active_tender is not None else "tender=unknown"
    )
    order = submit_market_order(
        client,
        STATE,
        RITC,
        action,
        quantity,
        reason="TENDER_UNWIND_RETRY",
        context=context,
        fee_per_share=FEE_MKT,
    )
    if not order:
        print("TENDER UNWIND RETRY | no order response", file=sys.stderr)
        return True

    filled = min(quantity, int(order.get("quantity_filled", 0)))
    if active_tender is not None:
        active_tender.quantity_unwound += filled

    remaining = abs(signed_remaining) - filled
    position_delta = filled if action == "BUY" else -filled
    STATE.positions[RITC] += position_delta
    if remaining:
        sign = 1 if action == "BUY" else -1
        STATE.hedge_remaining[RITC] = sign * remaining
    else:
        STATE.hedge_remaining.pop(RITC, None)
        STATE.strategy_status = "IDLE"
    print(
        f"TENDER UNWIND RETRY | {action} filled={filled}/{quantity} "
        f"remaining={remaining}"
    )
    return True

# --------- CORE LOGIC ----------
def step_once(client):
    # Get executable prices
    bull_book = get_order_book(client, BULL)
    bear_book = get_order_book(client, BEAR)
    ritc_book = get_order_book(client, RITC)
    usd_book = get_order_book(client, USD)   # USD quoted in CAD (USD/CAD)

    bull_bid, bull_ask = bull_book.best_bid or 0.0, bull_book.best_ask or 1e12
    bear_bid, bear_ask = bear_book.best_bid or 0.0, bear_book.best_ask or 1e12
    ritc_bid_usd = ritc_book.best_bid or 0.0
    ritc_ask_usd = ritc_book.best_ask or 1e12
    usd_bid, usd_ask = usd_book.best_bid or 0.0, usd_book.best_ask or 1e12

    # Convert RITC to CAD using USD book
    ritc_bid_cad = ritc_bid_usd * usd_bid
    ritc_ask_cad = ritc_ask_usd * usd_ask

    # Basket executable values in CAD
    basket_sell_value = bull_bid + bear_bid      # what we get if we SELL basket now
    basket_buy_cost   = bull_ask + bear_ask      # what we pay if we BUY basket now

    # Direction 1: Basket rich vs ETF
    # SELL basket (hit bids), BUY RITC in USD (lift ask) -> compare in CAD
    edge1 = basket_sell_value - ritc_ask_cad

    # Direction 2: ETF rich vs Basket
    # SELL RITC (hit bid in USD), BUY basket (lift asks) -> compare in CAD
    edge2 = ritc_bid_cad - basket_buy_cost
    STATE.record_edges(edge1, edge2)

    pos = positions_map(client)
    STATE.update_positions(pos)
    ok = within_limits(pos)
    traded = finish_tender_unwind(client)
    if not traded:
        traded = accept_active_tender_offers(client)

    if not traded and edge1 >= ARB_THRESHOLD_CAD and ok:
        # Basket rich: sell BULL & BEAR, buy RITC
        q = min(ORDER_QTY, MAX_SIZE_EQUITY)
        context = f"tick={STATE.case_tick} edge={edge1:.4f}CAD"
        place_mkt(client, BULL, "SELL", q, "ETF_ARB_BUY_ETF", context)
        place_mkt(client, BEAR, "SELL", q, "ETF_ARB_BUY_ETF", context)
        place_mkt(client, RITC, "BUY", q, "ETF_ARB_BUY_ETF", context)
        traded = True

    elif not traded and edge2 >= ARB_THRESHOLD_CAD and ok:
        # ETF rich: buy BULL & BEAR, sell RITC
        q = min(ORDER_QTY, MAX_SIZE_EQUITY)
        context = f"tick={STATE.case_tick} edge={edge2:.4f}CAD"
        place_mkt(client, BULL, "BUY", q, "ETF_ARB_SELL_ETF", context)
        place_mkt(client, BEAR, "BUY", q, "ETF_ARB_SELL_ETF", context)
        place_mkt(client, RITC, "SELL", q, "ETF_ARB_SELL_ETF", context)
        traded = True

    return traded, edge1, edge2, {
        "bull_bid": bull_bid, "bull_ask": bull_ask,
        "bear_bid": bear_bid, "bear_ask": bear_ask,
        "ritc_bid_usd": ritc_bid_usd, "ritc_ask_usd": ritc_ask_usd,
        "usd_bid": usd_bid, "usd_ask": usd_ask,
        "ritc_bid_cad": ritc_bid_cad, "ritc_ask_cad": ritc_ask_cad
    }
