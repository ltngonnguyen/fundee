import argparse
import hashlib
import hmac
import json
import time
import math
import os
import sys
import threading
from datetime import datetime
from queue import Queue
from urllib.parse import urlencode

import requests
from funding_shared import ExchangeInterface

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
BASE_URL = "https://fapi.asterdex.com"

API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')
USER_ADDRESS = os.getenv('ASTER_USER_ADDRESS')
SIGNER_ADDRESS = os.getenv('ASTER_SIGNER_ADDRESS', USER_ADDRESS)

class KillerLogic:
    def __init__(self, interface):
        self.interface = interface
        self.exchange = ExchangeInterface(base_url=BASE_URL)
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
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting KILLER MONITOR (LIVE MODE)...")
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
        yield Static(f"KILLER MONITOR (LIVE MODE)", id="header", classes="section_title")
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
    args = parser.parse_args()
    
    if args.headless: HeadlessInterface().run()
    else: KillerApp().run()