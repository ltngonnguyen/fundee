# Hyperliquid Migration Plan

**Goal:** Replace the Aster DEX custom web3 signing with `ccxt.hyperliquid`, parameterize the per-symbol funding interval, drop the hedge-mode code path, and self-containerize the project with `uv`.

**Status:** Complete. All 48 unit tests pass.

---

## Decisions

| Area | Decision |
| --- | --- |
| Exchange wrapper | `ccxt.hyperliquid` (auth via `walletAddress` + `privateKey`, `sandboxMode` for testnet) |
| Hedge mode | Removed — Hyperliquid is one-way only; `is_hedge_mode = False` is hard-coded |
| Funding interval | Per-symbol, captured in `ExchangeInterface.funding_interval_hours[sym]` and surfaced in the TUI scanner table |
| Env vars (new) | `HL_WALLET_ADDRESS`, `HL_API_PRIVATE_KEY`, `HL_BASE_URL` (optional), `HL_TESTNET` (1 = testnet) |
| Env vars (deprecated) | `ASTER_USER_ADDRESS`, `ASTER_API_SECRET`, `ASTER_API_KEY`, `ASTER_SIGNER_ADDRESS` — kept as aliases of `HL_*`; warn-once via `_emit_legacy_warning()` |
| Quote currency | `USDC` everywhere (no more USDT) |
| Slippage | `slippage=0.05` passed to ccxt on market and reduce-only market orders |
| TIF mapping | `GTX` → `Alo` (post-only on HL), `GTC` → `Gtc` |
| Project layout | `pyproject.toml` (hatchling) + `uv.lock`; `uv run` is the canonical launcher |
| Service file | `ExecStart=/home/johnd/fundee/.venv/bin/uv run python /home/johnd/fundee/fundee.py --headless` |

## Files Touched

- `pyproject.toml` — new; deps `pandas`, `requests`, `textual`, `rich`, `ccxt>=4.2.0`; dev deps `pytest`, `pytest-mock`, `coverage`, `ruff`
- `fundee_shared.py` — rewritten; `ExchangeInterface` is now a thin ccxt wrapper, `SmartOrderExecutor` is preserved
- `fundee.py` — refactored scanner (ccxt `fetch_funding_rates` / `fetch_tickers`); dropped Aster `position_side` plumbing; refreshed TUI columns
- `analyze_trades.py` — refactored to use `ccxt.fetch_my_trades` + `ccxt.fetch_funding_history` instead of the Aster `/fapi/v3/income` endpoint
- `fundee.service` — `ExecStart` updated to `uv run`
- `test_units.py` — new tests for `ExchangeInterface` (33), `TestFundingIntervalParameterization` (3), `TestNoHedgeMode` (2); existing `TestSmartOrderExecutor` and `TestFundeeLogic` updated for the new method signatures
- `test_suite.py` — removed (Aster V3 endpoint integration test is no longer applicable)
- `README.md` — installation now uses `uv sync --extra dev`

## Verification

- `uv run pytest test_units.py` — 48 passed in ~2.6s
- `uv run ruff check` — clean for newly-introduced code; pre-existing SIM114 in the ported `SmartOrderExecutor` branches are preserved
- `python -c "import fundee; import analyze_trades"` — both modules import cleanly

## Operator Runbook

```bash
# Install / sync
uv sync --extra dev

# Configure
cp .env.example .env
chmod 600 .env
$EDITOR .env

# Live
uv run --env-file .env python fundee.py --headless

# Testnet
HL_TESTNET=1 uv run --env-file .env python fundee.py --headless

# Analyze trades
uv run python analyze_trades.py logs/trade_anchors.csv

# Service
systemctl --user link ~/.config/systemd/user/fundee.service
systemctl --user daemon-reload
systemctl --user enable --now fundee
```

## Known Limitations / Manual Verification

- HL testnet smoke run was not executed in this environment; user must run with real (or testnet) creds to confirm TUI rendering and order routing end-to-end.
- `close_all_positions` uses a fixed `slippage=0.05`; this is conservative and can be made configurable later.
- The `position_side` keyword on `place_order` and `smart_execute` is preserved as a no-op for any out-of-tree caller.
- `get_commission_rate` returns a hardcoded `0.000432` maker / `0.000144` taker (user's HL tier rate). ccxt-hyperliquid does not implement `fetchTradingFees`.
- Credentials are loaded via `uv run --env-file .env` (uv 0.4+). If you forget the flag, the bot exits with `[fundee] Missing Hyperliquid credentials…` before any network call.
- `fetch_balance` and `fetch_positions` pass `params={"user": wallet}` explicitly to satisfy ccxt-hyperliquid's `handle_public_address` guard.

## 2026-06-06 (later): AutoKill toggle

Added a runtime toggle for the safety monitor ("killer bot"):

- **CLI**: `--no-kill` disables the safety monitor at startup. Headless and TUI both honor it.
- **TUI**: press `k` to toggle at runtime. The title bar shows `AutoKill: ON` (green) / `AutoKill: OFF` (red). State is also reported in the 60s heartbeat log.
- When disabled, the bot still opens/closes positions via `update_strategies` (entry/exit/SL/TP). It will **not** autonomously force-close a position that survived a funding cycle. The operator takes responsibility.
- The safety monitor's "safe zone" / "kill zone" timing still uses `datetime.now()` (local wall clock) — known mismatch with HL server time, deferred to a separate task.
