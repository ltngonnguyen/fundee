import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pandas as pd
import requests
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import DataTable, Footer, Header, Static
from textual.worker import Worker

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
        self.viable_pairs = []
        self.fee_cache = {}
        self.ticker_map = {}
        self.rsi_cache = {}
        self.rsi_entry_tracker = {} # Symbol -> {'type': 'OVER', 'extreme': 90.0}
        self.fee_queue = set()
        self.kline_queue = set()
        
        # Simulations: List of active/closed trade dicts
        # Keys: id, strategy, symbol, status, direction, entry_price, entry_time, ...
        self.active_sims = [] 
        self.closed_sims = []
        self.sim_counter = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
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

        self.set_interval(60.0, self.fetch_premiums)
        self.set_interval(1.0, self.fetch_tickers)
        self.set_interval(1.0, self.update_simulations)
        self.set_interval(2.0, self.process_fee_queue)
        self.set_interval(3.0, self.process_kline_queue)

        self.action_refresh_all()
        
        # Initialize log file
        if not os.path.exists("sim_trades.csv"):
            with open("sim_trades.csv", "w") as f:
                f.write("Timestamp,Strategy,Symbol,Direction,Entry,Exit,Payout,Net_PnL\n")

    def action_clear_sims(self):
        self.active_sims = []
        self.closed_sims = []
        self.update_sim_table()

    def action_refresh_all(self):
        self.run_worker(self.fetch_premiums_worker, thread=True)
        self.run_worker(self.fetch_tickers_worker, thread=True)

    def fetch_premiums(self): self.run_worker(self.fetch_premiums_worker, thread=True)
    def fetch_tickers(self): self.run_worker(self.fetch_tickers_worker, thread=True)
    def process_fee_queue(self):
        if self.fee_queue: self.run_worker(self.fetch_fee_worker(self.fee_queue.pop()), thread=True)
    def process_kline_queue(self):
        # Process batch of 15 to cycle through all pairs within ~60s
        for _ in range(15):
            if self.kline_queue: self.run_worker(self.fetch_kline_worker(self.kline_queue.pop()), thread=True)

    def fetch_premiums_worker(self):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v3/premiumIndex", timeout=10)
            if resp.status_code == 200: self.call_from_thread(self.process_premiums, resp.json())
        except: pass

    def fetch_tickers_worker(self):
        try:
            resp = requests.get(f"{BASE_URL}/fapi/v1/ticker/bookTicker", timeout=5)
            if resp.status_code == 200: self.call_from_thread(self.process_tickers, resp.json())
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
                    self.call_from_thread(self.update_fee_cache, symbol, float(d.get("makerCommissionRate", DEFAULT_MAKER)), float(d.get("takerCommissionRate", DEFAULT_TAKER)))
            except: pass
        return _work

    def fetch_kline_worker(self, symbol):
        def _work():
            try:
                resp = requests.get(f"{BASE_URL}/fapi/v1/klines", params={'symbol': symbol, 'interval': '15m', 'limit': 100}, timeout=5)
                if resp.status_code == 200:
                    closes = [float(x[4]) for x in resp.json()]
                    if len(closes) > 14:
                        self.call_from_thread(self.update_rsi_cache, symbol, self.calculate_rsi(closes))
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
        self.update_scanner_table()

    def update_fee_cache(self, symbol, maker, taker):
        self.fee_cache[symbol] = {'maker': maker, 'taker': taker}
        self.update_scanner_table()

    def update_rsi_cache(self, symbol, rsi):
        self.rsi_cache[symbol] = rsi
        self.update_scanner_table()

    def process_premiums(self, data):
        candidates = []
        for p in data:
            sym = p['symbol']
            try:
                rate = float(p.get('lastFundingRate', 0))
                nxt = p.get('nextFundingTime')
            except: continue
            
            # Always track RSI for USDT pairs (Update every cycle)
            if sym.endswith("USDT"):
                if sym not in self.kline_queue: self.kline_queue.add(sym)

            if abs(rate) > (DEFAULT_MAKER*2 + MIN_PROFIT_BUFFER):
                if sym not in self.fee_cache and sym not in self.fee_queue: self.fee_queue.add(sym)
                candidates.append({'symbol': sym, 'funding_rate': rate, 'next_funding_time': nxt, 'direction': "SHORT" if rate > 0 else "LONG"})
        
        candidates.sort(key=lambda x: abs(x['funding_rate']), reverse=True)
        self.viable_pairs = candidates
        self.update_scanner_table()

    # ==========================================
    # STRATEGY ENGINE
    # ==========================================
    
    def log_trade(self, sim):
        with open("sim_trades.csv", "a") as f:
            f.write(f"{datetime.now()},{sim['strategy']},{sim['symbol']},{sim['direction']},{sim['entry_price']},{sim['exit_price']},{sim['funding_payout_pct']:.6f},{sim['net_pnl_pct']:.6f}\n")

    def open_trade(self, strategy, symbol, direction):
        # Prevent duplicate open trades for same strategy/symbol
        for s in self.active_sims:
            if s['symbol'] == symbol and s['strategy'] == strategy and s['status'] == 'OPEN':
                return

        ticker = self.ticker_map.get(symbol)
        if not ticker: return
        
        # Taker Entry
        entry_price = ticker['ask'] if direction == "LONG" else ticker['bid']
        fees = self.fee_cache.get(symbol, {'maker': DEFAULT_MAKER, 'taker': DEFAULT_TAKER})
        
        sim = {
            'id': self.sim_counter,
            'strategy': strategy,
            'symbol': symbol,
            'status': 'OPEN',
            'direction': direction,
            'entry_price': entry_price,
            'entry_time': time.time(),
            'taker_fee': fees['taker'],
            'funding_payout_pct': 0.0,
            
            # Strategy Specifics
            'highest_rsi': self.rsi_cache.get(symbol, 50), # For trailing
            'lowest_rsi': self.rsi_cache.get(symbol, 50),
        }
        self.sim_counter += 1
        self.active_sims.append(sim)
        self.notify(f"OPEN {strategy} {symbol} @ {entry_price}")

    def close_trade(self, sim, reason="Exit"):
        ticker = self.ticker_map.get(sim['symbol'])
        if not ticker: return
        
        exit_price = ticker['bid'] if sim['direction'] == "LONG" else ticker['ask']
        
        # PnL Calc
        if sim['direction'] == "LONG":
            price_pnl = (exit_price - sim['entry_price']) / sim['entry_price']
        else:
            price_pnl = (sim['entry_price'] - exit_price) / sim['entry_price']
            
        fees = sim['taker_fee'] * 2
        net_pnl = price_pnl + sim['funding_payout_pct'] - fees
        
        sim['status'] = 'CLOSED'
        sim['exit_price'] = exit_price
        sim['exit_time'] = time.time()
        sim['price_pnl_pct'] = price_pnl
        sim['net_pnl_pct'] = net_pnl
        sim['reason'] = reason
        
        self.closed_sims.append(sim)
        if sim in self.active_sims:
            self.active_sims.remove(sim)
            
        self.log_trade(sim)
        self.notify(f"CLOSE {sim['strategy']} {sim['symbol']}: {net_pnl*100:.2f}%")

    def update_simulations(self):
        current_time = time.time()
        
        # 1. RSI SCALP STRATEGY (Scanner based + Trailing Entry)
        for symbol, rsi in self.rsi_cache.items():
            # Track Extremes
            if rsi > 90:
                if symbol not in self.rsi_entry_tracker or self.rsi_entry_tracker[symbol]['type'] != 'OVERBOUGHT':
                    self.rsi_entry_tracker[symbol] = {'type': 'OVERBOUGHT', 'extreme': rsi}
                else:
                    if rsi > self.rsi_entry_tracker[symbol]['extreme']:
                        self.rsi_entry_tracker[symbol]['extreme'] = rsi
            
            elif rsi < 10:
                if symbol not in self.rsi_entry_tracker or self.rsi_entry_tracker[symbol]['type'] != 'OVERSOLD':
                    self.rsi_entry_tracker[symbol] = {'type': 'OVERSOLD', 'extreme': rsi}
                else:
                    if rsi < self.rsi_entry_tracker[symbol]['extreme']:
                        self.rsi_entry_tracker[symbol]['extreme'] = rsi
            
            # Check Reversal Triggers
            if symbol in self.rsi_entry_tracker:
                data = self.rsi_entry_tracker[symbol]
                
                # Setup: OVERBOUGHT (Short) -> Trigger if RSI drops 2 points from Peak
                if data['type'] == 'OVERBOUGHT':
                    if rsi < (data['extreme'] - 2.0):
                        self.open_trade("RSI_SCALP", symbol, "SHORT")
                        del self.rsi_entry_tracker[symbol] # Reset tracker after entry
                    elif rsi < 50: # Reset if it normalized without triggering
                        del self.rsi_entry_tracker[symbol]

                # Setup: OVERSOLD (Long) -> Trigger if RSI rises 2 points from Bottom
                elif data['type'] == 'OVERSOLD':
                    if rsi > (data['extreme'] + 2.0):
                        self.open_trade("RSI_SCALP", symbol, "LONG")
                        del self.rsi_entry_tracker[symbol]
                    elif rsi > 50:
                        del self.rsi_entry_tracker[symbol]
                
        # 2. FUNDING STRATEGIES (Candidate based)
        for cand in self.viable_pairs:
            symbol = cand['symbol']
            if not cand['next_funding_time']: continue
            
            funding_ts = float(cand['next_funding_time']) / 1000
            diff = funding_ts - current_time
            rate = cand['funding_rate']
            direction = cand['direction']
            
            # STRAT: FRONT_RUN (Entry: T-300s, Exit: T-10s)
            if 290 < diff <= 300:
                self.open_trade("FRONT_RUN", symbol, direction)
            
            # STRAT: SNIPE (Entry: T-10s, Exit: T+5s)
            if 0 < diff <= 10:
                self.open_trade("SNIPE", symbol, direction)
                for s in self.active_sims:
                    if s['symbol'] == symbol and s['strategy'] == "SNIPE" and s['status'] == 'OPEN':
                        s['pending_funding_rate'] = rate

            # STRAT: DIP_BUY (Entry: T+10s, Exit: T+60s)
            if -15 <= diff < -10:
                self.open_trade("DIP_BUY", symbol, direction)

        # 3. MANAGE ACTIVE TRADES
        for sim in list(self.active_sims):
            symbol = sim['symbol']
            strategy = sim['strategy']
            
            # Calculate current PnL for Stop Loss check
            ticker = self.ticker_map.get(symbol)
            current_price = ticker['bid'] if sim['direction'] == "LONG" else ticker['ask'] if ticker else sim['entry_price']
            
            if sim['direction'] == "LONG": pnl = (current_price - sim['entry_price']) / sim['entry_price']
            else: pnl = (sim['entry_price'] - current_price) / sim['entry_price']
            
            net_pnl = pnl + sim.get('funding_payout_pct', 0.0) - sim['taker_fee'] # Approx net
            
            # --- GLOBAL STOP LOSS ---
            if net_pnl < -0.05: # -5% Stop Loss
                self.close_trade(sim, "Stop Loss (-5%)")
                continue

            # --- RSI TRAILING LOGIC ---
            if strategy == "RSI_SCALP":
                current_rsi = self.rsi_cache.get(symbol)
                if not current_rsi: continue
                
                if sim['direction'] == "SHORT":
                    # Track lowest RSI seen
                    if current_rsi < sim['lowest_rsi']: sim['lowest_rsi'] = current_rsi
                    # Trail: Exit if RSI bounces 10 points from bottom OR is back to neutral 70
                    if (current_rsi > sim['lowest_rsi'] + 10) or (current_rsi < 70 and sim['entry_price'] > 0): 
                            if current_rsi < 70:
                                if current_rsi > sim['lowest_rsi'] + 10:
                                    self.close_trade(sim, "RSI Trail")
                    elif current_rsi > 95: 
                        pass
                else: # LONG
                    if current_rsi > sim['highest_rsi']: sim['highest_rsi'] = current_rsi
                    if current_rsi > 30: # Activated
                        if current_rsi < sim['highest_rsi'] - 10:
                            self.close_trade(sim, "RSI Trail")

            # --- FUNDING TIME LOGIC ---
            else:
                cand = next((x for x in self.viable_pairs if x['symbol'] == symbol), None)
                if not cand: continue
                
                funding_ts = float(cand['next_funding_time']) / 1000
                diff = funding_ts - current_time
                
                if diff < 0 and sim.get('funding_payout_pct') == 0.0 and strategy in ["SNIPE", "FRONT_RUN"]:
                    rate = sim.get('pending_funding_rate', cand['funding_rate'])
                    dir_sign = 1 if sim['direction'] == "LONG" else -1
                    sim['funding_payout_pct'] = -1 * dir_sign * rate
                
                if strategy == "FRONT_RUN":
                    if diff < 10: self.close_trade(sim, "Time Exit")
                
                elif strategy == "SNIPE":
                    if diff < -5: self.close_trade(sim, "Time Exit")
                        
                elif strategy == "DIP_BUY":
                    if diff < -60: self.close_trade(sim, "Time Exit")

        self.update_sim_table()

    def update_scanner_table(self):
        table = self.query_one("#scanner_table", DataTable)
        rows = []
        for c in self.viable_pairs[:20]:
            sym = c['symbol']
            fees = self.fee_cache.get(sym, {'maker': DEFAULT_MAKER, 'taker': DEFAULT_TAKER})
            tik = self.ticker_map.get(sym, {'bid':0,'ask':0})
            spr = (tik['ask']-tik['bid'])/tik['ask'] if tik['ask']>0 else 0
            
            # Countdown
            cd = "N/A"
            if c['next_funding_time']:
                d = (float(c['next_funding_time'])/1000) - time.time()
                if d > 0: cd = str(timedelta(seconds=int(d)))
                else: cd = "FUNDING"
            
            rsi = self.rsi_cache.get(sym)
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
        sims = self.active_sims + sorted(self.closed_sims, key=lambda x: x['entry_time'], reverse=True)[:10]
        
        for s in sims:
            status = s['status']
            sym = s['symbol']
            tik = self.ticker_map.get(sym)
            curr = tik['bid'] if s['direction'] == "LONG" else tik['ask'] if tik else s['entry_price']
            
            if status == "OPEN":
                # Float PnL
                if s['direction'] == "LONG": pnl = (curr - s['entry_price'])/s['entry_price']
                else: pnl = (s['entry_price'] - curr)/s['entry_price']
                
                net = pnl + s.get('funding_payout_pct', 0.0) - s['taker_fee'] # One fee paid
                rows.append((s['strategy'], sym, "OPEN", s['direction'], f"{s['entry_price']:.4f}", f"{curr:.4f}", f"{s.get('funding_payout_pct',0)*100:.4f}%", f"{pnl*100:.2f}%", f"{net*100:.2f}%"))
            else:
                rows.append((s['strategy'], sym, "CLOSED", s['direction'], f"{s['entry_price']:.4f}", f"{s['exit_price']:.4f}", f"{s.get('funding_payout_pct',0)*100:.4f}%", f"{s['price_pnl_pct']*100:.2f}%", f"{s['net_pnl_pct']*100:.2f}%"))
                
        table.clear()
        table.add_rows(rows)

if __name__ == "__main__":
    app = FundingApp()
    app.run()