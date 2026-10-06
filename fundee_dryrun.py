#!/usr/bin/env python3
"""
Fundee Dry-Run Hypothesis Engine.
Simulates 3 funding-wave entry playbooks in parallel against live market data.
"""

import argparse
import csv
import datetime
import logging
import random
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
DEFAULT_TAKER_FEE = 0.000144
FEE_CACHE_TTL = 3600  # re-fetch fees every hour

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
            logger.info(
                f"[{self.playbook_key}] {self.symbol} Trailing Stop ACTIVE (Triggered at {self.leveraged_pnl_pct:.2f}%)"
            )

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

    def __init__(
        self,
        exchange,
        playbooks=PLAYBOOKS,
        min_volume=500000,
        spread_cap=0.01,
        min_net_yield=0,
        output_dir="logs/dryrun",
    ):
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
        self._consecutive_api_failures = 0
        self._last_filter_diag = 0
        self._fee_cache = {}  # symbol -> (taker_fee, timestamp)

        # CSV Paths
        today = datetime.datetime.now().strftime("%Y%m%d")
        self.trades_csv = self.output_dir / f"trades_{today}.csv"
        self.summary_csv = self.output_dir / f"summary_{today}.csv"

        # Initialize CSV files with headers if they don't exist
        if not self.trades_csv.exists():
            with open(self.trades_csv, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "timestamp",
                        "symbol",
                        "playbook",
                        "name",
                        "funding_rate_pct",
                        "entry_price",
                        "exit_price",
                        "dir",
                        "raw_pnl_pct",
                        "leveraged_pnl_pct",
                        "margin",
                        "leverage",
                        "exit_reason",
                        "entry_time",
                        "exit_time",
                    ]
                )

    def stop(self):
        self._running = False

    def _get_taker_fee(self, symbol):
        """Fetch taker fee for a symbol, with caching."""
        cached = self._fee_cache.get(symbol)
        if cached and (time.time() - cached[1]) < FEE_CACHE_TTL:
            return cached[0]
        try:
            rates = self.exchange.get_commission_rate(symbol)
            taker = rates.get("taker", DEFAULT_TAKER_FEE)
        except Exception as e:
            logger.warning(f"Failed to fetch fee for {symbol}: {e}, using default")
            taker = DEFAULT_TAKER_FEE
        self._fee_cache[symbol] = (taker, time.time())
        return taker

    def run(self):
        logger.info("Starting Dry-Run Engine...")
        self.exchange.start()

        logger.info("Playbook Configurations:")
        for k, v in self.playbooks.items():
            logger.info(
                f"  [{k}] {v['name']}: Entry T-{v['entry_seconds_before_funding']}s, "
                f"Lev {v['leverage']}x, SL {v['hard_sl_pct']}%"
            )

        while self._running:
            try:
                t0 = time.time()
                self._step()
                self._consecutive_api_failures = 0
                elapsed = time.time() - t0
                sleep_time = max(0, 5.0 - elapsed)
                time.sleep(sleep_time)
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                self._consecutive_api_failures += 1
                backoff = min(2**self._consecutive_api_failures, 60)
                jitter = random.uniform(0, backoff * 0.25)
                time.sleep(backoff + jitter)

    def _step(self):
        # 1. Fetch funding rates
        try:
            funding_rates = self.exchange.exchange.fetch_funding_rates()
        except Exception as e:
            self._consecutive_api_failures += 1
            backoff = min(2**self._consecutive_api_failures, 60)
            jitter = random.uniform(0, backoff * 0.25)
            logger.error(
                f"Failed to fetch funding rates: {e} (retry in {backoff + jitter:.1f}s, "
                f"consecutive failures: {self._consecutive_api_failures})"
            )
            time.sleep(backoff + jitter)
            return

        # 2. Filter for potential symbols
        symbols_to_watch = []
        funding_map = {}
        now = time.time()

        n_total = 0
        n_positive = 0
        n_in_window = 0

        for symbol, data in funding_rates.items():
            rate = data.get("fundingRate")
            if rate is None:
                continue
            nxt = data.get("nextFundingTime") or data.get("nextFundingTimestamp") or data.get("fundingTimestamp")
            if not nxt:
                continue

            n_total += 1

            # We only care about positive funding (SHORT)
            if rate <= 0:
                continue

            n_positive += 1
            funding_time = nxt / 1000.0  # ms to s

            # Check if any playbook is within its entry window
            for pk, pcfg in self.playbooks.items():
                entry_window_start = funding_time - pcfg["entry_seconds_before_funding"]
                if (
                    now >= entry_window_start
                    and now < funding_time
                    and (symbol, pk) not in self._positions
                    and (symbol, pk) not in self._pending_entries
                ):
                    n_in_window += 1
                    symbols_to_watch.append(symbol)
                    funding_map[symbol] = {"fundingRate": rate, "funding_time": funding_time}
                    break

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
        n_vol_fail = 0
        n_spread_fail = 0
        n_yield_fail = 0
        n_entries = 0

        for symbol in symbols_to_watch:
            ticker = tickers.get(symbol)
            if not ticker or not ticker.get("bid") or not ticker.get("ask"):
                continue

            bid = ticker["bid"]
            ask = ticker["ask"]
            quote_vol = ticker.get("quoteVolume", 0)
            spread = (ask - bid) / ask
            rate = funding_map[symbol]["fundingRate"]
            taker_fee = self._get_taker_fee(symbol)
            net_yield = rate - (2 * taker_fee) - spread

            # Viability filters
            if quote_vol < self.min_volume:
                n_vol_fail += 1
                continue
            if spread > self.spread_cap:
                n_spread_fail += 1
                continue
            if net_yield <= self.min_net_yield:
                n_yield_fail += 1
                continue

            # Create pending entry
            for pk, pcfg in self.playbooks.items():
                if (symbol, pk) in self._positions or (symbol, pk) in self._pending_entries:
                    continue

                funding_time = funding_map[symbol]["funding_time"]
                entry_window_start = funding_time - pcfg["entry_seconds_before_funding"]

                if now >= entry_window_start:
                    logger.info(
                        f"ENTRY [{pk}] {symbol} at {ask} (Rate: {rate * 100:.4f}%, Yield: {net_yield * 100:.4f}%)"
                    )
                    pos = SimPosition(
                        symbol=symbol,
                        playbook_key=pk,
                        playbook_cfg=pcfg,
                        entry_price=ask,  # SHORT at ask
                        funding_rate=rate,
                        funding_time=funding_time,
                    )
                    self._positions[(symbol, pk)] = pos
                    n_entries += 1

        # 5. Update Active Positions
        closed_positions = []
        for key, pos in self._positions.items():
            ticker = tickers.get(pos.symbol)
            if not ticker:
                continue

            status = pos.update(ticker["bid"], ticker["ask"])
            if status:
                logger.info(
                    f"EXIT [{pos.playbook_key}] {pos.symbol} at {pos.exit_price} ({pos.exit_reason}) PnL: {pos.leveraged_pnl_pct:.2f}%"
                )
                closed_positions.append(key)
                self._log_trade(pos)
                self._update_summary(pos)

        for key in closed_positions:
            del self._positions[key]

        # Filter diagnostics (every 5 min)
        if now - self._last_filter_diag > 300:
            self._last_filter_diag = now
            logger.info(
                f"Filter diag: {n_total} symbols, {n_positive} positive funding, "
                f"{n_in_window} in entry window, "
                f"{n_vol_fail} vol-fail, {n_spread_fail} spread-fail, "
                f"{n_yield_fail} yield-fail, {n_entries} entries"
            )

        # Heartbeat
        if now - self._last_heartbeat > 60:
            active_count = len(self._positions)
            logger.info(f"Heartbeat: {active_count} positions active.")
            self._last_heartbeat = now

    def _log_trade(self, pos):
        with open(self.trades_csv, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
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
                    datetime.datetime.fromtimestamp(pos.exit_time).isoformat(),
                ]
            )

    def _update_summary(self, pos):        # Read existing summary
        summary = {}
        if self.summary_csv.exists():
            with open(self.summary_csv) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    summary[(row["symbol"], row["playbook"])] = row

        key = (pos.symbol, pos.playbook_key)
        s = summary.get(
            key,
            {
                "symbol": pos.symbol,
                "playbook": pos.playbook_key,
                "playbook_name": pos.name,
                "wins": 0,
                "losses": 0,
                "total_trades": 0,
                "win_rate": 0,
                "avg_raw_pnl_pct": 0,
                "avg_leveraged_pnl_pct": 0,
                "sum_leveraged_pnl_pct": 0,
            },
        )

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
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "symbol",
                    "playbook",
                    "playbook_name",
                    "wins",
                    "losses",
                    "total_trades",
                    "win_rate",
                    "avg_raw_pnl_pct",
                    "avg_leveraged_pnl_pct",
                    "sum_leveraged_pnl_pct",
                ],
            )
            writer.writeheader()
            for row in summary.values():
                writer.writerow(row)


def scan_viable_pairs(exchange, min_volume=500000, spread_cap=0.01, min_net_yield=0):
    """One-shot scan: fetch all pairs, calculate viability, output sorted table."""
    exchange.start()

    logger.info("Fetching funding rates for all symbols...")
    funding_rates = exchange.exchange.fetch_funding_rates()

    # Filter for positive funding with next funding time
    candidates = []
    for symbol, data in funding_rates.items():
        rate = data.get("fundingRate")
        if rate is None:
            continue
        nxt = data.get("nextFundingTime") or data.get("nextFundingTimestamp") or data.get("fundingTimestamp")
        if not nxt:
            continue
        if rate <= 0:
            continue
        candidates.append((symbol, rate, nxt))

    logger.info(f"Found {len(candidates)} symbols with positive funding rates.")

    if not candidates:
        print("\nNo symbols with positive funding rates found.")
        return

    # Fetch tickers for all candidates
    symbols = [c[0] for c in candidates]
    logger.info(f"Fetching tickers for {len(symbols)} symbols...")
    tickers = exchange.exchange.fetch_tickers(symbols)

    # Calculate viability for each
    viable = []
    for symbol, rate, nxt in candidates:
        ticker = tickers.get(symbol)
        if not ticker or not ticker.get("bid") or not ticker.get("ask"):
            continue

        bid = ticker["bid"]
        ask = ticker["ask"]
        quote_vol = ticker.get("quoteVolume", 0)
        spread = (ask - bid) / ask
        taker_fee = exchange.get_commission_rate(symbol).get("taker", DEFAULT_TAKER_FEE)
        net_yield = rate - (2 * taker_fee) - spread

        funding_time = nxt / 1000.0
        secs_to_funding = funding_time - time.time()

        passes_volume = quote_vol >= min_volume
        passes_spread = spread <= spread_cap
        passes_yield = net_yield > min_net_yield
        is_viable = passes_volume and passes_spread and passes_yield

        viable.append({
            "symbol": symbol,
            "funding_rate_pct": rate * 100,
            "net_yield_pct": net_yield * 100,
            "spread_pct": spread * 100,
            "taker_fee_pct": taker_fee * 100,
            "quote_volume": quote_vol,
            "bid": bid,
            "ask": ask,
            "secs_to_funding": secs_to_funding,
            "passes_volume": passes_volume,
            "passes_spread": passes_spread,
            "passes_yield": passes_yield,
            "is_viable": is_viable,
        })

    # Sort by net_yield descending
    viable.sort(key=lambda x: x["net_yield_pct"], reverse=True)

    # Output
    print(f"\n{'='*120}")
    print(f"{'Symbol':<22} {'Rate%':>8} {'NetYield%':>10} {'Spread%':>9} {'TakerFee%':>10} "
          f"{'QuoteVol':>14} {'Bid':>12} {'Ask':>12} {'ToFunding':>10} {'Viable':>7}")
    print(f"{'='*120}")

    for v in viable:
        flag = "YES" if v["is_viable"] else "no"
        to_funding = f"{v['secs_to_funding']:.0f}s" if v["secs_to_funding"] > 0 else "PASSED"
        print(
            f"{v['symbol']:<22} {v['funding_rate_pct']:>8.4f} {v['net_yield_pct']:>10.4f} "
            f"{v['spread_pct']:>9.4f} {v['taker_fee_pct']:>10.4f} {v['quote_volume']:>14,.0f} "
            f"{v['bid']:>12} {v['ask']:>12} {to_funding:>10} {flag:>7}"
        )

    viable_only = [v for v in viable if v["is_viable"]]
    print(f"\n{'='*120}")
    print(f"Total positive-funding symbols: {len(viable)}")
    print(f"Viable pairs (pass all filters): {len(viable_only)}")
    if viable_only:
        print("\nViable pairs for investigation:")
        for v in viable_only:
            print(f"  {v['symbol']:<22} Rate={v['funding_rate_pct']:.4f}%  "
                  f"NetYield={v['net_yield_pct']:.4f}%  "
                  f"Spread={v['spread_pct']:.4f}%  "
                  f"TakerFee={v['taker_fee_pct']:.4f}%  "
                  f"Vol={v['quote_volume']:,.0f}  "
                  f"ToFunding={v['secs_to_funding']:.0f}s")

    # Write to CSV
    scan_csv = Path("logs/dryrun") / f"scan_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    scan_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(scan_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "symbol", "funding_rate_pct", "net_yield_pct", "spread_pct",
            "taker_fee_pct", "quote_volume", "bid", "ask", "secs_to_funding",
            "passes_volume", "passes_spread", "passes_yield", "is_viable",
        ])
        for v in viable:
            writer.writerow([
                datetime.datetime.now().isoformat(),
                v["symbol"], v["funding_rate_pct"], v["net_yield_pct"], v["spread_pct"],
                v["taker_fee_pct"], v["quote_volume"], v["bid"], v["ask"],
                v["secs_to_funding"], v["passes_volume"], v["passes_spread"],
                v["passes_yield"], v["is_viable"],
            ])
    logger.info(f"Scan results written to {scan_csv}")


def main():
    parser = argparse.ArgumentParser(description="Fundee Dry-Run Engine")
    parser.add_argument("--testnet", action="store_true", help="Use Hyperliquid testnet")
    parser.add_argument("--no-volume", action="store_true", help="Ignore volume filter")
    parser.add_argument("--scan", action="store_true",
                        help="One-shot scan: fetch all pairs, output viable pairs table")
    args = parser.parse_args()

    exchange = ExchangeInterface(testnet=args.testnet)

    min_volume = 0 if args.no_volume else 500000
    spread_cap = 0.05 if args.testnet else 0.01
    min_net_yield = -0.001 if args.testnet else 0

    if args.scan:
        if args.testnet:
            logger.info(
                f"Testnet mode: relaxed filters (spread_cap={spread_cap}, min_net_yield={min_net_yield})"
            )
        scan_viable_pairs(
            exchange,
            min_volume=min_volume,
            spread_cap=spread_cap,
            min_net_yield=min_net_yield,
        )
        return

    engine = DryRunEngine(
        exchange,
        min_volume=min_volume,
        spread_cap=spread_cap,
        min_net_yield=min_net_yield,
    )
    if args.testnet:
        logger.info(
            f"Testnet mode: relaxed filters (spread_cap={spread_cap}, min_net_yield={min_net_yield})"
        )

    def handle_sigint(sig, frame):
        logger.info("Shutdown signal received...")
        engine.stop()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    engine.run()


if __name__ == "__main__":
    main()
