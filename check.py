import time
import os
import hmac
import hashlib
from urllib.parse import urlencode
import pandas as pd
import requests

# Aster Dex API Base URL
BASE_URL = "https://fapi.asterdex.com"

# API Credentials
API_KEY = os.getenv('ASTER_API_KEY')
API_SECRET = os.getenv('ASTER_API_SECRET')

def get_signature(params, secret):
    """Generates HMAC SHA256 signature."""
    query_string = urlencode(params)
    return hmac.new(
        secret.encode('utf-8'),
        query_string.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

def get_commission_rate(symbol):
    """
    Retrieves the commission rate for a specific symbol.
    Endpoint: GET /fapi/v1/commissionRate
    """
    if not API_KEY or not API_SECRET:
        return None

    endpoint = '/fapi/v1/commissionRate'
    url = BASE_URL + endpoint
    
    params = {
        'symbol': symbol,
        'timestamp': int(time.time() * 1000),
        'recvWindow': 5000
    }
    
    params['signature'] = get_signature(params, API_SECRET)
    
    headers = {
        'X-MBX-APIKEY': API_KEY,
        'Content-Type': 'application/json'
    }

    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            return response.json()
        else:
            return None
    except Exception:
        return None

def get_all_tickers():
    """
    Retrieves the book ticker for all symbols to get Bid/Ask prices.
    Endpoint: GET /fapi/v1/ticker/bookTicker
    """
    url = f"{BASE_URL}/fapi/v1/ticker/bookTicker"
    try:
        response = requests.get(url)
        if response.status_code == 200:
            return response.json()
    except Exception:
        pass
    return []

def get_viable_scalps(min_profit_buffer=0.0002):
    """
    Finds pairs where Funding Rate > (2 * Maker Fee) + Buffer
    min_profit_buffer: 0.0002 (0.02%) profit target per hour
    """
    try:
        # 1. Get all Premium Indices (Predicted Funding)
        premium_url = f"{BASE_URL}/fapi/v3/premiumIndex"
        premiums = requests.get(premium_url).json()
        
        # 2. Get All Book Tickers (for Spread)
        tickers = get_all_tickers()
        tickers_map = {t['symbol']: t for t in tickers}

        viable_pairs = []

        print(f"Scanning {len(premiums)} pairs for opportunities...")

        # Default fees for initial filtering
        default_maker = 0.0002
        default_taker = 0.0004  # Assuming 0.04% default taker

        for p in premiums:
            symbol = p["symbol"]
            
            # Using lastFundingRate as proxy
            try:
                funding_rate = float(p["lastFundingRate"])
            except (ValueError, KeyError):
                continue

            abs_rate = abs(funding_rate)
            round_trip_fee = default_maker * 2

            # Calculate Countdown
            next_funding_ms = p.get("nextFundingTime")
            countdown_str = "N/A"
            if next_funding_ms:
                try:
                    delta_sec = (float(next_funding_ms) / 1000) - time.time()
                    if delta_sec > 0:
                        m, s = divmod(int(delta_sec), 60)
                        h, m = divmod(m, 60)
                        countdown_str = f"{h:02d}:{m:02d}:{s:02d}"
                    else:
                        countdown_str = "00:00:00"
                except Exception:
                    pass

            # Calculate Spread
            spread_pct = 0.0
            ticker = tickers_map.get(symbol)
            if ticker:
                try:
                    bid = float(ticker.get("bidPrice", 0))
                    ask = float(ticker.get("askPrice", 0))
                    if ask > 0:
                        spread_pct = (ask - bid) / ask
                except ValueError:
                    pass

            # Initial filter
            if abs_rate > (round_trip_fee + min_profit_buffer):
                direction = "SHORT" if funding_rate > 0 else "LONG"
                net_yield = abs_rate - round_trip_fee

                viable_pairs.append(
                    {
                        "symbol": symbol,
                        "funding_rate": funding_rate,
                        "direction": direction,
                        "net_yield_est": net_yield,
                        "countdown": countdown_str,
                        "spread": spread_pct,
                        # Placeholders
                        "maker_fee": default_maker,
                        "taker_fee": default_taker
                    }
                )

        # Sort by estimated yield first
        df = pd.DataFrame(viable_pairs)
        
        if not df.empty:
            df = df.sort_values(by="net_yield_est", ascending=False)
            
            # Process top 10 for detailed fees
            print("\nFetching real-time fees for top candidates...")
            
            # We will iterate over the indices of the top 10 rows
            top_indices = df.head(10).index
            
            for idx in top_indices:
                symbol = df.loc[idx, "symbol"]
                fee_data = get_commission_rate(symbol)
                
                if fee_data:
                    # Parse fees (API returns strings)
                    m_fee = float(fee_data.get("makerCommissionRate", default_maker))
                    t_fee = float(fee_data.get("takerCommissionRate", default_taker))
                    
                    df.loc[idx, "maker_fee"] = m_fee
                    df.loc[idx, "taker_fee"] = t_fee
            
            # Recalculate Earnings for the displayed rows based on specific fees
            # We'll calculate for the whole DF but only the top ones have updated fees
            
            # Columns:
            # 1. Earn (Mk/Mk): abs_funding - 2 * maker
            # 2. Earn (Tk/Tk): abs_funding - 2 * taker - spread
            # 3. Earn (Mix):   abs_funding - (maker + taker) - (spread / 2)
            
            abs_fund = df["funding_rate"].abs()
            spread = df["spread"]
            
            df["Earn_MkMk"] = abs_fund - (df["maker_fee"] * 2)
            df["Earn_TkTk"] = abs_fund - (df["taker_fee"] * 2) - spread
            df["Earn_Mix"] = abs_fund - (df["maker_fee"] + df["taker_fee"]) - (spread / 2.0)

            # Select and rename columns for display
            display_cols = [
                "symbol", "funding_rate", "direction", "countdown",
                "spread", "Earn_MkMk", "Earn_TkTk", "Earn_Mix"
            ]
            
            print("\n--- VIABLE HOURLY SCALPS (Top 10) ---")
            # Format nicely
            pd.options.display.float_format = '{:.6f}'.format
            
            # Format as percentages for display
            df_display = df[display_cols].head(10).copy()
            for col in ["funding_rate", "spread", "Earn_MkMk", "Earn_TkTk", "Earn_Mix"]:
                df_display[col] = df_display[col].apply(lambda x: f"{x*100:.4f}%")
            
            print(df_display.to_string(index=False))
            
        else:
            print("\nNo pairs found where Funding > Fees + Buffer.")
            print(f"Required Rate: > {(0.0004 + min_profit_buffer)*100:.4f}% per hour")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    get_viable_scalps()
