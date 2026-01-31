import argparse
import json
import os
import sys
import time
import math
from datetime import datetime
import requests

from funding_shared import ExchangeInterface, SmartOrderExecutor

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
    
    elif args.type in ["smart", "agg"]:
        aggressive = (args.type == "agg")
        print(f"--- Smart Execute: {side} {args.amount} {args.symbol} (Aggressive: {aggressive}) ---")
        
        def _on_event(event, *e_args):
            if event == 'SUCCESS':
                print(f"SUCCESS: Filled {e_args[0]} @ {e_args[1]:.4f}")
            elif event == 'FAIL':
                print(f"FAIL: {e_args[0]}")
            elif event == 'ORDER_UPDATE':
                # e_args: order_id, price, qty_left, status, filled, avg, note
                print(f"Status: {e_args[3]}, Filled: {e_args[4]}/{args.amount} ({e_args[6]})")
            elif event == 'DECISION':
                # e_args: type, status, note
                print(f"Decision: {e_args[2]}")

        executor = SmartOrderExecutor(
            exchange, args.symbol, side, args.amount, aggressive=aggressive, position_side=position_side,
            callbacks={'log': print, 'on_event': _on_event}
        )
        try:
            executor.run(timeout=30)
        except KeyboardInterrupt:
            print("\nInterrupted.")
            # Basic cleanup if needed, though run() should handle interrupts gracefully if possible
            # But run() captures exceptions, maybe not KeyboardInterrupt.
            # Let's rely on run()'s internal handling or add explicit if needed.
            # SmartOrderExecutor catches Exception, which doesn't include KeyboardInterrupt.
            if executor.order_id:
                exchange.cancel_order(args.symbol, executor.order_id)