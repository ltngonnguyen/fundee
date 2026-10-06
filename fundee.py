import argparse
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal
from queue import Queue

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, VerticalScroll
    from textual.widgets import DataTable, Footer, Header, Log, Static

    TEXTUAL_INSTALLED = True
except ImportError:
    # Allow running headless without textual installed
    App = object
    ComposeResult = None
    TEXTUAL_INSTALLED = False

from fundee_shared import BASE_URL, ExchangeInterface, SmartOrderExecutor

# ==========================================
# CONFIGURATION
# ==========================================
# BASE_URL imported from fundee_shared

TRADE_SIZE_USDC = Decimal("400.0")  # Size per trade in USDC
ACTIVE_STRATEGY = "APPROACH_B"  # Options: "STRADDLE", "APPROACH_B"

# Defaults
DEFAULT_MAKER = Decimal("0.00005")  # 0.005%
DEFAULT_TAKER = Decimal("0.0004")  # 0.04%
BALANCE_UTILIZATION_PERCENT = Decimal("0.99")
APPROACH_B_ENTRY_WINDOW = 1.0

QUOTE_CURRENCY = "USDC"
MIN_NOTIONAL = Decimal("5.5")


class FundeeLogic:
    def __init__(self, interface, volume_filter=True):
        self.interface = interface
        # Volume gate: when enabled, pairs with 24h quoteVolume < 500k are
        # excluded from viable_pairs. Disable via --no-volume or 'v' in TUI.
        self.volume_filter_enabled = volume_filter
        testnet = bool(os.getenv("HL_TESTNET"))
        self.exchange = ExchangeInterface(
            base_url=BASE_URL, logger=self.interface.log_message, testnet=testnet
        )
        self.balance = Decimal("0.0")
        # Last raw premiums payload, keyed by symbol (needed for next_funding_time
        # when rendering the scanner table from promising_pairs rather than
        # viable_pairs).
        self.premium_data = {}
        self.trade_size = TRADE_SIZE_USDC

        self.viable_pairs = []
        # Top 5 pairs by net yield (signed), populated on every fetch_premiums
        # cycle. Includes pairs below the entry threshold so the heartbeat
        # can show "almost promising" candidates the filter is rejecting.
        self.promising_pairs = []
        self.fee_cache = {}
        self.ticker_map = {}
        self.ticker_stats_cache = {}
        self.fee_queue = deque()  # Use deque for ordered processing
        self.pending_orders = set()
        self.ignored_dust = set()
        # Hyperliquid is one-way only (no hedge mode)
        self.is_hedge_mode = False
        self.is_fetching_tickers = False  # Prevent stacking requests

        # Track local strategy state
        self.active_strategies = []
        self.closed_strategies = []
        self.sim_counter = 0

        # Thread synchronization locks
        self._ticker_lock = threading.Lock()
        self._pending_orders_lock = threading.Lock()
        self._viable_pairs_lock = threading.Lock()
        self._fee_cache_lock = threading.Lock()
        self._fetching_tickers_lock = threading.Lock()
        self._active_strategies_lock = threading.Lock()
        self._fee_queue_lock = threading.Lock()

        # Log file setup with rotation
        self._setup_log_file()

    def _setup_log_file(self):
        """Setup log file with rotation support. Rotates file if > 10MB, keeping only 1 backup."""
        log_dir = "logs"
        log_file = os.path.join(log_dir, "trade_anchors.csv")
        header = "Timestamp,Strategy,Symbol,Direction,EntryTime,ExitTime,EntryPrice,ExitPrice,Status,Reason,Quantity\n"
        max_size = 10 * 1024 * 1024  # 10MB

        try:
            # Check if logs directory exists and is writable, create if needed
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)

            if not os.access(log_dir, os.W_OK):
                self.interface.log_message(
                    f"Error: logs directory is not writable: {log_dir}"
                )
                return

            # Check if log file exists and its size
            if os.path.exists(log_file):
                file_size = os.path.getsize(log_file)

                # If size > 10MB, rotate it
                if file_size > max_size:
                    rotated_file = log_file + ".1"

                    # Overwrite existing .1 file if it exists
                    if os.path.exists(rotated_file):
                        os.remove(rotated_file)

                    os.rename(log_file, rotated_file)
                    self.interface.log_message(
                        "Log file rotated: trade_anchors.csv -> trade_anchors.csv.1"
                    )

                    # Create new file with header
                    with open(log_file, "w") as f:
                        f.write(header)
            else:
                # File doesn't exist, create it with header
                with open(log_file, "w") as f:
                    f.write(header)

        except OSError as e:
            self.interface.log_message(f"Error setting up log file: {e}")
        except Exception as e:
            self.interface.log_message(f"Unexpected error in _setup_log_file: {e}")

    def start(self):
        self.interface.log_message(f"Starting Logic (Base: {BASE_URL})")
        self.interface.log_message("Position Mode: One-Way (Hyperliquid)")
        # Emit the verbose ccxt/exchange startup banner and load markets now
        # that the UI is composed and the log widget is reachable.
        self.exchange.start()

        self.interface.set_interval(1.0, self.fetch_premiums)
        self.interface.set_interval(60.0, self.fetch_24hr_stats)
        self.interface.set_interval(
            5.0, self.sync_balance_positions
        )  # Sync with exchange
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
        self.interface.call_from_thread(
            self.interface.log_message, "[fundee] sync: fetching balance + positions…"
        )
        try:
            bal = self.exchange.get_balance()
            positions = self.exchange.get_positions()
            n_pos = len(positions) if positions else 0
            self.interface.call_from_thread(
                self.interface.log_message,
                f"[fundee] sync: done — balance={bal} {QUOTE_CURRENCY}, "
                f"{n_pos} open position(s)",
            )
            self.interface.call_from_thread(self.update_account_state, bal, positions)
        except Exception as e:
            self.interface.call_from_thread(
                self.interface.log_message, f"[fundee] sync FAILED: {e}"
            )

    def update_account_state(self, balance, positions):
        if balance is not None:
            self.balance = balance
        if positions is not None:
            self.real_positions = positions
            # Clean up ignored dust if position changed or gone
            active_symbols = {p["symbol"]: float(p["positionAmt"]) for p in positions}
            for sym in list(self.ignored_dust):
                if sym not in active_symbols:
                    self.ignored_dust.remove(sym)  # Position gone
                else:
                    with self._ticker_lock:
                        bid = self.ticker_map.get(sym, {}).get("bid", 0)
                    if (
                        abs(active_symbols[sym]) > 0
                        and abs(active_symbols[sym]) * bid > 6.0
                    ):
                        # Position grew larger than dust (approx), retry managing it
                        self.ignored_dust.remove(sym)

        self.interface.update_ui()

    def fetch_premiums(self):
        self.interface.run_worker(self.fetch_premiums_worker)

    def fetch_tickers(self):
        if not self.is_fetching_tickers:
            self.interface.run_worker(self.fetch_tickers_worker)

    def fetch_24hr_stats(self):
        self.interface.run_worker(self.fetch_24hr_stats_worker)

    def process_fee_queue(self):
        with self._fee_queue_lock:
            if self.fee_queue:
                symbol = self.fee_queue.popleft()
                self.interface.run_worker(self.fetch_fee_worker(symbol))

    def fetch_premiums_worker(self):
        self.interface.call_from_thread(
            self.interface.log_message, "[fundee] fetch_premiums: requesting funding rates…"
        )
        t0 = time.time()
        try:
            data = self.exchange.exchange.fetch_funding_rates()
            payload = []
            for sym, fr in data.items():
                nxt = fr.get("nextFundingTimestamp") or fr.get("fundingTimestamp")
                payload.append(
                    {
                        "symbol": sym,
                        "lastFundingRate": str(fr.get("fundingRate", 0)),
                        "nextFundingTime": nxt,
                    }
                )
            self.interface.call_from_thread(
                self.interface.log_message,
                f"[fundee] fetch_premiums: got {len(payload)} funding rates "
                f"in {(time.time() - t0) * 1000:.0f}ms",
            )
            self.interface.call_from_thread(self.process_premiums, payload)
            # Store the raw premiums keyed by symbol so the scanner table can
            # look up next_funding_time when rendering promising_pairs.
            self.premium_data = {p["symbol"]: p for p in payload}
        except Exception as e:
            self.interface.call_from_thread(
                self.interface.log_message,
                f"[fundee] fetch_premiums FAILED after "
                f"{(time.time() - t0) * 1000:.0f}ms: {e}",
            )

    def fetch_tickers_worker(self):
        with self._fetching_tickers_lock:
            if self.is_fetching_tickers:
                return  # Already fetching, skip this request
            self.is_fetching_tickers = True

        t0 = time.time()
        try:
            symbols = list(self.exchange.precision_map.keys())
            data = self.exchange.exchange.fetch_tickers(symbols)
            self.interface.call_from_thread(
                self.interface.log_message,
                f"[fundee] fetch_tickers: got {len(data)} tickers "
                f"in {(time.time() - t0) * 1000:.0f}ms",
            )
            self.interface.call_from_thread(self.process_tickers, data)
        except Exception as e:
            self.interface.call_from_thread(
                self.interface.log_message,
                f"[fundee] fetch_tickers FAILED after "
                f"{(time.time() - t0) * 1000:.0f}ms: {e}",
            )
        finally:
            with self._fetching_tickers_lock:
                self.is_fetching_tickers = False

    def fetch_24hr_stats_worker(self):
        t0 = time.time()
        try:
            symbols = list(self.exchange.precision_map.keys())
            data = self.exchange.exchange.fetch_tickers(symbols)
            self.interface.call_from_thread(
                self.interface.log_message,
                f"[fundee] fetch_24hr_stats: got {len(data)} tickers "
                f"in {(time.time() - t0) * 1000:.0f}ms",
            )
            self.interface.call_from_thread(self.process_24hr_stats, data)
        except Exception as e:
            self.interface.log_message(
                f"[fundee] fetch_24hr_stats FAILED after "
                f"{(time.time() - t0) * 1000:.0f}ms: {e}"
            )

    def fetch_fee_worker(self, symbol):
        def _work():
            try:
                rates = self.exchange.get_commission_rate(symbol)
                self.interface.call_from_thread(
                    self.update_fee_cache,
                    symbol,
                    rates.get("maker", float(DEFAULT_MAKER)),
                    rates.get("taker", float(DEFAULT_TAKER)),
                )
            except Exception as e:
                self.interface.log_message(f"Fee fetch error for {symbol}: {e}")

        return _work

    def process_tickers(self, data):
        if not isinstance(data, dict):
            return
        with self._ticker_lock:
            for sym, t in data.items():
                self.ticker_map[sym] = {
                    "bid": float(t.get("bid") or 0),
                    "ask": float(t.get("ask") or 0),
                }
        self.interface.update_ui()

    def process_24hr_stats(self, data):
        if not isinstance(data, dict):
            return
        for sym, t in data.items():
            qv = t.get("quoteVolume")
            if qv is None:
                info = t.get("info", {}) or {}
                qv = info.get("quoteVolume") or info.get("dayNtlVlm") or 0
            self.ticker_stats_cache[sym] = {"quoteVolume": float(qv or 0)}

    def update_fee_cache(self, symbol, maker, taker):
        with self._fee_cache_lock:
            self.fee_cache[symbol] = {
                "maker": Decimal(str(maker)),
                "taker": Decimal(str(taker)),
            }

    def process_premiums(self, data):
        if not isinstance(data, list):
            return

        candidates = []
        # Track every pair with valid ticker data so the heartbeat can show
        # the top 5 by net yield even when the filter rejects them.
        # Tuple: (symbol, funding_rate, net_yield)
        all_yields = []
        for p in data:
            sym = p["symbol"]
            try:
                rate = Decimal(str(p.get("lastFundingRate", 0)))
                nxt = p.get("nextFundingTime")
            except (ValueError, TypeError) as e:
                self.interface.log_message(f"Invalid funding rate data for {sym}: {e}")
                continue

            # 1. Early Fee Fetching
            # Queue fee fetch for anything with decent funding, so we learn real rates.
            with self._fee_queue_lock:
                if (
                    abs(rate) > Decimal("0.0004")
                    and sym not in self.fee_cache
                    and sym not in self.fee_queue
                ):
                    self.fee_queue.append(sym)

            # 2. Fetch fee + ticker data needed to compute net yield
            fees = self.fee_cache.get(
                sym, {"maker": DEFAULT_MAKER, "taker": DEFAULT_TAKER}
            )
            # Cost = Entry Fee + Exit Fee.
            # We assume Taker for both to be safe during filtering.
            fee_cost = fees["taker"] * 2

            with self._ticker_lock:
                tik = self.ticker_map.get(sym)
            if not tik or tik["ask"] <= 0 or tik["bid"] < 0:
                continue  # Skip if no live price data or invalid prices

            # Guard against division by zero
            if tik["ask"] == 0:
                continue
            spread = (Decimal(str(tik["ask"])) - Decimal(str(tik["bid"]))) / Decimal(
                str(tik["ask"])
            )

            # 3. Volume Check (skipped when volume_filter_enabled is False)
            if self.volume_filter_enabled:
                stats = self.ticker_stats_cache.get(sym)
                if not stats or stats["quoteVolume"] < 500000:  # 500k Min Volume
                    continue

            # 4. Net yield formula: |funding_rate| - 2*taker_fee - spread.
            # `abs()` is restored: the bot trades BOTH sides, picking direction
            # by sign (rate > 0 → SHORT to collect, rate < 0 → LONG to collect).
            # No `MIN_PROFIT_BUFFER` — only the spread cap acts as the floor.
            net_yield = abs(rate) - Decimal(str(fee_cost))

            # Record for the top-5 promising log regardless of viability.
            all_yields.append((sym, rate, net_yield))

            # 5. Viability filter
            if spread > Decimal("0.005") or net_yield <= 0:
                continue

            candidates.append(
                {
                    "symbol": sym,
                    "funding_rate": rate,
                    "next_funding_time": nxt,
                    "direction": "SHORT" if rate > 0 else "LONG",
                    "net_yield": net_yield,
                }
            )

        # Sort viable pairs by funding rate descending so the scanner shows
        # the highest-paying opportunities first.
        candidates.sort(key=lambda x: x["funding_rate"], reverse=True)
        with self._viable_pairs_lock:
            self.viable_pairs = candidates

        # Top N by net_yield for the heartbeat (signed — negative is fine,
        # the user wants to see how close pairs are to clearing the threshold).
        all_yields.sort(key=lambda t: t[2], reverse=True)
        self.promising_pairs = [
            {"symbol": s, "funding_rate": r, "net_yield": n}
            for s, r, n in all_yields[:50]
        ]
        # Immediate log so the user sees the best opportunities right away
        # (the heartbeat only fires every 60 s).
        if self.promising_pairs:
            top_all = ", ".join(
                f"{p['symbol']}(rate={p['funding_rate']*100:.4f}%, "
                f"net={p['net_yield']*100:.4f}%)"
                for p in self.promising_pairs
            )
            self.interface.log_message(
                f"[fundee] promising_pairs (top {len(self.promising_pairs)}): {top_all}"
            )
        else:
            self.interface.log_message(
                "[fundee] promising_pairs: none (all pairs have negative net yield)"
            )
        self.interface.update_ui()

    def log_trade(self, s):
        # Simplified Anchor Logging
        # We only log the event facts. PnL is analyzed via API later.

        entry_ts = s.get("entry_time", 0)
        exit_ts = s.get("exit_time", 0)

        # Convert timestamps to readable string for the CSV timestamp column (Log Time)
        log_time = datetime.now()

        with open("logs/trade_anchors.csv", "a") as f:
            f.write(
                f"{log_time},{s['strategy']},{s['symbol']},{s['direction']},{entry_ts},{exit_ts},{s['entry_price']},{s['exit_price']},{s['status']},{s['reason']},{s['quantity']}\n"
            )

    def remove_pending(self, symbol):
        with self._pending_orders_lock:
            if symbol in self.pending_orders:
                self.pending_orders.remove(symbol)

    def smart_execute(
        self,
        symbol,
        side,
        qty,
        aggressive,
        on_success,
        on_fail,
        switch_mode_time=None,
        leverage=None,
    ):
        def _worker():
            def _on_event(event, *args):
                if event == "ORDER_UPDATE":
                    # args: order_id, price, qty_left, status, filled, avg, note
                    pass
                elif event == "SUCCESS":
                    # args: filled, avg, order_id, role
                    self.interface.call_from_thread(
                        on_success, args[0], args[1], args[2], args[3]
                    )
                elif event == "FAIL":
                    self.interface.call_from_thread(on_fail, args[0])

            executor = SmartOrderExecutor(
                self.exchange,
                symbol,
                side,
                qty,
                aggressive=aggressive,
                leverage=leverage,
                callbacks={"on_event": _on_event},
            )

            try:
                executor.run(timeout=59, switch_mode_time=switch_mode_time)
            except Exception as e:
                self.interface.call_from_thread(on_fail, str(e))
            finally:
                self.interface.call_from_thread(self.remove_pending, symbol)

        return _worker

    def execute_strategy_entry(
        self,
        strategy,
        symbol,
        direction,
        maker=False,
        funding_time_ms=None,
        funding_rate=0.0,
        use_market=False,
    ):
        # Check duplicates
        with self._active_strategies_lock:
            for s in self.active_strategies:
                if (
                    s["symbol"] == symbol
                    and s["strategy"] == strategy
                    and s["status"] in ["OPEN", "OPENING"]
                ):
                    return
        with self._pending_orders_lock:
            if symbol in self.pending_orders:
                return

        dynamic_trade_size = Decimal(str(self.balance)) * BALANCE_UTILIZATION_PERCENT

        # Validate minimum trade size (5.5 USDC minimum)
        if dynamic_trade_size < MIN_NOTIONAL:
            self.interface.notify(
                f"SKIP {strategy} {symbol}: Balance too low (99% = ${dynamic_trade_size:.2f} < ${MIN_NOTIONAL})"
            )
            return

        with self._ticker_lock:
            ticker = self.ticker_map.get(symbol)
        if not ticker:
            self.interface.log_message(f"SKIP {symbol}: No ticker data available")
            return

        # Validate symbol exists in exchange info
        if symbol not in self.exchange.precision_map:
            self.interface.log_message(f"SKIP {symbol}: Symbol not in exchange info")
            return

        # Mark pending
        with self._pending_orders_lock:
            self.pending_orders.add(symbol)

        # Calculate roughly qty
        price = Decimal(
            str(ticker["ask"] if direction == "LONG" else ticker["bid"])
        )  # Approx for sizing
        qty = dynamic_trade_size / price
        side = "BUY" if direction == "LONG" else "SELL"

        # Calculate Switch Time (T-29s)
        switch_ts = None
        if funding_time_ms:
            # Funding time is in MS
            funding_ts = funding_time_ms / 1000.0
            switch_ts = funding_ts - 29.0

        def _on_success(fill_qty, avg_price, oid, role):
            s = {
                "id": self.sim_counter,
                "strategy": strategy,
                "symbol": symbol,
                "status": "OPEN",
                "direction": direction,
                "entry_price": avg_price,
                "entry_role": role,
                "entry_time": time.time(),
                "funding_time": funding_time_ms / 1000.0 if funding_time_ms else 0,
                "funding_rate": funding_rate,
                "quantity": fill_qty,
                "margin": dynamic_trade_size,
                "order_id": oid,
            }
            self.sim_counter += 1
            with self._active_strategies_lock:
                self.active_strategies.append(s)
            self.interface.notify(f"OPENED {strategy} {symbol} @ {avg_price:.4f}")
            self.interface.update_ui()

        def _on_fail(reason):
            self.interface.notify(f"OPEN FAIL {symbol}: {reason}")

        if use_market:
            self.interface.notify(f"ENTRY {symbol}: Pure Market Order (Approach B)")

            def market_worker():
                try:
                    resp = self.exchange.place_order(symbol, side, "MARKET", qty)
                    if resp and "orderId" in resp:
                        _on_success(
                            Decimal(resp.get("executedQty", qty)),
                            Decimal(resp.get("avgPrice", 0)),
                            resp.get("orderId", "MARKET"),
                            "TAKER",
                        )
                    else:
                        _on_fail(f"Invalid response: {resp}")
                except Exception as e:
                    self.interface.log_message(f"Market entry error: {e}")
                    _on_fail(str(e))

            self.interface.run_worker(market_worker)
        else:
            self.interface.notify(f"ENTRY {symbol}: Passive first, Aggressive @ T-29s")
            self.interface.run_worker(
                self.smart_execute(
                    symbol,
                    side,
                    qty,
                    aggressive=False,  # Start Passive
                    on_success=_on_success,
                    on_fail=_on_fail,
                    switch_mode_time=switch_ts,
                    leverage=1,
                )
            )

    def execute_strategy_exit(self, s, reason, maker=False, use_market=False):
        if s["symbol"] in self.pending_orders:
            return
        self.pending_orders.add(s["symbol"])

        side = "SELL" if s["direction"] == "LONG" else "BUY"

        # Auto-switch to Aggressive if Maker (Passive) takes too long
        # We give it 30 seconds to fill passively, then we dump it.
        switch_ts = None
        if maker:
            switch_ts = time.time() + 30.0

        def _on_success(fill_qty, avg_price, oid, role):
            s["status"] = "CLOSED"
            s["exit_price"] = avg_price
            s["exit_role"] = role
            s["exit_time"] = time.time()
            s["reason"] = reason

            self.closed_strategies.append(s)
            if s in self.active_strategies:
                self.active_strategies.remove(s)

            self.log_trade(s)
            self.interface.notify(f"CLOSED {s['strategy']} {s['symbol']}")
            self.interface.update_ui()

        def _on_fail(err):
            self.interface.notify(f"CLOSE FAILED {s['symbol']}: {err}")

        self.interface.notify(f"CLOSING {s['strategy']} {s['symbol']} ({reason})...")

        if use_market:

            def market_exit_worker():
                try:
                    resp = self.exchange.place_order(
                        s["symbol"], side, "MARKET", s["quantity"]
                    )
                    if resp and "orderId" in resp:
                        _on_success(
                            Decimal(resp.get("executedQty", s["quantity"])),
                            Decimal(resp.get("avgPrice", 0)),
                            resp.get("orderId", "MARKET"),
                            "TAKER",
                        )
                    else:
                        _on_fail(f"Invalid response: {resp}")
                except Exception as e:
                    self.interface.log_message(f"Market exit error: {e}")
                    _on_fail(str(e))

            self.interface.run_worker(market_exit_worker)
        else:
            self.interface.run_worker(
                self.smart_execute(
                    s["symbol"],
                    side,
                    s["quantity"],
                    not maker,
                    _on_success,
                    _on_fail,
                    switch_mode_time=switch_ts,
                )
            )

    def update_strategies(self):
        current_time = time.time()

        # Heartbeat: log scanner state once per minute so the user can see
        # the bot is alive even when no trades fire.
        last_hb = getattr(self, "_last_heartbeat", 0)
        if current_time - last_hb >= 60:
            self._last_heartbeat = current_time
            with self._viable_pairs_lock:
                n_viable = len(self.viable_pairs)
            with self._ticker_lock:
                n_tickers = len(self.ticker_map)
            msg = (
                f"[fundee] heartbeat: balance={self.balance:.2f} {QUOTE_CURRENCY}, "
                f"tickers={n_tickers}, viable_pairs={n_viable}, "
                f"active_strategies={len(self.active_strategies)}, "
                f"strategy={ACTIVE_STRATEGY}, "
                f"volfilt={'ON' if self.volume_filter_enabled else 'OFF'}"
            )
            # Top promising by net yield — surfaces "almost promising" pairs that the
            # filter rejected, so the user can see when funding is close.
            if self.promising_pairs:
                top = ", ".join(
                    f"{p['symbol']}(rate={p['funding_rate']*100:.4f}%, "
                    f"net={p['net_yield']*100:.4f}%)"
                    for p in self.promising_pairs[:5]
                )
                msg += f" | promising_pairs (top 5): {top}"
            self.interface.log_message(msg)

        # ENTRY LOGIC - Copy viable_pairs under lock to avoid race conditions
        with self._viable_pairs_lock:
            viable_pairs_copy = list(self.viable_pairs)

        for cand in viable_pairs_copy:
            symbol = cand["symbol"]

            # 1. JIT VALIDATION (The Fix)
            # ---------------------------------------------------------
            with self._ticker_lock:
                ticker = self.ticker_map.get(symbol)
            if not ticker or ticker["ask"] <= 0 or ticker["bid"] < 0:
                continue

            # Guard against division by zero
            if ticker["ask"] == 0:
                continue

            # Re-calculate real-time spread
            spread = (
                Decimal(str(ticker["ask"])) - Decimal(str(ticker["bid"]))
            ) / Decimal(str(ticker["ask"]))

            # Re-calculate real-time costs
            with self._fee_cache_lock:
                fees = self.fee_cache.get(
                    symbol, {"maker": DEFAULT_MAKER, "taker": DEFAULT_TAKER}
                )
            fee_cost = fees["taker"] * 2  # Assume taker for entry safety

            # Re-calculate profitability (must match process_premiums formula)
            net_yield = abs(cand["funding_rate"]) - fee_cost

            # If the market has turned against us in the last 59 seconds, ABORT.
            if spread > Decimal("0.005") or net_yield <= 0:
                # Optional: Log this rejection so you know the safety check is working
                # self.interface.log_message(f"Skipping {symbol}: Spread blew out ({spread:.4f})")
                continue
            # ---------------------------------------------------------

            if not cand["next_funding_time"]:
                continue

            funding_ts = float(cand["next_funding_time"]) / 1000
            diff = funding_ts - current_time
            direction = cand["direction"]

            if ACTIVE_STRATEGY == "STRADDLE":
                # STRADDLE: Start 59s before funding.
                # Passive First -> Aggressive at T-29s
                if 29 < diff <= 60:
                    self.execute_strategy_entry(
                        "STRADDLE",
                        symbol,
                        direction,
                        maker=False,
                        funding_time_ms=cand["next_funding_time"],
                        funding_rate=cand["funding_rate"],
                    )
            elif ACTIVE_STRATEGY == "APPROACH_B" and 0 < diff <= APPROACH_B_ENTRY_WINDOW:
                # APPROACH B: 1s market in
                self.execute_strategy_entry(
                    "APPROACH_B",
                    symbol,
                    direction,
                    maker=False,
                    funding_time_ms=cand["next_funding_time"],
                    funding_rate=cand["funding_rate"],
                    use_market=True,
                )

        # EXIT LOGIC
        with self._active_strategies_lock:
            active_strategies_copy = list(self.active_strategies)
        for s in active_strategies_copy:
            symbol = s["symbol"]
            strategy = s["strategy"]

            with self._ticker_lock:
                ticker = self.ticker_map.get(symbol)
            if not ticker:
                continue

            with self._viable_pairs_lock:
                cand = next(
                    (x for x in self.viable_pairs if x["symbol"] == symbol), None
                )

            # TP/SL Logic
            current_price = ticker["bid"] if s["direction"] == "LONG" else ticker["ask"]
            pnl_pct = (
                (current_price - s["entry_price"]) / s["entry_price"]
                if s["direction"] == "LONG"
                else (s["entry_price"] - current_price) / s["entry_price"]
            )

            # SL (-1%)
            if pnl_pct < -0.01:
                self.execute_strategy_exit(s, "SL (-1%)", maker=False)

            # Trailing TP Logic (Trigger @ 1.5%, Trail 0.1%)
            elif s.get("trailing_active", False):
                # We are in trailing mode, check for pullback
                if s["direction"] == "LONG":
                    if current_price > s["extreme_price"]:
                        s["extreme_price"] = current_price
                    # 0.1% pullback from extreme
                    if current_price < s["extreme_price"] * (1 - 0.001):
                        self.execute_strategy_exit(
                            s, f"Trailing TP (Hit {current_price:.4f})", maker=False
                        )
                else:  # SHORT
                    if current_price < s["extreme_price"]:
                        s["extreme_price"] = current_price
                    # 0.1% pullback from extreme
                    if current_price > s["extreme_price"] * (1 + 0.001):
                        self.execute_strategy_exit(
                            s, f"Trailing TP (Hit {current_price:.4f})", maker=False
                        )

            elif pnl_pct >= 0.015:
                # Activate trailing
                s["trailing_active"] = True
                s["extreme_price"] = current_price
                self.interface.notify(
                    f"TRAILING ACTIVATED {s['symbol']} (PnL: {pnl_pct * 100:.2f}%)"
                )

            # Time-based Exit
            # Exit at funding time (funding credited atomically at snapshot).
            elif strategy == "STRADDLE":
                funding_time = s.get("funding_time", 0)
                if funding_time > 0:
                    time_since_funding = current_time - funding_time
                    if time_since_funding >= 0:
                        self.execute_strategy_exit(s, "Post-Funding Exit", maker=True)
            elif strategy == "APPROACH_B":
                funding_time = s.get("funding_time", 0)
                if funding_time > 0:
                    time_since_funding = current_time - funding_time
                    if time_since_funding >= 0:
                        self.execute_strategy_exit(
                            s,
                            "Post-Funding Exit (Approach B)",
                            maker=False,
                            use_market=True,
                        )

        self.interface.update_ui()


class HeadlessInterface:
    MAX_WORKERS = 10

    def __init__(self, volume_filter=True):
        self.running = True
        self.tasks = []
        self.queue = Queue()
        self._active_workers = 0
        self._workers_lock = threading.Lock()
        self._volume_filter = volume_filter

    def set_interval(self, interval, func):
        self.tasks.append([time.time() + interval, interval, func])

    def run_worker(self, func, thread=True):
        with self._workers_lock:
            if self._active_workers >= self.MAX_WORKERS:
                self.log_message(
                    f"WARNING: Max workers ({self.MAX_WORKERS}) reached, skipping task"
                )
                return
            self._active_workers += 1

        def _wrapped_worker():
            try:
                func()
            finally:
                with self._workers_lock:
                    self._active_workers -= 1

        t = threading.Thread(target=_wrapped_worker)
        t.daemon = True
        t.start()

    def call_from_thread(self, func, *args):
        self.queue.put((func, args))

    def notify(self, msg, title="", severity="information", timeout=3.0):
        prefix = ""
        if severity != "information":
            prefix = f"[{severity.upper()}] "
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {prefix}{msg}")
        sys.stdout.flush()

    def log_message(self, msg):
        print(f"[LOG] {msg}")

    def update_ui(self):
        pass

    def run(self):
        self.logic = FundeeLogic(self, volume_filter=self._volume_filter)
        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting REAL TRADING Bot (LIVE Mode)..."
        )
        if not self._volume_filter:
            print(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Volume filter: OFF (all pairs pass regardless of 24h volume)"
            )
        self.logic.start()

        try:
            while self.running:
                while not self.queue.empty():
                    try:
                        func, args = self.queue.get_nowait()
                        func(*args)
                    except Exception as e:
                        self.log_message(f"Queue processing error: {e}")

                now = time.time()
                for task in self.tasks:
                    if now >= task[0]:
                        task[2]()
                        task[0] = now + task[1]
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nStopping...")


class FundeeApp(App):
    MAX_WORKERS = 10

    CSS = """
    Screen { layout: vertical; }
    #balance_bar { height: 2; }
    #scanner_scroll { height: 3fr; }
    #sim_scroll { height: 1fr; }
    DataTable { height: 1fr; border: solid red; } /* Red border for REAL mode */
    .section_title { background: $primary; color: white; text-align: center; text-style: bold; height: 1; }
    .volfilter_on { background: $success; color: $text; text-style: bold; }
    .volfilter_off { background: $warning; color: $text; text-style: bold; }
    Log { height: 30%; border-top: solid $primary; background: $surface; display: none; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_all", "Refresh"),
        ("c", "clear_history", "Clear Hist"),
        ("l", "toggle_log", "Toggle Log"),
        ("v", "toggle_volume_filter", "Toggle VolFilter"),
    ]

    def __init__(self, volume_filter=True):
        super().__init__()
        self.logic = FundeeLogic(self, volume_filter=volume_filter)
        self._active_workers = 0
        self._workers_lock = threading.Lock()
        # Scanner sort state: column index (0-based), reverse flag
        self._scanner_sort_col = 2   # default sort by Funding
        self._scanner_sort_reverse = True  # highest first
        # Track which symbols are currently in the scanner table for in-place updates
        self._scanner_keys = {}  # symbol -> row_key

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(
            Static(
                f"REAL TRADING (LIVE MODE) | Balance: ... {QUOTE_CURRENCY}",
                id="balance_display",
                classes="section_title",
            ),
            Static(
                "VolFilter: ON",
                id="volfilt_display",
                classes="section_title volfilter_on",
            ),
            id="balance_bar",
        )
        yield VerticalScroll(
            Static("Market Scanner", classes="section_title"),
            DataTable(id="scanner_table"),
            id="scanner_scroll",
        )
        yield VerticalScroll(
            Static("Active Strategies & History", classes="section_title"),
            DataTable(id="sim_table"),
            id="sim_scroll",
        )
        yield Log(id="debug_log")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#scanner_table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns(
            "Symbol", "Price", "Funding", "Dir", "Interval", "Countdown", "Spread"
        )

        sim_table = self.query_one("#sim_table", DataTable)
        sim_table.cursor_type = "row"
        sim_table.add_columns(
            "Strategy", "Symbol", "Status", "Dir", "Entry", "Exit", "PnL"
        )

        self.logic.start()
        self.update_ui()  # Initial render so the autokill indicator reflects the startup state.
        self.notify("Press 'L' to toggle Debug Logs")

    def on_data_table_header_selected(self, event):
        """Handle clicks on DataTable headers to sort the scanner table."""
        if event.data_table.id != "scanner_table":
            return
        col = event.column_index
        if col == self._scanner_sort_col:
            self._scanner_sort_reverse = not self._scanner_sort_reverse
        else:
            self._scanner_sort_col = col
            # sensible defaults: Funding / Spread sort descending; others ascending
            self._scanner_sort_reverse = col in (2, 6)  # Funding(2) or Spread(6)
        self.update_scanner_table()

    def action_clear_history(self):
        self.logic.clear_history()

    def action_refresh_all(self):
        self.logic.refresh_all()

    def action_toggle_log(self):
        log = self.query_one("#debug_log", Log)
        log.styles.display = "block" if log.styles.display == "none" else "none"

    def action_toggle_volume_filter(self):
        self.logic.volume_filter_enabled = not self.logic.volume_filter_enabled
        state = "ON" if self.logic.volume_filter_enabled else "OFF"
        self.notify(
            f"Volume filter: {state}",
            severity="information" if self.logic.volume_filter_enabled else "warning",
        )
        self.update_ui()

    def log_message(self, msg):
        self.query_one("#debug_log", Log).write_line(
            f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        )

    def notify(self, msg, title="", severity="information", timeout=3.0):
        # Log all notifications
        self.log_message(f"NOTIFY: {msg}")
        super().notify(msg, title=title, severity=severity, timeout=timeout)

    def update_ui(self):
        self.query_one("#balance_display", Static).update(
            f"REAL TRADING (LIVE MODE) | Balance: ${self.logic.balance:.2f} {QUOTE_CURRENCY}"
        )
        volfilt_widget = self.query_one("#volfilt_display", Static)
        if self.logic.volume_filter_enabled:
            volfilt_widget.update("VolFilter: ON")
            volfilt_widget.set_classes("section_title volfilter_on")
        else:
            volfilt_widget.update("VolFilter: OFF")
            volfilt_widget.set_classes("section_title volfilter_off")
        self.update_scanner_table()
        self.update_sim_table()

    def run_worker(self, func, thread=True):
        with self._workers_lock:
            if self._active_workers >= self.MAX_WORKERS:
                self.log_message(
                    f"WARNING: Max workers ({self.MAX_WORKERS}) reached, skipping task"
                )
                return
            self._active_workers += 1

        def _wrapped_worker():
            try:
                func()
            finally:
                with self._workers_lock:
                    self._active_workers -= 1

        return super().run_worker(_wrapped_worker, thread=thread)

    def update_scanner_table(self):
        table = self.query_one("#scanner_table", DataTable)
        pairs = list(self.logic.promising_pairs)

        # Apply user-selected sort.
        # Build a sortable scalar per pair based on the active column.
        col_index = self._scanner_sort_col
        reverse = self._scanner_sort_reverse

        def _sort_scalar(pair):
            rate = float(pair["funding_rate"])
            if col_index == 0:  # Symbol
                return pair["symbol"].lower()
            elif col_index == 1:  # Price (ask)
                tik = self.logic.ticker_map.get(pair["symbol"])
                return float(tik["ask"]) if tik else 0.0
            elif col_index == 2:  # Funding
                return rate
            elif col_index == 3:  # Direction
                return 0 if rate > 0 else 1  # SHORT first, then LONG
            elif col_index == 4:  # Interval
                return float(self.logic.exchange.funding_interval_hours.get(pair["symbol"], 1))
            elif col_index == 5:  # Countdown (seconds until funding)
                nxt = self.logic.premium_data.get(pair["symbol"], {}).get("next_funding_time")
                if nxt:
                    d = (float(nxt) / 1000) - time.time()
                    return d
                return float("inf")
            elif col_index == 6:  # Spread
                tik = self.logic.ticker_map.get(pair["symbol"])
                if tik and tik["ask"] > 0:
                    return float((Decimal(str(tik["ask"])) - Decimal(str(tik["bid"]))) / Decimal(str(tik["ask"])))
                return 0.0
            return 0  # fallback

        pairs = sorted(pairs, key=_sort_scalar, reverse=reverse)

        wanted_symbols = {c["symbol"] for c in pairs}
        current_keys = dict(self._scanner_keys)

        # 1) Remove rows that are no longer in the top set
        for sym, key in list(current_keys.items()):
            if sym not in wanted_symbols:
                table.remove_row(key)
                del self._scanner_keys[sym]

        # 2) Build fresh row data and update / insert
        for c in pairs:
            sym = c["symbol"]
            tik = self.logic.ticker_map.get(sym, {"bid": 0, "ask": 0})
            spr = (
                (Decimal(str(tik["ask"])) - Decimal(str(tik["bid"])))
                / Decimal(str(tik["ask"]))
                if tik["ask"] > 0
                else Decimal("0")
            )

            cd = "N/A"
            nxt = self.logic.premium_data.get(sym, {}).get("next_funding_time")
            if nxt:
                d = (float(nxt) / 1000) - time.time()
                cd = str(timedelta(seconds=int(d))) if d > 0 else "FUNDING"

            interval_h = self.logic.exchange.funding_interval_hours.get(sym, 1)
            interval_lbl = f"{interval_h}h"

            row_data = [
                sym,
                f"{tik['ask']:.4f}",
                f"{c['funding_rate'] * 100:.4f}%",
                "SHORT" if c["funding_rate"] > 0 else "LONG",
                interval_lbl,
                cd,
                f"{spr * 100:.4f}%",
            ]

            if sym in self._scanner_keys:
                # Update existing row cells in-place
                key = self._scanner_keys[sym]
                col_keys = [c.key for c in table.ordered_columns]
                for col_idx, value in enumerate(row_data):
                    table.update_cell(key, col_keys[col_idx], value)
            else:
                # Insert new row, keep its key for later updates
                key = table.add_row(*row_data, key=sym)
                self._scanner_keys[sym] = key

    def update_sim_table(self):
        table = self.query_one("#sim_table", DataTable)
        rows = []
        sims = (
            self.logic.active_strategies
            + sorted(
                self.logic.closed_strategies,
                key=lambda x: x["entry_time"],
                reverse=True,
            )[:10]
        )
        for s in sims:
            rows.append(
                (
                    s["strategy"],
                    s["symbol"],
                    s["status"],
                    s["direction"],
                    f"{s['entry_price']:.4f}",
                    f"{s.get('exit_price', 0):.4f}",
                    f"${s.get('net_pnl_amt', 0):.4f}",
                )
            )
        table.clear()
        table.add_rows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use Hyperliquid testnet (sets sandboxMode on ccxt)",
    )
    parser.add_argument(
        "--no-volume",
        action="store_true",
        help="Disable the 500k volume gate. TUI 'v' toggles this at runtime.",
    )
    args = parser.parse_args()

    if args.testnet:
        os.environ["HL_TESTNET"] = "1"
        print("WARNING: Using Hyperliquid testnet")

    if not TEXTUAL_INSTALLED and not args.headless:
        print("Textual library not found. Falling back to headless mode.")
        args.headless = True

    volume_filter = not args.no_volume

    try:
        # Ensure logs directory exists
        if not os.path.exists("logs"):
            os.makedirs("logs")

        if args.headless:
            HeadlessInterface(volume_filter=volume_filter).run()
        else:
            app = FundeeApp(volume_filter=volume_filter)
            app.run()

    finally:
        pass
