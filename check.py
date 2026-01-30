import argparse
import json
import os
import sys
import time
import math
from datetime import datetime
import requests

from funding_shared import ExchangeInterface

# ==========================================
# CONFIGURATION
# ==========================================
MAINNET_URL = "https://fapi.asterdex.com"
TESTNET_URL = "https://fapi.asterdex-testnet.com"

# Global Config (set via args)
BASE_URL = TESTNET_URL 
API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')
USER_ADDRESS = os.getenv('ASTER_USER_ADDRESS')
SIGNER_ADDRESS = os.getenv('ASTER_SIGNER_ADDRESS', USER_ADDRESS)

# Shared ExchangeInterface handles the class definition now.

def check_volumes(exchange):
    print("Fetching 24h volume data...")
    url = f"{exchange.base_url}/fapi/v3/ticker/24hr"
    try:
        resp = exchange.session.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                sorted_data = sorted(data, key=lambda x: float(x.get('quoteVolume', 0)), reverse=True)
                
                print(f"{ 'Symbol':<20} {'Price':<15} {'24h Vol (USDT)':<20} {'24h Vol (Base)':<20}")
                print("-" * 80)
                for item in sorted_data:
                    symbol = item['symbol']
                    price = float(item['lastPrice'])
                    q_vol = float(item['quoteVolume'])
                    b_vol = float(item['volume'])
                    print(f"{symbol:<20} {price:<15.4f} {q_vol:,.2f}{'':<8} {b_vol:,.2f}")
            else:
                print("Unexpected response format (not a list).")
        else:
            print(f"Error fetching ticker data: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Exception: {e}")

def smart_execute_sync(exchange, symbol, side, qty, aggressive=False, position_side=None):
    """
    Synchronous version of smart_execute for CLI use.
    """
    order_id = None
    start_time = time.time()
    
    print(f"--- Smart Execute: {side} {qty} {symbol} (Aggressive: {aggressive}) ---")
    
    try:
        # Loop for chasing (max 30s)
        while (time.time() - start_time) < 30:
            ticker = exchange.get_book_ticker(symbol)
            if not ticker: 
                time.sleep(1)
                continue

            best_bid = float(ticker['bidPrice'])
            best_ask = float(ticker['askPrice'])
            
            # Target Price
            if aggressive:
                # Marketable Limit: Buy at Ask+1%, Sell at Bid-1% (Reduced from 2% to avoid price filters)
                price = best_ask * 1.01 if side == "BUY" else best_bid * 0.99
                time_in_force = "GTC" 
            else:
                # Chase: Buy at Bid, Sell at Ask (Maker attempt)
                price = best_bid if side == "BUY" else best_ask
                time_in_force = "GTC"
            
            if not order_id:
                print(f"Placing initial order @ {price:.4f}...")
                resp = exchange.place_order(symbol, side, "LIMIT", qty, price, time_in_force, position_side=position_side)
                if resp and 'orderId' in resp:
                    order_id = resp.get('orderId')
                    print(f"Order placed: ID {order_id}")
                    if aggressive: 
                        time.sleep(0.5)
                else:
                    print(f"Order placement failed: {resp}")
                    time.sleep(1)
                    continue
            
            # Check Status
            status = exchange.get_order(symbol, order_id)
            if status:
                s = status['status']
                filled = float(status.get('executedQty', 0))
                
                if s == 'FILLED' or (s == 'CANCELED' and filled >= qty*0.99):
                     avg = float(status.get('avgPrice', price))
                     if avg == 0 and filled > 0: avg = float(status.get('cumQuote', 0)) / filled
                     print(f"SUCCESS: Filled {filled} @ {avg:.4f}")
                     return True
                
                # Logic for Chase Update
                if not aggressive:
                     current_p = float(status['price'])
                     # Check if price moved
                     new_target = best_bid if side == "BUY" else best_ask
                     
                     reprice = False
                     # If trying to BUY, and Bid > Current Price, we are behind.
                     if side == "BUY" and new_target > current_p: reprice = True
                     # If trying to SELL, and Ask < Current Price, we are behind.
                     if side == "SELL" and new_target < current_p: reprice = True
                     
                     if reprice:
                         print(f"Price moved ({current_p} -> {new_target}). Repricing...")
                         exchange.cancel_order(symbol, order_id)
                         order_id = None 
                         continue
                
                print(f"Status: {s}, Filled: {filled}/{qty}...", end='\r')
            
            time.sleep(1)
        
        print("\nTimeout reached.")
        if order_id: 
            print("Canceling remaining order...")
            exchange.cancel_order(symbol, order_id)
        return False
        
    except KeyboardInterrupt:
        print("\nInterrupted.")
        if order_id: exchange.cancel_order(symbol, order_id)
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Manual Trade Checker")
    parser.add_argument("symbol", nargs="?", help="Trading Symbol (e.g., BTCUSDT)")
    parser.add_argument("--action", choices=["OPEN", "CLOSE"], help="Action: OPEN or CLOSE")
    parser.add_argument("--direction", choices=["LONG", "SHORT"], help="Position Direction")
    parser.add_argument("--amount", type=float, help="Quantity in UNITS (e.g. 0.1 for BTC)")
    
    parser.add_argument("--type", choices=["limit", "smart", "agg"], default="smart", help="Execution Type")
    parser.add_argument("--price", type=float, help="Limit Price (only for --type limit)")
    parser.add_argument("--live", action="store_true", help="Use Mainnet")
    parser.add_argument("--check-volumes", action="store_true", help="Check 24h volume for all pairs")
    
    args = parser.parse_args()

    # Setup Environment
    global BASE_URL
    if args.live:
        BASE_URL = MAINNET_URL
        print("USING MAINNET (REAL MONEY)")
    else:
        BASE_URL = TESTNET_URL
        print("USING TESTNET")
    
    def _logger(msg):
         print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    # Initialize exchange (needed for both modes)
    exchange = ExchangeInterface(base_url=BASE_URL, logger=_logger)
    
    if args.check_volumes:
        check_volumes(exchange)
        return

    # Manual Trade Validation
    if not args.symbol or not args.action or not args.direction or args.amount is None:
        print("Error: Missing required arguments for trading.")
        print("Usage: check.py SYMBOL --action {OPEN,CLOSE} --direction {LONG,SHORT} --amount AMOUNT")
        sys.exit(1)

    if not API_KEY or not API_SECRET:
        print("Error: ASTER_API_KEY and ASTER_API_SECRET must be set for trading.")
        sys.exit(1)

    # Determine Side
    side = None
    if args.action == "OPEN":
        side = "BUY" if args.direction == "LONG" else "SELL"
    else: # CLOSE
        side = "SELL" if args.direction == "LONG" else "BUY"
    
    print(f"Intent: {args.action} {args.direction} -> Order: {side} {args.amount} {args.symbol}")
    
    # Check Position Mode
    is_hedge_mode = exchange.get_position_mode()
    
    # Check Position Risk (Debug)
    risk = exchange.get_position_risk(args.symbol)
    if risk:
        print(f"DEBUG: Position Risk: {json.dumps(risk, indent=2)}")
        # Double check mode from risk
        # If risk has positionSide="BOTH", it's One-Way. If LONG/SHORT, it's Hedge.
        if isinstance(risk, list) and len(risk) > 0:
            p_side = risk[0].get('positionSide')
            if p_side in ['LONG', 'SHORT']:
                is_hedge_mode = True
                print("DEBUG: Detected Hedge Mode from Position Risk")
            elif p_side == 'BOTH':
                is_hedge_mode = False
                print("DEBUG: Detected One-Way Mode from Position Risk")

    position_side = None
    if is_hedge_mode:
        position_side = args.direction # LONG or SHORT
        print(f"Mode: Hedge Mode (PositionSide: {position_side})")
    else:
        print("Mode: One-Way Mode")

    # Execute
    if args.type == "limit":
        if args.price is None:
            print("Error: --price required for limit order")
            sys.exit(1)
        resp = exchange.place_order(args.symbol, side, "LIMIT", args.amount, args.price, position_side=position_side)
        print("Order Response:", json.dumps(resp, indent=2))
    
    elif args.type == "smart":
        smart_execute_sync(exchange, args.symbol, side, args.amount, aggressive=False, position_side=position_side)
        
    elif args.type == "agg":
        smart_execute_sync(exchange, args.symbol, side, args.amount, aggressive=True, position_side=position_side)

if __name__ == "__main__":
    main()