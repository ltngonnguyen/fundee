# Deployment

This project can be run directly with `uv` or supervised with systemd user services.

## Local Setup

```bash
uv sync --extra dev
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Required values:

```bash
HL_WALLET_ADDRESS=0xYourMainAccountAddress
HL_API_PRIVATE_KEY=0xYourApiWalletPrivateKey
```

Use an API wallet created in the Hyperliquid UI. Do not use a primary wallet private key.

## Headless Bot

```bash
uv run --env-file .env python fundee.py --headless
```

Testnet mode:

```bash
uv run --env-file .env python fundee.py --headless --testnet
```

## systemd User Services

The included service files are templates for a user-level systemd setup. They assume the repository lives at `%h/fundee` and that `uv` is available on the service `PATH`.

Install example:

```bash
mkdir -p ~/.config/systemd/user
cp fundee.service ~/.config/systemd/user/fundee.service
cp fundee-dryrun.service ~/.config/systemd/user/fundee-dryrun.service
systemctl --user daemon-reload
systemctl --user enable --now fundee.service
```

Dry-run service:

```bash
systemctl --user enable --now fundee-dryrun.service
```

Logs:

```bash
journalctl --user -u fundee.service -f
journalctl --user -u fundee-dryrun.service -f
```

If your checkout lives somewhere else, update `EnvironmentFile`, `WorkingDirectory`, and `ExecStart` in the copied service files.
