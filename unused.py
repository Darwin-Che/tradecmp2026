# Some unused function


# def positions_map(session):
#     # Tracks current positions (number of shares currently hold for a ticker/instrument), to help risk management
#     data = api_request(session, 'GET', 'securities')  # after switching /positions to /securities, no error popup.
#     # if data is None:
#     #     return {k: 0 for k in (BULL, BEAR, RITC, USD, CAD)}
#     # out = {p["ticker"]: int(p.get("position", 0)) for p in data}
#     # for k in (BULL, BEAR, RITC, USD, CAD):
#     #     out.setdefault(k, 0)
#     # return out

# def place_mkt(session, ticker, action, qty): # type: LMT?
#     # Sends Market orders; price param is ignored by most RIT cases when type=MARKET
#     resp = api_request(session, 'POST', 'orders',
#                        params={"ticker": ticker, "type": "MARKET",
#                                "quantity": int(qty), "action": action})
#     return resp is not None

# def within_limits(pos):
#     # Simple gross/net guard using equity legs only.
#     # NOTE: positions are fetched ONCE per cycle and passed in, rather than
#     # re-queried on every check. On the DMA API every avoidable request is a
#     # step closer to an HTTP 429.
#     gross = abs(pos[BULL]) + abs(pos[BEAR]) + abs(pos[RITC])
#     net   = pos[BULL] + pos[BEAR] + pos[RITC]  # simple net; refine as desired
#     return (gross < MAX_GROSS) and (MAX_SHORT_NET < net < MAX_LONG_NET)

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
# def step_once(session):
#     # Get executable prices
#     bull_bid, bull_ask = best_bid_ask(session, BULL)
#     bear_bid, bear_ask = best_bid_ask(session, BEAR)
#     ritc_bid_usd, ritc_ask_usd = best_bid_ask(session, RITC)
#     usd_bid, usd_ask = best_bid_ask(session, USD)   # USD quoted in CAD (USD/CAD)

#     # Convert RITC to CAD using USD book
#     ritc_bid_cad = ritc_bid_usd * usd_bid
#     ritc_ask_cad = ritc_ask_usd * usd_ask

#     # Basket executable values in CAD
#     basket_sell_value = bull_bid + bear_bid      # what we get if we SELL basket now
#     basket_buy_cost   = bull_ask + bear_ask      # what we pay if we BUY basket now

#     # Direction 1: Basket rich vs ETF
#     # SELL basket (hit bids), BUY RITC in USD (lift ask) -> compare in CAD
#     edge1 = basket_sell_value - ritc_ask_cad

#     # Direction 2: ETF rich vs Basket
#     # SELL RITC (hit bid in USD), BUY basket (lift asks) -> compare in CAD
#     edge2 = ritc_bid_cad - basket_buy_cost

#     accept_active_tender_offers(session) # Automatically checking and acceptting all of the tender offer

#     pos = positions_map(session)
#     ok = within_limits(pos)
#     traded = False

#     if edge1 >= ARB_THRESHOLD_CAD and ok:
#         # Basket rich: sell BULL & BEAR, buy RITC
#         q = min(ORDER_QTY, MAX_SIZE_EQUITY)
#         place_mkt(session, BULL, "SELL", q)
#         place_mkt(session, BEAR, "SELL", q)
#         place_mkt(session, RITC, "BUY",  q)
#         traded = True

#     elif edge2 >= ARB_THRESHOLD_CAD and ok:
#         # ETF rich: buy BULL & BEAR, sell RITC
#         q = min(ORDER_QTY, MAX_SIZE_EQUITY)
#         place_mkt(session, BULL, "BUY",  q)
#         place_mkt(session, BEAR, "BUY",  q)
#         place_mkt(session, RITC, "SELL", q)
#         traded = True

#     return traded, edge1, edge2, {
#         "bull_bid": bull_bid, "bull_ask": bull_ask,
#         "bear_bid": bear_bid, "bear_ask": bear_ask,
#         "ritc_bid_usd": ritc_bid_usd, "ritc_ask_usd": ritc_ask_usd,
#         "usd_bid": usd_bid, "usd_ask": usd_ask,
#         "ritc_bid_cad": ritc_bid_cad, "ritc_ask_cad": ritc_ask_cad
#     }
