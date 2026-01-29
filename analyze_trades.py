import pandas as pd
import argparse
import sys
import os

def analyze_trades(file_path):
    if not os.path.exists(file_path):
        print(f"Error: File '{file_path}' not found.")
        return

    try:
        df = pd.read_csv(file_path)
    except Exception as e:
        print(f"Error reading file: {e}")
        return

    if df.empty:
        print("No trades found in the file.")
        return

    # clean up column names
    df.columns = [c.strip() for c in df.columns]
    
    # Identify PnL columns
    pnl_pct_col = 'Net_PnL_Pct' if 'Net_PnL_Pct' in df.columns else 'Net_PnL'
    pnl_amt_col = 'Net_PnL_USDT' if 'Net_PnL_USDT' in df.columns else None

    if pnl_pct_col not in df.columns:
        print(f"Error: Could not find PnL column (expected '{pnl_pct_col}'). Available: {list(df.columns)}")
        return

    # Group by Strategy
    strategies = df['Strategy'].unique()
    
    print(f"{'Strategy':<15} | {'Trades':<6} | {'Win Rate':<8} | {'Avg PnL %':<10} | {'Total PnL %':<11} | {'Total USDT':<10} | {'Best %':<8} | {'Worst %':<8}")
    print("-" * 105)

    for strategy in strategies:
        strat_df = df[df['Strategy'] == strategy]
        
        total_trades = len(strat_df)
        wins = strat_df[strat_df[pnl_pct_col] > 0]
        win_rate = (len(wins) / total_trades) * 100
        
        avg_pnl = strat_df[pnl_pct_col].mean() * 100
        total_pnl = strat_df[pnl_pct_col].sum() * 100
        best_pnl = strat_df[pnl_pct_col].max() * 100
        worst_pnl = strat_df[pnl_pct_col].min() * 100
        
        total_usdt = strat_df[pnl_amt_col].sum() if pnl_amt_col else 0.0

        print(f"{strategy:<15} | {total_trades:<6} | {win_rate:>7.1f}% | {avg_pnl:>9.4f}% | {total_pnl:>10.4f}% | ${total_usdt:>9.2f} | {best_pnl:>7.2f}% | {worst_pnl:>7.2f}%")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze trade performance from CSV.")
    parser.add_argument("file", nargs="?", default="sim_trades.csv", help="Path to the trades CSV file")
    args = parser.parse_args()
    
    analyze_trades(args.file)
