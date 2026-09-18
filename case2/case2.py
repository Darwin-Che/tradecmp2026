"""
RIT Market Simulator Algorithmic ETF Arbitrage Case - Support File
Rotman BMO Finance Research and Trading Lab, Uniersity of Toronto (C)
All rights reserved.
"""

import signal
import requests
from time import sleep
import numpy as np
import base64

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

# ============================================================================
# DMA REST API CONNECTION BLOCK
# ----------------------------------------------------------------------------
# The DMA (Direct Market Access) REST API talks to the RIT *Server* directly,
# so you do NOT need the RIT Client running on your machine. Compared to the
# Client REST API only two things change:
#
#   1. AUTH. Basic authentication with your TraderID and password, instead of
#      the {'X-API-Key': 'Rotman'} header.
#   2. RATE LIMITING. The RIT Client used to absorb this for you. On DMA there
#      is nothing in between you and the server, so a burst of requests comes
#      back as HTTP 429 and you must back off yourself.
#
# Every endpoint, parameter and JSON response is otherwise IDENTICAL to the
# Client REST API, which is why the case logic below is unchanged.
# ============================================================================

API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16635/v1"
USERNAME = "goal"
PASSWORD = "credit"
AUTHORIZATION = {'Authorization': 'Basic ' + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()}

shutdown = False


# this class definition allows us to print error messages and stop the program when needed
class ApiException(Exception):
    pass


# this signal handler allows for a graceful shutdown when CTRL+C is pressed
def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True


# this helper method handles rate-limiting to pause for the next cycle
def handle_rate_limit(response):
    if response.status_code == 429:
        wait_time = float(response.headers.get('Retry-After', response.json().get('wait', 1)))
        print(f"Rate limit exceeded. Waiting for {wait_time} seconds before retrying.")
        sleep(wait_time)
        return True
    return False


# this helper method handles authorization failure
def handle_auth_failure(response):
    global shutdown
    if response.status_code == 401:
        print("Authentication failed. Please check your USERNAME and PASSWORD.")
        shutdown = True
        return True
    return False


# this helper method compiles possible API responses and handlers
def api_request(session, method, endpoint, params=None):
    while True:
        url = f"{API_ENDPOINT}/{endpoint}"
        if method == 'GET':
            resp = session.get(url, params=params)
        elif method == 'POST':
            resp = session.post(url, params=params)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        if handle_auth_failure(resp):
            return None
        if handle_rate_limit(resp):
            continue
        if resp.ok:
            return resp.json()
        raise ApiException(f"API request failed: {resp.text}")


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

# Seconds to wait between polling cycles. Raise this if you keep seeing the
# "Rate limit exceeded" message; each cycle costs ~7 requests.
LOOP_SLEEP = 0.5

# --------- HELPERS ----------
def get_tick_status(session):
    # Gets simulation status (active or stopped) for the tick
    j = api_request(session, 'GET', 'case')
    if j is None:
        return None, None
    return j["tick"], j["status"]

def best_bid_ask(session, ticker):
    # Returns best bid and ask prices for a ticker
    book = api_request(session, 'GET', 'securities/book', params={"ticker": ticker})
    if book is None:
        return 0.0, 1e12
    'Why choose [0] here, not [1]? Is the price for bids and asks also generated by r'
    bid = float(book["bids"][0]["price"]) if book["bids"] else 0.0
    ask = float(book["asks"][0]["price"]) if book["asks"] else 1e12
    return bid, ask

def positions_map(session):
    # Tracks current positions (number of shares currently hold for a ticker/instrument), to help risk management
    data = api_request(session, 'GET', 'securities')  # after switching /positions to /securities, no error popup.
    if data is None:
        return {k: 0 for k in (BULL, BEAR, RITC, USD, CAD)}
    out = {p["ticker"]: int(p.get("position", 0)) for p in data}
    for k in (BULL, BEAR, RITC, USD, CAD):
        out.setdefault(k, 0)
    return out

def place_mkt(session, ticker, action, qty): # type: LMT?
    # Sends Market orders; price param is ignored by most RIT cases when type=MARKET
    resp = api_request(session, 'POST', 'orders',
                       params={"ticker": ticker, "type": "MARKET",
                               "quantity": int(qty), "action": action})
    return resp is not None

def within_limits(pos):
    # Simple gross/net guard using equity legs only.
    # NOTE: positions are fetched ONCE per cycle and passed in, rather than
    # re-queried on every check. On the DMA API every avoidable request is a
    # step closer to an HTTP 429.
    gross = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC])
    net   = pos[BULL] + pos[BEAR] + pos[RITC]  # simple net; refine as desired
    return (gross < MAX_GROSS) and (MAX_SHORT_NET < net < MAX_LONG_NET)

def accept_active_tender_offers(session):
    # Retrieve active tender offers from the RIT API, and accept the offer
    offers = api_request(session, 'GET', 'tenders')
    if not offers:
        print("No active tenders")
        return
    tender_id = offers[0]['tender_id']
    price = offers[0]['price']
    if offers[0]['is_fixed_bid']:
        resp = api_request(session, 'POST', f"tenders/{tender_id}")
    else:
        resp = api_request(session, 'POST', f"tenders/{tender_id}", params={"price": price})
    print("Tender Offer Accepted:", resp is not None)

# --------- CORE LOGIC ----------
def step_once(session):
    # Get executable prices
    bull_bid, bull_ask = best_bid_ask(session, BULL)
    bear_bid, bear_ask = best_bid_ask(session, BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(session, RITC)
    usd_bid, usd_ask = best_bid_ask(session, USD)   # USD quoted in CAD (USD/CAD)

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

    accept_active_tender_offers(session) # Automatically checking and acceptting all of the tender offer

    pos = positions_map(session)
    ok = within_limits(pos)
    traded = False

    if edge1 >= ARB_THRESHOLD_CAD and ok:
        # Basket rich: sell BULL & BEAR, buy RITC
        q = min(ORDER_QTY, MAX_SIZE_EQUITY)
        place_mkt(session, BULL, "SELL", q)
        place_mkt(session, BEAR, "SELL", q)
        place_mkt(session, RITC, "BUY",  q)
        traded = True

    elif edge2 >= ARB_THRESHOLD_CAD and ok:
        # ETF rich: buy BULL & BEAR, sell RITC
        q = min(ORDER_QTY, MAX_SIZE_EQUITY)
        place_mkt(session, BULL, "BUY",  q)
        place_mkt(session, BEAR, "BUY",  q)
        place_mkt(session, RITC, "SELL", q)
        traded = True

    return traded, edge1, edge2, {
        "bull_bid": bull_bid, "bull_ask": bull_ask,
        "bear_bid": bear_bid, "bear_ask": bear_ask,
        "ritc_bid_usd": ritc_bid_usd, "ritc_ask_usd": ritc_ask_usd,
        "usd_bid": usd_bid, "usd_ask": usd_ask,
        "ritc_bid_cad": ritc_bid_cad, "ritc_ask_cad": ritc_ask_cad
    }

def main():
    with requests.Session() as s:
        s.headers.update(AUTHORIZATION)
        tick, status = get_tick_status(s)
        while status == "ACTIVE" and not shutdown:
            try:
                traded, e1, e2, info = step_once(s)
                # Optional: print a lightweight heartbeat every 1s
                print(f"tick={tick} e1={e1:.4f} e2={e2:.4f} ritc_ask_cad={info['ritc_ask_cad']:.4f}")
                sleep(LOOP_SLEEP)
                # IMPORTANT to update the tick at the end of the loop to check that the algorithm should still run or not
                tick, status = get_tick_status(s)
            except ApiException as e:
                print(f"API error: {str(e)}")
                sleep(1)

if __name__ == "__main__":
    # register the custom signal handler for graceful shutdowns
    signal.signal(signal.SIGINT, signal_handler)
    main()
