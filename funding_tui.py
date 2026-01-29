import argparse
import hashlib
import hmac
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from queue import Queue
from urllib.parse import urlencode

import pandas as pd
import requests

try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Container, Vertical
    from textual.widgets import DataTable, Footer, Header, Static
    from textual.worker import Worker
except ImportError:
    # Allow running headless without textual installed
    App = object
    ComposeResult = None
    pass

# ==========================================
# CONFIGURATION
# ==========================================
BASE_URL = "https://fapi.asterdex.com"
API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')

# Defaults
DEFAULT_MAKER = 0.0002
DEFAULT_TAKER = 0.0004
MIN_PROFIT_BUFFER = 0.0002  # Only simulate if est. profit > 0.02%

# Simulation Config
SIM_ENTRY_SECONDS = 10  # Enter 10s before funding
SIM_EXIT_SECONDS = 5    # Exit 5s after funding

class FundingLogic:
    def __init__(self, interface):
        self.interface = interface
        self.balance = 1000.0
        self.trade_size = 50.0
        self.viable_pairs = []
        self.fee_cache = {}
        self.ticker_map = {}
        self.ticker_stats_cache = {} # Symbol -> {'quoteVolume': 0.0}
        self.rsi_cache = {}
        self.rsi_entry_tracker = {} # Symbol -> {'type': 'OVER', 'extreme': 90.0}
        self.fee_queue = set()
        self.kline_queue = set()
        
        # Simulations: List of active/closed trade dicts
        self.active_sims = [] 
        self.closed_sims = []
        self.sim_counter = 0
        
        # Initialize log file
        if not os.path.exists("sim_trades.csv"):
            with open("sim_trades.csv", "w") as f:
                f.write("Timestamp,Strategy,Symbol,Direction,Entry,Exit,Payout,Net_PnL_Pct,Net_PnL_USDT,Balance_After\n")

    def start(self):
        self.interface.set_interval(60.0, self.fetch_premiums)
        self.interface.set_interval(60.0, self.fetch_24hr_stats)
        self.interface.set_interval(1.0, self.fetch_tickers)
        self.interface.set_interval(1.0, self.update_simulations)
        self.interface.set_interval(2.0, self.process_fee_queue)
        self.interface.set_interval(3.0, self.process_kline_queue)
        self.refresh_all()

    def refresh_all(self):
        self.interface.run_worker(self.fetch_premiums_worker)
        self.interface.run_worker(self.fetch_tickers_worker)
        self.interface.run_worker(self.fetch_24hr_stats_worker)

    def clear_sims(self):
        self.active_sims = []
        self.closed_sims = []
        self.interface.update_ui()

    def fetch_premiums(self): self.interface.run_worker(self.fetch_premiums_worker)
    def fetch_tickers(self): self.interface.run_worker(self.fetch_tickers_worker)
    def fetch_24hr_stats(self): self.interface.run_worker(self.fetch_24hr_stats_worker)
    def process_fee_queue(self):
        if self.fee_queue: self.interface.run_worker(self.fetch_fee_worker(self.fee_queue.pop()))
    def process_kline_queue(self):
        for _ in range(15):
            if self.kline_queue: self.interface.run_worker(self.fetch_kline_worker(self.kline_queue.pop()))

    def fetch_premiums_worker(self):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v3/premiumIndex", timeout=10)
            if resp.status_code == 200: self.interface.call_from_thread(self.process_premiums, resp.json())
        except: pass

    def fetch_tickers_worker(self):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v1/ticker/bookTicker", timeout=5)
            if resp.status_code == 200: self.interface.call_from_thread(self.process_tickers, resp.json())
        except: pass

    def fetch_24hr_stats_worker(self):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v1/ticker/24hr", timeout=10)
            if resp.status_code == 200: self.interface.call_from_thread(self.process_24hr_stats, resp.json())
        except: pass

    def fetch_fee_worker(self, symbol):
        def _work():
            if not API_KEY or not API_SECRET: return
            try:
                params = {'symbol': symbol, 'timestamp': int(time.time()*1000), 'recvWindow': 5000}
                params['signature'] = hmac.new(API_SECRET.encode('utf-8'), urlencode(params).encode('utf-8'), hashlib.sha256).hexdigest()
                headers = {'X-MBX-APIKEY': API_KEY}
                resp = requests.get(f"{BASE_URL}/fapi/v1/commissionRate", headers=headers, params=params, timeout=5)
                if resp.status_code == 200:
                    d = resp.json()
                    self.interface.call_from_thread(self.update_fee_cache, symbol, float(d.get("makerCommissionRate", DEFAULT_MAKER)), float(d.get("takerCommissionRate", DEFAULT_TAKER)))
            except: pass
        return _work

    def fetch_kline_worker(self, symbol):
        def _work():
            try:
                resp = requests.get(f"{BASE_URL}/fapi/v1/klines", params={'symbol': symbol, 'interval': '15m', 'limit': 100}, timeout=5)
                if resp.status_code == 200:
                    closes = [float(x[4]) for x in resp.json()]
                    if len(closes) > 14:
                        self.interface.call_from_thread(self.update_rsi_cache, symbol, self.calculate_rsi(closes))
            except: pass
        return _work

    def calculate_rsi(self, closes, period=14):
        series = pd.Series(closes)
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]

    def process_tickers(self, data):
        for t in data:
            self.ticker_map[t['symbol']] = {'bid': float(t.get('bidPrice', 0)), 'ask': float(t.get('askPrice', 0))}
        self.interface.update_ui()
    
    def process_24hr_stats(self, data):
        for t in data:
            self.ticker_stats_cache[t['symbol']] = {'quoteVolume': float(t.get('quoteVolume', 0))}

    def update_fee_cache(self, symbol, maker, taker):
        self.fee_cache[symbol] = {'maker': maker, 'taker': taker}
        self.interface.update_ui()

    def update_rsi_cache(self, symbol, rsi):
        self.rsi_cache[symbol] = rsi
        self.interface.update_ui()

    def process_premiums(self, data):
        candidates = []
        for p in data:
            sym = p['symbol']
            try:
                rate = float(p.get('lastFundingRate', 0))
                nxt = p.get('nextFundingTime')
            except: continue
            
            if sym.endswith("USDT"):
                if sym not in self.kline_queue: self.kline_queue.add(sym)

            if abs(rate) > (DEFAULT_MAKER*2 + MIN_PROFIT_BUFFER):
                tik = self.ticker_map.get(sym)
                if tik and tik['ask'] > 0:
                    spread = (tik['ask'] - tik['bid']) / tik['ask']
                    fees = self.fee_cache.get(sym, {'maker': DEFAULT_MAKER, 'taker': DEFAULT_TAKER})
                    
                    if spread > 0.005: continue
                    est_profit = abs(rate) - (fees['taker'] * 2)
                    if spread > est_profit: continue

                if sym not in self.fee_cache and sym not in self.fee_queue: self.fee_queue.add(sym)
                candidates.append({'symbol': sym, 'funding_rate': rate, 'next_funding_time': nxt, 'direction': "SHORT" if rate > 0 else "LONG"})
        
        candidates.sort(key=lambda x: abs(x['funding_rate']), reverse=True)
        self.viable_pairs = candidates
        self.interface.update_ui()

    def log_trade(self, sim):
        with open("sim_trades.csv", "a") as f:
            f.write(f"{datetime.now()},{sim['strategy']},{sim['symbol']},{sim['direction']},{sim['entry_price']},{sim['exit_price']},{sim['funding_payout_pct']:.6f},{sim['net_pnl_pct']:.6f},{sim['net_pnl_amt']:.4f},{self.balance:.4f}\n")

    def open_trade(self, strategy, symbol, direction, maker_entry=False):
        for s in self.active_sims:
            if s['symbol'] == symbol and s['strategy'] == strategy and s['status'] == 'OPEN':
                return

        if self.balance < self.trade_size:
            self.interface.notify(f"SKIP {strategy} {symbol}: Low Balance (${self.balance:.2f})")
            return

        ticker = self.ticker_map.get(symbol)
        if not ticker: return
        
        fees = self.fee_cache.get(symbol, {'maker': DEFAULT_MAKER, 'taker': DEFAULT_TAKER})
        
        if maker_entry:
            entry_price = ticker['bid'] if direction == "LONG" else ticker['ask']
            entry_fee_rate = fees['maker']
        else:
            entry_price = ticker['ask'] if direction == "LONG" else ticker['bid']
            entry_fee_rate = fees['taker']
        
        quantity = self.trade_size / entry_price
        entry_fee_amt = self.trade_size * entry_fee_rate
        
        # Deduct Margin + Fee
        self.balance -= (self.trade_size + entry_fee_amt)

        sim = {
            'id': self.sim_counter,
            'strategy': strategy,
            'symbol': symbol,
            'status': 'OPEN',
            'direction': direction,
            'entry_price': entry_price,
            'entry_time': time.time(),
            'quantity': quantity,
            'margin': self.trade_size,
            'entry_fee_rate': entry_fee_rate,
            'entry_fee_amt': entry_fee_amt,
            'maker_fee_rate': fees['maker'],
            'taker_fee_rate': fees['taker'],
            'funding_payout_pct': 0.0,
        }
        self.sim_counter += 1
        self.active_sims.append(sim)
        self.interface.notify(f"OPEN {strategy} {symbol} @ {entry_price} (Bal: {self.balance:.2f})")

    def close_trade(self, sim, reason="Exit", maker_exit=False, override_price=None):
        ticker = self.ticker_map.get(sim['symbol'])
        if not ticker and override_price is None: return
        
        if override_price is not None:
            exit_price = override_price
        else:
            if maker_exit:
                exit_price = ticker['ask'] if sim['direction'] == "LONG" else ticker['bid']
            else:
                exit_price = ticker['bid'] if sim['direction'] == "LONG" else ticker['ask']
        
        # Calculations
        entry_val = sim['margin']
        exit_val = sim['quantity'] * exit_price
        
        if sim['direction'] == "LONG":
            pnl_amt = exit_val - entry_val
            price_pnl = (exit_price - sim['entry_price']) / sim['entry_price']
        else:
            pnl_amt = entry_val - exit_val
            price_pnl = (sim['entry_price'] - exit_price) / sim['entry_price']
            
        exit_fee_rate = sim['maker_fee_rate'] if maker_exit else sim['taker_fee_rate']
        exit_fee_amt = exit_val * exit_fee_rate
        
        funding_amt = sim['margin'] * sim['funding_payout_pct']
        
        net_change = pnl_amt + funding_amt - exit_fee_amt
        return_amount = sim['margin'] + net_change # Return margin +/- PnL
        
        self.balance += return_amount
        
        sim['status'] = 'CLOSED'
        sim['exit_price'] = exit_price
        sim['exit_time'] = time.time()
        sim['price_pnl_pct'] = price_pnl
        sim['net_pnl_pct'] = net_change / sim['margin'] # ROI
        sim['net_pnl_amt'] = net_change
        sim['reason'] = reason
        
        self.closed_sims.append(sim)
        if sim in self.active_sims:
            self.active_sims.remove(sim)
            
        self.log_trade(sim)
        self.interface.notify(f"CLOSE {sim['strategy']} {sim['symbol']}: ${net_change:.4f} (Bal: {self.balance:.2f})")

    def update_simulations(self):
        current_time = time.time()
        
        # 2. FUNDING STRATEGIES
        for cand in self.viable_pairs:
            symbol = cand['symbol']
            if not cand['next_funding_time']: continue
            
            funding_ts = float(cand['next_funding_time']) / 1000
            diff = funding_ts - current_time
            rate = cand['funding_rate']
            direction = cand['direction']
            
            # STRADDLE: Start 30s before funding. Maker.
            if 28 < diff <= 30: 
                self.open_trade("STRADDLE", symbol, direction, maker_entry=True)

            # FRONT_RUN: 55-60s before. Maker.
            if 55 < diff <= 60: 
                self.open_trade("FRONT_RUN", symbol, direction, maker_entry=True)
            
            # SNIPE: Taker (Default)
            if 0 < diff <= 1:
                self.open_trade("SNIPE", symbol, direction)
                for s in self.active_sims:
                    if s['symbol'] == symbol and s['strategy'] == "SNIPE" and s['status'] == 'OPEN':
                        s['pending_funding_rate'] = rate
            
            # DIP_BUY: Taker (Default)
            if 0 < diff <= 1: 
                self.open_trade("DIP_BUY", symbol, direction)

        # 3. MANAGE ACTIVE
        for sim in list(self.active_sims):
            symbol = sim['symbol']
            strategy = sim['strategy']
            
            ticker = self.ticker_map.get(symbol)
            if not ticker: continue
            
            current_price = ticker['bid'] if sim['direction'] == "LONG" else ticker['ask']

            # Check 1% TP/SL (Limit - Maker Fee)
            if strategy in ["FRONT_RUN", "DIP_BUY", "STRADDLE"]:
                tp_price = sim['entry_price'] * (1.01 if sim['direction'] == "LONG" else 0.99)
                sl_price = sim['entry_price'] * (0.99 if sim['direction'] == "LONG" else 1.01)
                
                # Limit TP Check
                tp_hit = False
                if sim['direction'] == "LONG":
                    if ticker['bid'] >= tp_price: tp_hit = True
                else:
                    if ticker['ask'] <= tp_price: tp_hit = True
                
                if tp_hit:
                    self.close_trade(sim, "TP (+1%)", maker_exit=True, override_price=tp_price)
                    continue

                # Limit SL Check
                sl_hit = False
                if sim['direction'] == "LONG":
                    if ticker['bid'] <= sl_price: sl_hit = True
                else:
                    if ticker['ask'] >= sl_price: sl_hit = True
                
                if sl_hit:
                    self.close_trade(sim, "SL (-1%)", maker_exit=True, override_price=sl_price)
                    continue

            # Standard Logic
            cand = next((x for x in self.viable_pairs if x['symbol'] == symbol), None)
            if cand:
                funding_ts = float(cand['next_funding_time']) / 1000
                diff = funding_ts - current_time
                
                if diff < 0 and sim.get('funding_payout_pct') == 0.0 and strategy in ["SNIPE", "FRONT_RUN", "DIP_BUY", "STRADDLE"]:
                    rate = sim.get('pending_funding_rate', cand['funding_rate'])
                    dir_sign = 1 if sim['direction'] == "LONG" else -1
                    sim['funding_payout_pct'] = -1 * dir_sign * rate
                
                if strategy == "FRONT_RUN":
                    if (current_time - sim['entry_time']) > 61: self.close_trade(sim, "Time Exit")
                elif strategy == "SNIPE":
                    if diff < -1: self.close_trade(sim, "Time Exit")
                elif strategy == "DIP_BUY":
                    if (current_time - sim['entry_time']) > 60: self.close_trade(sim, "Time Exit")
                elif strategy == "STRADDLE":
                    if (current_time - sim['entry_time']) > 60: self.close_trade(sim, "Time Exit (1m)", maker_exit=True)
        
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
        
    def update_ui(self):
        pass
        
    def run(self):
        self.logic = FundingLogic(self)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting Headless Funding Bot...")
        self.logic.start()
        
        try:
            while self.running:
                # Process main thread callbacks
                while not self.queue.empty():
                    try:
                        func, args = self.queue.get_nowait()
                        func(*args)
                    except: pass
                
                # Check intervals
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
    Screen {
        layout: vertical;
    }
    DataTable {
        height: 1fr;
        border: solid green;
    }
    .section_title {
        background: $primary;
        color: white;
        text-align: center;
        text-style: bold;
        height: 1;
    }
    """
    
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_all", "Force Refresh"),
        ("c", "clear_sims", "Clear Sims"),
    ]

    def __init__(self):
        super().__init__()
        self.logic = FundingLogic(self)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Balance: $100.00", id="balance_display", classes="section_title")
        yield Vertical(
            Static("Market Scanner (RSI + Funding)", classes="section_title"),
            DataTable(id="scanner_table"),
            Static("Multi-Strategy Simulation (Funding Snipe, Front-Run, Dip-Buy, RSI Scalp)", classes="section_title"),
            DataTable(id="sim_table"),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#scanner_table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Symbol", "Price", "Funding", "Dir", "Countdown", "Spread", "RSI", "Earn(Mk/Mk)", "Earn(Tk/Tk)", "Earn(Mix)")

        sim_table = self.query_one("#sim_table", DataTable)
        sim_table.cursor_type = "row"
        sim_table.add_columns("Strategy", "Symbol", "Status", "Dir", "Entry", "Exit", "Payout", "Price PnL", "Net PnL %")

        self.logic.start()

    def action_clear_sims(self):
        self.logic.clear_sims()

    def action_refresh_all(self):
        self.logic.refresh_all()

    # Interface Methods (called by Logic)
    def update_ui(self):
        self.query_one("#balance_display", Static).update(f"Balance: ${self.logic.balance:.4f}")
        self.update_scanner_table()
        self.update_sim_table()

    # run_worker and call_from_thread are provided by App but we need to ensure signature matching if Logic uses them.
    # Logic calls: interface.run_worker(func). App.run_worker(func, thread=False/True, ...).
    # App.run_worker signature: (self, callback: Callable, *, name: str | None = None, group: str | None = None, description: str | None = None, thread: bool = False, ...) -> Worker
    # We need thread=True default if Logic expects it.
    
    def run_worker(self, func, thread=True):
        # Override or wrap? Textual's run_worker doesn't default thread=True.
        # But we can just use super().run_worker(func, thread=True)
        return super().run_worker(func, thread=thread)

    def update_scanner_table(self):
        table = self.query_one("#scanner_table", DataTable)
        rows = []
        for c in self.logic.viable_pairs[:20]:
            sym = c['symbol']
            fees = self.logic.fee_cache.get(sym, {'maker': DEFAULT_MAKER, 'taker': DEFAULT_TAKER})
            tik = self.logic.ticker_map.get(sym, {'bid':0,'ask':0})
            spr = (tik['ask']-tik['bid'])/tik['ask'] if tik['ask']>0 else 0
            
            # Countdown
            cd = "N/A"
            if c['next_funding_time']:
                d = (float(c['next_funding_time'])/1000) - time.time()
                if d > 0: cd = str(timedelta(seconds=int(d)))
                else: cd = "FUNDING"
            
            rsi = self.logic.rsi_cache.get(sym)
            rsi_s = f"{rsi:.1f}" if rsi else "N/A"
            
            fund = abs(c['funding_rate'])
            e1 = fund - fees['maker']*2
            e2 = fund - fees['taker']*2 - spr
            e3 = fund - (fees['maker']+fees['taker']) - spr/2
            
            rows.append((
                sym, f"{tik['ask']:.4f}", f"{c['funding_rate']*100:.4f}%", c['direction'],
                cd, f"{spr*100:.4f}%", rsi_s, 
                f"{e1*100:.4f}%", f"{e2*100:.4f}%", f"{e3*100:.4f}%"
            ))
        table.clear()
        table.add_rows(rows)

    def update_sim_table(self):
        table = self.query_one("#sim_table", DataTable)
        rows = []
        # Sort active first
        sims = self.logic.active_sims + sorted(self.logic.closed_sims, key=lambda x: x['entry_time'], reverse=True)[:10]
        
        for s in sims:
            status = s['status']
            sym = s['symbol']
            tik = self.logic.ticker_map.get(sym)
            curr = tik['bid'] if s['direction'] == "LONG" else tik['ask'] if tik else s['entry_price']
            
            if status == "OPEN":
                # Float PnL
                if s['direction'] == "LONG": pnl = (curr - s['entry_price'])/s['entry_price']
                else: pnl = (s['entry_price'] - curr)/s['entry_price']
                
                net = pnl + s.get('funding_payout_pct', 0.0) - s['entry_fee_rate'] # One fee paid so far
                rows.append((s['strategy'], sym, "OPEN", s['direction'], f"{s['entry_price']:.4f}", f"{curr:.4f}", f"{s.get('funding_payout_pct',0)*100:.4f}%", f"{pnl*100:.2f}%", f"{net*100:.2f}%"))
            else:
                rows.append((s['strategy'], sym, "CLOSED", s['direction'], f"{s['entry_price']:.4f}", f"{s['exit_price']:.4f}", f"{s.get('funding_payout_pct',0)*100:.4f}%", f"{s['price_pnl_pct']*100:.2f}%", f"{s['net_pnl_pct']*100:.2f}%"))
                
        table.clear()
        table.add_rows(rows)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    args = parser.parse_args()

    if args.headless:
        HeadlessInterface().run()
    else:
        app = FundingApp()
        app.run()
