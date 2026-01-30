import argparse
import json
import os
import sys
import time
import math
from datetime import datetime
import requests

# Try imports
try:
    from eth_abi.abi import encode
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from web3 import Web3
except ImportError:
    print("Error: Web3 libraries not installed. Please run 'pip install web3 eth-account eth-abi'")
    sys.exit(1)

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

class ExchangeInterface:
    def __init__(self, base_url=None):
        self.base_url = base_url or BASE_URL
        self.precision_map = {}
        self.load_exchange_info()
    
    def log(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def load_exchange_info(self):
        try:
            resp = requests.get(f"{self.base_url}/fapi/v1/exchangeInfo", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for s in data['symbols']:
                    filters = {f['filterType']: f for f in s['filters']}
                    tick_size = float(filters['PRICE_FILTER']['tickSize']) if 'PRICE_FILTER' in filters else 0.0
                    step_size = float(filters['LOT_SIZE']['stepSize']) if 'LOT_SIZE' in filters else 0.0
                    self.precision_map[s['symbol']] = {
                        'tick_size': tick_size,
                        'step_size': step_size,
                        'price_precision': s['pricePrecision'],
                        'qty_precision': s['quantityPrecision']
                    }
        except Exception as e:
            self.log(f"Error loading exchange info: {e}")

    def normalize_price(self, symbol, price):
        if symbol not in self.precision_map: return price
        p_info = self.precision_map[symbol]
        tick_size = p_info['tick_size']
        precision = p_info['price_precision']
        if tick_size == 0: return round(price, precision)
        return round(round(price / tick_size) * tick_size, precision)

    def normalize_quantity(self, symbol, qty):
        if symbol not in self.precision_map: return qty
        p_info = self.precision_map[symbol]
        step_size = p_info['step_size']
        precision = p_info['qty_precision']
        if step_size == 0: return round(qty, precision)
        # Use floor to avoid exceeding balance/limits
        normalized = round(math.floor(qty / step_size) * step_size, precision)
        if normalized <= 0 and qty > 0:
             self.log(f"Quantity {qty} too small for symbol {symbol}. Step size: {step_size}")
        return normalized

    def _trim_dict(self, data):
        for key in data:
            value = data[key]
            if isinstance(value, list):
                new_value = []
                for item in value:
                    if isinstance(item, dict):
                        new_value.append(json.dumps(self._trim_dict(item)))
                    else:
                        new_value.append(str(item))
                data[key] = json.dumps(new_value)
                continue
            if isinstance(value, dict):
                data[key] = json.dumps(self._trim_dict(value))
                continue
            data[key] = str(value)
        return data

    def _sign_request(self, params):
        if not API_SECRET: return None
        
        # Filter None
        params = {k: v for k, v in params.items() if v is not None}
        
        # Add required params
        params['recvWindow'] = 50000
        params['timestamp'] = int(time.time() * 1000)
        
        # Prepare for signing (convert to strings)
        self._trim_dict(params)
        
        # Generate Nonce
        nonce = int(time.time() * 1000000)
        
        # Generate JSON string for hashing
        # Ensure consistent sorting and no spaces
        json_str = json.dumps(params, sort_keys=True).replace(' ', '').replace("'", '"')
        
        user = USER_ADDRESS
        signer = SIGNER_ADDRESS if SIGNER_ADDRESS else USER_ADDRESS
        
        # ABI Encode and Hash
        encoded = encode(['string', 'address', 'address', 'uint256'], 
                         [json_str, user, signer, nonce])
        keccak_hex = Web3.keccak(encoded).hex()
        
        # Sign
        signable_msg = encode_defunct(hexstr=keccak_hex)
        signed_message = Account.sign_message(signable_message=signable_msg, private_key=API_SECRET)
        
        # Add Auth fields
        params['nonce'] = nonce
        params['user'] = user
        params['signer'] = signer
        params['signature'] = '0x' + signed_message.signature.hex()
        
        return params

    def place_order(self, symbol, side, type, quantity, price=None, time_in_force="GTC", position_side=None):
        if not API_SECRET: 
            self.log("Missing API Secret")
            return None
        
        qty = self.normalize_quantity(symbol, quantity)
        if qty <= 0: 
            self.log(f"Invalid Quantity: {qty}")
            return None

        params = {
            'symbol': symbol,
            'side': side,
            'type': type,
            'quantity': qty,
        }
        
        if position_side:
            params['positionSide'] = position_side
        
        if type == 'LIMIT':
            if price is None: 
                self.log("Limit order requires price")
                return None
            params['price'] = self.normalize_price(symbol, price)
            params['timeInForce'] = time_in_force

        try:
            query = self._sign_request(params)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'CheckBot/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            resp = requests.post(f"{self.base_url}/fapi/v3/order", data=query, headers=headers, timeout=5)
            return resp.json()
        except Exception as e:
            self.log(f"Order request failed: {e}")
            return None

    def cancel_order(self, symbol, order_id):
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol, 'orderId': order_id}
            query = self._sign_request(params)
            headers = {
                'User-Agent': 'CheckBot/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            resp = requests.delete(f"{self.base_url}/fapi/v3/order", data=query, headers=headers, timeout=5)
            return resp.json()
        except Exception as e:
            self.log(f"Cancel failed: {e}")
            return None

    def get_order(self, symbol, order_id):
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol, 'orderId': order_id}
            query = self._sign_request(params)
            headers = {
                'User-Agent': 'CheckBot/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            resp = requests.get(f"{self.base_url}/fapi/v3/order", params=query, headers=headers, timeout=5)
            return resp.json()
        except Exception as e: 
            self.log(f"Get order failed: {e}")
            return None

    def get_book_ticker(self, symbol):
        try:
            resp = requests.get(f"{self.base_url}/fapi/v1/ticker/bookTicker", params={'symbol': symbol}, timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except: pass
        return None

    def get_position_mode(self):
        try:
            params = {}
            if API_SECRET:
                # GET /fapi/v1/positionSide/dual requires auth
                query = self._sign_request(params)
                headers = {
                    'User-Agent': 'CheckBot/1.0',
                    'X-MBX-APIKEY': API_KEY
                }
                resp = requests.get(f"{self.base_url}/fapi/v1/positionSide/dual", params=query, headers=headers, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    print(f"DEBUG: Position Mode Response: {data}")
                    val = data.get('dualSidePosition')
                    if str(val).lower() == 'true': return True
                    return False
                else:
                    print(f"DEBUG: Position Mode Failed: {resp.status_code} {resp.text}")
        except Exception as e:
            self.log(f"Error checking position mode: {e}")
        return False # Default to One-way

    def get_position_risk(self, symbol):
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol}
            query = self._sign_request(params)
            headers = {
                'User-Agent': 'CheckBot/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            resp = requests.get(f"{self.base_url}/fapi/v3/positionRisk", params=query, headers=headers, timeout=5)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"DEBUG: Position Risk Failed: {resp.status_code} {resp.text}")
        except Exception as e:
            self.log(f"Error checking position risk: {e}")
        return None

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
    parser.add_argument("symbol", help="Trading Symbol (e.g., BTCUSDT)")
    parser.add_argument("--action", choices=["OPEN", "CLOSE"], required=True, help="Action: OPEN or CLOSE")
    parser.add_argument("--direction", choices=["LONG", "SHORT"], required=True, help="Position Direction")
    parser.add_argument("--amount", type=float, required=True, help="Quantity in UNITS (e.g. 0.1 for BTC)")
    
    parser.add_argument("--type", choices=["limit", "smart", "agg"], default="smart", help="Execution Type")
    parser.add_argument("--price", type=float, help="Limit Price (only for --type limit)")
    parser.add_argument("--live", action="store_true", help="Use Mainnet")
    
    args = parser.parse_args()

    # Setup Environment
    global BASE_URL
    if args.live:
        BASE_URL = MAINNET_URL
        print("USING MAINNET (REAL MONEY)")
    else:
        BASE_URL = TESTNET_URL
        print("USING TESTNET")
    
    if not API_KEY or not API_SECRET:
        print("Error: ASTER_API_KEY and ASTER_API_SECRET must be set.")
        sys.exit(1)

    # Determine Side
    side = None
    if args.action == "OPEN":
        side = "BUY" if args.direction == "LONG" else "SELL"
    else: # CLOSE
        side = "SELL" if args.direction == "LONG" else "BUY"
    
    print(f"Intent: {args.action} {args.direction} -> Order: {side} {args.amount} {args.symbol}")

    exchange = ExchangeInterface(base_url=BASE_URL)
    
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
