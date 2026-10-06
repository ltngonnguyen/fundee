# Fundee

Fundee is a Python research and execution project for Hyperliquid perpetual-futures funding-rate strategies. It combines a live trading bot, a dry-run simulator, a browser market scanner, and unit-tested exchange abstractions built on top of `ccxt`.

The project is designed to make funding opportunities observable, testable, and operationally safe before capital is deployed.

## Highlights

- Hyperliquid integration through `ccxt.hyperliquid`.
- Environment-based wallet authentication with API-wallet support.
- Headless live bot for unattended operation.
- Textual terminal UI for monitoring strategy state.
- Gradio market scanner for funding, spread, volume, fee, and net-yield review.
- Dry-run engine that simulates funding-wave playbooks without placing orders.
- CSV-based trade and simulation logs for post-run analysis.
- Unit tests around exchange setup, strategy behavior, sizing, and dry-run logic.

## Safety Notice

This is trading software. It can place real orders when run with live credentials. Use testnet first, use an API wallet rather than a primary wallet key, and review the strategy/risk settings before running with capital.

Fundee does not guarantee profitability. Funding rates, liquidity, spreads, fee tiers, and execution timing can change quickly.

## Quick Start

```bash
uv sync --extra dev
cp .env.example .env
chmod 600 .env
$EDITOR .env
uv run --env-file .env python fundee.py --headless --testnet
```

The `--env-file` flag is required when using `.env`; otherwise `os.getenv` will not see the configured credentials.

## Configuration

Required variables:

| Variable | Description |
| --- | --- |
| `HL_WALLET_ADDRESS` | Main Hyperliquid account public address. |
| `HL_API_PRIVATE_KEY` | Private key for a Hyperliquid API wallet. |

Optional variables:

| Variable | Description |
| --- | --- |
| `HL_BASE_URL` | Defaults to `https://api.hyperliquid.xyz`. |
| `HL_TESTNET=1` | Enables Hyperliquid testnet mode. |

Create an API wallet in the Hyperliquid UI under **More -> API Wallets**. API wallets can trade but cannot withdraw, which makes them safer for bot usage than a primary wallet key.

Legacy `ASTER_USER_ADDRESS` and `ASTER_API_SECRET` environment variables are still accepted as deprecated fallbacks.

## Running

Live/headless mode:

```bash
uv run --env-file .env python fundee.py --headless
```

Testnet mode:

```bash
uv run --env-file .env python fundee.py --headless --testnet
```

Terminal UI:

```bash
uv run --env-file .env python fundee.py
```

Browser market scanner:

```bash
uv run --env-file .env python gradio_app.py
```

Dry-run simulator:

```bash
uv run --env-file .env python fundee_dryrun.py
```

One-shot dry-run viability scan:

```bash
uv run --env-file .env python fundee_dryrun.py --scan
```

Analyze recorded trades:

```bash
uv run python analyze_trades.py logs/trade_anchors.csv
```

## Testing

```bash
uv run pytest
uv run ruff check
```

The repository also includes `run_tests.sh` for coverage-based local test runs.

## Project Layout

| Path | Purpose |
| --- | --- |
| `fundee.py` | Live strategy loop, headless runner, and Textual UI. |
| `fundee_shared.py` | Exchange adapter and smart order execution helpers. |
| `fundee_dryrun.py` | Dry-run funding strategy simulator. |
| `gradio_app.py` | Browser-based market scanner. |
| `analyze_trades.py` | Post-trade analysis utility. |
| `test_units.py` | Unit test suite. |
| `docs/architecture.md` | Architecture overview. |
| `docs/dry-run.md` | Dry-run and scanner documentation. |
| `docs/deployment.md` | systemd and deployment notes. |

## Deployment

Example user-level systemd service files are included:

- `fundee.service`
- `fundee-dryrun.service`

They are templates that assume the repository lives at `%h/fundee`. See `docs/deployment.md` before installing them.

## Public Repo Hygiene

- `.env` and runtime logs are ignored by git.
- `.env.example` is safe to commit and contains placeholders only.
- Generated logs and dry-run CSV output are written under `logs/` and ignored.
- Never commit private keys, wallet seed phrases, or real `.env` files.
