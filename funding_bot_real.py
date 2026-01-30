import argparse
import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from queue import Queue
from urllib.parse import urlencode

import requests

try:
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Container, Vertical
    from textual.widgets import DataTable, Footer, Header, Log, Static
    from textual.worker import Worker
except ImportError:
    # Allow running headless without textual installed
    App = object
    ComposeResult = None
    pass

from funding_shared import BASE_URL, ExchangeInterface

# ==========================================
# CONFIGURATION
# ==========================================
# BASE_URL imported from funding_shared

API_KEY = os.getenv("ASTER_API_KEY")
API_SECRET = os.getenv("ASTER_API_SECRET")
USER_ADDRESS = os.getenv("ASTER_USER_ADDRESS")
SIGNER_ADDRESS = os.getenv("ASTER_SIGNER_ADDRESS", USER_ADDRESS)
TRADE_SIZE_USDT = 100.0  # Size per trade in USDT

# Defaults
DEFAULT_MAKER = 0.00005  # 0.005%
DEFAULT_TAKER = 0.0004   # 0.04%
MIN_PROFIT_BUFFER = 0.0002


class FundingLogic:
    def __init__(self, interface):
        self.interface = interface
        self.exchange = ExchangeInterface(
            base_url=BASE_URL, logger=self.interface.log_message
        )
        self.balance = 0.0
        self.trade_size = TRADE_SIZE_USDT

        self.viable_pairs = []
        self.fee_cache = {}
        self.ticker_map = {}
        self.ticker_stats_cache = {}
        self.fee_queue = set()
        self.pending_orders = set()
        self.is_hedge_mode = False  # Default One-Way
        self.is_fetching_tickers = False  # Prevent stacking requests

        # Track local strategy state
        self.active_strategies = []
        self.closed_strategies = []
        self.real_positions = []
        self.sim_counter = 0

        # Log file
        if not os.path.exists("logs/live_trades.csv"):
            with open("logs/live_trades.csv", "w") as f:
                f.write(
                    "Timestamp,Strategy,Symbol,Direction,Entry,Exit,Net_PnL_USDT,Net_PnL_Pct,Balance\n"
                )

        # Enhanced Logging for Backtesting
        if not os.path.exists("logs/market_log.csv"):
            with open("logs/market_log.csv", "w") as f:
                f.write(
                    "Timestamp,Symbol,FundingRate,NextFundingTime,Bid,Ask,Spread,EstProfit,MakerFee,TakerFee\n"
                )

        if not os.path.exists("logs/order_log.csv"):
            with open("logs/order_log.csv", "w") as f:
                f.write(
                    "Timestamp,Symbol,OrderId,Side,Type,Price,Qty,Status,ExecutedQty,AvgPrice,Msg\n"
                )

        if not os.path.exists("logs/decision_log.csv"):
            with open("logs/decision_log.csv", "w") as f:
                f.write("Timestamp,Symbol,Strategy,Action,Reason\n")

    def log_market_data(self, candidates):
        """Log market snapshot of viable pairs for backtesting."""
        ts = datetime.now()
        with open("logs/market_log.csv", "a") as f:
            for c in candidates:
                sym = c["symbol"]
                tik = self.ticker_map.get(sym)
                if not tik:
                    continue

                fees = self.fee_cache.get(
                    sym, {"maker": DEFAULT_MAKER, "taker": DEFAULT_TAKER}
                )
                spread = (tik["ask"] - tik["bid"]) / tik["ask"] if tik["ask"] > 0 else 0
                est_profit = abs(c["funding_rate"]) - (fees["taker"] * 2)

                f.write(
                    f"{ts},{sym},{c['funding_rate']:.6f},{c['next_funding_time']},{tik['bid']},{tik['ask']},{spread:.6f},{est_profit:.6f},{fees['maker']},{fees['taker']}\n"
                )

    def log_order_event(
        self,
        symbol,
        order_id,
        side,
        type,
        price,
        qty,
        status,
        executed,
        avg_price,
        msg="",
    ):
        """Log granular order events."""
        with open("logs/order_log.csv", "a") as f:
            f.write(
                f"{datetime.now()},{symbol},{order_id},{side},{type},{price},{qty},{status},{executed},{avg_price},{msg}\n"
            )

    def log_decision(self, symbol, strategy, action, reason):
        """Log why we did or did not take a trade."""
        with open("logs/decision_log.csv", "a") as f:
            f.write(f"{datetime.now()},{symbol},{strategy},{action},{reason}\n")

    def start(self):
        self.interface.log_message(f"Starting Logic (Base: {BASE_URL})")

        # Check Position Mode
        self.is_hedge_mode = self.exchange.get_position_mode()
        self.interface.log_message(
            f"Position Mode: {'Hedge' if self.is_hedge_mode else 'One-Way'}"
        )

        self.interface.set_interval(60.0, self.fetch_premiums)
        self.interface.set_interval(60.0, self.fetch_24hr_stats)
        self.interface.set_interval(
            5.0, self.sync_balance_positions
        )  # Sync with exchange
        self.interface.set_interval(1.0, self.fetch_tickers)
        self.interface.set_interval(1.0, self.update_strategies)
        self.interface.set_interval(2.0, self.process_fee_queue)
        self.interface.set_interval(5.0, self.safety_monitor)  # Run safety check every 5s
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
            self.interface.call_from_thread(
                self.interface.log_message, f"Sync Error: {e}"
            )

    def update_account_state(self, balance, positions):
        if balance is not None:
            self.balance = balance
        if positions is not None:
            self.real_positions = positions
        self.interface.update_ui()

    def safety_monitor(self):
        """
        Integrated 'Killer Bot' logic.
        Ensures no positions remain open outside the designated funding window (xx:59 - xx:00:30).
        """
        now = datetime.now()
        minute = now.minute
        second = now.second

        # Safe Zones: 59 (Pre-Funding), 00:00-00:30 (Funding + Buffer)
        # Kill Zone: All others
        if minute == 59:
            return
        if minute == 0 and second < 30:
            return

        # Check for stray positions
        for p in self.real_positions:
            symbol = p["symbol"]
            amt = float(p["positionAmt"])

            if amt == 0:
                continue
            if symbol in self.pending_orders:
                continue  # Don't kill if we are already working on it

            # KILL IT
            self.interface.notify(
                f"SAFETY: Killing stray position {symbol} ({amt})", severity="warning"
            )
            self.pending_orders.add(symbol)  # Lock it

            direction = "SHORT" if amt < 0 else "LONG"
            close_side = "BUY" if amt < 0 else "SELL"
            qty = abs(amt)

            # Define callbacks
            def _on_success(fill, avg, oid):
                self.interface.notify(
                    f"SAFETY: Killed {symbol} @ {avg}", severity="warning"
                )
                self.remove_pending(symbol)
                # Also try to find and close any matching local strategy to keep UI in sync
                found = False
                for s in self.active_strategies:
                    if s["symbol"] == symbol and s["status"] != "CLOSED":
                        s["status"] = "CLOSED"
                        s["exit_price"] = avg
                        s["exit_time"] = time.time()
                        s["reason"] = "Safety Kill"
                        s["net_pnl_amt"] = (
                            (avg - s["entry_price"]) * s["quantity"]
                            if s["direction"] == "LONG"
                            else (s["entry_price"] - avg) * s["quantity"]
                        )
                        self.closed_strategies.append(s)
                        found = True
                if found:
                    self.active_strategies = [
                        s for s in self.active_strategies if s["status"] != "CLOSED"
                    ]
                
                self.sync_balance_positions()

            def _on_fail(err):
                self.interface.notify(
                    f"SAFETY: Kill failed for {symbol}: {err}", severity="error"
                )
                self.remove_pending(symbol)

            # Determine Position Side (Hedge Mode Support)
            # If closing a SHORT (amt < 0), pos side is SHORT.
            position_side = direction if self.is_hedge_mode else None

            # Use smart_execute aggressive
            self.interface.run_worker(
                self.smart_execute(
                    symbol,
                    close_side,
                    qty,
                    aggressive=True,
                    on_success=_on_success,
                    on_fail=_on_fail,
                    position_side=position_side,
                )
            )

    def fetch_premiums(self):
        self.interface.run_worker(self.fetch_premiums_worker)

    def fetch_tickers(self):
        if not self.is_fetching_tickers:
            self.interface.run_worker(self.fetch_tickers_worker)

    def fetch_24hr_stats(self):
        self.interface.run_worker(self.fetch_24hr_stats_worker)

    def process_fee_queue(self):
        if self.fee_queue:
            self.interface.run_worker(self.fetch_fee_worker(self.fee_queue.pop()))

    def fetch_premiums_worker(self):
        try:
            # self.interface.call_from_thread(self.interface.log_message, "Fetching premiums...")
            resp = self.exchange.session.get(
                f"{self.exchange.base_url}/fapi/v3/premiumIndex", timeout=10
            )
            if resp.status_code == 200:
                self.interface.call_from_thread(self.process_premiums, resp.json())
            else:
                self.interface.call_from_thread(
                    self.interface.log_message,
                    f"Premium fetch failed: {resp.status_code}",
                )
        except Exception as e:
            self.interface.call_from_thread(
                self.interface.log_message, f"Premium fetch error: {e}"
            )

    def fetch_tickers_worker(self):
        self.is_fetching_tickers = True
        try:
            # self.interface.call_from_thread(self.interface.log_message, "Fetching tickers...")
            resp = self.exchange.session.get(
                f"{self.exchange.base_url}/fapi/v3/ticker/bookTicker", timeout=10
            )
            if resp.status_code == 200:
                self.interface.call_from_thread(self.process_tickers, resp.json())
            else:
                self.interface.call_from_thread(
                    self.interface.log_message, f"Ticker fail: {resp.status_code}"
                )
        except Exception as e:
            self.interface.call_from_thread(
                self.interface.log_message, f"Ticker error: {e}"
            )
        finally:
            self.is_fetching_tickers = False

    def fetch_24hr_stats_worker(self):
        try:
            resp = self.exchange.session.get(
                f"{self.exchange.base_url}/fapi/v3/ticker/24hr", timeout=10
            )
            if resp.status_code == 200:
                self.interface.call_from_thread(self.process_24hr_stats, resp.json())
        except:
            pass

    def fetch_fee_worker(self, symbol):
        def _work():
            if not API_SECRET:
                return
            try:
                params = {"symbol": symbol}
                query = self.exchange._sign_request(params)
                # headers = {'X-MBX-APIKEY': API_KEY}
                resp = self.exchange.session.get(
                    f"{self.exchange.base_url}/fapi/v3/commissionRate",
                    params=query,
                    timeout=5,
                )
                if resp.status_code == 200:
                    d = resp.json()
                    self.interface.call_from_thread(
                        self.update_fee_cache,
                        symbol,
                        float(d.get("makerCommissionRate", DEFAULT_MAKER)),
                        float(d.get("takerCommissionRate", DEFAULT_TAKER)),
                    )
            except:
                pass

        return _work

    def process_tickers(self, data):
        if not isinstance(data, list):
            return
        for t in data:
            self.ticker_map[t["symbol"]] = {
                "bid": float(t.get("bidPrice", 0)),
                "ask": float(t.get("askPrice", 0)),
            }
        self.interface.update_ui()

    def process_24hr_stats(self, data):
        if not isinstance(data, list):
            return
        for t in data:
            self.ticker_stats_cache[t["symbol"]] = {
                "quoteVolume": float(t.get("quoteVolume", 0))
            }

    def update_fee_cache(self, symbol, maker, taker):
        self.fee_cache[symbol] = {"maker": maker, "taker": taker}

    def process_premiums(self, data):
        if not isinstance(data, list):
            return

        candidates = []
        for p in data:
            sym = p["symbol"]
            try:
                rate = float(p.get("lastFundingRate", 0))
                nxt = p.get("nextFundingTime")
            except:
                continue

            # 1. Early Fee Fetching
            # Queue fee fetch for anything with decent funding, so we learn real rates.
            if abs(rate) > 0.0004 and sym not in self.fee_cache and sym not in self.fee_queue:
                self.fee_queue.add(sym)

            # 2. Basic Rate Threshold (Using Taker Fees for safety)
            # STRADDLE strategy uses Taker orders.
            fees = self.fee_cache.get(sym, {"maker": DEFAULT_MAKER, "taker": DEFAULT_TAKER})

            # Cost = Entry Fee + Exit Fee.
            # We assume Taker for both to be safe during filtering.
            fee_cost = fees["taker"] * 2

            if abs(rate) <= (fee_cost + MIN_PROFIT_BUFFER):
                continue

            # 3. Ticker Data & Spread Check
            tik = self.ticker_map.get(sym)
            if not tik or tik["ask"] <= 0:
                continue  # Skip if no live price data

            spread = (tik["ask"] - tik["bid"]) / tik["ask"]

            # 4. Volume Check
            stats = self.ticker_stats_cache.get(sym)
            if not stats or stats["quoteVolume"] < 500000:  # 500k Min Volume
                continue

            if spread > 0.005:
                # self.log_decision(sym, "SCAN", "SKIP", f"High Spread {spread:.4f}")
                continue

            # 5. Net Profitability (Rate - Fees - Spread)
            # We treat Spread as a cost (slippage).
            est_profit = abs(rate) - fee_cost
            net_yield = est_profit - spread

            if net_yield < MIN_PROFIT_BUFFER:
                # self.log_decision(sym, "SCAN", "SKIP", f"Low Net Yield ({net_yield:.5f} < {MIN_PROFIT_BUFFER})")
                continue

            candidates.append(
                {
                    "symbol": sym,
                    "funding_rate": rate,
                    "next_funding_time": nxt,
                    "direction": "SHORT" if rate > 0 else "LONG",
                }
            )

        candidates.sort(key=lambda x: abs(x["funding_rate"]), reverse=True)
        self.viable_pairs = candidates
        self.log_market_data(candidates)
        self.interface.update_ui()

    def log_trade(self, s):
        # Calc PnL %
        if s["entry_price"] > 0:
            if s["direction"] == "LONG":
                pnl_pct = (s["exit_price"] - s["entry_price"]) / s["entry_price"]
            else:
                pnl_pct = (s["entry_price"] - s["exit_price"]) / s["entry_price"]
        else:
            pnl_pct = 0.0

        with open("logs/live_trades.csv", "a") as f:
            f.write(
                f"{datetime.now()},{s['strategy']},{s['symbol']},{s['direction']},{s['entry_price']},{s['exit_price']},{s.get('net_pnl_amt',0):.4f},{pnl_pct:.6f},{self.balance:.4f}\n"
            )

    def remove_pending(self, symbol):
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
        position_side=None,
        switch_mode_time=None,
        leverage=None,
    ):
        def _worker():
            order_id = None
            cumulative_filled = 0.0
            qty_left = qty

            try:
                # Set Leverage if requested
                if leverage is not None:
                    self.interface.log_message(f"Setting leverage for {symbol} to {leverage}x")
                    self.exchange.set_leverage(symbol, leverage)

                start_time = time.time()

                # Loop for chasing (max 70s to allow for the 60s window + buffer)
                while (time.time() - start_time) < 70:
                    # 1. Check for Aggressive Switch
                    if (
                        not aggressive
                        and switch_mode_time
                        and time.time() >= switch_mode_time
                    ):
                        aggressive = True
                        # If we have an active passive order, we need to cancel it first to go aggressive
                        if order_id:
                            # The cancellation logic below will handle it in the next loop iteration
                            # But we can force a 'continue' by ensuring we don't sleep too long
                            pass

                    # 2. Fetch Price
                    ticker = self.exchange.get_book_ticker(symbol)
                    if not ticker:
                        time.sleep(1)
                        continue

                    best_bid = float(ticker["bidPrice"])
                    best_ask = float(ticker["askPrice"])

                    # 3. Determine Price based on Mode
                    if aggressive:
                        # Marketable Limit: Buy at Ask+1%, Sell at Bid-1%
                        price = best_ask * 1.01 if side == "BUY" else best_bid * 0.99
                        time_in_force = "GTC"
                    else:
                        # Chase: Buy at Bid, Sell at Ask (Passive)
                        price = best_bid if side == "BUY" else best_ask
                        time_in_force = "GTC"

                    # 4. Place Order if None exists
                    if not order_id:
                        # Safety check on Min Qty (approx 5 USDT)
                        if (qty_left * price) < 5.5:
                            # Remainder too small, assume done
                            self.interface.call_from_thread(
                                on_success, cumulative_filled, price, "Partial-Done"
                            )
                            return

                        resp = self.exchange.place_order(
                            symbol,
                            side,
                            "LIMIT",
                            qty_left,
                            price,
                            time_in_force,
                            position_side=position_side,
                        )
                        if resp and "orderId" in resp:
                            order_id = resp.get("orderId")
                            self.interface.call_from_thread(
                                self.log_order_event,
                                symbol,
                                order_id,
                                side,
                                "LIMIT",
                                price,
                                qty_left,
                                "NEW",
                                0,
                                0,
                                f"Placed ({'Agg' if aggressive else 'Pas'})",
                            )
                            if aggressive:
                                time.sleep(0.5)
                        else:
                            self.interface.call_from_thread(
                                self.log_decision,
                                symbol,
                                "EXEC",
                                "FAIL",
                                f"Place Error: {resp}",
                            )
                            time.sleep(1)
                            continue

                    # 5. Check Status
                    status = self.exchange.get_order(symbol, order_id)
                    if status:
                        s = status["status"]
                        # 'executedQty' is cumulative for *this* order_id
                        this_order_filled = float(status.get("executedQty", 0))

                        # Done?
                        if s == "FILLED" or (
                            s == "CANCELED" and this_order_filled >= qty_left * 0.99
                        ):
                            cumulative_filled += this_order_filled
                            avg = float(status.get("avgPrice", price))
                            if avg == 0 and this_order_filled > 0:
                                avg = (
                                    float(status.get("cumQuote", 0)) / this_order_filled
                                )

                            self.interface.call_from_thread(
                                self.log_order_event,
                                symbol,
                                order_id,
                                side,
                                "LIMIT",
                                price,
                                qty_left,
                                s,
                                this_order_filled,
                                avg,
                                "Done",
                            )
                            self.interface.call_from_thread(
                                on_success, cumulative_filled, avg, order_id
                            )
                            return

                        # Logic for Chase / Reprice
                        should_cancel = False

                        # A: Switch to Aggressive?
                        if (
                            not aggressive
                            and switch_mode_time
                            and time.time() >= switch_mode_time
                        ):
                            should_cancel = True

                        # B: Passive Reprice?
                        elif not aggressive:
                            current_p = float(status["price"])
                            new_target = best_bid if side == "BUY" else best_ask
                            # If price moved away from our limit
                            if (side == "BUY" and new_target > current_p) or (
                                side == "SELL" and new_target < current_p
                            ):
                                should_cancel = True

                        if should_cancel:
                            self.interface.call_from_thread(
                                self.log_order_event,
                                symbol,
                                order_id,
                                side,
                                "LIMIT",
                                0,  # price irrelevant here
                                qty_left,
                                "CANCEL_REPRICE",
                                this_order_filled,
                                0,
                                "Reprice/Switch",
                            )
                            self.exchange.cancel_order(symbol, order_id)

                            # Update State for next loop
                            cumulative_filled += this_order_filled
                            qty_left -= this_order_filled
                            order_id = None
                            continue

                    time.sleep(1)

                # Timeout / Cleanup
                if order_id:
                    self.exchange.cancel_order(symbol, order_id)
                self.interface.call_from_thread(on_fail, "Timeout")

            except Exception as e:
                self.interface.call_from_thread(on_fail, str(e))
            finally:
                self.interface.call_from_thread(self.remove_pending, symbol)

        return _worker

    def execute_strategy_entry(
        self, strategy, symbol, direction, maker=False, funding_time_ms=None
    ):
        # Check duplicates
        for s in self.active_strategies:
            if (
                s["symbol"] == symbol
                and s["strategy"] == strategy
                and s["status"] in ["OPEN", "OPENING"]
            ):
                return
        if symbol in self.pending_orders:
            return

        if self.balance < self.trade_size:
            self.interface.notify(
                f"SKIP {strategy} {symbol}: Low Balance (${self.balance:.2f})"
            )
            return

        ticker = self.ticker_map.get(symbol)
        if not ticker:
            return

        # Mark pending
        self.pending_orders.add(symbol)

        # Calculate roughly qty
        price = (
            ticker["ask"] if direction == "LONG" else ticker["bid"]
        )  # Approx for sizing
        qty = self.trade_size / price
        side = "BUY" if direction == "LONG" else "SELL"

        # Determine Position Side (Hedge Mode Support)
        position_side = direction if self.is_hedge_mode else None

        # Calculate Switch Time (T-29s)
        switch_ts = None
        if funding_time_ms:
            # Funding time is in MS
            funding_ts = funding_time_ms / 1000.0
            switch_ts = funding_ts - 29.0

        def _on_success(fill_qty, avg_price, oid):
            s = {
                "id": self.sim_counter,
                "strategy": strategy,
                "symbol": symbol,
                "status": "OPEN",
                "direction": direction,
                "entry_price": avg_price,
                "entry_time": time.time(),
                "funding_time": funding_time_ms / 1000.0 if funding_time_ms else 0,
                "quantity": fill_qty,
                "margin": self.trade_size,
                "order_id": oid,
            }
            self.sim_counter += 1
            self.active_strategies.append(s)
            self.interface.notify(f"OPENED {strategy} {symbol} @ {avg_price:.4f}")
            self.interface.update_ui()

        def _on_fail(reason):
            self.interface.notify(f"OPEN FAIL {symbol}: {reason}")

        self.interface.notify(f"ENTRY {symbol}: Passive first, Aggressive @ T-29s")
        self.interface.run_worker(
            self.smart_execute(
                symbol,
                side,
                qty,
                aggressive=False,  # Start Passive
                on_success=_on_success,
                on_fail=_on_fail,
                position_side=position_side,
                switch_mode_time=switch_ts,
                leverage=1,
            )
        )

    def execute_strategy_exit(self, s, reason, maker=False):
        if s["symbol"] in self.pending_orders:
            return
        self.pending_orders.add(s["symbol"])

        side = "SELL" if s["direction"] == "LONG" else "BUY"

        # Determine Position Side (Hedge Mode Support)
        position_side = s["direction"] if self.is_hedge_mode else None

        def _on_success(fill_qty, avg_price, oid):
            s["status"] = "CLOSED"
            s["exit_price"] = avg_price
            s["exit_time"] = time.time()
            s["reason"] = reason

            # PnL Calc (Approx)
            if s["direction"] == "LONG":
                pnl = (avg_price - s["entry_price"]) * s["quantity"]
            else:
                pnl = (s["entry_price"] - avg_price) * s["quantity"]

            s["net_pnl_amt"] = pnl
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
            self.smart_execute(
                s["symbol"],
                side,
                s["quantity"],
                not maker,
                _on_success,
                _on_fail,
                position_side=position_side,
            )
        )

    def update_strategies(self):
        current_time = time.time()

        # ENTRY LOGIC
        for cand in self.viable_pairs:
            symbol = cand["symbol"]
            if not cand["next_funding_time"]:
                continue

            funding_ts = float(cand["next_funding_time"]) / 1000
            diff = funding_ts - current_time
            direction = cand["direction"]

            # STRADDLE: Start 59s before funding.
            # Passive First -> Aggressive at T-29s
            if 29 < diff <= 60:
                self.execute_strategy_entry(
                    "STRADDLE",
                    symbol,
                    direction,
                    maker=False,
                    funding_time_ms=cand["next_funding_time"],
                )

        # EXIT LOGIC
        for s in list(self.active_strategies):
            symbol = s["symbol"]
            strategy = s["strategy"]

            ticker = self.ticker_map.get(symbol)
            if not ticker:
                continue

            cand = next((x for x in self.viable_pairs if x["symbol"] == symbol), None)

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
                    f"TRAILING ACTIVATED {s['symbol']} (PnL: {pnl_pct*100:.2f}%)"
                )

            # Time-based Exit
            # Exit passively 1s after funding.
            # If we fail, killer_bot (running in parallel) will sweep us at T+60s.
            elif strategy == "STRADDLE":
                funding_time = s.get("funding_time", 0)
                if funding_time > 0:
                    time_since_funding = current_time - funding_time
                    if time_since_funding > 1.0:
                        self.execute_strategy_exit(s, "Post-Funding Exit", maker=True)

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
        print(
            f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting REAL TRADING Bot (LIVE Mode)..."
        )
        self.logic.start()

        try:
            while self.running:
                while not self.queue.empty():
                    try:
                        func, args = self.queue.get_nowait()
                        func(*args)
                    except:
                        pass

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
        ("l", "toggle_log", "Toggle Log"),
    ]

    def __init__(self):
        super().__init__()
        self.logic = FundingLogic(self)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"REAL TRADING (LIVE MODE) | Balance: ...",
            id="balance_display",
            classes="section_title",
        )
        yield Vertical(
            Static("Market Scanner", classes="section_title"),
            DataTable(id="scanner_table"),
            Static("Active Strategies & History", classes="section_title"),
            DataTable(id="sim_table"),
            Log(id="debug_log"),
        )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#scanner_table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("Symbol", "Price", "Funding", "Dir", "Countdown", "Spread")

        sim_table = self.query_one("#sim_table", DataTable)
        sim_table.cursor_type = "row"
        sim_table.add_columns(
            "Strategy", "Symbol", "Status", "Dir", "Entry", "Exit", "PnL"
        )

        self.logic.start()
        self.notify("Press 'L' to toggle Debug Logs")

    def action_clear_history(self):
        self.logic.clear_history()

    def action_refresh_all(self):
        self.logic.refresh_all()

    def action_toggle_log(self):
        log = self.query_one("#debug_log", Log)
        log.styles.display = "block" if log.styles.display == "none" else "none"

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
            f"REAL TRADING (LIVE MODE) | Balance: ${self.logic.balance:.2f}"
        )
        self.update_scanner_table()
        self.update_sim_table()

    def run_worker(self, func, thread=True):
        return super().run_worker(func, thread=thread)

    def update_scanner_table(self):
        table = self.query_one("#scanner_table", DataTable)
        rows = []
        for c in self.logic.viable_pairs[:20]:
            sym = c["symbol"]
            tik = self.logic.ticker_map.get(sym, {"bid": 0, "ask": 0})
            spr = (tik["ask"] - tik["bid"]) / tik["ask"] if tik["ask"] > 0 else 0

            cd = "N/A"
            if c["next_funding_time"]:
                d = (float(c["next_funding_time"]) / 1000) - time.time()
                if d > 0:
                    cd = str(timedelta(seconds=int(d)))
                else:
                    cd = "FUNDING"

            rows.append(
                (
                    sym,
                    f"{tik['ask']:.4f}",
                    f"{c['funding_rate']*100:.4f}%",
                    c["direction"],
                    cd,
                    f"{spr*100:.4f}%",
                )
            )
        table.clear()
        table.add_rows(rows)

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
                    f"{s.get('exit_price',0):.4f}",
                    f"${s.get('net_pnl_amt',0):.4f}",
                )
            )
        table.clear()
        table.add_rows(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true", help="Run in headless mode")
    parser.add_argument(
        "--testnet", action="store_true", help="Use testnet (NOT RECOMMENDED)"
    )
    args = parser.parse_args()

    if args.testnet:
        BASE_URL = "https://fapi.asterdex-testnet.com"
        print("WARNING: Using Testnet")

    try:
        # Ensure logs directory exists
        if not os.path.exists("logs"):
            os.makedirs("logs")

        if args.headless:
            HeadlessInterface().run()
        else:
            app = FundingApp()
            app.run()

    finally:
        pass
