"""
RIT Market Simulator Volatility Trading Case - Support File
Rotman BMO Finance Research and Trading Lab, Uniersity of Toronto (C)
All rights reserved.
"""
import warnings
import signal
import requests
from time import sleep
import pandas as pd
import numpy as np
import base64
#black scholes libraries
from py_vollib.black_scholes import black_scholes as bs
from py_vollib.black_scholes.greeks.analytical import delta
import py_vollib.black.implied_volatility as iv
#graphs

# Define variables 
# risk free rate r
# Stock price s
# strike price k
# time remaining (in years)

#ESTIMATE YOUR VOLATILITY:
vol = 0.25

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

API_ENDPOINT = "http://flserver.rotman.utoronto.ca:16595/v1"    # Volatility Trading case
USERNAME = "goal-2"
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


#code that gets the current tick
def get_tick(session):
    case = api_request(session, 'GET', 'case')
    if case is None:
        return None
    return case['tick']

#code that gets the securities via json  
def get_s(session):
    return api_request(session, 'GET', 'securities')

def years_r(mat, tick):
    yr = (mat - tick)/3600 
    return yr
    
def main():
    with requests.Session() as session:
        session.headers.update(AUTHORIZATION)
        tick = get_tick(session)
        while tick is not None and tick < 300 and not shutdown:
            try:
                securities = get_s(session)
                if securities is None:
                    break
                assets = pd.DataFrame(securities)
                assets2 = assets.drop(columns=['vwap', 'nlv', 'bid_size', 'ask_size', 'volume', 'realized', 'unrealized', 'currency', 
                                               'total_volume', 'limits', 'is_tradeable', 'is_shortable', 'interest_rate', 'start_period', 'stop_period', 'unit_multiplier', 
                                               'description', 'unit_multiplier', 'display_unit', 'min_price', 'max_price', 'start_price', 'quoted_decimals', 'trading_fee', 'limit_order_rebate',
                                               'min_trade_size', 'max_trade_size', 'required_tickers', 'underlying_tickers', 'bond_coupon', 'interest_payments_per_period', 'base_security', 'fixing_ticker',
                                               'api_orders_per_second', 'execution_delay_ms', 'interest_rate_ticker', 'otc_price_range'])
                helper = pd.DataFrame(index = range(1),columns = ['share_exposure', 'required_hedge', 'must_be_traded', 'current_pos', 'required_pos', 'SAME?'])
                assets2['delta'] = np.nan
                assets2['i_vol'] = np.nan
                assets2['bsprice'] = np.nan
                assets2['diffcom'] = np.nan
                assets2['abs_val'] = np.nan
                assets2['decision'] = pd.Series(index=assets2.index, dtype='object')
                assets2
                
                for row in assets2.index.values:
                    if 'P' in assets2['ticker'].iloc[row]:
                        assets2['type'].iloc[row] = 'PUT'
                        if tick < 300:
                            assets2['delta'].iloc[row] = delta('p', assets2['last'].iloc[0], float(assets2['ticker'].iloc[row][3:5]), 
                                                               years_r(300, tick), 0, vol)
                            assets2['bsprice'].iloc[row] = bs('p', assets2['last'].iloc[0], float(assets2['ticker'].iloc[row][3:5]), 
                                                               years_r(300, tick), 0, vol)
                            #assets2['i_vol'].iloc[row] = iv.implied_volatility(assets2['last'].iloc[row], assets2['last'].iloc[0],
                            #                                                   float(assets2['ticker'].iloc[row][-2:]), 0, years_r(300, tick),
                            #                                                   'p')
                    elif 'C' in assets2['ticker'].iloc[row]:
                        assets2['type'].iloc[row] = 'CALL'
                        if tick < 300:
                            assets2['delta'].iloc[row] = delta('c', assets2['last'].iloc[0], float(assets2['ticker'].iloc[row][3:5]), 
                                                               years_r(300, tick), 0, vol)
                            assets2['bsprice'].iloc[row] = bs('c', assets2['last'].iloc[0], float(assets2['ticker'].iloc[row][3:5]), 
                                                               years_r(300, tick), 0, vol)
                            #assets2['i_vol'].iloc[row] = iv.implied_volatility(assets2['last'].iloc[row], assets2['last'].iloc[0],
                            #                                                   float(assets2['ticker'].iloc[row][-2:]), 0, years_r(300, tick),
                            #                                                   'c')
                    if assets2['last'].iloc[row] - assets2['bsprice'].iloc[row] > 0:
                        assets2['diffcom'].iloc[row] = assets2['last'].iloc[row] - assets2['bsprice'].iloc[row] - 0.02
                        assets2['abs_val'].iloc[row] = abs(assets2['diffcom'].iloc[row])
                    elif assets2['last'].iloc[row] - assets2['bsprice'].iloc[row] < 0:
                        assets2['diffcom'].iloc[row] = assets2['last'].iloc[row] - assets2['bsprice'].iloc[row] + 0.02
                        assets2['abs_val'].iloc[row] = abs(assets2['diffcom'].iloc[row])
                    if assets2['diffcom'].iloc[row] > 0.02:
                        assets2.loc[row, 'decision'] = 'SELL'
                    elif assets2['diffcom'].iloc[row] < -0.02:
                        assets2.loc[row, 'decision'] = 'BUY'
                    else:
                        assets2.loc[row, 'decision'] = 'NO DECISION'
                    warnings.filterwarnings('ignore')
                    
                a1 = np.array(assets2['position'].iloc[1:])
                a2 = np.array(assets2['size'].iloc[1:])
                a3 = np.array(assets2['delta'].iloc[1:])
                
                helper['share_exposure'] = np.nansum(a1 * a2 * a3)
                helper['required_hedge'] = helper['share_exposure'].iloc[0] * -1
                helper['must_be_traded'] = helper['required_hedge']/assets2['position'].iloc[0] - assets2['position'].iloc[0]
                if assets2['position'].iloc[0] > 0:
                    helper['current_pos'] = 'LONG'
                elif assets2['position'].iloc[0] < 0:
                    helper['current_pos'] = 'SHORT'
                else:
                    helper['current_pos'] = 'NO POSITION'
                if helper['required_hedge'].iloc[0] > 0:
                    helper['required_pos'] = 'LONG'
                elif helper['required_hedge'].iloc[0] < 0:
                    helper['required_pos'] = 'SHORT'
                else:
                    helper['required_pos'] = 'NO POSITION'
                helper['SAME?'] = (helper['required_pos'] == helper['current_pos'])
                print(assets2.to_markdown(), end='\n'*2)
                print(helper.to_markdown(), end='\n'*2)
                #y = assets2['last']
                #plt.plot(y)
                #plt.plotsize(50, 30)
                sleep(0.5)
                # IMPORTANT to update the tick at the end of the loop to check that the algorithm should still run or not
                tick = get_tick(session)

            except ApiException as e:
                print(f"API error: {str(e)}")
                sleep(1)

if __name__ == '__main__':
        # register the custom signal handler for graceful shutdowns
        signal.signal(signal.SIGINT, signal_handler)
        main()
