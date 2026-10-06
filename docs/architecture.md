# Architecture

Fundee is a Python trading research project for Hyperliquid perpetual futures. It combines a live funding-rate arbitrage bot, a dry-run hypothesis engine, a market scanner, and unit-tested exchange abstractions.

## Components

| File | Purpose |
| --- | --- |
| `fundee_shared.py` | Shared Hyperliquid exchange adapter and smart order execution helpers. |
| `fundee.py` | Live bot, strategy loop, headless mode, and Textual terminal UI. |
| `fundee_dryrun.py` | Simulated funding-wave engine that evaluates strategy playbooks without placing orders. |
| `gradio_app.py` | Browser-based market scanner for funding, spread, volume, fees, and viability. |
| `analyze_trades.py` | Post-trade analysis for anchor logs and exchange fills. |
| `test_units.py` | Unit coverage for exchange setup, strategy logic, sizing, and dry-run behavior. |

## Exchange Layer

The exchange adapter is intentionally small and isolated:

- Uses `ccxt.hyperliquid` for market data and order placement.
- Loads wallet credentials from environment variables only.
- Passes the wallet address explicitly for account-specific reads.
- Centralizes precision, sizing, fee lookup, balance reads, positions, and order placement.

This keeps trading logic testable without depending directly on ccxt internals throughout the codebase.

## Strategy Loop

The live bot monitors funding-rate candidates, filters for tradability, and manages active strategy state. The main viability checks are:

- Positive or negative funding direction depending on strategy intent.
- Bid/ask availability and spread cap.
- Quote volume threshold.
- Estimated net yield after fees and spread.
- Position and pending-order de-duplication.

The project emphasizes safety and observability: logs are written locally, runtime state is exposed in the UI, and tests cover key edge cases around order flow and strategy transitions.

## Dry-Run Engine

The dry-run engine uses live market data but never places orders. It simulates several funding-wave playbooks in parallel and records outcomes to CSV files under `logs/dryrun/`.

This makes it useful for validating assumptions before changing live strategy behavior.

## Interfaces

Fundee supports three operating modes:

- Headless CLI for unattended operation.
- Textual TUI for terminal monitoring and runtime controls.
- Gradio market scanner for browser-based market review.

## Configuration

Configuration is environment-driven. The only required variables are:

- `HL_WALLET_ADDRESS`
- `HL_API_PRIVATE_KEY`

Optional variables include:

- `HL_BASE_URL`
- `HL_TESTNET`

Credentials are intentionally not stored in code and should be supplied through `.env`, shell environment variables, or a process manager environment file.
