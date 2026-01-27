import pandas as pd
import sys

def analyze_trades(file_path):
    try:
        df = pd.read_csv(file_path)
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return
    except Exception as e:
        print(f"Error reading file: {e}")
        return

    if df.empty:
        print("No trades found in the file.")
        return

    # clean up column names just in case
    df.columns = [c.strip() for c in df.columns]

    # Group by Strategy
    strategies = df['Strategy'].unique()
    
    results = []

    print(f"{'Strategy':<15} | {'Trades':<6} | {'Win Rate':<8} | {'Avg PnL %':<10} | {'Total PnL %':<11} | {'Best PnL':<10} | {'Worst PnL':<10}")
    print("-" * 95)

    for strategy in strategies:
        strat_df = df[df['Strategy'] == strategy]
        
        total_trades = len(strat_df)
        wins = strat_df[strat_df['Net_PnL'] > 0]
        win_rate = (len(wins) / total_trades) * 100
        
        avg_pnl = strat_df['Net_PnL'].mean() * 100
        total_pnl = strat_df['Net_PnL'].sum() * 100
        best_pnl = strat_df['Net_PnL'].max() * 100
        worst_pnl = strat_df['Net_PnL'].min() * 100

        print(f"{strategy:<15} | {total_trades:<6} | {win_rate:>7.1f}% | {avg_pnl:>9.4f}% | {total_pnl:>10.4f}% | {best_pnl:>9.4f}% | {worst_pnl:>9.4f}%")

if __name__ == "__main__":
    analyze_trades("sim_trades.csv")
