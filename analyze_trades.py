import pandas as pd
import argparse
import os
import sys
import time
from datetime import datetime, timedelta
import threading

# Add current directory to path
sys.path.append(os.getcwd())

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box
    from rich.progress import Progress, SpinnerColumn, TextColumn
except ImportError:
    print("Please install 'rich' library: pip install rich")
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
        # Parse Timestamps
        if 'Timestamp' in df.columns:
            df['Timestamp'] = pd.to_datetime(df['Timestamp'])
        
        return df
    except Exception as e:
        console.print(f"[red]Error reading anchors: {e}[/red]")
        return None

def fetch_income_history(start_time_ms, end_time_ms):
    """
    Fetches all income history between start and end times.
    Handles pagination (limit 1000 per call).
    """
    all_income = []
    current_start = start_time_ms
    
    # Safety: Don't query future
    now_ms = int(time.time() * 1000)
    if end_time_ms > now_ms:
        end_time_ms = now_ms

    while True:
        try:
            params = {
                'startTime': current_start,
                'endTime': end_time_ms,
                'limit': 1000
            }
            query = exchange._sign_request(params)
            headers = {'User-Agent': 'PythonApp/1.0', 'X-MBX-APIKEY': os.getenv("ASTER_API_KEY")}
            url = f"{exchange.base_url}/fapi/v3/income"
            
            resp = exchange.session.get(url, params=query, headers=headers, timeout=20)
            
            if resp.status_code != 200:
                console.print(f"[red]API Error: {resp.status_code} {resp.text}[/red]")
                break
                
            data = resp.json()
            if not data:
                break
                
            all_income.extend(data)
            
            if len(data) < 1000:
                break
                
            # Update cursor
            # The API might not be strictly sorted by time in a way that allows simple pagination by last time
            # But usually standard way is last_time + 1. 
            # Check the last item time
            last_time = int(data[-1]['time'])
            if last_time >= end_time_ms:
                break
            
            # Avoid infinite loop if timestamps are same
            if last_time == current_start:
                current_start += 1
            else:
                current_start = last_time + 1
                
        except Exception as e:
            console.print(f"[red]Exception fetching income: {e}[/red]")
            break
            
    return pd.DataFrame(all_income)

def analyze_trades_with_api(file_path):
    # 1. Load Anchors
    anchors = load_anchors(file_path)
    if anchors is None or anchors.empty:
        console.print("[yellow]No trade anchors found.[/yellow]")
        return

    console.print(Panel(f"[bold blue]Trade Analysis (API-Backed)[/bold blue]\nSource: {file_path}", border_style="blue"))

    # 2. Determine Time Range
    # We need to cover the earliest entry to the latest exit.
    # Anchors have EntryTime and ExitTime as float timestamps (seconds).
    
    min_time = anchors['EntryTime'].min()
    max_time = anchors['ExitTime'].max()
    
    # Buffer: -5 min before, +5 min after
    start_ms = int((min_time - 300) * 1000)
    end_ms = int((max_time + 300) * 1000)
    
    # 3. Fetch Income Data
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Fetching Income Data from API ({datetime.fromtimestamp(start_ms/1000)} to {datetime.fromtimestamp(end_ms/1000)})...", total=None)
        income_df = fetch_income_history(start_time_ms=start_ms, end_time_ms=end_ms)
        progress.update(task, completed=100)

    if income_df.empty:
        console.print("[red]No income data returned from API. Cannot analyze PnL.[/red]")
        return

    # Process Income Data
    income_df['time'] = pd.to_numeric(income_df['time'])
    income_df['income'] = pd.to_numeric(income_df['income'])
    
    # 4. Correlate Anchors with Income
    # We will add columns to 'anchors' df
    
    anchors['Realized_PnL'] = 0.0
    anchors['Funding_Fee'] = 0.0
    anchors['Commission'] = 0.0
    anchors['Net_PnL'] = 0.0
    
    matched_income_indices = set()

    # Iterate anchors
    # For performance, maybe filter income by symbol first
    
    for idx, row in anchors.iterrows():
        symbol = row['Symbol']
        entry_ts_ms = int(row['EntryTime'] * 1000)
        exit_ts_ms = int(row['ExitTime'] * 1000)
        
        # Define window for this specific trade
        # Loose window: Entry - 2s to Exit + 2s
        window_start = entry_ts_ms - 2000
        window_end = exit_ts_ms + 2000
        
        # Filter income
        mask = (
            (income_df['symbol'] == symbol) & 
            (income_df['time'] >= window_start) & 
            (income_df['time'] <= window_end)
        )
        
        trade_income = income_df[mask]
        
        # Calculate components
        realized = trade_income[trade_income['incomeType'] == 'REALIZED_PNL']['income'].sum()
        funding = trade_income[trade_income['incomeType'] == 'FUNDING_FEE']['income'].sum()
        commission = trade_income[trade_income['incomeType'] == 'COMMISSION']['income'].sum()
        
        anchors.at[idx, 'Realized_PnL'] = realized
        anchors.at[idx, 'Funding_Fee'] = funding
        anchors.at[idx, 'Commission'] = commission
        anchors.at[idx, 'Net_PnL'] = realized + funding + commission
        
        matched_income_indices.update(trade_income.index.tolist())

    # 5. Display Statistics
    
    # Global Stats
    total_trades = len(anchors)
    total_pnl = anchors['Net_PnL'].sum()
    total_commission = anchors['Commission'].sum()
    total_funding = anchors['Funding_Fee'].sum()
    
    winners = anchors[anchors['Net_PnL'] > 0]
    win_rate = (len(winners) / total_trades * 100) if total_trades > 0 else 0
    
    grid = Table.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    
    grid.add_row("Total Trades:", str(total_trades))
    grid.add_row("Total Net PnL:", f"[bold {'green' if total_pnl >= 0 else 'red'}]${total_pnl:.4f}[/]")
    grid.add_row("  - Realized PnL:", f"${anchors['Realized_PnL'].sum():.4f}")
    grid.add_row("  - Funding:", f"[green]${total_funding:.4f}[/green]")
    grid.add_row("  - Commission:", f"[red]${total_commission:.4f}[/red]")
    grid.add_row("Win Rate:", f"{win_rate:.2f}%")
    
    console.print(Panel(grid, title="Global Statistics (API Verified)", border_style="green"))
    
    # Strategy Breakdown
    strat_table = Table(title="Strategy Performance", box=box.ROUNDED)
    strat_table.add_column("Strategy")
    strat_table.add_column("Count")
    strat_table.add_column("Win Rate")
    strat_table.add_column("Net PnL")
    strat_table.add_column("Avg PnL")
    
    for strat, gdf in anchors.groupby('Strategy'):
        count = len(gdf)
        wr = (len(gdf[gdf['Net_PnL'] > 0]) / count * 100)
        pnl = gdf['Net_PnL'].sum()
        avg = gdf['Net_PnL'].mean()
        
        strat_table.add_row(
            strat,
            str(count),
            f"{wr:.1f}%",
            f"[{'green' if pnl>=0 else 'red'}]${pnl:.2f}[/]",
            f"${avg:.2f}"
        )
    console.print(strat_table)
    
    # Detailed List (Latest 20)
    details = Table(title="Latest Trades", box=box.MINIMAL_DOUBLE_HEAD)
    details.add_column("Time", style="dim")
    details.add_column("Symbol")
    details.add_column("Strategy")
    details.add_column("Prices (In->Out)")
    details.add_column("Funding")
    details.add_column("Comm")
    details.add_column("Net PnL")
    
    for _, row in anchors.sort_values('Timestamp', ascending=False).head(20).iterrows():
        pnl = row['Net_PnL']
        color = "green" if pnl >= 0 else "red"
        
        details.add_row(
            row['Timestamp'].strftime('%H:%M:%S'),
            row['Symbol'],
            row['Strategy'],
            f"{row['EntryPrice']:.4f} -> {row['ExitPrice']:.4f}",
            f"{row['Funding_Fee']:.4f}",
            f"{row['Commission']:.4f}",
            f"[{color}]${pnl:.4f}[/{color}]"
        )
    console.print(details)

    # Orphaned Income Check (Optional)
    # Check if there is significant income not matched to anchors (e.g. manual trades or missed logs)
    # unmatched_income = income_df[~income_df.index.isin(matched_income_indices)]
    # if not unmatched_income.empty:
    #     val = unmatched_income['income'].sum()
    #     if abs(val) > 0.01:
    #         console.print(f"\n[yellow]Warning: Unmatched Income detected: ${val:.4f} (Manual trades?)[/yellow]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze Trades using API Income History")
    parser.add_argument("file", nargs="?", default="logs/trade_anchors.csv", help="Path to Anchor CSV")
    args = parser.parse_args()
    
    analyze_trades_with_api(args.file)