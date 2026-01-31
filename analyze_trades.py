import pandas as pd
import argparse
import os
import sys
import json
from datetime import datetime, timedelta
import numpy as np

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.text import Text
except ImportError:
    print("Please install 'rich' library: pip install rich")
    sys.exit(1)

console = Console()

def load_csv(file_path):
    if not os.path.exists(file_path):
        return None
    try:
        df = pd.read_csv(file_path)
        df.columns = [c.strip() for c in df.columns]
        if 'Timestamp' in df.columns:
             df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        return df
    except: return None

def analyze_order_quality(order_path):
    df = load_csv(order_path)
    if df is None or df.empty: return
    
    console.print(Panel("[bold yellow]Order Quality Analysis[/bold yellow]", border_style="yellow"))
    
    # Fill Rate
    total_orders = len(df[df['Status'] == 'NEW']) # Assuming 'NEW' marks intent
    # Or just count unique OrderIds
    unique_orders = df['OrderId'].nunique()
    filled = df[df['Status'] == 'FILLED']['OrderId'].nunique()
    
    fill_rate = (filled / unique_orders * 100) if unique_orders > 0 else 0
    
    # Slippage (AvgPrice vs Price) - Only for Limit orders that filled
    filled_orders = df[df['Status'] == 'FILLED'].copy()
    if not filled_orders.empty:
        # Avoid div by zero
        filled_orders['Slippage'] = (filled_orders['AvgPrice'] - filled_orders['Price']) / filled_orders['Price'] * 100
        avg_slippage = filled_orders['Slippage'].abs().mean()
    else:
        avg_slippage = 0.0

    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_row("Unique Orders:", str(unique_orders))
    grid.add_row("Filled Orders:", str(filled))
    grid.add_row("Fill Rate:", f"{fill_rate:.2f}%")
    grid.add_row("Avg Slippage:", f"{avg_slippage:.4f}%")
    console.print(grid)

def analyze_market_opportunities(market_path):
    df = load_csv(market_path)
    if df is None or df.empty: return
    
    console.print(Panel("[bold cyan]Market Opportunity Analysis[/bold cyan]", border_style="cyan"))
    
    # Avg Spread
    avg_spread = df['Spread'].mean() * 100
    
    # Potential Profit
    avg_est_profit = df['EstProfit'].mean() * 100
    
    # Count High Yield Events (> 0.1%)
    high_yield = df[df['FundingRate'].abs() > 0.001]
    
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_row("Avg Market Spread:", f"{avg_spread:.4f}%")
    grid.add_row("Avg Est. Profit:", f"{avg_est_profit:.4f}%")
    grid.add_row("High Yield Events (>0.1%):", str(len(high_yield)))
    console.print(grid)

def load_trades(file_path):
    if not os.path.exists(file_path):
        console.print(f"[red]Error: File '{file_path}' not found.[/red]")
        return None

    try:
        df = pd.read_csv(file_path)
    except Exception as e:
        console.print(f"[red]Error reading file: {e}[/red]")
        return None

    if df.empty:
        console.print("[yellow]No trades found in the file.[/yellow]")
        return None

    # Clean columns
    df.columns = [c.strip() for c in df.columns]
    
    # Parse Timestamp
    try:
        df['Timestamp'] = pd.to_datetime(df['Timestamp'])
    except Exception as e:
        console.print(f"[red]Error parsing timestamps: {e}[/red]")
        return None
        
    df = df.sort_values('Timestamp')
    return df

def generate_period_stats(df, period_code, label):
    # Resample
    # grouping by period. 
    # We take the sum of PnL, count of trades.
    
    # Set index
    temp_df = df.set_index('Timestamp')
    
    resampler = temp_df.resample(period_code)
    
    stats = resampler.agg({
        'Net_PnL_USDT': 'sum',
        'Net_PnL_Pct': 'mean', 
        'Strategy': 'count' # Trade count
    })
    
    # Win rate per period
    def win_rate(x):
        if len(x) == 0: return 0.0
        return (x > 0).sum() / len(x) * 100
    
    win_rates = resampler['Net_PnL_USDT'].apply(win_rate)
    stats['Win_Rate'] = win_rates
    
    # Drop empty periods
    stats = stats[stats['Strategy'] > 0].copy()
    
    return stats, label

def print_period_table(stats, title):
    table = Table(title=title, box=box.SIMPLE_HEAVY, show_lines=True)
    table.add_column("Period", style="cyan", no_wrap=True)
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("PnL (USDT)", justify="right")
    
    for index, row in stats.iterrows():
        # Colorize PnL
        pnl = row['Net_PnL_USDT']
        pnl_str = f"${pnl:.2f}"
        if pnl > 0: pnl_str = f"[green]+{pnl_str}[/green]"
        elif pnl < 0: pnl_str = f"[red]{pnl_str}[/red]"
        
        # Win Rate color
        wr = row['Win_Rate']
        wr_style = "green" if wr >= 50 else "red"
        
        table.add_row(
            str(index),
            str(int(row['Strategy'])),
            f"[{wr_style}]{wr:.1f}%[/{wr_style}]",
            pnl_str
        )
    
    console.print(table)

def print_latest_trades(df, count=50):
    table = Table(title=f"Latest {count} Trades", box=box.MINIMAL_DOUBLE_HEAD, show_lines=True)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Symbol")
    table.add_column("Side")
    table.add_column("Type")
    table.add_column("Price")
    table.add_column("Funding")
    table.add_column("PnL")

    # Sort descending for display
    latest = df.sort_values('Timestamp', ascending=False).head(count)

    for _, row in latest.iterrows():
        pnl = row['Net_PnL_USDT']
        pnl_color = "green" if pnl >= 0 else "red"
        
        # Handle Funding column if exists
        funding = row.get('Funding', 0.0)
        
        table.add_row(
            row['Timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
            row['Symbol'],
            row['Direction'],
            row['Strategy'],
            f"{row['Entry']:.4f} -> {row['Exit']:.4f}",
            f"{funding:.4f}",
            f"[{pnl_color}]${pnl:.4f}[/{pnl_color}]"
        )
    console.print(table)

def analyze_trades(file_path):
    df = load_trades(file_path)
    if df is None: return

    # --- Header ---
    console.print(Panel.fit(f"[bold blue]Trade Performance Analysis[/bold blue]\nFile: {file_path}", border_style="blue"))

    # --- Global Stats ---
    total_trades = len(df)
    total_pnl = df['Net_PnL_USDT'].sum()
    win_rate = (df[df['Net_PnL_USDT'] > 0].shape[0] / total_trades * 100)
    
    # Profit Factor
    gross_win = df[df['Net_PnL_USDT'] > 0]['Net_PnL_USDT'].sum()
    gross_loss = abs(df[df['Net_PnL_USDT'] < 0]['Net_PnL_USDT'].sum())
    profit_factor = gross_win / gross_loss if gross_loss > 0 else 999.0
    
    # Funding Stats
    total_funding = df['Funding'].sum() if 'Funding' in df.columns else 0.0
    
    # Summary Grid
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    grid.add_row("Total Trades:", f"{total_trades}")
    grid.add_row("Net PnL:", f"[bold {'green' if total_pnl >=0 else 'red'}]${total_pnl:.2f}[/]")
    grid.add_row("  - From Trade:", f"${(total_pnl - total_funding):.2f}")
    grid.add_row("  - From Funding:", f"[bold green]${total_funding:.4f}[/]")
    grid.add_row("Win Rate:", f"{win_rate:.1f}%")
    grid.add_row("Profit Factor:", f"{profit_factor:.2f}")
    
    console.print(Panel(grid, title="Global Statistics", border_style="green"))

    # --- Strategy Breakdown ---
    strat_table = Table(title="Performance by Strategy", box=box.ROUNDED)
    strat_table.add_column("Strategy", style="magenta")
    strat_table.add_column("Count")
    strat_table.add_column("Win Rate")
    strat_table.add_column("Total PnL")
    strat_table.add_column("Avg PnL")
    
    for strat, gdf in df.groupby('Strategy'):
        cnt = len(gdf)
        wr = (gdf[gdf['Net_PnL_USDT'] > 0].shape[0] / cnt) * 100
        tpnl = gdf['Net_PnL_USDT'].sum()
        apnl = gdf['Net_PnL_USDT'].mean()
        
        strat_table.add_row(
            strat,
            str(cnt),
            f"{wr:.1f}%",
            f"[{'green' if tpnl>=0 else 'red'}]${tpnl:.2f}[/]",
            f"${apnl:.2f}"
        )
    console.print(strat_table)

    # --- Time-Based Analysis (3H, 8H, Daily, Weekly) ---
    console.print("\n[bold yellow]--- Period Analysis ---[/bold yellow]")
    
    # 3 Hours
    stats_3h, _ = generate_period_stats(df, '3h', "3-Hour Performance")
    print_period_table(stats_3h.tail(8), "Recent 3-Hour Intervals (Last 8)")
    
    # 8 Hours
    stats_8h, _ = generate_period_stats(df, '8h', "8-Hour Performance")
    print_period_table(stats_8h.tail(6), "Recent 8-Hour Intervals (Last 6)")
    
    # Daily
    stats_1d, _ = generate_period_stats(df, 'D', "Daily Performance")
    print_period_table(stats_1d.tail(7), "Daily Performance (Last 7 Days)")

    # Weekly
    stats_1w, _ = generate_period_stats(df, 'W', "Weekly Performance")
    if not stats_1w.empty:
        print_period_table(stats_1w, "Weekly Performance")

    # --- New Granular Analysis ---
    base_dir = os.path.dirname(file_path)
    analyze_market_opportunities(os.path.join(base_dir, "market_log.csv"))
    analyze_order_quality(os.path.join(base_dir, "order_log.csv"))
    
    # --- Latest Trades ---
    print_latest_trades(df, count=50)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rich Trade Analysis")
    parser.add_argument("file", nargs="?", default="logs/live_trades.csv", help="Path to CSV")
    args = parser.parse_args()
    
    analyze_trades(args.file)
