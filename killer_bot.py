import argparse
import hashlib
import hmac
import time
import math
import os
import sys
import threading
from datetime import datetime
from queue import Queue
from urllib.parse import urlencode

import requests

try:
    from eth_abi.abi import encode
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from web3 import Web3
except ImportError:
    pass

try:
    from textual.app import App, ComposeResult
    from textual.containers import Vertical
    from textual.widgets import DataTable, Footer, Header, Static
except ImportError:
    App = object
    ComposeResult = None
    pass

# ==========================================
# CONFIGURATION
# ==========================================
MAINNET_URL = "https://fapi.asterdex.com"
TESTNET_URL = "https://fapi.asterdex-testnet.com"

# REAL TRADING CONFIG
DRY_RUN = True  # Set to False via --live flag

BASE_URL = TESTNET_URL if DRY_RUN else MAINNET_URL
API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')
USER_ADDRESS = os.getenv('ASTER_USER_ADDRESS')
SIGNER_ADDRESS = os.getenv('ASTER_SIGNER_ADDRESS', USER_ADDRESS)

class ExchangeInterface:
    def __init__(self):
        self.precision_map = {}
        self.load_exchange_info()

    def load_exchange_info(self):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v1/exchangeInfo", timeout=10)
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
            print(f"Error loading exchange info: {e}")

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
        return round(math.floor(qty / step_size) * step_size, precision)

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

    def get_positions(self):
        # if DRY_RUN: ... (Removed for Testnet)
            
        if not API_SECRET: return []
        try:
            params = {}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            resp = requests.get(f"{BASE_URL}/fapi/v3/positionRisk", params=query, headers=headers, timeout=5)
            if resp.status_code == 200:
                return [p for p in resp.json() if float(p['positionAmt']) != 0]
        except: pass
        return []

    def place_order(self, symbol, side, type, quantity, price=None, time_in_force="GTC"):
        # if DRY_RUN: ... (Removed for Testnet)

        if not API_SECRET: return None
        
        qty = self.normalize_quantity(symbol, quantity)
        if qty <= 0: return None

        params = {
            'symbol': symbol,
            'side': side,
            'type': type,
            'quantity': qty,
        }
        
        if type == 'LIMIT':
            if price is None: return None
            params['price'] = self.normalize_price(symbol, price)
            params['timeInForce'] = time_in_force

        try:
            query = self._sign_request(params)
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'PythonApp/1.0'
            }
            resp = requests.post(f"{BASE_URL}/fapi/v3/order", data=query, headers=headers, timeout=5)
            return resp.json()
        except Exception as e:
            print(f"Order failed: {e}")
            return None

    def cancel_order(self, symbol, order_id):
        # if DRY_RUN: ... (Removed for Testnet)
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol, 'orderId': order_id}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            resp = requests.delete(f"{BASE_URL}/fapi/v3/order", data=query, headers=headers, timeout=5)
            return resp.json()
        except Exception as e:
            print(f"Cancel failed: {e}")
            return None

    def get_order(self, symbol, order_id):
        # if DRY_RUN: ... (Removed for Testnet)
        if not API_SECRET: return None
        try:
            params = {'symbol': symbol, 'orderId': order_id}
            query = self._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0'}
            resp = requests.get(f"{BASE_URL}/fapi/v3/order", params=query, headers=headers, timeout=5)
            return resp.json()
        except: return None

    def get_book_ticker(self, symbol):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v1/ticker/bookTicker", params={'symbol': symbol}, timeout=5)
            if resp.status_code == 200:
                return resp.json()
        except: pass
        return None

class KillerLogic:
    def __init__(self, interface):
        self.interface = interface
        self.exchange = ExchangeInterface()
        self.killing_active = False
        self.pending_kills = set() # Symbols being killed
        self.killed_history = []
        
    def start(self):
        self.interface.set_interval(1.0, self.monitor_and_kill)
        self.interface.set_interval(5.0, self.refresh_positions)

    def refresh_positions(self):
        self.interface.run_worker(self.fetch_positions_worker)

    def fetch_positions_worker(self):
        positions = self.exchange.get_positions()
        self.interface.call_from_thread(self.update_positions, positions)

    def update_positions(self, positions):
        self.active_positions = positions
        self.interface.update_ui()

    def monitor_and_kill(self):
        now = datetime.now()
        minute = now.minute
        
        # DEFINED SAFE ZONES (Where we DO NOT kill):
        # Minute 59 (xx:59:00 - xx:59:59) -> Pre-Funding freeze
        # Minute 00 (xx:00:00 - xx:00:59) -> Funding Event
        #
        # KILL ZONE:
        # ALL OTHER TIMES (Active 58/60 minutes per hour)
        
        if minute == 59 or minute == 0:
            self.killing_active = False
        else:
            self.killing_active = True
            self.execute_kill_sequence()
            
        self.interface.update_ui()

    def execute_kill_sequence(self):
        if not hasattr(self, 'active_positions'): return
        
        for p in self.active_positions:
            symbol = p['symbol']
            amt = float(p['positionAmt'])
            
            if symbol in self.pending_kills: continue
            if amt == 0: continue
            
            self.pending_kills.add(symbol)
            direction = "SHORT" if amt < 0 else "LONG" # Closing direction is opposite
            close_side = "BUY" if amt < 0 else "SELL"
            qty = abs(amt)
            
            self.interface.notify(f"KILLING {symbol} ({direction} {qty})...")
            
            # Use Smart Execute to Close
            def _on_success(fill, avg, oid):
                self.killed_history.insert(0, {'time': datetime.now().strftime('%H:%M:%S'), 'symbol': symbol, 'qty': fill, 'price': avg})
                if len(self.killed_history) > 100: self.killed_history.pop() # Prevent memory leak
                self.interface.notify(f"KILLED {symbol} @ {avg}")
                if symbol in self.pending_kills: self.pending_kills.remove(symbol)
                self.refresh_positions() # Refresh immediately
                
            def _on_fail(err):
                self.interface.notify(f"KILL FAIL {symbol}: {err}")
                if symbol in self.pending_kills: self.pending_kills.remove(symbol)

            self.interface.run_worker(
                self.smart_execute(symbol, close_side, qty, aggressive=True, on_success=_on_success, on_fail=_on_fail)
            )

    def smart_execute(self, symbol, side, qty, aggressive, on_success, on_fail):
        def _worker():
            order_id = None
            try:
                start_time = time.time()
                # Chase for 30s max
                while (time.time() - start_time) < 30:
                    ticker = self.exchange.get_book_ticker(symbol)
                    if not ticker: 
                        time.sleep(1)
                        continue

                    best_bid = float(ticker['bidPrice'])
                    best_ask = float(ticker['askPrice'])
                    
                    if aggressive:
                        # Marketable Limit: 2% Buffer for Safety/Speed
                        price = best_ask * 1.02 if side == "BUY" else best_bid * 0.98
                        time_in_force = "GTC" 
                    else:
                        price = best_bid if side == "BUY" else best_ask
                        time_in_force = "GTC"
                    
                    if not order_id:
                        resp = self.exchange.place_order(symbol, side, "LIMIT", qty, price, time_in_force)
                        if resp and 'orderId' in resp:
                            order_id = resp.get('orderId')
                            if aggressive: time.sleep(0.5)
                        else:
                            time.sleep(1)
                            continue
                    
                    # Check Fill
                    status = self.exchange.get_order(symbol, order_id)
                    if status:
                        s = status.get('status', 'NEW')
                        filled = float(status.get('executedQty', 0))
                        
                        if s == 'FILLED' or (s == 'CANCELED' and filled >= qty*0.99):
                             avg = float(status.get('avgPrice', price))
                             if avg == 0 and filled > 0: avg = float(status.get('cumQuote', 0)) / filled
                             if avg == 0: avg = price
                             self.interface.call_from_thread(on_success, filled, avg, order_id)
                             return
                        
                        # Aggressive orders don't chase, they just sit deep or fill. 
                        # But if not filled in 5s, maybe re-aggressive?
                        # Current logic: just wait. Marketable 2% should fill instantly.
                    
                    time.sleep(1)
                
                if order_id: self.exchange.cancel_order(symbol, order_id)
                self.interface.call_from_thread(on_fail, "Timeout")
                
            except Exception as e:
                self.interface.call_from_thread(on_fail, str(e))
        return _worker

class HeadlessInterface:
    def __init__(self):
        self.running = True
        self.tasks = []
        self.queue = Queue()
        
    def set_interval(self, interval, func):
        self.tasks.append([time.time() + interval, interval, func])
        
    def run_worker(self, func, thread=True):
        t = threading.Thread(target=func)
        t.daemon = True
        t.start()
        
    def call_from_thread(self, func, *args):
        self.queue.put((func, args))
        
    def notify(self, msg):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        sys.stdout.flush()
        
    def update_ui(self):
        pass
        
    def run(self):
        self.logic = KillerLogic(self)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting KILLER MONITOR (Dry Run: {DRY_RUN})...")
        self.logic.start()
        try:
            while self.running:
                while not self.queue.empty():
                    try:
                        func, args = self.queue.get_nowait()
                        func(*args)
                    except: pass
                now = time.time()
                for task in self.tasks:
                    if now >= task[0]:
                        task[2]()
                        task[0] = now + task[1]
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nStopping...")

class KillerApp(App):
    CSS = """
    Screen { layout: vertical; }
    DataTable { height: 1fr; border: solid red; }
    .section_title { background: $primary; color: white; text-align: center; text-style: bold; height: 1; }
    .status_active { background: green; color: white; text-align: center; }
    .status_idle { background: grey; color: white; text-align: center; }
    """
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self):
        super().__init__()
        self.logic = KillerLogic(self)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"KILLER MONITOR (Dry Run: {DRY_RUN})", id="header", classes="section_title")
        yield Static("IDLE - WAITING FOR KILL WINDOW", id="status_bar", classes="status_idle")
        yield Vertical(
            Static("Detected Stray Positions", classes="section_title"),
            DataTable(id="pos_table"),
            Static("Kill History", classes="section_title"),
            DataTable(id="hist_table"),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pos_table", DataTable)
        table.add_columns("Symbol", "Size", "Entry Price", "PnL")
        
        hist = self.query_one("#hist_table", DataTable)
        hist.add_columns("Time", "Symbol", "Qty", "Fill Price")
        
        self.logic.start()

    def update_ui(self):
        # Update Header
        now = datetime.now()
        color = "green" if self.logic.killing_active else "grey"
        status_text = "KILL MODE ACTIVE" if self.logic.killing_active else "IDLE - SAFE"
        
        sb = self.query_one("#status_bar", Static)
        sb.update(status_text)
        sb.classes = "status_active" if self.logic.killing_active else "status_idle"

        # Update Positions
        table = self.query_one("#pos_table", DataTable)
        rows = []
        if hasattr(self.logic, 'active_positions'):
            for p in self.logic.active_positions:
                rows.append((p['symbol'], p['positionAmt'], p['entryPrice'], p['unRealizedProfit']))
        table.clear()
        table.add_rows(rows)

        # Update History
        hist = self.query_one("#hist_table", DataTable)
        h_rows = []
        for h in self.logic.killed_history:
            h_rows.append((h['time'], h['symbol'], f"{h['qty']}", f"{h['price']}"))
        hist.clear()
        hist.add_rows(h_rows)

    def run_worker(self, func, thread=True):
        return super().run_worker(func, thread=thread)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--live", action="store_true", help="DISABLE Dry Run (Real Money)")
    args = parser.parse_args()

    if args.live:
        DRY_RUN = False
        BASE_URL = MAINNET_URL
    else:
        DRY_RUN = True
        BASE_URL = TESTNET_URL
    
    if args.headless: HeadlessInterface().run()
    else: KillerApp().run()