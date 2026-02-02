import os
import time
import json
import math
import requests

try:
    from web3 import Web3
    from eth_account import Account
    from eth_abi.abi import encode
    from eth_account.messages import encode_defunct
    WEB3_AVAILABLE = True
except ImportError:
    WEB3_AVAILABLE = False
    # Mock/Placeholder if needed or just handle in _sign_request

# ==========================================
# CONFIGURATION
# ==========================================
BASE_URL = "https://fapi.asterdex.com"

API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')
USER_ADDRESS = os.getenv('ASTER_USER_ADDRESS')
SIGNER_ADDRESS = os.getenv('ASTER_SIGNER_ADDRESS', USER_ADDRESS)

class ExchangeInterface:
    def __init__(self, base_url=None, logger=None):
        self.base_url = base_url or BASE_URL
        self.logger = logger
        self.precision_map = {}
        self.session = requests.Session()
        self.load_exchange_info()
    
    def log(self, msg):
        if self.logger:
            self.logger(msg)
        else:
            print(f"[EXCHANGE] {msg}")

    def load_exchange_info(self):
        try:
            resp = self.session.get(f"{self.base_url}/fapi/v3/exchangeInfo", timeout=10)
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
        if symbol not in self.precision_map: 
            return None
        p_info = self.precision_map[symbol]
        step_size = p_info['step_size']
        precision = p_info['qty_precision']
        if step_size == 0: 
            return round(qty, precision)
        # Use floor to avoid exceeding balance/limits
        normalized = round(math.floor(qty / step_size) * step_size, precision)
        if normalized <= 0 and qty > 0:
            self.log(f"Quantity {qty} too small for symbol {symbol}. Step size: {step_size}")
            return None
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
        if not WEB3_AVAILABLE:
            self.log("Error: Web3 libraries not installed. Signing failed.")
            return None
        
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

        try:
            user = Web3.to_checksum_address(user)
            signer = Web3.to_checksum_address(signer)
        except Exception:
            self.log(f"Invalid Address Format: User={user}, Signer={signer}")
            return None
        
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

    def get_balance(self):
        if not API_SECRET: 
            self.log("No API_SECRET set.")
            return 0.0
        if not USER_ADDRESS:
            self.log("No ASTER_USER_ADDRESS set. Required for Aster Dex.")
            return 0.0
            
        try:
            params = {}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            # Try v3 balance
            resp = self.session.get(f"{self.base_url}/fapi/v3/balance", params=query, headers=headers, timeout=10)
            if resp.status_code == 200:
                for b in resp.json():
                    if b['asset'] == 'USDT':
                        val = float(b['availableBalance'])
                        return val
            else:
                self.log(f"Balance fetch failed: {resp.status_code} {resp.text}")
        except Exception as e:
            self.log(f"Balance Exception: {e}")
        return None

    def get_positions(self):
        if not API_SECRET: return []
        try:
            params = {}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            resp = self.session.get(f"{self.base_url}/fapi/v3/positionRisk", params=query, headers=headers, timeout=10)
            if resp.status_code == 200:
                # Return only active positions
                return [p for p in resp.json() if float(p['positionAmt']) != 0]
            else:
                 self.log(f"Pos Error: {resp.status_code} {resp.text}")
        except Exception as e:
             self.log(f"Pos Exception: {e}")
        return []

    def get_position_mode(self):
        try:
            params = {}
            if API_SECRET:
                # GET /fapi/v3/positionSide/dual requires auth
                query = self._sign_request(params)
                headers = {
                    'User-Agent': 'PythonApp/1.0',
                    'X-MBX-APIKEY': API_KEY
                }
                resp = self.session.get(f"{self.base_url}/fapi/v3/positionSide/dual", params=query, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    val = data.get('dualSidePosition')
                    if str(val).lower() == 'true': return True
                    return False
                else:
                    self.log(f"Position Mode Failed: {resp.status_code} {resp.text}")
        except Exception as e:
            self.log(f"Error checking position mode: {e}")
        return False # Default to One-way

    def get_position_risk(self, symbol):
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol}
            query = self._sign_request(params)
            headers = {
                'User-Agent': 'PythonApp/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            resp = self.session.get(f"{self.base_url}/fapi/v3/positionRisk", params=query, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            else:
                self.log(f"Position Risk Failed: {resp.status_code} {resp.text}")
        except Exception as e:
            self.log(f"Error checking position risk: {e}")
        return None

    def place_order(self, symbol, side, type, quantity, price=None, time_in_force="GTC", position_side=None):
        if not API_SECRET: return None
        
        qty = self.normalize_quantity(symbol, quantity)
        if qty is None or qty <= 0: 
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
            if price is None: return None
            params['price'] = self.normalize_price(symbol, price)
            params['timeInForce'] = time_in_force

        try:
            query = self._sign_request(params)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'PythonApp/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            resp = self.session.post(f"{self.base_url}/fapi/v3/order", data=query, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            self.log(f"Order failed: {e}")
            return None

    def cancel_order(self, symbol, order_id):
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol, 'orderId': order_id}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            resp = self.session.delete(f"{self.base_url}/fapi/v3/order", data=query, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            self.log(f"Cancel failed: {e}")
            return None

    def get_order(self, symbol, order_id):
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol, 'orderId': order_id}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            resp = self.session.get(f"{self.base_url}/fapi/v3/order", params=query, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            self.log(f"Get order error: {e}")
            return None

    def get_book_ticker(self, symbol):
        try:
            resp = self.session.get(f"{self.base_url}/fapi/v3/ticker/bookTicker", params={'symbol': symbol}, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            self.log(f"Book ticker fetch error for {symbol}: {e}")
        return None

    def set_leverage(self, symbol, leverage):
        if not API_SECRET: return None
        try:
            params = {
                'symbol': symbol,
                'leverage': int(leverage)
            }
            query = self._sign_request(params)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'PythonApp/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            # The docs say /fapi/v3/leverage
            resp = self.session.post(f"{self.base_url}/fapi/v3/leverage", data=query, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            self.log(f"Set leverage failed: {e}")
            return None

    def cancel_all_orders(self, symbol):
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            # Try v3 as requested by user
            resp = self.session.delete(f"{self.base_url}/fapi/v3/allOpenOrders", data=query, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            self.log(f"Cancel All Orders failed: {e}")
            return None

    def close_all_positions(self, symbol, side, position_side=None):
        """
        Uses STOP_MARKET with closePosition=true to close the entire position (including dust).
        """
        if not API_SECRET: return None

        # 1. Cancel existing orders to free up slots/limits
        self.cancel_all_orders(symbol)
        time.sleep(0.5)
        
        # Get Current Price
        ticker = self.get_book_ticker(symbol)
        if not ticker:
            self.log(f"Close All Failed: No ticker for {symbol}")
            return None
        
        bid = float(ticker['bidPrice'])
        ask = float(ticker['askPrice'])
        
        # Determine Trigger Price
        # STOP_MARKET SELL (Close Long): Trigger if Price <= Stop. Set Stop slightly below Bid.
        # STOP_MARKET BUY (Close Short): Trigger if Price >= Stop. Set Stop slightly above Ask.
        
        if side == 'SELL':
            stop_price = bid * 0.99
        elif side == 'BUY':
            stop_price = ask * 1.01
        else:
            return None

        params = {
            'symbol': symbol,
            'side': side,
            'type': 'STOP_MARKET',
            'stopPrice': self.normalize_price(symbol, stop_price),
            'closePosition': 'true',
        }
        if position_side:
            params['positionSide'] = position_side

        try:
            query = self._sign_request(params)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'PythonApp/1.0',
                'X-MBX-APIKEY': API_KEY
            }
            resp = self.session.post(f"{self.base_url}/fapi/v3/order", data=query, headers=headers, timeout=10)
            return resp.json()
        except Exception as e:
            self.log(f"Close All failed: {e}")
            return None

class SmartOrderExecutor:
    """
    Shared logic for smart order execution (Chasing/Passive -> Aggressive).
    Can be used synchronously or wrapped in a thread.
    """
    def __init__(self, exchange, symbol, side, qty, aggressive=False, position_side=None, leverage=None, callbacks=None):
        self.exchange = exchange
        self.symbol = symbol
        self.side = side
        self.qty = float(qty)
        self.initial_qty = self.qty
        self.aggressive = aggressive
        self.position_side = position_side
        self.leverage = leverage
        self.callbacks = callbacks or {}
        
        # State
        self.order_id = None
        self.cumulative_filled = 0.0
        self.qty_left = self.qty
        self.last_price = 0.0

    def log(self, msg):
        if 'log' in self.callbacks: 
            self.callbacks['log'](msg)
        # Fallback is silent or handled by caller

    def emit_event(self, event_type, *args):
        """
        Generic event emitter.
        event_type: 'ORDER_UPDATE', 'DECISION', 'SUCCESS', 'FAIL'
        """
        if 'on_event' in self.callbacks:
            self.callbacks['on_event'](event_type, *args)

    def run(self, timeout=70, switch_mode_time=None):
        # Set Leverage if requested
        if self.leverage is not None:
            self.log(f"Setting leverage for {self.symbol} to {self.leverage}x")
            res = self.exchange.set_leverage(self.symbol, self.leverage)
            # Check for success
            if not res or 'leverage' not in res or int(res['leverage']) != int(self.leverage):
                 self.log(f"CRITICAL: Failed to set leverage to {self.leverage}x. Response: {res}")
                 self.emit_event('FAIL', f"Leverage Set Failed: {res}")
                 return False

        start_time = time.time()

        try:
            while (time.time() - start_time) < timeout:
                # 1. Check for Aggressive Switch
                if not self.aggressive and switch_mode_time and time.time() >= switch_mode_time:
                    self.aggressive = True
                    self.log(f"TIMEOUT: Passive limit reached for {self.symbol}. Switching to AGGRESSIVE (Taker).")
                    # If we have an active passive order, we need to cancel it first to go aggressive
                    # We continue; the reprice logic below will handle cancellation if price/mode mismatch
                    pass

                # 2. Fetch Price
                ticker = self.exchange.get_book_ticker(self.symbol)
                if not ticker:
                    self.log(f"Warn: No ticker data for {self.symbol}")
                    time.sleep(1)
                    continue

                best_bid = float(ticker["bidPrice"])
                best_ask = float(ticker["askPrice"])

                # 3. Determine Price based on Mode
                if self.aggressive:
                    # Marketable Limit: Buy at Ask+1%, Sell at Bid-1%
                    price = best_ask * 1.01 if self.side == "BUY" else best_bid * 0.99
                    time_in_force = "GTC"
                else:
                    # Chase: Buy at Bid, Sell at Ask (Passive)
                    price = best_bid if self.side == "BUY" else best_ask
                    time_in_force = "GTX"  # Post Only to ensure Maker rebate

                # 4. Place Order if None exists
                if not self.order_id:
                    # Safety check on Min Qty (approx 5.5 USDT)
                    # TODO: Read MIN_NOTIONAL from exchange info if available
                    if (self.qty_left * price) < 5.5:
                        if self.qty_left == self.initial_qty:
                            self.log(f"Position size {self.qty_left} ({self.qty_left*price:.2f} USDT) too small to trade.")
                            self.emit_event('FAIL', "Dust Position - Too small to close")
                            return False
                        else:
                            self.log("Remainder too small, marking done.")
                            role = 'TAKER' if self.aggressive else 'MAKER'
                            self.emit_event('SUCCESS', self.cumulative_filled, price, "Partial-Done", role)
                            return True

                    resp = self.exchange.place_order(
                        self.symbol,
                        self.side,
                        "LIMIT",
                        self.qty_left,
                        price,
                        time_in_force,
                        position_side=self.position_side,
                    )
                    
                    if resp and "orderId" in resp:
                        self.order_id = resp.get("orderId")
                        self.last_price = price
                        self.emit_event('ORDER_UPDATE', self.order_id, price, self.qty_left, "NEW", 0, 0, f"Placed ({'Agg' if self.aggressive else 'Pas'})")
                        if self.aggressive:
                            time.sleep(0.5)
                    else:
                        self.emit_event('DECISION', "EXEC", "FAIL", f"Place Error: {resp}")
                        time.sleep(1)
                        continue

                # 5. Check Status
                status = self.exchange.get_order(self.symbol, self.order_id)
                if status:
                    s = status["status"]
                    this_order_filled = float(status.get("executedQty", 0))

                    # Done?
                    if s == "FILLED" or (s == "CANCELED" and this_order_filled >= self.qty_left * 0.99):
                        self.cumulative_filled += this_order_filled
                        avg = float(status.get("avgPrice", self.last_price))
                        if avg == 0 and this_order_filled > 0:
                            avg = float(status.get("cumQuote", 0)) / this_order_filled

                        self.emit_event('ORDER_UPDATE', self.order_id, self.last_price, self.qty_left, s, this_order_filled, avg, "Done")
                        role = 'TAKER' if self.aggressive else 'MAKER'
                        self.emit_event('SUCCESS', self.cumulative_filled, avg, self.order_id, role)
                        return True

                    # Logic for Chase / Reprice
                    should_cancel = False

                    # A: Switch to Aggressive?
                    if not self.aggressive and switch_mode_time and time.time() >= switch_mode_time:
                        should_cancel = True
                    
                    # B: Price Moved? (Reprice if market runs away)
                    if not should_cancel:
                        current_p = float(status.get("price", self.last_price))
                        
                        if self.side == "BUY":
                            # Passive: If Best Bid > Order Price -> We are behind (chase up)
                            if not self.aggressive and price > current_p:
                                should_cancel = True
                            # Aggressive: If Best Ask > Order Price -> Our 1% buffer was exceeded, we are now a limit order.
                            elif self.aggressive and best_ask > current_p:
                                should_cancel = True
                                
                        elif self.side == "SELL":
                            # Passive: If Best Ask < Order Price -> We are behind (chase down)
                            if not self.aggressive and price < current_p:
                                should_cancel = True
                            # Aggressive: If Best Bid < Order Price -> Our 1% buffer was exceeded.
                            elif self.aggressive and best_bid < current_p:
                                should_cancel = True
                    
                    if should_cancel:
                        self.log(f"Repricing {self.symbol}: Current Order {current_p} vs Market {price} (Aggressive: {self.aggressive})")
                        self.exchange.cancel_order(self.symbol, self.order_id)
                        # We wait for the next loop to verify cancellation or just clear ID
                        # Ideally, wait for CANCELED status, but to be fast, we just clear ID.
                        # However, to be safe, we check filled qty on cancel in next loop or assume cancel worked.
                        # Simple approach: clear ID, loop will re-check or re-place.
                        self.order_id = None
                        
                        # Check if we got any fill during that time (optional optimization)
                        if this_order_filled > 0:
                            self.cumulative_filled += this_order_filled
                            self.qty_left -= this_order_filled
                            self.emit_event('ORDER_UPDATE', self.order_id, self.last_price, self.qty_left, "PARTIAL", this_order_filled, 0, "Repricing")
                        
                        continue # Loop immediately

                time.sleep(1)

            # Timeout
            self.log(f"EXECUTION TIMEOUT: Failed to fill {self.symbol} in {timeout}s.")
            if self.order_id:
                self.exchange.cancel_order(self.symbol, self.order_id)
            self.emit_event('FAIL', "Timeout")
            return False

        except Exception as e:
            self.log(f"Exception in SmartOrder: {e}")
            if self.order_id:
                self.exchange.cancel_order(self.symbol, self.order_id)
            self.emit_event('FAIL', str(e))
            return False

