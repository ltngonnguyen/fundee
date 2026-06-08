# fundee

Hyperliquid perpetual-futures funding-rate arbitrage bot, backed by [ccxt](https://github.com/ccxt/ccxt).

## Quick start

```bash
uv sync --extra dev
cp .env.example .env
chmod 600 .env          # protect your private key
$EDITOR .env            # fill in HL_WALLET_ADDRESS and HL_API_PRIVATE_KEY
uv run --env-file .env python fundee.py --headless
```

The `--env-file` flag is **required** — without it the `os.getenv` calls in
`fundee_shared.py` will return `None` and the bot will exit with a clear error.

## Auth

Hyperliquid uses wallet-based auth (not HMAC):

- `HL_WALLET_ADDRESS` — your main Hyperliquid account public address.
- `HL_API_PRIVATE_KEY` — private key of an **API wallet** (create one in the Hyperliquid UI under **More → API Wallets**). API wallets can trade but cannot withdraw.

Optional env vars:

- `HL_BASE_URL` — defaults to `https://api.hyperliquid.xyz` (mainnet).
- `HL_TESTNET=1` — switches ccxt into `sandboxMode` (Hyperliquid testnet).

The legacy `ASTER_USER_ADDRESS` / `ASTER_API_SECRET` are still accepted as fallbacks for one release cycle, with a deprecation warning logged at startup.

## Tests

```bash
uv run pytest
uv run ruff check
```

## Layout

- `fundee_shared.py` — `ExchangeInterface` (ccxt-backed) and `SmartOrderExecutor` (passive → aggressive chaser).
- `fundee.py` — `FundeeLogic`: scanner, STRADDLE & APPROACH_B strategies, Textual TUI.
- `analyze_trades.py` — post-trade PnL analyzer (reads `logs/trade_anchors.csv` + on-chain fills).
- `fundee.service` — systemd unit (uses `uv run --env-file`).
