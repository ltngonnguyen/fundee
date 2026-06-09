#!/usr/bin/env python3
"""
Fundee Dry-Run Hypothesis Engine.
Simulates 3 funding-wave entry playbooks in parallel against live market data.
"""
import argparse
import csv
import datetime
import logging
import signal
import sys
import time
from pathlib import Path

from fundee_shared import ExchangeInterface

# Configuration
PLAYBOOKS = {
    "A": {
        "name": "Shitcoin Hunter",
        "entry_seconds_before_funding": 600,
        "leverage": 3,
        "hard_sl_pct": -1.5,
        "max_margin": None,
    },
    "B": {
        "name": "Funding Surfer",
        "entry_seconds_before_funding": 1200,
        "leverage": 3,
        "hard_sl_pct": -2.5,
        "max_margin": None,
    },
    "C": {
        "name": "Pure Thesis",
        "entry_seconds_before_funding": 1200,
        "leverage": 2,
        "hard_sl_pct": None,
        "max_margin": 50.0,
    },
}

TRAIL_TRIGGER_MULT = 2.0
TRAIL_PULLBACK_MULT = 1.0
TAKER_FEE = 0.000144

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("fundee_dryrun")


class SimPosition:
    """Tracks a simulated trade."""

    def __init__(self, symbol, playbook_key, playbook_cfg, entry_price, funding_rate, funding_time):
        self.symbol = symbol
        self.playbook_key = playbook_key
        self.name = playbook_cfg["name"]
        self.leverage = playbook_cfg["leverage"]
        self.hard_sl_pct = playbook_cfg["hard_sl_pct"]
        self.max_margin = playbook_cfg["max_margin"]
        self.funding_rate = funding_rate
        self.funding_time = funding_time

        self.entry_price = entry_price
        self.entry_time = time.time()
        self.status = "OPEN"
        self.exit_reason = None
        self.exit_price = None
        self.exit_time = None
        self.raw_pnl_pct = 0.0
        self.leveraged_pnl_pct = 0.0
        self.max_leveraged_pnl_pct = 0.0

        # Trailing stop logic
        self.trail_trigger_pct = abs(funding_rate) * 100 * TRAIL_TRIGGER_MULT
        self.trail_pullback_pct = abs(funding_rate) * 100 * TRAIL_PULLBACK_MULT
        self.trail_active = False

        # Hard SL price for SHORT (price goes up = loss)
        if self.hard_sl_pct is not None:
            # PnL = (entry - exit) / entry
            # exit = entry * (1 - PnL)
            # For SL at -1.5% (leveraged), raw_sl = -1.5 / leverage
            raw_sl_pct = self.hard_sl_pct / self.leverage
            self.hard_sl_price = self.entry_price * (1 - (raw_sl_pct / 100))
        else:
            self.hard_sl_price = None

    def update(self, bid, ask):
        """Update PnL and check exit conditions.
        Returns the status if closed, else None.
        SHORT position: exit at bid price (conservative).
        """
        if self.status != "OPEN":
            return self.status

        # Current exit price for a SHORT is the current BID
        current_exit_price = bid
        self.raw_pnl_pct = (self.entry_price - current_exit_price) / self.entry_price * 100
        self.leveraged_pnl_pct = self.raw_pnl_pct * self.leverage

        if self.leveraged_pnl_pct > self.max_leveraged_pnl_pct:
            self.max_leveraged_pnl_pct = self.leveraged_pnl_pct

        # 1. Hard Stop Loss
        if self.hard_sl_price and current_exit_price >= self.hard_sl_price:
            self._close("CLOSED_SL", current_exit_price)
            return self.status

        # 2. Trailing Stop
        if not self.trail_active and self.leveraged_pnl_pct >= self.trail_trigger_pct:
            self.trail_active = True
            logger.info(f"[{self.playbook_key}] {self.symbol} Trailing Stop ACTIVE (Triggered at {self.leveraged_pnl_pct:.2f}%)")

        if self.trail_active:
            pullback = self.max_leveraged_pnl_pct - self.leveraged_pnl_pct
            if pullback >= self.trail_pullback_pct:
                self._close("CLOSED_TRAIL", current_exit_price)
                return self.status

        # 3. Expiration (5 min after funding time)
        if time.time() > self.funding_time + 300:
            self._close("CLOSED_EXPIRED", current_exit_price)
            return self.status

        return None

    def _close(self, reason, price):
        self.status = reason
        self.exit_reason = reason
        self.exit_price = price
        self.exit_time = time.time()
        # Recalculate final PnL at exit price
        self.raw_pnl_pct = (self.entry_price - self.exit_price) / self.entry_price * 100
        self.leveraged_pnl_pct = self.raw_pnl_pct * self.leverage


class DryRunEngine:
    """Main engine for dry-run hypothesis testing."""

    def __init__(self, exchange, playbooks=PLAYBOOKS, min_volume=500000,
                 spread_cap=0.005, min_net_yield=0, output_dir="logs/dryrun"):
        self.exchange = exchange
        self.playbooks = playbooks
        self.min_volume = min_volume
        self.spread_cap = spread_cap
        self.min_net_yield = min_net_yield
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self._positions = {}  # (symbol, playbook_key) -> SimPosition
        self._pending_entries = {}  # (symbol, playbook_key) -> entry_data
        self._last_heartbeat = 0
        self._running = True

        # CSV Paths
        today = datetime.datetime.now().strftime("%Y%m%d")
        self.trades_csv = self.output_dir / f"trades_{today}.csv"
        self.summary_csv = self.output_dir / f"summary_{today}.csv"

        # Initialize CSV files with headers if they don't exist
        if not self.trades_csv.exists():
            with open(self.trades_csv, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "symbol", "playbook", "name", "funding_rate_pct",
                    "entry_price", "exit_price", "dir", "raw_pnl_pct", "leveraged_pnl_pct",
                    "margin", "leverage", "exit_reason", "entry_time", "exit_time"
                ])

    def stop(self):
        self._running = False

    def run(self):
        logger.info("Starting Dry-Run Engine...")
        self.exchange.start()

        logger.info("Playbook Configurations:")
        for k, v in self.playbooks.items():
            logger.info(f"  [{k}] {v['name']}: Entry T-{v['entry_seconds_before_funding']}s, "
                        f"Lev {v['leverage']}x, SL {v['hard_sl_pct']}%")

        while self._running:
            try:
                t0 = time.time()
                self._step()
                elapsed = time.time() - t0
                sleep_time = max(0, 1.0 - elapsed)
                time.sleep(sleep_time)
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(5)

    def _step(self):
        # 1. Fetch funding rates
        try:
            funding_rates = self.exchange.exchange.fetch_funding_rates()
        except Exception as e:
            logger.error(f"Failed to fetch funding rates: {e}")
            return

        # 2. Filter for potential symbols
        symbols_to_watch = []
        funding_map = {}
        now = time.time()

        for symbol, data in funding_rates.items():
            if not data.get("fundingRate") or not data.get("nextFundingTime"):
                continue

            # We only care about positive funding (SHORT)
            if data["fundingRate"] <= 0:
                continue

            rate = data["fundingRate"]
            funding_time = data["nextFundingTime"] / 1000.0  # ms to s

            # Check if any playbook is within its entry window
            for pk, pcfg in self.playbooks.items():
                entry_window_start = funding_time - pcfg["entry_seconds_before_funding"]
                if (now >= entry_window_start and now < funding_time
                        and (symbol, pk) not in self._positions
                        and (symbol, pk) not in self._pending_entries):
                    symbols_to_watch.append(symbol)
                    funding_map[symbol] = data

        if not symbols_to_watch and not self._positions:
            if now - self._last_heartbeat > 60:
                logger.info("Heartbeat: No active positions or pending entries.")
                self._last_heartbeat = now
            return

        # 3. Fetch tickers for symbols we care about
        # Include active positions' symbols
        active_symbols = list(set([pos.symbol for pos in self._positions.values()]))
        all_symbols = list(set(symbols_to_watch + active_symbols))

        if not all_symbols:
            return

        try:
            tickers = self.exchange.exchange.fetch_tickers(all_symbols)
        except Exception as e:
            logger.error(f"Failed to fetch tickers: {e}")
            return

        # 4. Process Entries
        for symbol in symbols_to_watch:
            ticker = tickers.get(symbol)
            if not ticker or not ticker.get("bid") or not ticker.get("ask"):
                continue

            bid = ticker["bid"]
            ask = ticker["ask"]
            quote_vol = ticker.get("quoteVolume", 0)
            spread = (ask - bid) / ask
            rate = funding_map[symbol]["fundingRate"]
            net_yield = rate - (2 * TAKER_FEE) - spread

            # Viability filters
            if quote_vol < self.min_volume:
                continue
            if spread > self.spread_cap:
                continue
            if net_yield <= self.min_net_yield:
                continue

            # Create pending entry
            for pk, pcfg in self.playbooks.items():
                if (symbol, pk) in self._positions or (symbol, pk) in self._pending_entries:
                    continue

                funding_time = funding_map[symbol]["nextFundingTime"] / 1000.0
                entry_window_start = funding_time - pcfg["entry_seconds_before_funding"]

                if now >= entry_window_start:
                    logger.info(f"ENTRY [{pk}] {symbol} at {ask} (Rate: {rate*100:.4f}%, Yield: {net_yield*100:.4f}%)")
                    pos = SimPosition(
                        symbol=symbol,
                        playbook_key=pk,
                        playbook_cfg=pcfg,
                        entry_price=ask,  # SHORT at ask
                        funding_rate=rate,
                        funding_time=funding_time
                    )
                    self._positions[(symbol, pk)] = pos

        # 5. Update Active Positions
        closed_positions = []
        for key, pos in self._positions.items():
            ticker = tickers.get(pos.symbol)
            if not ticker:
                continue

            status = pos.update(ticker["bid"], ticker["ask"])
            if status:
                logger.info(f"EXIT [{pos.playbook_key}] {pos.symbol} at {pos.exit_price} ({pos.exit_reason}) PnL: {pos.leveraged_pnl_pct:.2f}%")
                closed_positions.append(key)
                self._log_trade(pos)
                self._update_summary(pos)

        for key in closed_positions:
            del self._positions[key]

        # Heartbeat
        if now - self._last_heartbeat > 60:
            active_count = len(self._positions)
            logger.info(f"Heartbeat: {active_count} positions active.")
            self._last_heartbeat = now

    def _log_trade(self, pos):
        with open(self.trades_csv, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.datetime.now().isoformat(),
                pos.symbol,
                pos.playbook_key,
                pos.name,
                pos.funding_rate * 100,
                pos.entry_price,
                pos.exit_price,
                "SHORT",
                pos.raw_pnl_pct,
                pos.leveraged_pnl_pct,
                pos.max_margin or 0,
                pos.leverage,
                pos.exit_reason,
                datetime.datetime.fromtimestamp(pos.entry_time).isoformat(),
                datetime.datetime.fromtimestamp(pos.exit_time).isoformat()
            ])

    def _update_summary(self, pos):
        # Read existing summary
        summary = {}
        if self.summary_csv.exists():
            with open(self.summary_csv) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    summary[(row["symbol"], row["playbook"])] = row

        key = (pos.symbol, pos.playbook_key)
        s = summary.get(key, {
            "symbol": pos.symbol,
            "playbook": pos.playbook_key,
            "playbook_name": pos.name,
            "wins": 0,
            "losses": 0,
            "total_trades": 0,
            "win_rate": 0,
            "avg_raw_pnl_pct": 0,
            "avg_leveraged_pnl_pct": 0,
            "sum_leveraged_pnl_pct": 0
        })

        # Update stats
        s["total_trades"] = int(s["total_trades"]) + 1
        if pos.leveraged_pnl_pct > 0:
            s["wins"] = int(s["wins"]) + 1
        else:
            s["losses"] = int(s["losses"]) + 1

        s["win_rate"] = (int(s["wins"]) / int(s["total_trades"])) * 100

        # Incremental averages
        total = int(s["total_trades"])
        s["sum_leveraged_pnl_pct"] = float(s["sum_leveraged_pnl_pct"]) + pos.leveraged_pnl_pct
        s["avg_leveraged_pnl_pct"] = float(s["sum_leveraged_pnl_pct"]) / total

        # For raw avg, we don't store sum, so just re-calculate
        old_avg_raw = float(s["avg_raw_pnl_pct"])
        s["avg_raw_pnl_pct"] = ((old_avg_raw * (total - 1)) + pos.raw_pnl_pct) / total

        summary[key] = s

        # Write back summary
        with open(self.summary_csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "symbol", "playbook", "playbook_name", "wins", "losses", "total_trades",
                "win_rate", "avg_raw_pnl_pct", "avg_leveraged_pnl_pct", "sum_leveraged_pnl_pct"
            ])
            writer.writeheader()
            for row in summary.values():
                writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Fundee Dry-Run Engine")
    parser.add_argument("--testnet", action="store_true", help="Use Hyperliquid testnet")
    parser.add_argument("--no-volume", action="store_true", help="Ignore volume filter")
    args = parser.parse_args()

    exchange = ExchangeInterface(testnet=args.testnet)

    min_volume = 0 if args.no_volume else 500000
    engine = DryRunEngine(exchange, min_volume=min_volume)

    def handle_sigint(sig, frame):
        logger.info("Shutdown signal received...")
        engine.stop()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    engine.run()


if __name__ == "__main__":
    main()
