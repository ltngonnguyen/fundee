import argparse
import os
import sys
from datetime import datetime

import pandas as pd

sys.path.append(os.getcwd())

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.table import Table
except ImportError:
    print("Please install 'rich' library: uv add rich")
    sys.exit(1)

try:
    from fundee_shared import ExchangeInterface
except ImportError:
    print("Could not import ExchangeInterface. Make sure fundee_shared.py is in the directory.")
    sys.exit(1)

console = Console()
exchange = ExchangeInterface()


def load_anchors(file_path):
    if not os.path.exists(file_path):
        console.print(f"[red]Error: Anchor file '{file_path}' not found.[/red]")
        return None

    try:
        df = pd.read_csv(file_path)
        df.columns = [c.strip() for c in df.columns]
        if "Timestamp" in df.columns:
            df["Timestamp"] = pd.to_datetime(df["Timestamp"])
        return df
    except Exception as e:
        console.print(f"[red]Error reading anchors: {e}[/red]")
        return None


def fetch_trades_history(start_ms, end_ms, symbol=None):
    """Fetch trade history (with fees) from Hyperliquid via ccxt."""
    all_trades = []
    since = start_ms
    while True:
        try:
            trades = exchange.exchange.fetch_my_trades(
                symbol=symbol, since=since, limit=1000
            )
        except Exception as e:
            console.print(f"[red]Exception fetching trades: {e}[/red]")
            break
        if not trades:
            break
        all_trades.extend(trades)
        last_ts = int(trades[-1].get("timestamp") or 0)
        if last_ts >= end_ms or len(trades) < 1000:
            break
        since = last_ts + 1
    return all_trades


def fetch_funding_history(start_ms, end_ms, symbol=None):
    """Fetch funding payment history from Hyperliquid via ccxt."""
    all_funding = []
    since = start_ms
    while True:
        try:
            history = exchange.exchange.fetch_funding_history(
                symbol=symbol, since=since, limit=1000
            )
        except Exception as e:
            console.print(f"[red]Exception fetching funding history: {e}[/red]")
            break
        if not history:
            break
        all_funding.extend(history)
        last_ts = int(history[-1].get("timestamp") or 0)
        if last_ts >= end_ms or len(history) < 1000:
            break
        since = last_ts + 1
    return all_funding


def analyze_trades_with_api(file_path):
    anchors = load_anchors(file_path)
    if anchors is None or anchors.empty:
        console.print("[yellow]No trade anchors found.[/yellow]")
        return

    console.print(
        Panel(
            f"[bold blue]Trade Analysis (API-Backed)[/bold blue]\nSource: {file_path}",
            border_style="blue",
        )
    )

    min_time = anchors["EntryTime"].min()
    max_time = anchors["ExitTime"].max()
    start_ms = int((min_time - 300) * 1000)
    end_ms = int((max_time + 300) * 1000)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"Fetching trades ({datetime.fromtimestamp(start_ms / 1000)} to "
            f"{datetime.fromtimestamp(end_ms / 1000)})...",
            total=None,
        )
        trades = fetch_trades_history(start_ms, end_ms)
        progress.update(task, completed=50)
        task2 = progress.add_task("Fetching funding history...", total=None)
        funding = fetch_funding_history(start_ms, end_ms)
        progress.update(task2, completed=100)

    if not trades and not funding:
        console.print("[red]No trade/funding data returned from API.[/red]")
        return

    trades_df = pd.DataFrame(trades)
    funding_df = pd.DataFrame(funding)

    if not trades_df.empty:
        trades_df["timestamp"] = pd.to_numeric(trades_df["timestamp"])
        trades_df["amount"] = pd.to_numeric(trades_df["amount"])
        trades_df["price"] = pd.to_numeric(trades_df["price"])
        trades_df["fee_cost"] = trades_df.apply(
            lambda r: r.get("fee", {}).get("cost") if isinstance(r.get("fee"), dict) else 0,
            axis=1,
        )
    if not funding_df.empty:
        funding_df["timestamp"] = pd.to_numeric(funding_df["timestamp"])
        funding_df["amount"] = pd.to_numeric(funding_df["amount"])

    anchors["Realized_PnL"] = 0.0
    anchors["Funding_Fee"] = 0.0
    anchors["Commission"] = 0.0
    anchors["Net_PnL"] = 0.0

    for idx, row in anchors.iterrows():
        symbol = row["Symbol"]
        entry_ms = int(row["EntryTime"] * 1000)
        exit_ms = int(row["ExitTime"] * 1000)
        window_start = entry_ms - 2000
        window_end = exit_ms + 2000

        if not trades_df.empty:
            t_mask = (
                (trades_df["symbol"] == symbol)
                & (trades_df["timestamp"] >= window_start)
                & (trades_df["timestamp"] <= window_end)
            )
            trade_window = trades_df[t_mask]
        else:
            trade_window = pd.DataFrame()

        if not funding_df.empty:
            f_mask = (
                (funding_df["symbol"] == symbol)
                & (funding_df["timestamp"] >= window_start)
                & (funding_df["timestamp"] <= window_end)
            )
            funding_window = funding_df[f_mask]
        else:
            funding_window = pd.DataFrame()

        if not trade_window.empty:
            buys = trade_window[trade_window["side"] == "buy"]
            sells = trade_window[trade_window["side"] == "sell"]
            realized = (sells["price"] * sells["amount"]).sum() - (
                buys["price"] * buys["amount"]
            ).sum()
            commission = float(trade_window["fee_cost"].astype(float).sum())
        else:
            realized = 0.0
            commission = 0.0

        if not funding_window.empty:
            funding_fee = float(funding_window["amount"].astype(float).sum())
        else:
            funding_fee = 0.0

        anchors.at[idx, "Realized_PnL"] = realized
        anchors.at[idx, "Funding_Fee"] = funding_fee
        anchors.at[idx, "Commission"] = -abs(commission)
        anchors.at[idx, "Net_PnL"] = realized + funding_fee - abs(commission)

    total_trades = len(anchors)
    total_pnl = anchors["Net_PnL"].sum()
    total_commission = anchors["Commission"].sum()
    total_funding = anchors["Funding_Fee"].sum()

    winners = anchors[anchors["Net_PnL"] > 0]
    win_rate = (len(winners) / total_trades * 100) if total_trades > 0 else 0

    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")

    grid.add_row("Total Trades:", str(total_trades))
    grid.add_row(
        "Total Net PnL:",
        f"[bold {'green' if total_pnl >= 0 else 'red'}]${total_pnl:.4f}[/]",
    )
    grid.add_row("  - Realized PnL:", f"${anchors['Realized_PnL'].sum():.4f}")
    grid.add_row("  - Funding:", f"[green]${total_funding:.4f}[/green]")
    grid.add_row("  - Commission:", f"[red]${total_commission:.4f}[/red]")
    grid.add_row("Win Rate:", f"{win_rate:.2f}%")

    console.print(Panel(grid, title="Global Statistics (API Verified)", border_style="green"))

    strat_table = Table(title="Strategy Performance", box=box.ROUNDED)
    strat_table.add_column("Strategy")
    strat_table.add_column("Count")
    strat_table.add_column("Win Rate")
    strat_table.add_column("Net PnL")
    strat_table.add_column("Avg PnL")

    for strat, gdf in anchors.groupby("Strategy"):
        count = len(gdf)
        wr = (len(gdf[gdf["Net_PnL"] > 0]) / count * 100)
        pnl = gdf["Net_PnL"].sum()
        avg = gdf["Net_PnL"].mean()
        strat_table.add_row(
            strat,
            str(count),
            f"{wr:.1f}%",
            f"[{'green' if pnl >= 0 else 'red'}]${pnl:.2f}[/]",
            f"${avg:.2f}",
        )
    console.print(strat_table)

    details = Table(title="Latest Trades", box=box.MINIMAL_DOUBLE_HEAD)
    details.add_column("Time", style="dim")
    details.add_column("Symbol")
    details.add_column("Strategy")
    details.add_column("Prices (In->Out)")
    details.add_column("Funding")
    details.add_column("Comm")
    details.add_column("Net PnL")

    for _, row in anchors.sort_values("Timestamp", ascending=False).head(20).iterrows():
        pnl = row["Net_PnL"]
        color = "green" if pnl >= 0 else "red"
        details.add_row(
            row["Timestamp"].strftime("%H:%M:%S"),
            row["Symbol"],
            row["Strategy"],
            f"{row['EntryPrice']:.4f} -> {row['ExitPrice']:.4f}",
            f"{row['Funding_Fee']:.4f}",
            f"{row['Commission']:.4f}",
            f"[{color}]${pnl:.4f}[/{color}]",
        )
    console.print(details)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze Trades using ccxt API history")
    parser.add_argument(
        "file", nargs="?", default="logs/trade_anchors.csv", help="Path to Anchor CSV"
    )
    args = parser.parse_args()

    analyze_trades_with_api(args.file)
