import pandas as pd
import argparse
import os
import sys
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

def calculate_drawdown(df, balance_col='Balance'):
    # Calculate High Water Mark
    df['HWM'] = df[balance_col].cummax()
    df['Drawdown_Amt'] = df['HWM'] - df[balance_col]
    df['Drawdown_Pct'] = (df['Drawdown_Amt'] / df['HWM']) * 100
    
    max_dd_amt = df['Drawdown_Amt'].max()
    max_dd_pct = df['Drawdown_Pct'].max()
    
    return max_dd_amt, max_dd_pct

def generate_period_stats(df, period_code, label):
    # Resample
    # grouping by period. 
    # We take the sum of PnL, count of trades, and the LAST balance of the period.
    
    # Set index
    temp_df = df.set_index('Timestamp')
    
    resampler = temp_df.resample(period_code)
    
    stats = resampler.agg({
        'Net_PnL_USDT': 'sum',
        'Net_PnL_Pct': 'mean', # Average ROI per trade in this period? Or simple sum of pct? usually sum of R is better, but let's stick to sum of USDT
        'Balance': 'last',
        'Strategy': 'count' # Trade count
    })
    
    # Win rate per period is harder with simple agg, need custom
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
    table.add_column("Closing Bal", justify="right", style="green")
    
    # Calculate cumulative metrics for the table to show trend? No, just per period. 
    
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
            pnl_str,
            f"${row['Balance']:.2f}"
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
    
    # Drawdown
    max_dd_amt, max_dd_pct = calculate_drawdown(df.copy())
    
    # Summary Grid
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    grid.add_row("Total Trades:", f"{total_trades}")
    grid.add_row("Net PnL:", f"[bold {'green' if total_pnl >=0 else 'red'}]${total_pnl:.2f}[/]")
    grid.add_row("Win Rate:", f"{win_rate:.1f}%")
    grid.add_row("Profit Factor:", f"{profit_factor:.2f}")
    grid.add_row("Max Drawdown:", f"[red]${max_dd_amt:.2f} ({max_dd_pct:.2f}%)[/red]")
    grid.add_row("Current Balance:", f"${df.iloc[-1]['Balance']:.2f}")
    
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

    # --- "Memory" / Streak Analysis ---
    console.print("\n[bold yellow]--- Streak & Anomaly Monitor ---[/bold yellow]")
    
    # Consecutive Losses
    df['Win'] = df['Net_PnL_USDT'] > 0
    # Group consecutive elements
    df['grp'] = (df['Win'] != df['Win'].shift()).cumsum()
    streaks = df.groupby('grp')
    
    worst_loss_streak = 0
    best_win_streak = 0
    
    for _, s in streaks:
        if s.iloc[0]['Win'] == False:
            worst_loss_streak = max(worst_loss_streak, len(s))
        else:
            best_win_streak = max(best_win_streak, len(s))
            
    console.print(f"Longest Win Streak: [green]{best_win_streak}[/green] trades")
    console.print(f"Longest Loss Streak: [red]{worst_loss_streak}[/red] trades")
    
    # Check for rapid drawdown (e.g. > 5% in 3 hours)
    if not stats_3h.empty:
        bad_periods = stats_3h[stats_3h['Net_PnL_USDT'] < - (df['Balance'].mean() * 0.05)]
        if not bad_periods.empty:
            console.print(f"[red]ALERT: {len(bad_periods)} periods (3h) detected with >5% loss![/red]")
            for idx, row in bad_periods.iterrows():
                console.print(f"  - {idx}: ${row['Net_PnL_USDT']:.2f}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rich Trade Analysis")
    parser.add_argument("file", nargs="?", default="live_trades.csv", help="Path to CSV")
    args = parser.parse_args()
    
    analyze_trades(args.file)