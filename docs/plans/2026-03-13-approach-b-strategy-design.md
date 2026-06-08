# Approach B Strategy Design Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement Approach B (1s market-in/market-out) strategy as the default while retaining the STRADDLE strategy (inactive), and dynamically sizing trades using 99% of available balance.

**Architecture:** We will introduce an `ACTIVE_STRATEGY` global flag. We'll modify `execute_strategy_entry` and `execute_strategy_exit` to accept a `use_market` boolean flag. This flag will bypass the `SmartOrderExecutor`'s limit order logic and place pure MARKET orders directly via `ExchangeInterface.place_order`. We will dynamically calculate `trade_size` within `execute_strategy_entry` using 99% of the currently fetched balance.

**Tech Stack:** Python, Requests, web3 (Aster DEX API via `fundee_shared.py`)

---

### Task 1: Add ACTIVE_STRATEGY global configuration

**Files:**
- Modify: `fundee.py` (near the top, around line 43)

**Step 1: Write minimal implementation**

Add the `ACTIVE_STRATEGY` configuration variable below `TRADE_SIZE_USDT`.

```python
TRADE_SIZE_USDT = Decimal("400.0")  # Size per trade in USDT
ACTIVE_STRATEGY = "APPROACH_B"  # Options: "STRADDLE", "APPROACH_B"
```

**Step 2: Commit**

```bash
git add fundee.py
git commit -m "feat(config): add ACTIVE_STRATEGY global flag"
```

---

### Task 2: Modify method signatures and execution logic for pure market orders

**Files:**
- Modify: `fundee.py:544` (approx) - `execute_strategy_entry`
- Modify: `fundee.py:646` (approx) - `execute_strategy_exit`

**Step 1: Write minimal implementation for `execute_strategy_entry`**

Update the signature to include `use_market=False`.

```python
    def execute_strategy_entry(
        self, strategy, symbol, direction, maker=False, funding_time_ms=None, funding_rate=0.0, use_market=False
    ):
```

Update the execution block (around line 631) to bypass `smart_execute` if `use_market` is true.

```python
        if use_market:
            self.interface.notify(f"ENTRY {symbol}: Pure Market Order (Approach B)")
            
            def market_worker():
                try:
                    resp = self.exchange.place_order(
                        symbol,
                        side,
                        "MARKET",
                        qty,
                        position_side=position_side
                    )
                    if resp and "orderId" in resp:
                        _on_success()
                    else:
                        _on_fail()
                except Exception as e:
                    self.interface.log_message(f"Market entry error: {e}")
                    _on_fail()

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
                    position_side=position_side,
                    switch_mode_time=switch_ts,
                    leverage=1,
                )
            )
```

**Step 2: Write minimal implementation for `execute_strategy_exit`**

Update the signature to include `use_market=False`.

```python
    def execute_strategy_exit(self, s, reason, maker=False, use_market=False):
```

Update the execution block (around line 680).

```python
        self.interface.notify(f"CLOSING {s['strategy']} {s['symbol']} ({reason})...")
        
        if use_market:
            def market_exit_worker():
                try:
                    resp = self.exchange.place_order(
                        s["symbol"],
                        side,
                        "MARKET",
                        s["quantity"],
                        position_side=position_side
                    )
                    if resp and "orderId" in resp:
                        _on_success()
                    else:
                        _on_fail()
                except Exception as e:
                    self.interface.log_message(f"Market exit error: {e}")
                    _on_fail()
            
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
                    position_side=position_side,
                    switch_mode_time=switch_ts,
                )
            )
```

**Step 3: Commit**

```bash
git add fundee.py
git commit -m "feat(strategy): add use_market flag to bypass SmartOrderExecutor for pure market orders"
```

---

### Task 3: Implement dynamic trade sizing (99% of balance)

**Files:**
- Modify: `fundee.py:560` (approx) - inside `execute_strategy_entry`

**Step 1: Write minimal implementation**

Replace the fixed `self.trade_size` logic with dynamic calculation.

Change:
```python
        if self.balance < self.trade_size:
            self.interface.notify(
                f"SKIP {strategy} {symbol}: Low Balance (${self.balance:.2f})"
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

        # Validate minimum trade size (5.5 USDT minimum)
        min_notional = Decimal("5.5")
        if self.trade_size < min_notional:
            self.interface.notify(
                f"SKIP {strategy} {symbol}: Trade size {self.trade_size} below minimum {min_notional} USDT"
            )
            return

        # Mark pending
        with self._pending_orders_lock:
            self.pending_orders.add(symbol)

        # Calculate roughly qty
        price = (
            ticker["ask"] if direction == "LONG" else ticker["bid"]
        )  # Approx for sizing
        qty = self.trade_size / price
```

To:
```python
        dynamic_trade_size = Decimal(str(self.balance)) * Decimal("0.99")

        # Validate minimum trade size (5.5 USDT minimum)
        min_notional = Decimal("5.5")
        if dynamic_trade_size < min_notional:
            self.interface.notify(
                f"SKIP {strategy} {symbol}: Balance too low (99% = ${dynamic_trade_size:.2f} < ${min_notional})"
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
        price = (
            ticker["ask"] if direction == "LONG" else ticker["bid"]
        )  # Approx for sizing
        qty = dynamic_trade_size / price
```

*Note: Further down in `_on_success` (~line 619), `s["margin"] = self.trade_size` needs to be changed to `s["margin"] = dynamic_trade_size` to reflect accurately on the UI.*

Change:
```python
            s["margin"] = self.trade_size
```

To:
```python
            s["margin"] = dynamic_trade_size
```

**Step 2: Commit**

```bash
git add fundee.py
git commit -m "feat(sizing): use 99% of available balance dynamically instead of static TRADE_SIZE_USDT"
```

---

### Task 4: Integrate Approach B into the Strategy Update Loop

**Files:**
- Modify: `fundee.py:740` (approx) - `update_strategies` ENTRY LOGIC
- Modify: `fundee.py:807` (approx) - `update_strategies` EXIT LOGIC

**Step 1: Write minimal implementation for ENTRY LOGIC**

Replace the existing STRADDLE logic:

```python
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
```

With:

```python
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
            elif ACTIVE_STRATEGY == "APPROACH_B":
                # APPROACH B: 1s market in
                if 0 < diff <= 1:
                    self.execute_strategy_entry(
                        "APPROACH_B",
                        symbol,
                        direction,
                        maker=False,
                        funding_time_ms=cand["next_funding_time"],
                        funding_rate=cand["funding_rate"],
                        use_market=True,
                    )
```

**Step 2: Write minimal implementation for EXIT LOGIC**

Add `APPROACH_B` exit logic below `STRADDLE`:

```python
            # Time-based Exit
            # Exit passively 1s after funding.
            # If we fail, killer_bot (running in parallel) will sweep us at T+60s.
            elif strategy == "STRADDLE":
                funding_time = s.get("funding_time", 0)
                if funding_time > 0:
                    time_since_funding = current_time - funding_time
                    if time_since_funding > 1.0:
                        self.execute_strategy_exit(s, "Post-Funding Exit", maker=True)
            elif strategy == "APPROACH_B":
                funding_time = s.get("funding_time", 0)
                if funding_time > 0:
                    time_since_funding = current_time - funding_time
                    # Exit strictly via Market 1s after funding
                    if time_since_funding > 1.0:
                        self.execute_strategy_exit(s, "Post-Funding Exit (Approach B)", maker=False, use_market=True)
```

**Step 3: Run project tests/linters**

Run the appropriate linters/tests in your project, e.g. `pytest` or `ruff check .`

**Step 4: Commit**

```bash
git add fundee.py
git commit -m "feat(strategy): integrate Approach B into strategy loop and make STRADDLE inactive"
```
