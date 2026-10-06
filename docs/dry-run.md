# Dry-Run And Market Scanner

Fundee includes tools for researching funding-rate opportunities without placing live trades.

## Dry-Run Engine

Run the simulator with:

```bash
uv run --env-file .env python fundee_dryrun.py
```

The engine watches live funding and ticker data, simulates entries before funding events, and records closed simulated positions to CSV.

Useful flags:

```bash
uv run --env-file .env python fundee_dryrun.py --testnet
uv run --env-file .env python fundee_dryrun.py --no-volume
uv run --env-file .env python fundee_dryrun.py --scan
```

Output is written to `logs/dryrun/`, which is intentionally ignored by git.

## Playbooks

The default playbooks compare different entry timing, leverage, stop-loss, and max-margin assumptions around funding events.

| Playbook | Intent | Entry Timing | Leverage | Stop Loss | Max Margin |
| --- | --- | --- | --- | --- | --- |
| A | Short-window funding capture | 10 minutes before funding | 3x | -1.5% | Uncapped |
| B | Earlier funding-wave entry | 20 minutes before funding | 3x | -2.5% | Uncapped |
| C | Lower-risk thesis check | 20 minutes before funding | 2x | None | 50 USDC |

The simulator uses conservative fills for short entries and exits:

- Entry: ask price.
- Exit: bid price.
- PnL: calculated from price movement and leverage.

## Browser Scanner

Run the Gradio scanner with:

```bash
uv run --env-file .env python gradio_app.py
```

For testnet:

```bash
uv run --env-file .env python gradio_app.py --testnet
```

The scanner displays funding, spread, quote volume, estimated taker fees, net yield, funding interval, countdown, and a viability marker. It is intended for market review and research, not as a guarantee that a trade is safe or profitable.

## Notes

Dry-run and scanner results depend on live exchange data, liquidity, fee tier, and timing. Treat outputs as research signals only. Always validate behavior on testnet or with small size before using live capital.
