import argparse
import hashlib
import hmac
import json
import os
import sys
import subprocess
import threading
import time
import math
from datetime import datetime, timedelta
from queue import Queue
from urllib.parse import urlencode

import requests

try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Container, Vertical
    from textual.widgets import DataTable, Footer, Header, Static, Log
    from textual.worker import Worker
except ImportError:
    # Allow running headless without textual installed
    App = object
    ComposeResult = None
    pass

from funding_shared import ExchangeInterface, BASE_URL

from funding_shared import ExchangeInterface, BASE_URL

# ==========================================
# CONFIGURATION
# ==========================================
# BASE_URL imported from funding_shared

API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')
USER_ADDRESS = os.getenv('ASTER_USER_ADDRESS')
SIGNER_ADDRESS = os.getenv('ASTER_SIGNER_ADDRESS', USER_ADDRESS)
TRADE_SIZE_USDT = 100.0  # Size per trade in USDT

# Defaults
DEFAULT_MAKER = 0.0002
DEFAULT_TAKER = 0.0004
MIN_PROFIT_BUFFER = 0.0002

class FundingLogic:
    def __init__(self, interface):
        self.interface = interface
        self.exchange = ExchangeInterface(base_url=BASE_URL, logger=self.interface.log_message)
        self.balance = 0.0
        self.trade_size = TRADE_SIZE_USDT
        
        self.viable_pairs = []
        self.fee_cache = {}
        self.ticker_map = {}
        self.ticker_stats_cache = {}
        self.fee_queue = set()
        self.pending_orders = set()
        self.is_hedge_mode = False  # Default One-Way
        self.is_fetching_tickers = False # Prevent stacking requests
        
        # Track local strategy state
        self.active_strategies = [] 
        self.closed_strategies = []
        self.sim_counter = 0
        
        # Log file
        if not os.path.exists("logs/live_trades.csv"):
            with open("logs/live_trades.csv", "w") as f:
                f.write("Timestamp,Strategy,Symbol,Direction,Entry,Exit,Net_PnL_USDT,Net_PnL_Pct,Balance\n")

        # Enhanced Logging for Backtesting
        if not os.path.exists("logs/market_log.csv"):
            with open("logs/market_log.csv", "w") as f:
                f.write("Timestamp,Symbol,FundingRate,NextFundingTime,Bid,Ask,Spread,EstProfit,MakerFee,TakerFee\n")
                
        if not os.path.exists("logs/order_log.csv"):
            with open("logs/order_log.csv", "w") as f:
                f.write("Timestamp,Symbol,OrderId,Side,Type,Price,Qty,Status,ExecutedQty,AvgPrice,Msg\n")
                
        if not os.path.exists("logs/decision_log.csv"):
            with open("logs/decision_log.csv", "w") as f:
                f.write("Timestamp,Symbol,Strategy,Action,Reason\n")

    def log_market_data(self, candidates):
        """Log market snapshot of viable pairs for backtesting."""
        ts = datetime.now()
        with open("logs/market_log.csv", "a") as f:
            for c in candidates:
                sym = c['symbol']
                tik = self.ticker_map.get(sym)
                if not tik: continue
                
                fees = self.fee_cache.get(sym, {'maker': DEFAULT_MAKER, 'taker': DEFAULT_TAKER})
                spread = (tik['ask'] - tik['bid']) / tik['ask'] if tik['ask'] > 0 else 0
                est_profit = abs(c['funding_rate']) - (fees['taker'] * 2)
                
                f.write(f"{ts},{sym},{c['funding_rate']:.6f},{c['next_funding_time']},{tik['bid']},{tik['ask']},{spread:.6f},{est_profit:.6f},{fees['maker']},{fees['taker']}\n")

    def log_order_event(self, symbol, order_id, side, type, price, qty, status, executed, avg_price, msg=""):
        """Log granular order events."""
        with open("logs/order_log.csv", "a") as f:
            f.write(f"{datetime.now()},{symbol},{order_id},{side},{type},{price},{qty},{status},{executed},{avg_price},{msg}\n")

    def log_decision(self, symbol, strategy, action, reason):
        """Log why we did or did not take a trade."""
        with open("logs/decision_log.csv", "a") as f:
            f.write(f"{datetime.now()},{symbol},{strategy},{action},{reason}\n")

    def start(self):
        self.interface.log_message(f"Starting Logic (Base: {BASE_URL})")
        
        # Check Position Mode
        self.is_hedge_mode = self.exchange.get_position_mode()
        self.interface.log_message(f"Position Mode: {'Hedge' if self.is_hedge_mode else 'One-Way'}")
        
        self.interface.set_interval(60.0, self.fetch_premiums)
        self.interface.set_interval(60.0, self.fetch_24hr_stats)
        self.interface.set_interval(5.0, self.sync_balance_positions) # Sync with exchange
        self.interface.set_interval(1.0, self.fetch_tickers)
        self.interface.set_interval(1.0, self.update_strategies)
        self.interface.set_interval(2.0, self.process_fee_queue)
        self.refresh_all()

    def refresh_all(self):
        self.interface.run_worker(self.fetch_premiums_worker)
        self.interface.run_worker(self.fetch_tickers_worker)
        self.interface.run_worker(self.fetch_24hr_stats_worker)
        self.sync_balance_positions()

    def clear_history(self):
        self.closed_strategies = []
        self.interface.update_ui()

    def sync_balance_positions(self):
        self.interface.run_worker(self.sync_account_worker)

    def sync_account_worker(self):
        try:
            bal = self.exchange.get_balance()
            positions = self.exchange.get_positions()
            self.interface.call_from_thread(self.update_account_state, bal, positions)
        except Exception as e:
            self.interface.call_from_thread(self.interface.log_message, f"Sync Error: {e}")

    def update_account_state(self, balance, positions):
        if balance is not None:
            self.balance = balance
        # Map real positions to strategies? 
        # For now, we just trust our local state for strategy logic, 
        # but we could add a safety check here to close "orphan" positions not in our list.
        self.interface.update_ui()

    def fetch_premiums(self): self.interface.run_worker(self.fetch_premiums_worker)
    def fetch_tickers(self): 
        if not self.is_fetching_tickers:
            self.interface.run_worker(self.fetch_tickers_worker)

    def fetch_24hr_stats(self): self.interface.run_worker(self.fetch_24hr_stats_worker)
    
    def process_fee_queue(self):
        if self.fee_queue: self.interface.run_worker(self.fetch_fee_worker(self.fee_queue.pop()))
        
    def fetch_premiums_worker(self):
        try:
            # self.interface.call_from_thread(self.interface.log_message, "Fetching premiums...")
            resp = requests.get(f"{BASE_URL}/fapi/v3/premiumIndex", timeout=10)
            if resp.status_code == 200: 
                self.interface.call_from_thread(self.process_premiums, resp.json())
            else:
                self.interface.call_from_thread(self.interface.log_message, f"Premium fetch failed: {resp.status_code}")
        except Exception as e: 
             self.interface.call_from_thread(self.interface.log_message, f"Premium fetch error: {e}")

    def fetch_tickers_worker(self):
        self.is_fetching_tickers = True
        try:
            # self.interface.call_from_thread(self.interface.log_message, "Fetching tickers...")
            resp = requests.get(f"{BASE_URL}/fapi/v3/ticker/bookTicker", timeout=10)
            if resp.status_code == 200: self.interface.call_from_thread(self.process_tickers, resp.json())
            else: self.interface.call_from_thread(self.interface.log_message, f"Ticker fail: {resp.status_code}")
        except Exception as e:
            self.interface.call_from_thread(self.interface.log_message, f"Ticker error: {e}")
        finally:
            self.is_fetching_tickers = False

    def fetch_24hr_stats_worker(self):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v3/ticker/24hr", timeout=10)
            if resp.status_code == 200: self.interface.call_from_thread(self.process_24hr_stats, resp.json())
        except: pass

    def fetch_fee_worker(self, symbol):
        def _work():
            if not API_SECRET: return
            try:
                params = {'symbol': symbol}
                query = self.exchange._sign_request(params)
                # headers = {'X-MBX-APIKEY': API_KEY}
                resp = requests.get(f"{BASE_URL}/fapi/v3/commissionRate", params=query, timeout=5)
                if resp.status_code == 200:
                    d = resp.json()
                    self.interface.call_from_thread(self.update_fee_cache, symbol, float(d.get("makerCommissionRate", DEFAULT_MAKER)), float(d.get("takerCommissionRate", DEFAULT_TAKER)))
            except: pass
        return _work

    def process_tickers(self, data):
        for t in data:
            self.ticker_map[t['symbol']] = {'bid': float(t.get('bidPrice', 0)), 'ask': float(t.get('askPrice', 0))}
        self.interface.update_ui()
    
    def process_24hr_stats(self, data):
        for t in data:
            self.ticker_stats_cache[t['symbol']] = {'quoteVolume': float(t.get('quoteVolume', 0))}

    def update_fee_cache(self, symbol, maker, taker):
        self.fee_cache[symbol] = {'maker': maker, 'taker': taker}

    def process_premiums(self, data):
        candidates = []
        for p in data:
            sym = p['symbol']
            try:
                rate = float(p.get('lastFundingRate', 0))
                nxt = p.get('nextFundingTime')
            except: continue
            
            if abs(rate) > (DEFAULT_MAKER*2 + MIN_PROFIT_BUFFER):
                tik = self.ticker_map.get(sym)
                if tik and tik['ask'] > 0:
                    spread = (tik['ask'] - tik['bid']) / tik['ask']
                    fees = self.fee_cache.get(sym, {'maker': DEFAULT_MAKER, 'taker': DEFAULT_TAKER})
                    
                    if spread > 0.005: 
                        self.log_decision(sym, "SCAN", "SKIP", f"High Spread {spread:.4f}")
                        continue # Spread too high
                    
                    est_profit = abs(rate) - (fees['taker'] * 2)
                    if spread > est_profit: 
                        self.log_decision(sym, "SCAN", "SKIP", f"Spread > Profit ({spread:.4f} > {est_profit:.4f})")
                        continue

                if sym not in self.fee_cache and sym not in self.fee_queue: self.fee_queue.add(sym)
                candidates.append({'symbol': sym, 'funding_rate': rate, 'next_funding_time': nxt, 'direction': "SHORT" if rate > 0 else "LONG"})
        
        candidates.sort(key=lambda x: abs(x['funding_rate']), reverse=True)
        self.viable_pairs = candidates
        self.log_market_data(candidates)
        self.interface.update_ui()

    def log_trade(self, s):
        # Calc PnL %
        if s['entry_price'] > 0:
            if s['direction'] == "LONG": pnl_pct = (s['exit_price'] - s['entry_price']) / s['entry_price']
            else: pnl_pct = (s['entry_price'] - s['exit_price']) / s['entry_price']
        else: pnl_pct = 0.0

        with open("logs/live_trades.csv", "a") as f:
            f.write(f"{datetime.now()},{s['strategy']},{s['symbol']},{s['direction']},{s['entry_price']},{s['exit_price']},{s.get('net_pnl_amt',0):.4f},{pnl_pct:.6f},{self.balance:.4f}\n")

    def remove_pending(self, symbol):
        if symbol in self.pending_orders: self.pending_orders.remove(symbol)

    def smart_execute(self, symbol, side, qty, aggressive, on_success, on_fail, position_side=None):
        def _worker():
            order_id = None
            try:
                start_time = time.time()
                
                # Loop for chasing (max 30s)
                while (time.time() - start_time) < 30:
                    ticker = self.exchange.get_book_ticker(symbol)
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
                        # Chase: Buy at Bid, Sell at Ask
                        price = best_bid if side == "BUY" else best_ask
                        time_in_force = "GTC"
                    
                    if not order_id:
                        resp = self.exchange.place_order(symbol, side, "LIMIT", qty, price, time_in_force, position_side=position_side)
                        if resp and 'orderId' in resp:
                            order_id = resp.get('orderId')
                            self.interface.call_from_thread(self.log_order_event, symbol, order_id, side, "LIMIT", price, qty, "NEW", 0, 0, "Placed")
                            if aggressive: 
                                # Assume filled for dry run or wait short for agg
                                time.sleep(0.5)
                        else:
                            self.interface.call_from_thread(self.log_decision, symbol, "EXEC", "FAIL", f"Place Error: {resp}")
                            time.sleep(1)
                            continue
                    
                    # Check Status
                    status = self.exchange.get_order(symbol, order_id)
                    if status:
                        s = status['status']
                        filled = float(status.get('executedQty', 0))
                        
                        if s == 'FILLED' or (s == 'CANCELED' and filled >= qty*0.99):
                             avg = float(status.get('avgPrice', price))
                             if avg == 0 and filled > 0: avg = float(status.get('cumQuote', 0)) / filled
                             if avg == 0: avg = price # Fallback
                             
                             self.interface.call_from_thread(self.log_order_event, symbol, order_id, side, "LIMIT", price, qty, s, filled, avg, "Done")
                             self.interface.call_from_thread(on_success, filled, avg, order_id)
                             return
                        
                        # Logic for Chase Update
                        if not aggressive:
                             current_p = float(status['price'])
                             # Check if price moved
                             new_target = best_bid if side == "BUY" else best_ask
                             
                             reprice = False
                             if side == "BUY" and new_target > current_p: reprice = True
                             if side == "SELL" and new_target < current_p: reprice = True
                             
                             if reprice:
                                 self.interface.call_from_thread(self.log_order_event, symbol, order_id, side, "LIMIT", current_p, qty, "CANCEL_REPRICE", filled, 0, f"Reprice to {new_target}")
                                 self.exchange.cancel_order(symbol, order_id)
                                 order_id = None 
                                 continue
                    
                    time.sleep(1)
                
                # Timeout / Cleanup
                if order_id: 
                    self.interface.call_from_thread(self.log_order_event, symbol, order_id, side, "LIMIT", 0, qty, "TIMEOUT_CANCEL", 0, 0, "Timeout")
                    self.exchange.cancel_order(symbol, order_id)
                self.interface.call_from_thread(on_fail, "Timeout")
                
            except Exception as e:
                self.interface.call_from_thread(on_fail, str(e))
            finally:
                self.interface.call_from_thread(self.remove_pending, symbol)
        return _worker

    def execute_strategy_entry(self, strategy, symbol, direction, maker=False):
        # Check duplicates
        for s in self.active_strategies:
            if s['symbol'] == symbol and s['strategy'] == strategy and s['status'] in ['OPEN', 'OPENING']:
                return
        if symbol in self.pending_orders: return

        if self.balance < self.trade_size:
            self.interface.notify(f"SKIP {strategy} {symbol}: Low Balance (${self.balance:.2f})")
            return

        ticker = self.ticker_map.get(symbol)
        if not ticker: return

        # Mark pending
        self.pending_orders.add(symbol)

        # Calculate roughly qty
        price = ticker['ask'] if direction == "LONG" else ticker['bid'] # Approx for sizing
        qty = self.trade_size / price
        side = "BUY" if direction == "LONG" else "SELL"
        
        # Determine Position Side (Hedge Mode Support)
        position_side = direction if self.is_hedge_mode else None
        
        def _on_success(fill_qty, avg_price, oid):
             s = {
                'id': self.sim_counter,
                'strategy': strategy,
                'symbol': symbol,
                'status': 'OPEN',
                'direction': direction,
                'entry_price': avg_price,
                'entry_time': time.time(),
                'quantity': fill_qty,
                'margin': self.trade_size,
                'order_id': oid
            }
             self.sim_counter += 1
             self.active_strategies.append(s)
             self.interface.notify(f"OPENED {strategy} {symbol} @ {avg_price:.4f}")
             self.interface.update_ui()

        def _on_fail(reason):
             self.interface.notify(f"OPEN FAIL {symbol}: {reason}")

        self.interface.notify(f"CHASING ENTRY {symbol} ({'Agg' if not maker else 'Pas'})...")
        self.interface.run_worker(
            self.smart_execute(symbol, side, qty, not maker, _on_success, _on_fail, position_side=position_side)
        )

    def execute_strategy_exit(self, s, reason, maker=False):
        if s['symbol'] in self.pending_orders: return
        self.pending_orders.add(s['symbol'])
        
        side = "SELL" if s['direction'] == "LONG" else "BUY"
        
        # Determine Position Side (Hedge Mode Support)
        position_side = s['direction'] if self.is_hedge_mode else None
        
        def _on_success(fill_qty, avg_price, oid):
            s['status'] = 'CLOSED'
            s['exit_price'] = avg_price
            s['exit_time'] = time.time()
            s['reason'] = reason
            
            # PnL Calc (Approx)
            if s['direction'] == "LONG": pnl = (avg_price - s['entry_price']) * s['quantity']
            else: pnl = (s['entry_price'] - avg_price) * s['quantity']
            
            s['net_pnl_amt'] = pnl
            self.closed_strategies.append(s)
            if s in self.active_strategies:
                self.active_strategies.remove(s)
            
            self.log_trade(s)
            self.interface.notify(f"CLOSED {s['strategy']} {s['symbol']}: ${pnl:.4f}")
            self.interface.update_ui()

        def _on_fail(err):
             self.interface.notify(f"CLOSE FAILED {s['symbol']}: {err}")

        self.interface.notify(f"CLOSING {s['strategy']} {s['symbol']} ({reason})...")
        self.interface.run_worker(
            self.smart_execute(s['symbol'], side, s['quantity'], not maker, _on_success, _on_fail, position_side=position_side)
        )

    def update_strategies(self):
        current_time = time.time()
        
        # ENTRY LOGIC
        for cand in self.viable_pairs:
            symbol = cand['symbol']
            if not cand['next_funding_time']: continue
            
            funding_ts = float(cand['next_funding_time']) / 1000
            diff = funding_ts - current_time
            direction = cand['direction']
            
            # STRADDLE: Start 30s before funding. Market.
            if 28 < diff <= 30: 
                self.execute_strategy_entry("STRADDLE", symbol, direction, maker=False)

        # EXIT LOGIC
        for s in list(self.active_strategies):
            symbol = s['symbol']
            strategy = s['strategy']
            
            ticker = self.ticker_map.get(symbol)
            if not ticker: continue
            
            cand = next((x for x in self.viable_pairs if x['symbol'] == symbol), None)
            
            # TP/SL Logic
            current_price = ticker['bid'] if s['direction'] == "LONG" else ticker['ask']
            pnl_pct = (current_price - s['entry_price'])/s['entry_price'] if s['direction'] == "LONG" else (s['entry_price'] - current_price)/s['entry_price']
            
            # SL (-1%)
            if pnl_pct < -0.01:
                self.execute_strategy_exit(s, "SL (-1%)", maker=False)

            # Trailing TP Logic (Trigger @ 1.5%, Trail 0.1%)
            elif s.get('trailing_active', False):
                # We are in trailing mode, check for pullback
                if s['direction'] == "LONG":
                    if current_price > s['extreme_price']: s['extreme_price'] = current_price
                    # 0.1% pullback from extreme
                    if current_price < s['extreme_price'] * (1 - 0.001):
                        self.execute_strategy_exit(s, f"Trailing TP (Hit {current_price:.4f})", maker=False)
                else: # SHORT
                    if current_price < s['extreme_price']: s['extreme_price'] = current_price
                    # 0.1% pullback from extreme
                    if current_price > s['extreme_price'] * (1 + 0.001):
                        self.execute_strategy_exit(s, f"Trailing TP (Hit {current_price:.4f})", maker=False)

            elif pnl_pct >= 0.015:
                # Activate trailing
                s['trailing_active'] = True
                s['extreme_price'] = current_price
                self.interface.notify(f"TRAILING ACTIVATED {s['symbol']} (PnL: {pnl_pct*100:.2f}%)")
            
            # Time-based Exit
            elif cand:
                funding_ts = float(cand['next_funding_time']) / 1000
                diff = funding_ts - current_time
                
                if strategy == "STRADDLE" and (current_time - s['entry_time']) > 65:
                     self.execute_strategy_exit(s, "Time Exit", maker=True)

        self.interface.update_ui()


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

    def log_message(self, msg):
        print(f"[LOG] {msg}")
        
    def update_ui(self):
        pass
        
    def run(self):
        self.logic = FundingLogic(self)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting REAL TRADING Bot (LIVE Mode)...")
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

class FundingApp(App):
    CSS = """
    Screen { layout: vertical; }
    DataTable { height: 1fr; border: solid red; } /* Red border for REAL mode */
    .section_title { background: $primary; color: white; text-align: center; text-style: bold; height: 1; }
    Log { height: 30%; border-top: solid $primary; background: $surface; display: none; }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"), 
        ("r", "refresh_all", "Refresh"), 
        ("c", "clear_history", "Clear Hist"),
        ("l", "toggle_log", "Toggle Log")
    ]

    def __init__(self):
        super().__init__()
        self.logic = FundingLogic(self)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f"REAL TRADING (LIVE MODE) | Balance: ...", id="balance_display", classes="section_title")
        yield Vertical(
            Static("Market Scanner", classes="section_title"),
            DataTable(id="scanner_table"),
            Static("Active Strategies & History", classes="section_title"),
            DataTable(id="sim_table"),
            Log(id="debug_log")
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#scanner_table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Symbol", "Price", "Funding", "Dir", "Countdown", "Spread")

        sim_table = self.query_one("#sim_table", DataTable)
        sim_table.cursor_type = "row"
        sim_table.add_columns("Strategy", "Symbol", "Status", "Dir", "Entry", "Exit", "PnL")

        self.logic.start()
        self.notify("Press 'L' to toggle Debug Logs")

    def action_clear_history(self): self.logic.clear_history()
    def action_refresh_all(self): self.logic.refresh_all()
    
    def action_toggle_log(self):
        log = self.query_one("#debug_log", Log)
        log.styles.display = "block" if log.styles.display == "none" else "none"

    def log_message(self, msg):
        self.query_one("#debug_log", Log).write_line(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

    def notify(self, msg, title="", severity="information", timeout=3.0):
        # Log all notifications
        self.log_message(f"NOTIFY: {msg}")
        super().notify(msg, title=title, severity=severity, timeout=timeout)

    def update_ui(self):
        self.query_one("#balance_display", Static).update(f"REAL TRADING (LIVE MODE) | Balance: ${self.logic.balance:.2f}")
        self.update_scanner_table()
        self.update_sim_table()

    def run_worker(self, func, thread=True):
        return super().run_worker(func, thread=thread)

    def update_scanner_table(self):
        table = self.query_one("#scanner_table", DataTable)
        rows = []
        for c in self.logic.viable_pairs[:20]:
            sym = c['symbol']
            tik = self.logic.ticker_map.get(sym, {'bid':0,'ask':0})
            spr = (tik['ask']-tik['bid'])/tik['ask'] if tik['ask']>0 else 0
            
            cd = "N/A"
            if c['next_funding_time']:
                d = (float(c['next_funding_time'])/1000) - time.time()
                if d > 0: cd = str(timedelta(seconds=int(d)))
                else: cd = "FUNDING"
            
            rows.append((sym, f"{tik['ask']:.4f}", f"{c['funding_rate']*100:.4f}%", c['direction'], cd, f"{spr*100:.4f}%"))
        table.clear()
        table.add_rows(rows)

    def update_sim_table(self):
        table = self.query_one("#sim_table", DataTable)
        rows = []
        sims = self.logic.active_strategies + sorted(self.logic.closed_strategies, key=lambda x: x['entry_time'], reverse=True)[:10]
        for s in sims:
            rows.append((s['strategy'], s['symbol'], s['status'], s['direction'], f"{s['entry_price']:.4f}", f"{s.get('exit_price',0):.4f}", f"${s.get('net_pnl_amt',0):.4f}"))
        table.clear()
        table.add_rows(rows)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument("--testnet", action="store_true", help="Use testnet (NOT RECOMMENDED)")
    args = parser.parse_args()

    if args.testnet:
        BASE_URL = "https://fapi.asterdex-testnet.com"
        print("WARNING: Using Testnet")

    killer_process = None
    try:
        # Ensure logs directory exists
        if not os.path.exists("logs"):
            os.makedirs("logs")

        # Launch killer_bot.py in the background
        # We always run it headless to avoid TUI conflicts with the main bot
        cmd = [sys.executable, "killer_bot.py", "--headless"]
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Launching Companion: killer_bot.py...")

        
        # Open log file for the background process
        with open("logs/killer_bot.log", "a") as log_file:
            log_file.write(f"\n[{datetime.now()}] STARTING KILLER BOT SESSION\n")
            killer_process = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)

        if args.headless:
            HeadlessInterface().run()
        else:
            app = FundingApp()
            app.run()
            
    finally:
        # Ensure killer bot is terminated when we exit
        if killer_process:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Terminating Companion: killer_bot.py...")
            killer_process.terminate()
            try:
                killer_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                killer_process.kill()
