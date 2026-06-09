# Dry-Run Hypothesis-Testing Engine Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a standalone dry-run engine that simulates 3 funding-wave entry playbooks in parallel against live market data, logging results to CSV without placing real trades.

**Architecture:** A new `fundee_dryrun.py` module that reuses `ExchangeInterface` from `fundee_shared.py` for market data, runs a background loop fetching funding rates + tickers, tracks simulated positions per playbook with PnL events, and writes structured CSV logs. The engine is headless (print to stdout) and ships with a systemd service file for production background running.

**Tech Stack:** Python 3.10+, ccxt, ExchangeInterface (existing), csv/stdout logging.

---

### Task 1: Create `fundee_dryrun.py` — the main engine file

**Files:**
- Create: `fundee_dryrun.py`
- Modify: `pyproject.toml` (add entry point)
- Test: `test_units.py` (add basic structure tests)

**Step 1: Define the playbook configs**

Three playbooks with these parameters:

| Playbook | Name            | Entry (before funding) | Leverage | Hard SL  | Trail Trigger        | Trail Pullback       | Max Margin |
|----------|-----------------|------------------------|----------|----------|----------------------|----------------------|------------|
| A        | Shitcoin Hunter | T-10 min (600s)        | 3x       | -1.5%    | 2×\|funding_rate\|    | 1×\|funding_rate\|    | none       |
| B        | Funding Surfer  | T-20 min (1200s)       | 3x       | -2.5%    | 2×\|funding_rate\|    | 1×\|funding_rate\|    | none       |
| C        | Pure Thesis     | T-20 min (1200s)       | 2x       | none     | 2×\|funding_rate\|    | 1×\|funding_rate\|    | $50        |

All playbooks enter SHORT when funding_rate > 0. Direction = SHORT to receive funding + ride the exit wave.
Trailing stop: activates when PnL reaches trigger threshold (2×|funding_rate|% leveraged), then exits if pullback exceeds 1×|funding_rate|% from peak.
Simulated fills: ENTRY at ask (conservative for SHORT), EXIT at bid (conservative for SHORT).

```python
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
```

Common params: `trail_trigger_mult=2.0`, `trail_pullback_mult=1.0`.

**Step 2: Write the position tracker class**

`SimPosition` tracks one simulated position:
- `symbol`, `playbook_key`, `entry_price`, `entry_time`, `entry_ask`, `entry_bid`
- `max_leveraged_pnl_pct` (track peak for trailing stop)
- `hard_sl_price`, `trail_trigger_price`, `trail_stop_price`
- `status`: "WAITING", "OPEN", "CLOSED_WIN", "CLOSED_SL", "CLOSED_TRAIL", "CLOSED_EXPIRED", "SKIPPED"
- `exit_reason`, `exit_price`, `exit_time`, `raw_pnl_pct`, `leveraged_pnl_pct`, `funding_rate`

Method: `update(current_bid, current_ask)` — returns new status if closed on this tick.

Entry fill: `entry_price = ask` (SHORT at ask, conservative).
Exit fill: `exit_price = bid` (cover SHORT at bid, conservative).
PnL for SHORT: `pnl_pct = (entry_price - exit_price) / entry_price`.
Leveraged: `pnl_pct * leverage`.

**Step 3: Write the `DryRunEngine` class**

- `__init__(exchange_interface, playbooks=PLAYBOOKS, min_volume=500000, spread_cap=0.005, min_net_yield=0, output_dir="logs/dryrun")`
- Creates `logs/dryrun/` directory.
- Opens a CSV writer for `trades_YYYYMMDD.csv` with columns:
  `timestamp,symbol,playbook,name,funding_rate_pct,entry_price,exit_price,dir,raw_pnl_pct,leveraged_pnl_pct,margin,leverage,exit_reason,entry_time,exit_time`
- Opens a summary CSV `summary_YYYYMMDD.csv` keyed by symbol+playbook with aggregate stats.

Data structures:
- `_positions`: dict of active SimPosition keyed by `(symbol, playbook_key)`
- `_pending_entries`: dict of `(symbol, playbook_key)` → `{"funding_rate", "funding_time_epoch", "ask", "bid"}`
- `_premium_data`: latest funding data from `fetch_funding_rates()`
- `_ticker_map`: latest bid/ask from `fetch_tickers()`

**Step 4: Implement the main loop**

`run()` method:
1. Load markets via `exchange.load_markets()` (reuse existing ExchangeInterface).
2. Print startup banner with playbook configs.
3. Main loop: every 1s:
   a. Fetch funding rates (`exchange.exchange.fetch_funding_rates()`)
   b. Fetch tickers for funding-rate symbols (`exchange.exchange.fetch_tickers(symbols)`)
   c. Process new entries: for each pair with funding_time, check if within entry window, add to `_pending_entries`
   d. Check `_pending_entries`: if entry time reached, create SimPosition, move to `_positions`
   e. For each active position: call `pos.update(bid, ask)`. If closed, log to CSV.
   f. Check expired: positions past funding_time + 5min = "CLOSED_EXPIRED"
   g. Print status heartbeat every 60s.

Entry eligibility:
- `funding_rate` sign must be positive (for SHORT direction)
- `net_yield = abs(rate) - 2*taker_fee - spread` must be > 0
- Volume check: `quoteVolume >= min_volume`
- Spread check: `spread <= spread_cap`

Only track symbols that pass viability AND have funding data.

**Step 5: Implement CSV logging**

Two files:
1. `trades_YYYYMMDD.csv` — one row per closed position. Written immediately on close.
2. `summary_YYYYMMDD.csv` — updated after each close: `symbol,playbook,playbook_name,wins,losses,total_trades,win_rate,avg_raw_pnl_pct,avg_leveraged_pnl_pct,sum_leveraged_pnl_pct`

**Step 6: CLI entry point**

```python
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--no-volume", action="store_true")
    args = parser.parse_args()
    ...
```

**Step 7: Add to pyproject.toml**

```toml
[project.scripts]
fundee-dryrun = "fundee_dryrun:main"
```

**Step 8: Run ruff to verify**

Run: `uv run ruff check fundee_dryrun.py`
Expected: clean

**Step 9: Write unit tests**

Add to `test_units.py`:
- Test `SimPosition` creation and PnL calculation
- Test hard SL triggers
- Test trailing stop triggers
- Test that only positive funding rate symbols are entered

**Step 10: Commit**

```bash
git add fundee_dryrun.py pyproject.toml test_units.py
git commit -m "feat: add dry-run hypothesis-testing engine with 3 playbooks"
```

---

### Task 2: Create systemd service file

**Files:**
- Create: `fundee-dryrun.service`

**Step 1: Write the service file**

Same pattern as `fundee.service` but points to `fundee_dryrun.py` instead.

```ini
[Unit]
Description=Fundee Dry-Run Hypothesis Engine
After=network.target

[Service]
Type=simple
EnvironmentFile=/home/johnd/fundee/.env
User=johnd
WorkingDirectory=/home/johnd/fundee
ExecStart=/home/johnd/.local/bin/uv run --env-file /home/johnd/fundee/.env python /home/johnd/fundee/fundee_dryrun.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Step 2: Commit**

```bash
git add fundee-dryrun.service
git commit -m "feat: add systemd service for dry-run engine"
```

---

### Task 3: End-to-end verification

**Step 1: Start the engine for 5 minutes, verify logs are produced**

```bash
timeout 120 uv run --env-file .env python fundee_dryrun.py
```

Check: `ls -la logs/dryrun/` should show `trades_*.csv` and `summary_*.csv`.

**Step 2: Verify CSV format**

Check columns match expected schema.

**Step 3: Final ruff check**

```bash
uv run ruff check fundee_dryrun.py test_units.py
```

**Step 4: Final commit**

```bash
git add -A
git commit -m "chore: final verification of dry-run engine"
```
