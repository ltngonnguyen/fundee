"""Gradio MVP — Hyperliquid market scanner showing all pairs ranked by abs(funding_rate).

Includes native Hyperliquid perps plus HIP-3 builder-deployed markets (XYZ, Felix, Kinetiq, etc.)
by querying the raw Info API for each perp DEX.

Run:
    uv run python gradio_app.py
    uv run python gradio_app.py --testnet
"""

import argparse
import logging
import threading
import time
from datetime import timedelta

import gradio as gr
import pandas as pd

from fundee_shared import ExchangeInterface

logger = logging.getLogger(__name__)

# ── Data-fetching thread ────────────────────────

class DataFetcher:
    def __init__(self, exchange):
        self.exchange = exchange
        self._lock = threading.Lock()
        self.rows = []
        self.balance = "N/A"
        self._running = True
        # Maps raw HIP-3 symbol name (e.g. "xyz:SKHX") → ccxt-normalized symbol
        self._hip3_symbol_map = {}
        # List of all HIP-3 ccxt symbols for bulk ticker fetching
        self._hip3_ccxt_syms_all = []
        # Per-dex raw symbol groupings: {"xyz": ["xyz:SKHX", ...], "flx": [...], ...}
        self._hip3_per_dex = {}
        # Dex names in order matching allPerpMetas indices (after native[0])
        self._hip3_dex_names = []

    def start(self):
        self.exchange.log("[gradio] loading markets from Hyperliquid (may take a few seconds)…")
        t0 = time.time()
        self.exchange.load_markets()
        self.exchange.log(
            f"[gradio] load_markets done in {(time.time() - t0):.2f}s — "
            f"{len(self.exchange.precision_map)} swap markets available"
        )

        # Discover ALL perp DEXes (native + HIP-3 builders) via allPerpMetas.
        # allPerpMetas returns a flat list of dicts, one per DEX, each with:
        #   universe[] — list of {name, szDecimals, maxLeverage, ...}
        # It does NOT include asset_ctxs (funding rates). We fetch those
        # per-DEX later via metaAndAssetCtxs.
        self.exchange.log("[gradio] discovering perp dexes via allPerpMetas…")
        try:
            perp_metas = self.exchange.exchange.public_post_info({"type": "allPerpMetas"})
        except Exception as e:
            self.exchange.log(f"[gradio] allPerpMetas failed: {e}")
            perp_metas = []

        # perp_metas is a list of per-dex metadata dicts (no asset ctxs).
        # Index 0 = native HL perps, 1 = xyz, 2 = flx, 3 = vntl, etc.
        # Each entry: {"universe": [...], "marginTables": [...], "collateralToken": N}
        native_markets = []
        self._hip3_dex_names = []
        for i, meta in enumerate(perp_metas):
            if not isinstance(meta, dict):
                continue
            universe = meta.get("universe", [])
            if i == 0:
                native_markets = [u.get("name", "") for u in universe]
            else:
                # Figure out the dex name from the first asset's prefix
                dex_name = "unknown"
                if universe:
                    raw = universe[0].get("name", "")
                    prefix = raw.split(":")[0] if ":" in raw else "?"
                    dex_name = prefix
                self._hip3_dex_names.append(dex_name)
                self.exchange.log(
                    f"[gradio]   {dex_name}: {len(universe)} markets (e.g. {universe[0].get('name', '?')}, ... {universe[-1].get('name', '?')})"
                )
            self.exchange.log(
                f"[gradio]   native: {len(native_markets)} markets from allPerpMetas"
            )

        # Build a mapping: raw HIP-3 symbol (e.g. "xyz:SKHX") → ccxt symbol
        # We need this because ccxt normalizes "xyz:SKHX" → "XYZ-SKHX/USDC:USDC"
        # but different HIP-3 dexes use different collateral tokens.
        # Strategy: query all available ccxt markets and do prefix matching.
        markets_all = self.exchange.exchange.load_markets()
        # Index by upper-cased base for fast lookup
        _by_base = {}
        for sym, m in markets_all.items():
            if not m.get("swap") or not m.get("active", True):
                continue
            base = m.get("base", "")
            _by_base[base.upper()] = sym

        # Build hip3 symbol map across ALL dexes
        self._hip3_symbol_map = {}
        self._hip3_ccxt_syms_all = []
        per_dex = {name: [] for name in self._hip3_dex_names}
        for i, meta in enumerate(perp_metas):
            if i == 0 or not isinstance(meta, dict):
                continue
            universe = meta.get("universe", [])
            dex_name = self._hip3_dex_names[i - 1]  # offset by native
            for u in universe:
                raw_name = u.get("name", "")
                # raw_name e.g. "xyz:SKHX" → ccxt base becomes "XYZ-SKHX"
                parts = raw_name.split(":")
                if len(parts) != 2:
                    continue
                prefix, asset = parts
                ccxt_base = prefix.upper() + "-asset}"
                ccxt_sym = _by_base.get(ccxt_base)
                if ccxt_sym:
                    self._hip3_symbol_map[raw_name] = ccxt_sym
                    self._hip3_ccxt_syms_all.append(ccxt_sym)
                    per_dex[dex_name].append(raw_name)
                else:
                    # Try alternate: ccxt might use different base format
                    for candidate_base, candidate_sym in _by_base.items():
                        if candidate_base.startswith(prefix.upper() + "-") and candidate_base.endswith(asset.upper()):
                            self._hip3_symbol_map[raw_name] = candidate_sym
                            self._hip3_ccxt_syms_all.append(candidate_sym)
                            per_dex[dex_name].append(raw_name)
                            break

        # Store per-dex groups for per-dex metaAndAssetCtxs calls
        self._hip3_per_dex = per_dex

        for name, syms in self._hip3_per_dex.items():
            self.exchange.log(f"[gradio]   {name}: mapped {len(syms)} ccxt symbols")
        self.exchange.log(
            f"[gradio] total HIP-3 symbols mapped: {len(self._hip3_symbol_map)}"
        )
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._fetch_cycle()
            except Exception as e:
                logger.warning(f"[gradio] fetch cycle error: {e}")
            time.sleep(2)

    def _fetch_cycle(self):
        rows = []

        # ── 1. Native Hyperliquid perps (via ccxt) ──
        try:
            funding_rates = self.exchange.exchange.fetch_funding_rates()
            # Only fetch tickers for symbols that have funding rates (much faster:
            # ~180 symbols vs 341, avoids dead HIP-3 symbols with no live price data)
            fr_symbols = set(funding_rates.keys()) & set(self.exchange.precision_map.keys())
            tickers = self.exchange.exchange.fetch_tickers(list(fr_symbols))
        except Exception:
            return  # skip cycle

        for sym, fr_data in funding_rates.items():
            if sym not in self.exchange.precision_map:
                continue
            tik = tickers.get(sym, {})
            ask = float(tik.get("ask") or 0)
            bid = float(tik.get("bid") or 0)
            if ask <= 0 or bid < 0:
                continue
            rate = float(fr_data.get("fundingRate", 0) or 0)
            spread_pct = ((ask - bid) / ask * 100) if ask > 0 else 0.0
            nxt = fr_data.get("nextFundingTimestamp") or fr_data.get("fundingTimestamp")
            cd = self._countdown(nxt)
            interval_h = self.exchange.funding_interval_hours.get(sym, 1)
            rows.append(self._make_row(sym, ask, rate, interval_h, cd, spread_pct))

        # ── 2. HIP-3 builder-deployed perps (all dexes: xyz, flx, vntl, hyna, km, etc.) ──
        # Fetch all HIP-3 tickers in one bulk call
        try:
            hip3_tickers = self.exchange.exchange.fetch_tickers(self._hip3_ccxt_syms_all) if self._hip3_ccxt_syms_all else {}
        except Exception:
            hip3_tickers = {}

        for dex_name, raw_symbols in self._hip3_per_dex.items():
            if not raw_symbols:
                continue
            try:
                resp = self.exchange.exchange.public_post_info({
                    "type": "metaAndAssetCtxs",
                    "dex": dex_name,
                })
            except Exception:
                continue

            universe = resp[0].get("universe", [])
            asset_ctxs = resp[1]
            # Build index from raw name → ctx for O(1) lookup
            ctx_by_name = {}
            for i, u in enumerate(universe):
                if i < len(asset_ctxs):
                    ctx_by_name[u.get("name", "")] = asset_ctxs[i]

            for raw_sym in raw_symbols:
                ccxt_sym = self._hip3_symbol_map.get(raw_sym)
                if not ccxt_sym:
                    continue
                ctx = ctx_by_name.get(raw_sym)
                if ctx is None:
                    continue
                rate_str = ctx.get("funding", "0")
                rate = float(rate_str)

                tik = hip3_tickers.get(ccxt_sym, {})
                ask = float(tik.get("ask") or 0)
                bid = float(tik.get("bid") or 0)
                if ask <= 0:
                    continue

                spread_pct = ((ask - bid) / ask * 100) if ask > 0 else 0.0
                cd = self._countdown(None)
                interval_h = self.exchange.funding_interval_hours.get(ccxt_sym, 1)
                rows.append(self._make_row(ccxt_sym, ask, rate, interval_h, cd, spread_pct))

        # Sort by abs(funding) descending
        rows.sort(key=lambda r: r["Abs Funding"], reverse=True)

        # Try to fetch balance (non-critical)
        balance = "N/A"
        try:
            bal = self.exchange.get_balance()
            if bal is not None:
                balance = f"{bal:,.2f} USDC"
        except Exception:
            pass

        with self._lock:
            self.rows = rows
            self.balance = balance

    def _countdown(self, nxt):
        if nxt:
            d = (float(nxt) / 1000) - time.time()
            return str(timedelta(seconds=int(d))) if d > 0 else "FUNDING"
        return "N/A"

    def _make_row(self, sym, ask, rate, interval_h, cd, spread_pct):
        abs_funding = abs(rate) * 100
        interval_lbl = f"{interval_h}h"
        return {
            "Symbol": sym,
            "Price": ask,
            "Abs Funding": abs_funding,
            "Funding": rate * 100,
            "Dir": "SHORT" if rate > 0 else "LONG" if rate < 0 else "—",
            "Interval": interval_lbl,
            "Countdown": cd,
            "Spread": spread_pct,
        }

    def get_df(self):
        with self._lock:
            return pd.DataFrame(self.rows)

    def get_balance(self):
        with self._lock:
            return self.balance


# ── App ──────────────────

def build_header(fetcher):
    bal = fetcher.get_balance()
    with gr.Row():
        balance_md = gr.Markdown(f"**Balance:** {bal}", elem_id="balance")
        refresh_btn = gr.Button("Refresh", variant="secondary", size="sm")
    return balance_md, refresh_btn


def refresh_ui(fetcher):
    df = fetcher.get_df()
    bal = fetcher.get_balance()
    n_pairs = len(df)
    header = f"**Balance:** {bal}  |  **Pairs:** {n_pairs}  |  **Updated:** {time.strftime('%H:%M:%S')}"
    return df, header


def create_app(testnet=False):
    # Bubble "still loading" warnings to stderr only
    exchange_ui_logger = logging.getLogger("gradio_ui")
    exchange = ExchangeInterface(logger=exchange_ui_logger.info, testnet=testnet)

    fetcher = DataFetcher(exchange)
    fetcher.start()

    with gr.Blocks(title="FunDee — Hyperliquid Market Scanner", theme=gr.themes.Soft()) as app:
        gr.Markdown("# FunDee — Hyperliquid Market Scanner")
        header_md = gr.Markdown("**Balance:** —  |  **Pairs:** —  |  **Updated:** —")

        table = gr.Dataframe(
            headers=["Symbol", "Price", "Abs Funding", "Funding", "Dir", "Interval", "Countdown", "Spread"],
            datatype=["str", "number", "number", "number", "str", "str", "str", "number"],
            column_widths=["16%", "10%", "12%", "10%", "10%", "8%", "10%", "12%"],
            interactive=False,
            max_height=800,
            wrap=True,
        )

        # Refresh every 2 seconds
        timer = gr.Timer(2)

        @timer.tick(inputs=[], outputs=[table, header_md])
        def _tick():
            return refresh_ui(fetcher)

        # Initial render on page open
        app.load(_tick, inputs=[], outputs=[table, header_md])

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FunDee Gradio Market Scanner")
    parser.add_argument("--testnet", action="store_true", help="Use Hyperliquid testnet")
    parser.add_argument("--port", type=int, default=7860, help="Port to listen on (default: 7860)")
    parser.add_argument("--share", action="store_true", help="Create a public share link")
    args = parser.parse_args()

    app = create_app(testnet=args.testnet)
    app.queue(default_concurrency_limit=10)
    app.launch(server_port=args.port, share=args.share)
