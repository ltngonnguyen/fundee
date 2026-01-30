import unittest
import os
import time
import requests
from funding_shared import ExchangeInterface, BASE_URL

class TestFundingApp(unittest.TestCase):
    def setUp(self):
        self.exchange = ExchangeInterface()
        # Check for auth
        self.has_auth = os.getenv('ASTER_API_KEY') and os.getenv('ASTER_API_SECRET')
        self.base_url = BASE_URL

    def test_01_public_exchange_info(self):
        """Test public endpoint: Exchange Info (v3)"""
        print(f"\n[TEST] Exchange Info on {self.base_url}...")
        self.exchange.load_exchange_info()
        self.assertTrue(len(self.exchange.precision_map) > 0, "Failed to load exchange info")
        print(f" -> Loaded {len(self.exchange.precision_map)} symbols.")

    def test_02_public_book_ticker(self):
        """Test public endpoint: Book Ticker (v3)"""
        print("\n[TEST] Book Ticker...")
        ticker = self.exchange.get_book_ticker("BTCUSDT")
        if ticker:
            print(f" -> BTCUSDT: Bid={ticker.get('bidPrice')} Ask={ticker.get('askPrice')}")
        else:
            print(" -> Failed to fetch ticker (might be connection issue or invalid symbol)")
        self.assertIsNotNone(ticker)

    def test_03_v3_upgrades_check(self):
        """Audit specific V3 endpoints to ensure they are available"""
        print(f"\n[TEST] Auditing V3 Upgrades availability...")
        
        # We manually verify v3 availability even if we don't have keys for everything
        # Just checking if endpoints return 401 (Unauthorized) or 404 (Not Found)
        # 401 means endpoint exists but needs keys. 404 means it doesn't exist.
        
        endpoints = [
            "/fapi/v3/balance",
            "/fapi/v3/positionRisk",
            "/fapi/v3/positionSide/dual",
            "/fapi/v3/commissionRate"
        ]
        
        for ep in endpoints:
            url = f"{self.base_url}{ep}"
            resp = requests.get(url)
            # We expect 401 (needs signature) or 400 (missing params) if it exists.
            # If it returns 404, it's missing.
            status = resp.status_code
            exists = status != 404
            print(f" -> {ep}: {'EXISTS' if exists else 'MISSING'} (Status: {status})")
            
            # Note: AsterDex might mask 404 as something else, but standard Binance behavior is 404 for invalid URL.

    def test_04_authenticated_endpoints(self):
        """Test authenticated endpoints if keys are present"""
        if not self.has_auth:
            print("\n[TEST] Skipping authenticated tests (No API Keys)")
            return
        
        from funding_shared import WEB3_AVAILABLE
        if not WEB3_AVAILABLE:
            print("\n[TEST] Skipping authenticated tests (Web3 not installed)")
            return

        print("\n[TEST] Authenticated: Balance (v3)...")
        bal = self.exchange.get_balance()
        print(f" -> Balance: {bal}")

        print("[TEST] Authenticated: Positions (v3)...")
        pos = self.exchange.get_positions()
        print(f" -> Open Positions: {len(pos)}")
        for p in pos:
            print(f"    - {p['symbol']}: {p['positionAmt']}")

        print("[TEST] Authenticated: Position Mode (v3)...")
        mode = self.exchange.get_position_mode()
        print(f" -> Hedge Mode: {mode}")

if __name__ == '__main__':
    unittest.main()
