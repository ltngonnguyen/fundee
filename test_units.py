import unittest
from unittest.mock import MagicMock, patch, ANY
import time
import json
import os
import sys

# Ensure we can import the modules
sys.path.append(os.getcwd())

from fundee_shared import ExchangeInterface, SmartOrderExecutor
from fundee import FundeeLogic

class TestExchangeInterface(unittest.TestCase):
    def setUp(self):
        self.exchange = ExchangeInterface(base_url="https://mock.url")
        # Mock the session
        self.exchange.session = MagicMock()
        
        # Setup fake precision map for testing normalization
        self.exchange.precision_map = {
            'BTCUSDT': {
                'tick_size': 0.1,
                'step_size': 0.001,
                'price_precision': 1,
                'qty_precision': 3
            },
            'ETHUSDT': {
                'tick_size': 0.01,
                'step_size': 0.01,
                'price_precision': 2,
                'qty_precision': 2
            },
            'DOGEUSDT': { # Case where tick/step might be 0 or tiny
                'tick_size': 0.00001,
                'step_size': 1.0,
                'price_precision': 5,
                'qty_precision': 0
            }
        }

    def test_normalize_price(self):
        # BTC: Tick 0.1
        self.assertEqual(self.exchange.normalize_price('BTCUSDT', 50000.15), 50000.2)
        self.assertEqual(self.exchange.normalize_price('BTCUSDT', 50000.14), 50000.1)
        
        # ETH: Tick 0.01
        self.assertEqual(self.exchange.normalize_price('ETHUSDT', 3000.123), 3000.12)
        
        # Unknown symbol
        self.assertEqual(self.exchange.normalize_price('UNKNOWN', 123.456), 123.456)

    def test_normalize_quantity(self):
        # BTC: Step 0.001
        self.assertEqual(self.exchange.normalize_quantity('BTCUSDT', 0.1234), 0.123)
        self.assertEqual(self.exchange.normalize_quantity('BTCUSDT', 0.1236), 0.123) # Floor/Round logic check
        
        # DOGE: Step 1.0
        self.assertEqual(self.exchange.normalize_quantity('DOGEUSDT', 100.5), 100.0)
        
        # Unknown
        self.assertEqual(self.exchange.normalize_quantity('UNKNOWN', 10.55), 10.55)

    def test_trim_dict(self):
        data = {
            'a': 1,
            'b': {'c': 2, 'd': [3, 4]},
            'e': [1, {'f': 5}]
        }
        # Expected: All values converted to strings or JSON strings
        result = self.exchange._trim_dict(data)
        self.assertEqual(result['a'], '1')
        
        # nested dict becomes json string
        b_val = json.loads(result['b'])
        self.assertEqual(b_val['c'], '2')
        
        # nested list becomes json string of strings
        e_val = json.loads(result['e'])
        self.assertEqual(e_val[0], '1')
        self.assertTrue(isinstance(json.loads(e_val[1]), dict))

    @patch('fundee_shared.WEB3_AVAILABLE', False)
    def test_sign_request_no_web3(self):
        # Should fail gracefully
        with patch.dict(os.environ, {'ASTER_API_SECRET': 'fake_secret'}):
            res = self.exchange._sign_request({'test': 1})
            self.assertIsNone(res)

    @patch('fundee_shared.WEB3_AVAILABLE', True)
    @patch('fundee_shared.Web3')
    @patch('fundee_shared.Account')
    def test_sign_request_success(self, mock_account, mock_web3):
        # Setup mocks
        mock_web3.to_checksum_address.side_effect = lambda x: x
        mock_web3.keccak.return_value.hex.return_value = '0xdeadbeef'
        mock_account.sign_message.return_value.signature.hex.return_value = 'signature'
        
        with patch.dict(os.environ, {
            'ASTER_API_KEY': 'key', 
            'ASTER_API_SECRET': '0xsecret',
            'ASTER_USER_ADDRESS': '0xUser'
        }):
            res = self.exchange._sign_request({'param': 1})
            
            self.assertIsNotNone(res)
            self.assertIn('signature', res)
            self.assertIn('timestamp', res)
            self.assertIn('nonce', res)
            self.assertEqual(res['param'], '1')

class TestSmartOrderExecutor(unittest.TestCase):
    def setUp(self):
        self.exchange = MagicMock()
        self.symbol = 'BTCUSDT'
        self.callbacks = {'on_event': MagicMock()}
        
    def test_passive_fill(self):
        # Scenario: Place passive order, gets filled immediately
        executor = SmartOrderExecutor(self.exchange, self.symbol, 'BUY', 1.0, aggressive=False, callbacks=self.callbacks)
        
        # Mocks
        self.exchange.get_book_ticker.return_value = {'bidPrice': '50000', 'askPrice': '50001'}
        self.exchange.place_order.return_value = {'orderId': '123'}
        self.exchange.get_order.return_value = {'status': 'FILLED', 'executedQty': '1.0', 'avgPrice': '50000'}
        
        # Run
        result = executor.run(timeout=1)
        
        self.assertTrue(result)
        self.exchange.place_order.assert_called_with(
            self.symbol, 'BUY', 'LIMIT', 1.0, 50000.0, 'GTX', position_side=None
        )
        # Verify success callback
        # args: event, filled, avg, oid, role
        # We need to check specific calls to on_event
        # self.callbacks['on_event'].assert_any_call('SUCCESS', 1.0, 50000.0, '123', 'MAKER')

    def test_aggressive_switch(self):
        # Scenario: Passive timeout -> Switch to Aggressive -> Fill
        executor = SmartOrderExecutor(self.exchange, self.symbol, 'BUY', 1.0, aggressive=False, callbacks=self.callbacks)
        
        # Mocks
        self.exchange.get_book_ticker.return_value = {'bidPrice': '50000', 'askPrice': '50001'}
        
        # First call: Place Passive
        # Second call: Status returns NEW (not filled)
        # Third call: Time passes, switch to aggressive -> Cancel
        # Fourth call: Place Aggressive
        # Fifth call: Status Filled
        
        self.exchange.place_order.side_effect = [
            {'orderId': '101'}, # Passive
            {'orderId': '102'}  # Aggressive
        ]
        
        # We need to control time or the loop to simulate switching.
        # It's easier to set switch_mode_time to now
        switch_time = time.time() - 1 # Already passed
        
        # Because we passed switch_time in the past, it should start aggressive or switch immediately.
        # But wait, logic:
        # 1. loop start.
        # 2. check aggressive switch -> Yes.
        # 3. get ticker.
        # 4. determine price (aggressive).
        # 5. place order.
        
        # Let's test actual switching logic by running it normally but forcing aggressive=False initially
        # and mocking time to trigger switch inside loop? Hard with while loop.
        # Easier: Pass switch_time=0 (already passed) -> Should act aggressive immediately
        
        res = executor.run(timeout=1, switch_mode_time=switch_time)
        
        # Check if it placed aggressive order (Ask * 1.01)
        self.exchange.place_order.assert_called()
        args, _ = self.exchange.place_order.call_args
        # args: symbol, side, type, qty, price, time_in_force, pos_side
        price = args[4]
        # Should be around 50001 * 1.01 = 50501.01
        self.assertAlmostEqual(price, 50001 * 1.01)

    def test_reprice_logic(self):
        # Scenario: Passive order. Price moves away. Cancel and Replace.
        executor = SmartOrderExecutor(self.exchange, self.symbol, 'BUY', 1.0, aggressive=False, callbacks=self.callbacks)
        
        # 1. First ticker: Bid 50000. Place at 50000.
        # 2. Next loop: Order is NEW. Ticker Bid moves to 50010.
        # 3. Logic sees 50000 < 50010 (Buying low). Should Cancel.
        # 4. Next loop: Order is CANCELED (simulated). Place new at 50010.
        
        self.exchange.get_book_ticker.side_effect = [
            {'bidPrice': '50000', 'askPrice': '50001'}, # Loop 1
            {'bidPrice': '50010', 'askPrice': '50011'}, # Loop 2 (Price moved up)
            {'bidPrice': '50010', 'askPrice': '50011'}  # Loop 3
        ]
        
        self.exchange.place_order.side_effect = [
            {'orderId': '101'}, # First order
            {'orderId': '102'}  # Second order
        ]
        
        self.exchange.get_order.side_effect = [
            {'status': 'NEW', 'price': '50000', 'executedQty': '0'}, # Status check for 101 (Loop 1)
            {'status': 'NEW', 'price': '50000', 'executedQty': '0'}, # Status check for 101 (Loop 2) -> Trigger Cancel
            {'status': 'FILLED', 'executedQty': '1.0', 'avgPrice': '50010'} # Status check for 102 (Loop 3)
        ]
        
        # Need to allow cancel_order to be called
        self.exchange.cancel_order.return_value = {'status': 'CANCELED'}
        
        res = executor.run(timeout=3)
        
        self.assertTrue(res)
        # Verify cancel called for 101
        self.exchange.cancel_order.assert_called_with(self.symbol, '101')
        # Verify placed 102
        self.assertEqual(self.exchange.place_order.call_count, 2)


class TestFundeeLogic(unittest.TestCase):
    def setUp(self):
        self.interface = MagicMock()
        self.logic = FundeeLogic(self.interface)
        
        # Setup Logic State
        self.logic.ticker_map = {
            'BTCUSDT': {'bid': 50000.0, 'ask': 50001.0},
            'ETHUSDT': {'bid': 3000.0, 'ask': 3001.0}
        }
        self.logic.fee_cache = {
            'BTCUSDT': {'maker': 0.0001, 'taker': 0.0002}
        }
        self.logic.ticker_stats_cache = {
            'BTCUSDT': {'quoteVolume': 1000000},
            'ETHUSDT': {'quoteVolume': 1000000}
        }
        
    def test_process_premiums_filtering(self):
        # Data with 1 viable, 1 low vol, 1 low rate
        data = [
            # Good candidate
            {
                'symbol': 'BTCUSDT', 
                'lastFundingRate': '0.001', # 0.1% -> High enough
                'nextFundingTime': int((time.time() + 60)*1000)
            },
            # Low rate
            {
                'symbol': 'ETHUSDT', 
                'lastFundingRate': '0.00001', # Tiny
                'nextFundingTime': int((time.time() + 60)*1000)
            }
        ]
        
        self.logic.process_premiums(data)
        
        # Expect BTC in viable, ETH not
        viable_syms = [x['symbol'] for x in self.logic.viable_pairs]
        self.assertIn('BTCUSDT', viable_syms)
        self.assertNotIn('ETHUSDT', viable_syms)
        
        # Check direction
        btc_cand = next(x for x in self.logic.viable_pairs if x['symbol'] == 'BTCUSDT')
        self.assertEqual(btc_cand['direction'], 'SHORT') # Pos rate -> Short

    def test_strategy_entry_trigger(self):
        # Setup viable pair close to funding
        future_time = (time.time() + 40) * 1000 # 40s from now
        self.logic.viable_pairs = [{
            'symbol': 'BTCUSDT',
            'next_funding_time': future_time,
            'direction': 'SHORT',
            'funding_rate': 0.001
        }]
        
        # Strategy Logic: "Start 59s before funding. Passive First -> Aggressive at T-29s"
        # 40s is within 29-60s window.
        
        # Balance check
        self.logic.balance = 200 # Sufficient
        
        self.logic.update_strategies()
        
        # Should trigger execute_strategy_entry -> calling smart_execute (via run_worker)
        # We can check if pending_orders has it
        self.assertIn('BTCUSDT', self.logic.pending_orders)
        self.interface.run_worker.assert_called()

    def test_strategy_exit_sl(self):
        # Setup active strategy
        strat = {
            'id': 1,
            'strategy': 'STRADDLE',
            'symbol': 'BTCUSDT',
            'status': 'OPEN',
            'direction': 'SHORT',
            'entry_price': 50000.0,
            'quantity': 0.002
        }
        self.logic.active_strategies = [strat]
        
        # Price moves against us (SHORT) -> Price goes UP
        # SL is -1%. 50000 * 1.01 = 50500.
        # Set market price to 51000
        self.logic.ticker_map['BTCUSDT'] = {'bid': 51000.0, 'ask': 51001.0}
        
        self.logic.update_strategies()
        
        # Should trigger exit
        # Check if run_worker called (for exit)
        self.interface.run_worker.assert_called()
        # Should NOT remove from active immediately (async), but should add to pending
        self.assertIn('BTCUSDT', self.logic.pending_orders)

    def test_trailing_tp(self):
        # Setup strategy with profit > 1.5% to activate trailing
        strat = {
            'id': 1,
            'strategy': 'STRADDLE',
            'symbol': 'BTCUSDT',
            'status': 'OPEN',
            'direction': 'LONG',
            'entry_price': 50000.0,
            'quantity': 0.002
        }
        self.logic.active_strategies = [strat]
        
        # 1. Price jumps to +2% (51000) -> Activates Trailing
        self.logic.ticker_map['BTCUSDT'] = {'bid': 51000.0, 'ask': 51001.0}
        self.logic.update_strategies()
        
        self.assertTrue(strat.get('trailing_active'))
        self.assertEqual(strat.get('extreme_price'), 51000.0)
        
        # 2. Price goes higher to 52000 -> Updates Extreme
        self.logic.ticker_map['BTCUSDT'] = {'bid': 52000.0, 'ask': 52001.0}
        self.logic.update_strategies()
        self.assertEqual(strat.get('extreme_price'), 52000.0)
        
        # 3. Price drops 0.2% (Hit Trail 0.1%) -> Should Exit
        # 52000 * (1 - 0.002) = 51896
        self.logic.ticker_map['BTCUSDT'] = {'bid': 51896.0, 'ask': 51897.0}
        self.logic.update_strategies()
        
        self.interface.run_worker.assert_called()
        self.assertIn('BTCUSDT', self.logic.pending_orders)

    def test_safety_monitor_kill(self):
        # Logic: If xx:xx:01 to xx:xx:29 (Funding window) -> Safe
        # If xx:xx:31 -> Kill
        
        # Let's mock datetime in fundee.py? 
        # Or just manually set self.real_positions and call safety_monitor with mocked datetime
        
        self.logic.real_positions = [
            {'symbol': 'BTCUSDT', 'positionAmt': '0.1'} # Lingering position
        ]
        
        with patch('fundee.datetime') as mock_dt:
            # Set time to Minute: 0, Second: 45 (Outside safe zone 0-30 and 59)
            mock_dt.now.return_value.minute = 0
            mock_dt.now.return_value.second = 45
            
            self.logic.safety_monitor()
            
            # Should trigger kill
            self.interface.notify.assert_called_with(ANY, severity='warning')
            self.assertIn('BTCUSDT', self.logic.pending_orders)
            self.interface.run_worker.assert_called()

if __name__ == '__main__':
    unittest.main()
