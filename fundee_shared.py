"""Shared exchange wrapper and order-execution helpers (ccxt-backed)."""
import logging
import math
import os
import time

import ccxt

logger = logging.getLogger(__name__)


HL_WALLET_ADDRESS = os.getenv("HL_WALLET_ADDRESS") or os.getenv("ASTER_USER_ADDRESS")
HL_API_PRIVATE_KEY = os.getenv("HL_API_PRIVATE_KEY") or os.getenv("ASTER_API_SECRET")

# Back-compat: kept for callers that still import BASE_URL (e.g. fundee.py startup log).
BASE_URL = os.getenv("HL_BASE_URL", "https://api.hyperliquid.xyz")

HL_LEGACY_DEFAULTS = {
    "BASE_URL": BASE_URL,
    "API_KEY": os.getenv("ASTER_API_KEY"),
    "API_SECRET": HL_API_PRIVATE_KEY,
    "USER_ADDRESS": HL_WALLET_ADDRESS,
    "SIGNER_ADDRESS": os.getenv("ASTER_SIGNER_ADDRESS", HL_WALLET_ADDRESS),
}

QUOTE_CURRENCY = "USDC"
MIN_NOTIONAL = 5.5
DEFAULT_SLIPPAGE = "0.05"


def _resolve_wallet():
    """Return the effective wallet address, honoring HL_* first, then ASTER_* legacy."""
    return HL_WALLET_ADDRESS or os.getenv("ASTER_USER_ADDRESS")


def _emit_legacy_warning():
    has_legacy = os.getenv("ASTER_USER_ADDRESS") or os.getenv("ASTER_API_SECRET")
    has_modern = os.getenv("HL_WALLET_ADDRESS") and os.getenv("HL_API_PRIVATE_KEY")
    if has_legacy and not has_modern:
            logger.warning(
                "ASTER_USER_ADDRESS / ASTER_API_SECRET are set but HL_WALLET_ADDRESS / "
                "HL_API_PRIVATE_KEY are not. The ASTER_* env vars are deprecated and will "
                "be removed in a future release. Please set HL_* variables."
            )


class ExchangeInterface:
    """Thin wrapper around ccxt.hyperliquid preserving the legacy method surface."""

    def __init__(self, base_url=None, logger=None, testnet=False):
        _emit_legacy_warning()
        wallet = HL_WALLET_ADDRESS or os.getenv("ASTER_USER_ADDRESS")
        key = HL_API_PRIVATE_KEY or os.getenv("ASTER_API_SECRET")
        if not wallet or not key:
            raise SystemExit(
                "[fundee] Missing Hyperliquid credentials. Set HL_WALLET_ADDRESS "
                "and HL_API_PRIVATE_KEY in the environment (or in a .env file passed "
                "via `uv run --env-file .env ...`). The legacy ASTER_USER_ADDRESS / "
                "ASTER_API_SECRET are also accepted but deprecated."
            )
        self._user_logger = logger
        config = {
            "walletAddress": wallet,
            "privateKey": key,
            "enableUnifiedMargin": False,
        }
        self._testnet = testnet
        self._wallet = wallet
        if testnet:
            config["sandboxMode"] = True
        self.exchange = ccxt.hyperliquid(config)
        self.precision_map = {}
        self.funding_interval_hours = {}

    def start(self):
        """Emit the verbose startup banner and load markets. Safe to call
        after the UI is composed (i.e. from on_mount / FundeeLogic.start)."""
        self.log(
            f"[fundee] creds resolved: wallet={self._wallet[:6]}…{self._wallet[-4:]} "
            f"testnet={self._testnet}"
        )
        self.log("[fundee] ccxt.hyperliquid instance ready")
        self.log("[fundee] loading markets from Hyperliquid (may take a few seconds)…")
        t0 = time.time()
        self.load_markets()
        self.log(
            f"[fundee] load_markets done in {(time.time() - t0):.2f}s — "
            f"{len(self.precision_map)} swap markets available"
        )

    def log(self, msg):
        if self._user_logger:
            try:
                self._user_logger(msg)
            except Exception:
                # Logger may not be ready yet (e.g. UI still composing).
                # Fall back to stdout so the message isn't lost.
                print(msg)
        else:
            logger.info(msg)

    # ---------- Market data ----------

    def load_markets(self):
        # Print a "still working" heartbeat every 2s while ccxt is blocked on
        # the network so the UI doesn't look frozen.
        import threading
        stop_heartbeat = threading.Event()
        hb_done = threading.Event()

        def _heartbeat():
            n = 0
            while not stop_heartbeat.wait(2.0):
                n += 1
                self.log(
                    f"[fundee] load_markets: still working… ({n * 2}s elapsed)"
                )
            hb_done.set()

        hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        hb_thread.start()
        try:
            markets = self.exchange.load_markets()
        finally:
            stop_heartbeat.set()
            hb_done.wait(timeout=3)
        for sym, m in markets.items():
            if not m.get("swap") or not m.get("active", True):
                continue
            price_prec = m["precision"]["price"]
            amount_prec = m["precision"]["amount"]
            self.precision_map[sym] = {
                "tick_size": 10 ** -price_prec,
                "step_size": 10 ** -amount_prec,
                "price_precision": price_prec,
                "qty_precision": amount_prec,
            }
            self.funding_interval_hours[sym] = (
                m.get("info", {}).get("fundingIntervalHours", 1)
            )
        return markets

    def get_book_ticker(self, symbol):
        try:
            t = self.exchange.fetch_ticker(symbol)
            return {
                "bidPrice": float(t.get("bid") or 0),
                "askPrice": float(t.get("ask") or 0),
            }
        except Exception as e:
            self.log(f"Book ticker fetch error for {symbol}: {e}")
            return None

    def get_funding_rate(self, symbol):
        try:
            fr = self.exchange.fetch_funding_rate(symbol)
            return fr
        except Exception as e:
            self.log(f"Funding rate fetch error for {symbol}: {e}")
            return None

    def get_commission_rate(self, symbol):
        try:
            fee = self.exchange.fetch_trading_fee(symbol)
            return {
                "maker": fee.get("maker", 0.00015),
                "taker": fee.get("taker", 0.00045),
            }
        except Exception as e:
            self.log(f"Fee fetch error for {symbol}: {e}")
            return {"maker": 0.00015, "taker": 0.00045}

    # ---------- Account ----------

    def get_balance(self):
        t0 = time.time()
        try:
            bal = self.exchange.fetch_balance(
                {"type": "swap", "user": _resolve_wallet()}
            )
            usdc = bal.get(QUOTE_CURRENCY)
            if not usdc:
                self.log(
                    f"[fundee] get_balance: no {QUOTE_CURRENCY} entry in response "
                    f"(took {(time.time() - t0) * 1000:.0f}ms)"
                )
                return None
            free = float(usdc.get("free", 0.0))
            self.log(
                f"[fundee] get_balance: {free:.2f} {QUOTE_CURRENCY} free "
                f"(took {(time.time() - t0) * 1000:.0f}ms)"
            )
            return free
        except Exception as e:
            self.log(
                f"[fundee] get_balance FAILED after {(time.time() - t0) * 1000:.0f}ms: {e}"
            )
            return None

    def get_positions(self, symbol=None):
        t0 = time.time()
        try:
            params = {"user": _resolve_wallet()}
            if symbol:
                params["symbol"] = symbol
            positions = self.exchange.fetch_positions(symbols=None, params=params)
            normalized = []
            for p in positions:
                amt = float(p.get("contracts") or 0)
                if amt == 0:
                    continue
                if p.get("side") == "short":
                    amt = -abs(amt)
                normalized.append(
                    {
                        "symbol": p["symbol"],
                        "positionAmt": amt,
                        "side": p.get("side"),
                        "info": p.get("info", {}),
                    }
                )
            return normalized
        except Exception as e:
            self.log(
                f"[fundee] get_positions FAILED after {(time.time() - t0) * 1000:.0f}ms: {e}"
            )
            return []

    # ---------- Normalization ----------

    def normalize_price(self, symbol, price):
        if symbol not in self.precision_map:
            return price
        p = self.precision_map[symbol]
        tick = p["tick_size"]
        prec = p["price_precision"]
        if tick == 0:
            return round(price, prec)
        return round(round(price / tick) * tick, prec)

    def normalize_quantity(self, symbol, qty):
        if symbol not in self.precision_map:
            return None
        p = self.precision_map[symbol]
        step = p["step_size"]
        prec = p["qty_precision"]
        if step == 0:
            return round(qty, prec)
        normalized = round(math.floor(qty / step) * step, prec)
        if normalized <= 0 and qty > 0:
            self.log(f"Quantity {qty} too small for symbol {symbol}. Step size: {step}")
            return None
        return normalized

    # ---------- Orders ----------

    def place_order(
        self,
        symbol,
        side,
        type,
        quantity,
        price=None,
        time_in_force="GTC",
        position_side=None,
        reduce_only=False,
    ):
        if symbol not in self.precision_map:
            self.log(f"Unknown symbol: {symbol}")
            return None
        qty = self.normalize_quantity(symbol, quantity)
        if qty is None or qty <= 0:
            self.log(f"Invalid quantity: {qty}")
            return None

        params = {}
        if type == "limit":
            if price is None:
                return None
            params["timeInForce"] = "Alo" if time_in_force == "GTX" else "Gtc"
        elif type == "market":
            params["slippage"] = DEFAULT_SLIPPAGE
        if reduce_only:
            params["reduceOnly"] = True
        # position_side is accepted for back-compat but ignored (no hedge mode on HL)

        try:
            return self.exchange.create_order(
                symbol, type, side.lower(), qty, price, params=params
            )
        except Exception as e:
            self.log(f"Order failed: {e}")
            return None

    def cancel_order(self, symbol, order_id):
        try:
            return self.exchange.cancel_order(order_id, symbol)
        except Exception as e:
            self.log(f"Cancel failed: {e}")
            return None

    def cancel_all_orders(self, symbol):
        try:
            return self.exchange.cancel_all_orders(symbol)
        except Exception as e:
            self.log(f"Cancel all failed: {e}")
            return None

    def get_order(self, symbol, order_id):
        try:
            o = self.exchange.fetch_order(order_id, symbol)
            info = o.get("info", {}) or {}
            return {
                "orderId": o.get("id"),
                "symbol": o.get("symbol"),
                "status": o.get("status", "").upper() or info.get("status"),
                "price": float(o.get("price") or 0),
                "executedQty": float(o.get("filled") or info.get("executedQty") or 0),
                "avgPrice": float(o.get("average") or info.get("avgPrice") or 0),
                "cumQuote": float(info.get("cumQuote", 0)),
                "info": info,
            }
        except Exception as e:
            self.log(f"Get order error: {e}")
            return None

    def set_leverage(self, symbol, leverage):
        try:
            return self.exchange.set_leverage(int(leverage), symbol)
        except Exception as e:
            self.log(f"Set leverage failed: {e}")
            return None

    def close_all_positions(self, symbol, side, amount=None, position_side=None):
        if amount is not None and amount * 0 < MIN_NOTIONAL:
            self.log(f"close_all_positions: amount {amount} below MIN_NOTIONAL for {symbol}")
            return None
        # Best-effort cancel of any resting orders for this symbol. Hyperliquid
        # does not implement bulk cancel_all_orders, so we fall back to
        # fetch_open_orders + cancel_orders. The cancel step must NEVER block
        # the market close below — closing the position is what matters.
        self._best_effort_cancel_symbol(symbol)
        time.sleep(0.2)
        try:
            return self.exchange.create_order(
                symbol, "market", side.lower(), amount, None,
                params={"reduceOnly": True, "slippage": DEFAULT_SLIPPAGE},
            )
        except Exception as e:
            self.log(f"Close all failed: {e}")
            return None

    def _best_effort_cancel_symbol(self, symbol):
        """Cancel all open orders for `symbol` without raising.

        Tries bulk cancel first (Binance-style exchanges), then falls back to
        fetching + cancelling per-order (Hyperliquid). All errors are logged
        and swallowed; the caller proceeds regardless.
        """
        import ccxt
        # Path 1: bulk cancel.
        try:
            return self.exchange.cancel_all_orders(symbol)
        except ccxt.NotSupported:
            pass  # HL — fall through to per-order cancel.
        except Exception as e:
            self.log(f"cancel_all_orders({symbol}) error (continuing): {e}")
        # Path 2: per-order cancel.
        try:
            orders = self.exchange.fetch_open_orders(symbol) or []
            ids = [o["id"] for o in orders if o.get("id")]
            if not ids:
                return []
            return self.exchange.cancel_orders(ids, symbol)
        except Exception as e:
            self.log(f"per-order cancel({symbol}) error (continuing): {e}")
            return None


class SmartOrderExecutor:
    """
    Shared logic for smart order execution (Chasing/Passive -> Aggressive).
    Can be used synchronously or wrapped in a thread.
    """

    def __init__(
        self,
        exchange,
        symbol,
        side,
        qty,
        aggressive=False,
        position_side=None,
        leverage=None,
        callbacks=None,
    ):
        self.exchange = exchange
        self.symbol = symbol
        self.side = side.upper()
        self.qty = float(qty)
        self.initial_qty = self.qty
        self.aggressive = aggressive
        self.position_side = position_side
        self.leverage = leverage
        self.callbacks = callbacks or {}

        self.order_id = None
        self.cumulative_filled = 0.0
        self.qty_left = self.qty
        self.last_price = 0.0

    def log(self, msg):
        if "log" in self.callbacks:
            self.callbacks["log"](msg)

    def emit_event(self, event_type, *args):
        if "on_event" in self.callbacks:
            self.callbacks["on_event"](event_type, *args)

    def run(self, timeout=70, switch_mode_time=None):
        if self.leverage is not None:
            self.log(f"Setting leverage for {self.symbol} to {self.leverage}x")
            res = self.exchange.set_leverage(self.symbol, self.leverage)
            if not res or int(res.get("leverage", 0)) != int(self.leverage):
                self.log(f"CRITICAL: Failed to set leverage to {self.leverage}x. Response: {res}")
                self.emit_event("FAIL", f"Leverage Set Failed: {res}")
                return False

        start_time = time.time()
        try:
            while (time.time() - start_time) < timeout:
                if not self.aggressive and switch_mode_time and time.time() >= switch_mode_time:
                    self.aggressive = True
                    self.log(
                        f"TIMEOUT: Passive limit reached for {self.symbol}. "
                        "Switching to AGGRESSIVE (Taker)."
                    )

                ticker = self.exchange.get_book_ticker(self.symbol)
                if not ticker:
                    self.log(f"Warn: No ticker data for {self.symbol}")
                    time.sleep(1)
                    continue

                best_bid = float(ticker["bidPrice"])
                best_ask = float(ticker["askPrice"])

                if self.aggressive:
                    price = best_ask * 1.01 if self.side == "BUY" else best_bid * 0.99
                    time_in_force = "GTC"
                else:
                    price = best_bid if self.side == "BUY" else best_ask
                    time_in_force = "GTX"

                if not self.order_id:
                    if (self.qty_left * price) < 5.5:
                        if self.qty_left == self.initial_qty:
                            self.log(
                                f"Position size {self.qty_left} "
                                f"({self.qty_left * price:.2f} USDC) too small to trade."
                            )
                            self.emit_event("FAIL", "Dust Position - Too small to close")
                            return False
                        self.log("Remainder too small, marking done.")
                        role = "TAKER" if self.aggressive else "MAKER"
                        self.emit_event("SUCCESS", self.cumulative_filled, price, "Partial-Done", role)
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
                        self.emit_event(
                            "ORDER_UPDATE",
                            self.order_id,
                            price,
                            self.qty_left,
                            "NEW",
                            0,
                            0,
                            f"Placed ({'Agg' if self.aggressive else 'Pas'})",
                        )
                        if self.aggressive:
                            time.sleep(0.5)
                    else:
                        self.emit_event("DECISION", "EXEC", "FAIL", f"Place Error: {resp}")
                        time.sleep(1)
                        continue

                status = self.exchange.get_order(self.symbol, self.order_id)
                if status:
                    s = status["status"]
                    this_order_filled = float(status.get("executedQty", 0))

                    if s == "FILLED" or (
                        s == "CANCELED" and this_order_filled >= self.qty_left * 0.99
                    ):
                        self.cumulative_filled += this_order_filled
                        avg = float(status.get("avgPrice", self.last_price))
                        if avg == 0 and this_order_filled > 0:
                            avg = float(status.get("cumQuote", 0)) / this_order_filled
                        self.emit_event(
                            "ORDER_UPDATE",
                            self.order_id,
                            self.last_price,
                            self.qty_left,
                            s,
                            this_order_filled,
                            avg,
                            "Done",
                        )
                        role = "TAKER" if self.aggressive else "MAKER"
                        self.emit_event(
                            "SUCCESS", self.cumulative_filled, avg, self.order_id, role
                        )
                        return True

                    should_cancel = False
                    if not self.aggressive and switch_mode_time and time.time() >= switch_mode_time:
                        should_cancel = True

                    if not should_cancel:
                        current_p = float(status.get("price", self.last_price))
                        if (
                            self.side == "BUY"
                            and (
                                (not self.aggressive and price > current_p)
                                or (self.aggressive and best_ask > current_p)
                            )
                        ) or (
                            self.side == "SELL"
                            and (
                                (not self.aggressive and price < current_p)
                                or (self.aggressive and best_bid < current_p)
                            )
                        ):
                            should_cancel = True

                    if should_cancel:
                        self.log(
                            f"Repricing {self.symbol}: Current Order {current_p} "
                            f"vs Market {price} (Aggressive: {self.aggressive})"
                        )
                        self.exchange.cancel_order(self.symbol, self.order_id)
                        self.order_id = None
                        if this_order_filled > 0:
                            self.cumulative_filled += this_order_filled
                            self.qty_left -= this_order_filled
                            self.emit_event(
                                "ORDER_UPDATE",
                                self.order_id,
                                self.last_price,
                                self.qty_left,
                                "PARTIAL",
                                this_order_filled,
                                0,
                                "Repricing",
                            )
                        continue

                time.sleep(1)

            self.log(f"EXECUTION TIMEOUT: Failed to fill {self.symbol} in {timeout}s.")
            if self.order_id:
                self.exchange.cancel_order(self.symbol, self.order_id)
            self.emit_event("FAIL", "Timeout")
            return False

        except Exception as e:
            self.log(f"Exception in SmartOrder: {e}")
            if self.order_id:
                self.exchange.cancel_order(self.symbol, self.order_id)
            self.emit_event("FAIL", str(e))
            return False
