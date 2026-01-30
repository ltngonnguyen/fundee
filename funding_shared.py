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
        except: return None

    def get_book_ticker(self, symbol):
        try:
            resp = self.session.get(f"{self.base_url}/fapi/v3/ticker/bookTicker", params={'symbol': symbol}, timeout=10)
            if resp.status_code == 200:
                return resp.json()
        except: pass
        return None
