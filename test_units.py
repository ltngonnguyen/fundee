import os
import sys
import time
import unittest
from decimal import Decimal
from unittest.mock import ANY, MagicMock, patch

# Ensure we can import the modules
sys.path.append(os.getcwd())

from fundee import FundeeLogic
from fundee_shared import ExchangeInterface, SmartOrderExecutor
from fundee_dryrun import SimPosition, PLAYBOOKS

VALID_WALLET = "0x" + "a" * 40
VALID_KEY = "0x" + "b" * 64
HL_ENV = {
    "HL_WALLET_ADDRESS": VALID_WALLET,
    "HL_API_PRIVATE_KEY": VALID_KEY,
}


def _make_fake_market(symbol, price_prec, amount_prec, funding_hours=1):
    return {
        "symbol": symbol,
        "precision": {"price": price_prec, "amount": amount_prec, "base": amount_prec, "quote": price_prec},
        "swap": True,
        "active": True,
        "info": {"fundingIntervalHours": funding_hours},
    }


class _MockHyperliquidFactory:
    """Helper to install a fake ccxt.hyperliquid into fundee_shared."""

    def __init__(self, markets=None):
        self.markets = markets or {
            "BTC/USDC:USDC": _make_fake_market("BTC/USDC:USDC", 1, 4, funding_hours=1),
            "ETH/USDC:USDC": _make_fake_market("ETH/USDC:USDC", 2, 3, funding_hours=1),
            "SOL/USDC:USDC": _make_fake_market("SOL/USDC:USDC", 3, 2, funding_hours=4),
        }
        self.instance = MagicMock()
        self.instance.load_markets.return_value = self.markets
        self.class_mock = MagicMock(return_value=self.instance)

    def install(self):
        return patch("fundee_shared.ccxt.hyperliquid", self.class_mock)


class TestExchangeInterfaceCCXT(unittest.TestCase):
    """Tests for the new ccxt.hyperliquid-backed ExchangeInterface."""

    def setUp(self):
        self.factory = _MockHyperliquidFactory()
        self.patcher = self.factory.install()
        self.patcher.start()
        # ExchangeInterface reads HL_WALLET_ADDRESS / HL_API_PRIVATE_KEY at module import;
        # patch the module-level constants directly.
        self._wallet_patcher = patch.multiple(
            "fundee_shared",
            HL_WALLET_ADDRESS=VALID_WALLET,
            HL_API_PRIVATE_KEY=VALID_KEY,
        )
        self._wallet_patcher.start()
        self.exchange = ExchangeInterface(logger=MagicMock())
        # Markets are now loaded by start() (post-UI-composition), not __init__.
        self.exchange.start()

    def tearDown(self):
        self.patcher.stop()
        self._wallet_patcher.stop()

    # ----- Construction / credentials -----

    def _config(self, factory=None):
        """Get the config dict passed to ccxt.hyperliquid(...) constructor."""
        f = factory or self.factory
        call = f.class_mock.call_args
        # ccxt.hyperliquid accepts a single config dict
        if call.args:
            return call.args[0]
        return call.kwargs

    def test_init_uses_hl_creds_from_env(self):
        cfg = self._config()
        self.assertEqual(cfg["walletAddress"], VALID_WALLET)
        self.assertEqual(cfg["privateKey"], VALID_KEY)

    def test_init_sandbox_mode_with_testnet_flag(self):
        factory = _MockHyperliquidFactory()
        patcher = factory.install()
        patcher.start()
        with patch("fundee_shared.HL_WALLET_ADDRESS", VALID_WALLET), \
             patch("fundee_shared.HL_API_PRIVATE_KEY", VALID_KEY):
            ExchangeInterface(testnet=True, logger=MagicMock())
        cfg = self._config(factory)
        self.assertTrue(cfg.get("sandboxMode"))
        patcher.stop()

    def test_init_does_not_load_markets(self):
        # load_markets is now deferred to start() so the verbose startup logs
        # can run after the UI is composed. Verify with a fresh instance.
        factory = _MockHyperliquidFactory()
        patcher = factory.install()
        patcher.start()
        with patch("fundee_shared.HL_WALLET_ADDRESS", VALID_WALLET), \
             patch("fundee_shared.HL_API_PRIVATE_KEY", VALID_KEY):
            fresh = ExchangeInterface(logger=MagicMock())
        # __init__ must not have called load_markets yet.
        factory.instance.load_markets.assert_not_called()
        self.assertEqual(fresh.precision_map, {})
        # start() is what loads markets.
        fresh.start()
        self.assertGreater(len(fresh.precision_map), 0)
        patcher.stop()

    def test_init_emits_warning_for_legacy_aster_env(self):
        factory = _MockHyperliquidFactory()
        patcher = factory.install()
        patcher.start()
        with patch("fundee_shared.HL_WALLET_ADDRESS", None), \
             patch("fundee_shared.HL_API_PRIVATE_KEY", None), \
             patch.dict(
                 os.environ,
                 {
                     "ASTER_USER_ADDRESS": VALID_WALLET,
                     "ASTER_API_SECRET": VALID_KEY,
                 },
                 clear=True,
             ):
            with self.assertLogs("fundee_shared", level="WARNING") as cm:
                ExchangeInterface(logger=MagicMock())
            self.assertTrue(any("ASTER" in m for m in cm.output))
        patcher.stop()

    def test_init_exits_when_creds_missing(self):
        factory = _MockHyperliquidFactory()
        patcher = factory.install()
        patcher.start()
        with patch("fundee_shared.HL_WALLET_ADDRESS", None), \
             patch("fundee_shared.HL_API_PRIVATE_KEY", None):
            with self.assertRaises(SystemExit):
                ExchangeInterface(logger=MagicMock())
        patcher.stop()

    def test_init_exits_when_creds_empty_string(self):
        factory = _MockHyperliquidFactory()
        patcher = factory.install()
        patcher.start()
        with patch("fundee_shared.HL_WALLET_ADDRESS", ""), \
             patch("fundee_shared.HL_API_PRIVATE_KEY", ""):
            with self.assertRaises(SystemExit):
                ExchangeInterface(logger=MagicMock())
        patcher.stop()

    # ----- load_markets / precision -----

    def test_load_markets_populates_precision_map(self):
        self.assertIn("BTC/USDC:USDC", self.exchange.precision_map)
        pm = self.exchange.precision_map["BTC/USDC:USDC"]
        self.assertEqual(pm["price_precision"], 1)
        self.assertEqual(pm["qty_precision"], 4)

    def test_load_markets_records_funding_interval(self):
        self.assertEqual(
            self.exchange.funding_interval_hours["BTC/USDC:USDC"], 1
        )
        self.assertEqual(
            self.exchange.funding_interval_hours["SOL/USDC:USDC"], 4
        )

    # ----- normalize -----

    def test_normalize_quantity(self):
        # BTC/USDC:USDC: qty_precision=4
        result = self.exchange.normalize_quantity("BTC/USDC:USDC", 0.123456789)
        self.assertEqual(result, 0.1234)

    def test_normalize_quantity_unknown_symbol(self):
        self.assertIsNone(self.exchange.normalize_quantity("UNKNOWN", 1.0))

    def test_normalize_price(self):
        result = self.exchange.normalize_price("BTC/USDC:USDC", 50000.15)
        # 1 decimal place -> 50000.1 or 50000.2
        self.assertIn(result, [50000.1, 50000.2])

    def test_normalize_price_unknown_symbol(self):
        self.assertEqual(self.exchange.normalize_price("UNKNOWN", 123.456), 123.456)

    # ----- get_balance -----

    def test_get_balance_returns_usdc(self):
        self.factory.instance.fetch_balance.return_value = {
            "USDC": {"free": 100.0, "used": 0.0, "total": 100.0}
        }
        bal = self.exchange.get_balance()
        self.assertEqual(bal, 100.0)
        # User param must be passed explicitly to satisfy hyperliquid's guard.
        self.factory.instance.fetch_balance.assert_called_with(
            {"type": "swap", "user": VALID_WALLET}
        )

    def test_get_balance_returns_none_when_missing(self):
        self.factory.instance.fetch_balance.return_value = {"BTC": {"free": 1.0}}
        self.assertIsNone(self.exchange.get_balance())

    def test_get_balance_handles_exception(self):
        self.factory.instance.fetch_balance.side_effect = Exception("boom")
        self.assertIsNone(self.exchange.get_balance())

    # ----- get_positions -----

    def test_get_positions_normalizes_to_legacy_shape(self):
        self.factory.instance.fetch_positions.return_value = [
            {
                "symbol": "BTC/USDC:USDC",
                "contracts": 0.5,
                "side": "long",
                "info": {"positionAmt": "0.5"},
            }
        ]
        positions = self.exchange.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "BTC/USDC:USDC")
        self.assertEqual(float(positions[0]["positionAmt"]), 0.5)
        self.factory.instance.fetch_positions.assert_called_with(
            symbols=None, params={"user": VALID_WALLET}
        )

    def test_get_positions_filters_zero(self):
        self.factory.instance.fetch_positions.return_value = [
            {"symbol": "X", "contracts": 0.5, "info": {"positionAmt": "0.5"}},
            {"symbol": "Y", "contracts": 0.0, "info": {"positionAmt": "0"}},
        ]
        positions = self.exchange.get_positions()
        self.assertEqual(len(positions), 1)

    def test_get_positions_short_normalizes_to_negative(self):
        self.factory.instance.fetch_positions.return_value = [
            {"symbol": "BTC/USDC:USDC", "contracts": 0.5, "side": "short",
             "info": {"positionAmt": "0.5"}}
        ]
        positions = self.exchange.get_positions()
        self.assertEqual(float(positions[0]["positionAmt"]), -0.5)

    # ----- get_book_ticker -----

    def test_get_book_ticker_returns_legacy_shape(self):
        self.factory.instance.fetch_ticker.return_value = {
            "bid": 49999.0,
            "ask": 50001.0,
        }
        ticker = self.exchange.get_book_ticker("BTC/USDC:USDC")
        self.assertEqual(ticker["bidPrice"], 49999.0)
        self.assertEqual(ticker["askPrice"], 50001.0)

    def test_get_book_ticker_handles_string_values(self):
        self.factory.instance.fetch_ticker.return_value = {
            "bid": "49999.5",
            "ask": "50001.5",
        }
        ticker = self.exchange.get_book_ticker("BTC/USDC:USDC")
        self.assertEqual(ticker["bidPrice"], 49999.5)
        self.assertEqual(ticker["askPrice"], 50001.5)

    # ----- get_commission_rate -----

    def test_get_commission_rate_returns_user_tier(self):
        # HL has no fetchTradingFees endpoint; we hardcode the user's tier rate.
        result = self.exchange.get_commission_rate("BTC/USDC:USDC")
        self.assertEqual(result["maker"], 0.000432)  # 0.0432%
        self.assertEqual(result["taker"], 0.000144)  # 0.0144%
        # And we must NOT have called the unsupported endpoint.
        self.factory.instance.fetch_trading_fees.assert_not_called()

    # ----- get_funding_rate -----

    def test_get_funding_rate(self):
        self.factory.instance.fetch_funding_rate.return_value = {
            "symbol": "BTC/USDC:USDC",
            "fundingRate": 0.0001,
            "nextFundingTime": 1234571490000,
            "nextFundingTimestamp": 1234571490000,
        }
        result = self.exchange.get_funding_rate("BTC/USDC:USDC")
        self.assertEqual(result["fundingRate"], 0.0001)
        self.assertEqual(result["nextFundingTime"], 1234571490000)

    # ----- place_order -----

    def test_place_order_limit_passes_gtx_as_alo(self):
        self.factory.instance.create_order.return_value = {"id": "123"}
        self.exchange.place_order(
            "BTC/USDC:USDC", "buy", "limit", 0.1, 50000.0, time_in_force="GTX"
        )
        args, kwargs = self.factory.instance.create_order.call_args
        self.assertEqual(args[0], "BTC/USDC:USDC")
        self.assertEqual(args[1], "limit")
        self.assertEqual(args[2], "buy")
        self.assertEqual(args[3], 0.1)
        self.assertEqual(args[4], 50000.0)
        self.assertEqual(kwargs["params"]["timeInForce"], "Alo")

    def test_place_order_limit_passes_gtc(self):
        self.factory.instance.create_order.return_value = {"id": "123"}
        self.exchange.place_order(
            "BTC/USDC:USDC", "buy", "limit", 0.1, 50000.0, time_in_force="GTC"
        )
        kwargs = self.factory.instance.create_order.call_args.kwargs
        self.assertEqual(kwargs["params"]["timeInForce"], "Gtc")

    def test_place_order_market_passes_slippage(self):
        self.factory.instance.create_order.return_value = {"id": "456"}
        self.exchange.place_order("BTC/USDC:USDC", "buy", "market", 0.1)
        kwargs = self.factory.instance.create_order.call_args.kwargs
        self.assertEqual(kwargs["params"]["slippage"], "0.05")

    def test_place_order_drops_position_side_arg(self):
        # position_side is accepted for back-compat but ignored (no hedge mode on HL)
        self.factory.instance.create_order.return_value = {"id": "789"}
        self.exchange.place_order(
            "BTC/USDC:USDC", "buy", "limit", 0.1, 50000.0, position_side="LONG"
        )
        kwargs = self.factory.instance.create_order.call_args.kwargs
        self.assertNotIn("positionSide", kwargs.get("params", {}))

    def test_place_order_reduce_only(self):
        self.factory.instance.create_order.return_value = {"id": "789"}
        self.exchange.place_order(
            "BTC/USDC:USDC", "sell", "market", 0.1, reduce_only=True
        )
        kwargs = self.factory.instance.create_order.call_args.kwargs
        self.assertTrue(kwargs["params"]["reduceOnly"])

    def test_place_order_invalid_quantity_returns_none(self):
        result = self.exchange.place_order(
            "BTC/USDC:USDC", "buy", "limit", 0.0, 50000.0
        )
        self.assertIsNone(result)

    def test_place_order_unknown_symbol_returns_none(self):
        result = self.exchange.place_order(
            "UNKNOWN/USDC:USDC", "buy", "limit", 0.1, 50000.0
        )
        self.assertIsNone(result)

    # ----- cancel / fetch order -----

    def test_cancel_order(self):
        self.factory.instance.cancel_order.return_value = {"id": "1", "status": "canceled"}
        self.exchange.cancel_order("BTC/USDC:USDC", "1")
        self.factory.instance.cancel_order.assert_called_with("1", "BTC/USDC:USDC")

    def test_get_order_returns_normalized_shape(self):
        self.factory.instance.fetch_order.return_value = {
            "id": "1",
            "status": "closed",
            "filled": 0.1,
            "average": 50000.0,
            "price": 50000.0,
            "info": {"executedQty": "0.1", "avgPrice": "50000.0"},
        }
        result = self.exchange.get_order("BTC/USDC:USDC", "1")
        # Legacy shape with status, executedQty, avgPrice
        self.assertIn("status", result)
        self.assertIn("executedQty", result)
        self.assertIn("avgPrice", result)

    # ----- set_leverage -----

    def test_set_leverage(self):
        self.factory.instance.set_leverage.return_value = {"leverage": 10}
        self.exchange.set_leverage("BTC/USDC:USDC", 10)
        self.factory.instance.set_leverage.assert_called_with(10, "BTC/USDC:USDC")

    # ----- close_all_positions -----

    def test_close_all_positions_uses_reduce_only_market(self):
        self.factory.instance.create_order.return_value = {"id": "999"}
        self.factory.instance.fetch_ticker.return_value = {
            "bid": 49999.0,
            "ask": 50001.0,
        }
        result = self.exchange.close_all_positions("BTC/USDC:USDC", "sell")
        self.assertIsNotNone(result)
        call = self.factory.instance.create_order.call_args
        # params may be passed positionally or as kwarg; check both
        if call.kwargs.get("params") is not None:
            params = call.kwargs["params"]
        else:
            params = call.args[5] if len(call.args) >= 6 else {}
        self.assertTrue(params["reduceOnly"])
        self.assertEqual(call.args[1], "market")  # type=market
        self.assertIn("slippage", params)

    def test_close_all_positions_dust_returns_none(self):
        # Amount below MIN_NOTIONAL should yield None and NOT submit
        result = self.exchange.close_all_positions(
            "BTC/USDC:USDC", "sell", amount=0.0001
        )
        self.assertIsNone(result)
        self.factory.instance.create_order.assert_not_called()

    def test_close_all_positions_proceeds_when_cancel_raises_not_supported(self):
        # Hyperliquid does not implement cancel_all_orders; the market close
        # must still happen, otherwise the safety monitor is stuck.
        import ccxt
        self.factory.instance.cancel_all_orders.side_effect = ccxt.NotSupported(
            "hyperliquid cancelAllOrders() is not supported yet"
        )
        self.factory.instance.fetch_open_orders.return_value = []
        self.factory.instance.create_order.return_value = {"id": "1234"}
        result = self.exchange.close_all_positions("HYPE/USDC:USDC", "sell")
        self.assertIsNotNone(result)
        self.factory.instance.create_order.assert_called_once()

    def test_close_all_positions_falls_back_to_per_order_cancel(self):
        # When bulk cancel isn't supported, fetch open orders and cancel each.
        import ccxt
        self.factory.instance.cancel_all_orders.side_effect = ccxt.NotSupported(
            "hyperliquid cancelAllOrders() is not supported yet"
        )
        self.factory.instance.fetch_open_orders.return_value = [
            {"id": "oid-1"}, {"id": "oid-2"},
        ]
        self.factory.instance.cancel_orders.return_value = [{"id": "oid-1"}, {"id": "oid-2"}]
        self.factory.instance.create_order.return_value = {"id": "close-1"}
        self.exchange.close_all_positions("HYPE/USDC:USDC", "sell")
        # Per-order cancel was used with both ids.
        self.factory.instance.cancel_orders.assert_called_once()
        args = self.factory.instance.cancel_orders.call_args
        ids = args.args[0] if args.args else args.kwargs.get("ids")
        self.assertEqual(set(ids), {"oid-1", "oid-2"})
        # And the market close still happened.
        self.factory.instance.create_order.assert_called_once()

    def test_close_all_positions_proceeds_when_per_order_cancel_also_fails(self):
        # If both bulk and per-order cancel fail (network blip, etc.), we still
        # submit the reduceOnly market close — the position is what matters.
        import ccxt
        self.factory.instance.cancel_all_orders.side_effect = ccxt.NotSupported(
            "nope"
        )
        self.factory.instance.fetch_open_orders.side_effect = ccxt.NetworkError("down")
        self.factory.instance.create_order.return_value = {"id": "close-2"}
        result = self.exchange.close_all_positions("HYPE/USDC:USDC", "sell")
        self.assertIsNotNone(result)
        self.factory.instance.create_order.assert_called_once()

    # ----- cancel_all_orders -----

    def test_cancel_all_orders(self):
        self.factory.instance.cancel_all_orders.return_value = []
        self.exchange.cancel_all_orders("BTC/USDC:USDC")
        self.factory.instance.cancel_all_orders.assert_called_with("BTC/USDC:USDC")


class TestFundingIntervalParameterization(unittest.TestCase):
    """Verify the entry window is parameterized by per-symbol funding interval."""

    def setUp(self):
        self.interface = MagicMock()
        # Patch ExchangeInterface to avoid network calls in FundeeLogic.__init__
        self._exchange_patcher = patch("fundee.ExchangeInterface")
        mock_ei = self._exchange_patcher.start()
        self.mock_exchange = MagicMock()
        mock_ei.return_value = self.mock_exchange
        self.logic = FundeeLogic(self.interface)
        self.logic.exchange = self.mock_exchange
        # Make exchange methods safe to call
        self.logic.exchange.funding_interval_hours = {}
        self.logic.exchange.precision_map = {
            "BTC/USDC:USDC": {
                "tick_size": 0.1, "step_size": 0.0001,
                "price_precision": 1, "qty_precision": 4,
            }
        }
        self.logic.ticker_map = {
            "BTC/USDC:USDC": {"bid": 50000.0, "ask": 50001.0},
        }
        self.logic.fee_cache = {
            "BTC/USDC:USDC": {"maker": Decimal("0.0001"), "taker": Decimal("0.0002")},
        }
        self.logic.ticker_stats_cache = {
            "BTC/USDC:USDC": {"quoteVolume": 5_000_000},
        }
        self.logic.balance = Decimal("200")

    def _make_pair(self, funding_time, interval_h):
        self.logic.exchange.funding_interval_hours["BTC/USDC:USDC"] = interval_h
        return {
            "symbol": "BTC/USDC:USDC",
            "next_funding_time": funding_time,
            "direction": "SHORT",
            "funding_rate": Decimal("0.001"),
        }

    def test_1h_funding_triggers_straddle_in_window(self):
        future = (time.time() + 40) * 1000  # 40s out
        self.logic.viable_pairs = [self._make_pair(future, interval_h=1)]
        with patch("fundee.ACTIVE_STRATEGY", "STRADDLE"):
            self.logic.update_strategies()
        self.assertIn("BTC/USDC:USDC", self.logic.pending_orders)

    def test_4h_funding_triggers_straddle_in_window(self):
        future = (time.time() + 40) * 1000
        self.logic.viable_pairs = [self._make_pair(future, interval_h=4)]
        with patch("fundee.ACTIVE_STRATEGY", "STRADDLE"):
            self.logic.update_strategies()
        self.assertIn("BTC/USDC:USDC", self.logic.pending_orders)

    def test_far_funding_does_not_trigger(self):
        # 10 minutes out — well outside 29-60s window
        future = (time.time() + 600) * 1000
        self.logic.viable_pairs = [self._make_pair(future, interval_h=1)]
        with patch("fundee.ACTIVE_STRATEGY", "STRADDLE"):
            self.logic.update_strategies()
        self.assertNotIn("BTC/USDC:USDC", self.logic.pending_orders)

    def tearDown(self):
        self._exchange_patcher.stop()


class TestNoHedgeMode(unittest.TestCase):
    """Verify hedge-mode is removed (HL has no hedge mode)."""

    def setUp(self):
        self.interface = MagicMock()
        self._exchange_patcher = patch("fundee.ExchangeInterface")
        mock_ei = self._exchange_patcher.start()
        self.mock_exchange = MagicMock()
        mock_ei.return_value = self.mock_exchange
        self.logic = FundeeLogic(self.interface)
        self.logic.exchange = self.mock_exchange

    def test_is_hedge_mode_false(self):
        self.assertFalse(self.logic.is_hedge_mode)

    def test_safety_monitor_does_not_pass_position_side(self):
        self.logic.exchange.close_all_positions = MagicMock(
            return_value={"orderId": "x"}
        )
        self.logic.real_positions = [
            {"symbol": "BTC/USDC:USDC", "positionAmt": "0.1"}
        ]
        with patch("fundee.datetime") as mock_dt:
            mock_dt.now.return_value.minute = 0
            mock_dt.now.return_value.second = 45
            self.logic.safety_monitor()
        if self.logic.exchange.close_all_positions.called:
            call = self.logic.exchange.close_all_positions.call_args
            self.assertIsNone(call.kwargs.get("position_side"))

    def tearDown(self):
        self._exchange_patcher.stop()


class TestSmartOrderExecutor(unittest.TestCase):
    def setUp(self):
        self.exchange = MagicMock()
        self.symbol = "BTCUSDT"
        self.callbacks = {"on_event": MagicMock()}

    def test_passive_fill(self):
        # Scenario: Place passive order, gets filled immediately
        executor = SmartOrderExecutor(
            self.exchange,
            self.symbol,
            "BUY",
            1.0,
            aggressive=False,
            callbacks=self.callbacks,
        )

        # Mocks
        self.exchange.get_book_ticker.return_value = {
            "bidPrice": "50000",
            "askPrice": "50001",
        }
        self.exchange.place_order.return_value = {"orderId": "123"}
        self.exchange.get_order.return_value = {
            "status": "FILLED",
            "executedQty": "1.0",
            "avgPrice": "50000",
        }

        # Run
        result = executor.run(timeout=1)

        self.assertTrue(result)
        self.exchange.place_order.assert_called_with(
            self.symbol, "BUY", "LIMIT", 1.0, 50000.0, "GTX", position_side=None
        )
        # Verify success callback
        # args: event, filled, avg, oid, role
        # We need to check specific calls to on_event
        # self.callbacks['on_event'].assert_any_call('SUCCESS', 1.0, 50000.0, '123', 'MAKER')

    def test_aggressive_switch(self):
        # Scenario: Passive timeout -> Switch to Aggressive -> Fill
        executor = SmartOrderExecutor(
            self.exchange,
            self.symbol,
            "BUY",
            1.0,
            aggressive=False,
            callbacks=self.callbacks,
        )

        # Mocks
        self.exchange.get_book_ticker.return_value = {
            "bidPrice": "50000",
            "askPrice": "50001",
        }

        # First call: Place Passive
        # Second call: Status returns NEW (not filled)
        # Third call: Time passes, switch to aggressive -> Cancel
        # Fourth call: Place Aggressive
        # Fifth call: Status Filled

        self.exchange.place_order.side_effect = [
            {"orderId": "101"},  # Passive
            {"orderId": "102"},  # Aggressive
        ]

        # We need to control time or the loop to simulate switching.
        # It's easier to set switch_mode_time to now
        switch_time = time.time() - 1  # Already passed

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

        res = executor.run(timeout=1, switch_mode_time=switch_time)  # noqa: F841

        # Check if it placed aggressive order (Ask * 1.01)
        self.exchange.place_order.assert_called()
        args, _ = self.exchange.place_order.call_args
        # args: symbol, side, type, qty, price, time_in_force, pos_side
        price = args[4]
        # Should be around 50001 * 1.01 = 50501.01
        self.assertAlmostEqual(price, 50001 * 1.01)

    def test_reprice_logic(self):
        # Scenario: Passive order. Price moves away. Cancel and Replace.
        executor = SmartOrderExecutor(
            self.exchange,
            self.symbol,
            "BUY",
            1.0,
            aggressive=False,
            callbacks=self.callbacks,
        )

        # 1. First ticker: Bid 50000. Place at 50000.
        # 2. Next loop: Order is NEW. Ticker Bid moves to 50010.
        # 3. Logic sees 50000 < 50010 (Buying low). Should Cancel.
        # 4. Next loop: Order is CANCELED (simulated). Place new at 50010.

        self.exchange.get_book_ticker.side_effect = [
            {"bidPrice": "50000", "askPrice": "50001"},  # Loop 1
            {"bidPrice": "50010", "askPrice": "50011"},  # Loop 2 (Price moved up)
            {"bidPrice": "50010", "askPrice": "50011"},  # Loop 3
        ]

        self.exchange.place_order.side_effect = [
            {"orderId": "101"},  # First order
            {"orderId": "102"},  # Second order
        ]

        self.exchange.get_order.side_effect = [
            {
                "status": "NEW",
                "price": "50000",
                "executedQty": "0",
            },  # Status check for 101 (Loop 1)
            {
                "status": "NEW",
                "price": "50000",
                "executedQty": "0",
            },  # Status check for 101 (Loop 2) -> Trigger Cancel
            {
                "status": "FILLED",
                "executedQty": "1.0",
                "avgPrice": "50010",
            },  # Status check for 102 (Loop 3)
        ]

        # Need to allow cancel_order to be called
        self.exchange.cancel_order.return_value = {"status": "CANCELED"}

        res = executor.run(timeout=3)

        self.assertTrue(res)
        # Verify cancel called for 101
        self.exchange.cancel_order.assert_called_with(self.symbol, "101")
        # Verify placed 102
        self.assertEqual(self.exchange.place_order.call_count, 2)


class TestFundeeLogic(unittest.TestCase):
    def setUp(self):
        self.interface = MagicMock()
        # Patch ExchangeInterface so FundeeLogic construction doesn't hit the network
        self._exchange_patcher = patch("fundee.ExchangeInterface")
        mock_ei = self._exchange_patcher.start()
        self.mock_exchange = MagicMock()
        mock_ei.return_value = self.mock_exchange
        self.logic = FundeeLogic(self.interface)
        self.logic.exchange = self.mock_exchange

        # Setup Logic State
        self.logic.ticker_map = {
            "BTCUSDT": {"bid": 50000.0, "ask": 50001.0},
            "ETHUSDT": {"bid": 3000.0, "ask": 3001.0},
        }
        self.logic.fee_cache = {
            "BTCUSDT": {"maker": Decimal("0.0001"), "taker": Decimal("0.0002")}
        }
        self.logic.ticker_stats_cache = {
            "BTCUSDT": {"quoteVolume": 5000000},
            "ETHUSDT": {"quoteVolume": 5000000},
        }
        # Mock exchange precision_map for validation
        self.logic.exchange.precision_map = {
            "BTCUSDT": {
                "tick_size": 0.01,
                "step_size": 0.001,
                "price_precision": 2,
                "qty_precision": 3,
            }
        }
        # Default: no hedge mode (HL has no hedge mode)
        self.logic.is_hedge_mode = False

    def test_process_premiums_filtering(self):
        # Data with 1 viable, 1 low vol, 1 low rate
        data = [
            # Good candidate
            {
                "symbol": "BTCUSDT",
                "lastFundingRate": "0.001",  # 0.1% -> High enough
                "nextFundingTime": int((time.time() + 60) * 1000),
            },
            # Low rate
            {
                "symbol": "ETHUSDT",
                "lastFundingRate": "0.00001",  # Tiny
                "nextFundingTime": int((time.time() + 60) * 1000),
            },
        ]

        self.logic.process_premiums(data)

        # Expect BTC in viable, ETH not
        viable_syms = [x["symbol"] for x in self.logic.viable_pairs]
        self.assertIn("BTCUSDT", viable_syms)
        self.assertNotIn("ETHUSDT", viable_syms)

        # Check direction
        btc_cand = next(x for x in self.logic.viable_pairs if x["symbol"] == "BTCUSDT")
        self.assertEqual(btc_cand["direction"], "SHORT")  # Pos rate -> Short

    def test_process_premiums_accepts_negative_funding_as_long(self):
        # abs() is restored: a negative funding rate is a LONG-side opportunity
        # (shorts pay longs). Bot should include it with direction="LONG".
        data = [
            {
                "symbol": "BABYUSDT",
                "lastFundingRate": "-0.001",  # -0.1% — shorts pay longs
                "nextFundingTime": int((time.time() + 60) * 1000),
            },
        ]
        # Seed ticker / stats for BABY
        self.logic.ticker_map["BABYUSDT"] = {"bid": 100.0, "ask": 100.05}
        self.logic.ticker_stats_cache["BABYUSDT"] = {"quoteVolume": 5000000}
        self.logic.fee_cache["BABYUSDT"] = {
            "maker": Decimal("0.0001"),
            "taker": Decimal("0.0002"),
        }
        self.logic.process_premiums(data)
        viable_syms = [x["symbol"] for x in self.logic.viable_pairs]
        self.assertIn("BABYUSDT", viable_syms)
        baby_cand = next(x for x in self.logic.viable_pairs if x["symbol"] == "BABYUSDT")
        self.assertEqual(baby_cand["direction"], "LONG")
        # Net yield is positive because |rate| > 2*taker + spread
        self.assertGreater(baby_cand["net_yield"], 0)

    def test_process_premiums_promising_pairs_top5(self):
        # The promising_pairs list should contain the top 5 by net yield
        # across ALL pairs with valid ticker data — including ones that
        # fail the viability filter.
        data = []
        rates = ["0.0030", "0.0025", "0.0020", "0.0015", "0.0010", "0.0005", "0.0001"]
        syms = [f"COIN{i}USDT" for i in range(7)]
        # Set up tickers and stats for all 7 (so they all pass the data check)
        for s in syms:
            self.logic.ticker_map[s] = {"bid": 100.0, "ask": 100.05}
            self.logic.ticker_stats_cache[s] = {"quoteVolume": 1000000}
        for s, r in zip(syms, rates):
            data.append(
                {
                    "symbol": s,
                    "lastFundingRate": r,
                    "nextFundingTime": int((time.time() + 60) * 1000),
                }
            )
        self.logic.process_premiums(data)
        # Top 5 should be the 5 highest rates, all included
        self.assertEqual(len(self.logic.promising_pairs), 5)
        # Ordered by net_yield descending
        top_syms = [p["symbol"] for p in self.logic.promising_pairs]
        self.assertEqual(top_syms, syms[:5])
        # Each has the required keys
        for p in self.logic.promising_pairs:
            self.assertIn("funding_rate", p)
            self.assertIn("net_yield", p)
            self.assertIn("symbol", p)

    def test_process_premiums_promising_includes_below_threshold(self):
        # Even when only one pair passes the threshold, promising_pairs
        # should still show the top 5 by net yield — including sub-threshold ones
        # with negative net yield.
        data = [
            # Clearly viable
            {
                "symbol": "BTCUSDT",
                "lastFundingRate": "0.005",  # 0.5%
                "nextFundingTime": int((time.time() + 60) * 1000),
            },
        ]
        # Add 4 more sub-threshold pairs (rate < 2*taker + spread)
        for i, rate in enumerate(["0.0003", "0.0002", "0.0001", "0.00005"], start=1):
            sym = f"SUB{i}USDT"
            self.logic.ticker_map[sym] = {"bid": 100.0, "ask": 100.05}
            self.logic.ticker_stats_cache[sym] = {"quoteVolume": 1000000}
            data.append(
                {
                    "symbol": sym,
                    "lastFundingRate": rate,
                    "nextFundingTime": int((time.time() + 60) * 1000),
                }
            )
        self.logic.process_premiums(data)
        # viable_pairs only has BTC
        viable_syms = [x["symbol"] for x in self.logic.viable_pairs]
        self.assertEqual(viable_syms, ["BTCUSDT"])
        # promising_pairs has top 5 (BTC + 4 sub-threshold) sorted by net yield
        self.assertEqual(len(self.logic.promising_pairs), 5)
        self.assertEqual(self.logic.promising_pairs[0]["symbol"], "BTCUSDT")
        # Sub-threshold pairs are present even though their net_yield is negative
        sub_in_promising = [p for p in self.logic.promising_pairs if p["symbol"].startswith("SUB")]
        self.assertEqual(len(sub_in_promising), 4)
        for p in sub_in_promising:
            self.assertLess(p["net_yield"], 0)

    def test_heartbeat_logs_promising_pairs(self):
        # Force the heartbeat to fire by resetting _last_heartbeat.
        self.logic._last_heartbeat = 0
        self.logic.promising_pairs = [
            {"symbol": "BTCUSDT", "funding_rate": Decimal("0.001"), "net_yield": Decimal("0.0005")},
            {"symbol": "ETHUSDT", "funding_rate": Decimal("0.0008"), "net_yield": Decimal("0.0003")},
        ]
        # Set a balance so the heartbeat message is well-formed
        self.logic.balance = Decimal("200")
        self.logic.active_strategies = {}
        self.logic.ticker_map = {"BTCUSDT": {"bid": 100, "ask": 101}}
        # Call update_strategies (it will run the heartbeat branch)
        self.logic.viable_pairs = []
        self.logic.update_strategies()
        # Find the heartbeat log call
        hb_calls = [
            c for c in self.interface.log_message.call_args_list
            if "heartbeat" in str(c)
        ]
        self.assertGreater(len(hb_calls), 0)
        msg = hb_calls[0].args[0]
        self.assertIn("promising_pairs", msg)
        self.assertIn("BTCUSDT", msg)
        self.assertIn("ETHUSDT", msg)
        self.assertIn("net=0.0500%", msg)  # 0.0005 * 100 = 0.0500%


    @patch("fundee.ACTIVE_STRATEGY", "STRADDLE")

    @patch("fundee.ACTIVE_STRATEGY", "STRADDLE")
    def test_strategy_entry_trigger(self):
        # Setup viable pair close to funding
        future_time = (time.time() + 40) * 1000  # 40s from now
        self.logic.viable_pairs = [
            {
                "symbol": "BTCUSDT",
                "next_funding_time": future_time,
                "direction": "SHORT",
                "funding_rate": Decimal("0.001"),
            }
        ]

        # Strategy Logic: "Start 59s before funding. Passive First -> Aggressive at T-29s"
        # 40s is within 29-60s window.

        # Balance check
        self.logic.balance = Decimal("200")  # Sufficient

        self.logic.update_strategies()

        # Should trigger execute_strategy_entry -> calling smart_execute (via run_worker)
        # We can check if pending_orders has it
        self.assertIn("BTCUSDT", self.logic.pending_orders)
        self.interface.run_worker.assert_called()

    @patch("fundee.ACTIVE_STRATEGY", "APPROACH_B")
    def test_approach_b_entry_trigger(self):
        from fundee import APPROACH_B_ENTRY_WINDOW

        # Setup viable pair close to funding
        future_time = (
            time.time() + (APPROACH_B_ENTRY_WINDOW / 2)
        ) * 1000  # Within 1s window
        self.logic.viable_pairs = [
            {
                "symbol": "BTCUSDT",
                "next_funding_time": future_time,
                "direction": "SHORT",
                "funding_rate": Decimal("0.001"),
            }
        ]

        self.logic.balance = Decimal("200")

        # Mock the execute_strategy_entry to check arguments or just let it call and verify internal behavior
        # But letting it run is fine since it calls run_worker for the market order
        self.logic.update_strategies()

        self.assertIn("BTCUSDT", self.logic.pending_orders)
        self.interface.run_worker.assert_called()

        # Verify strategy entry added to active if we let run_worker execute...
        # Wait, run_worker is a mock, so the inner market_worker doesn't run.
        # We can extract the worker and run it to verify place_order is called with MARKET.
        args, _ = self.interface.run_worker.call_args
        worker_func = args[0]

        # Setup exchange mock
        self.logic.exchange.place_order = MagicMock(
            return_value={
                "orderId": "market123",
                "executedQty": "0.002",
                "avgPrice": "50000",
            }
        )

        # Run worker
        worker_func()

        self.logic.exchange.place_order.assert_called_with(
            "BTCUSDT", "SELL", "MARKET", ANY
        )

        # Verify it got added to active strategies
        self.assertEqual(len(self.logic.active_strategies), 1)
        self.assertEqual(self.logic.active_strategies[0]["strategy"], "APPROACH_B")
        self.assertEqual(self.logic.active_strategies[0]["order_id"], "market123")

    def test_approach_b_exit_trigger(self):
        from fundee import APPROACH_B_ENTRY_WINDOW

        # Setup active strategy
        strat = {
            "id": 1,
            "strategy": "APPROACH_B",
            "symbol": "BTCUSDT",
            "status": "OPEN",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "quantity": 0.002,
            "funding_time": time.time()
            - (APPROACH_B_ENTRY_WINDOW + 0.5),  # Time passed
        }
        self.logic.active_strategies = [strat]
        self.logic.viable_pairs = [
            {
                "symbol": "BTCUSDT",
                "direction": "SHORT",
                "funding_rate": Decimal("0.001"),
                "next_funding_time": time.time() * 1000,
            }
        ]

        self.logic.update_strategies()

        self.assertIn("BTCUSDT", self.logic.pending_orders)
        self.interface.run_worker.assert_called()

        args, _ = self.interface.run_worker.call_args
        worker_func = args[0]

        # Setup exchange mock
        self.logic.exchange.place_order = MagicMock(
            return_value={
                "orderId": "exit_market123",
                "executedQty": "0.002",
                "avgPrice": "49000",
            }
        )

        # Run worker
        worker_func()

        # Short exit is BUY
        self.logic.exchange.place_order.assert_called_with(
            "BTCUSDT", "BUY", "MARKET", 0.002
        )

        # Verify it got closed
        self.assertEqual(len(self.logic.active_strategies), 0)
        self.assertEqual(len(self.logic.closed_strategies), 1)
        self.assertEqual(
            self.logic.closed_strategies[0]["reason"], "Post-Funding Exit (Approach B)"
        )

    def test_strategy_exit_sl(self):
        # Setup active strategy
        strat = {
            "id": 1,
            "strategy": "STRADDLE",
            "symbol": "BTCUSDT",
            "status": "OPEN",
            "direction": "SHORT",
            "entry_price": 50000.0,
            "quantity": 0.002,
        }
        self.logic.active_strategies = [strat]

        # Price moves against us (SHORT) -> Price goes UP
        # SL is -1%. 50000 * 1.01 = 50500.
        # Set market price to 51000
        self.logic.ticker_map["BTCUSDT"] = {"bid": 51000.0, "ask": 51001.0}

        self.logic.update_strategies()

        # Should trigger exit
        # Check if run_worker called (for exit)
        self.interface.run_worker.assert_called()
        # Should NOT remove from active immediately (async), but should add to pending
        self.assertIn("BTCUSDT", self.logic.pending_orders)

    def test_trailing_tp(self):
        # Setup strategy with profit > 1.5% to activate trailing
        strat = {
            "id": 1,
            "strategy": "STRADDLE",
            "symbol": "BTCUSDT",
            "status": "OPEN",
            "direction": "LONG",
            "entry_price": 50000.0,
            "quantity": 0.002,
        }
        self.logic.active_strategies = [strat]

        # 1. Price jumps to +2% (51000) -> Activates Trailing
        self.logic.ticker_map["BTCUSDT"] = {"bid": 51000.0, "ask": 51001.0}
        self.logic.update_strategies()

        self.assertTrue(strat.get("trailing_active"))
        self.assertEqual(strat.get("extreme_price"), 51000.0)

        # 2. Price goes higher to 52000 -> Updates Extreme
        self.logic.ticker_map["BTCUSDT"] = {"bid": 52000.0, "ask": 52001.0}
        self.logic.update_strategies()
        self.assertEqual(strat.get("extreme_price"), 52000.0)

        # 3. Price drops 0.2% (Hit Trail 0.1%) -> Should Exit
        # 52000 * (1 - 0.002) = 51896
        self.logic.ticker_map["BTCUSDT"] = {"bid": 51896.0, "ask": 51897.0}
        self.logic.update_strategies()

        self.interface.run_worker.assert_called()
        self.assertIn("BTCUSDT", self.logic.pending_orders)

    def test_safety_monitor_kill(self):
        # Logic: If xx:xx:01 to xx:xx:29 (Funding window) -> Safe
        # If xx:xx:31 -> Kill

        # Let's mock datetime in fundee.py?
        # Or just manually set self.real_positions and call safety_monitor with mocked datetime

        self.logic.real_positions = [
            {"symbol": "BTCUSDT", "positionAmt": "0.1"}  # Lingering position
        ]

        with patch("fundee.datetime") as mock_dt:
            # Set time to Minute: 0, Second: 45 (Outside safe zone 0-30 and 59)
            mock_dt.now.return_value.minute = 0
            mock_dt.now.return_value.second = 45

            self.logic.safety_monitor()

            # Should trigger kill
            self.interface.notify.assert_called_with(ANY, severity="warning")
            self.assertIn("BTCUSDT", self.logic.pending_orders)
            self.interface.run_worker.assert_called()

    def test_safety_monitor_skips_when_autokill_disabled(self):
        self.logic.autokill_enabled = False
        self.logic.real_positions = [
            {"symbol": "BTCUSDT", "positionAmt": "0.1"}  # Lingering position
        ]
        with patch("fundee.datetime") as mock_dt:
            mock_dt.now.return_value.minute = 0
            mock_dt.now.return_value.second = 45  # outside safe zone
            self.logic.safety_monitor()
        # No kill, no pending order, no worker.
        self.interface.run_worker.assert_not_called()
        self.assertNotIn("BTCUSDT", self.logic.pending_orders)
        # Safety zone should NOT have been notified either (the function is a no-op).
        self.interface.notify.assert_not_called()

    def test_safety_monitor_runs_when_autokill_enabled(self):
        # Sanity: default behavior preserved when autokill is on.
        self.logic.autokill_enabled = True
        self.logic.real_positions = [
            {"symbol": "BTCUSDT", "positionAmt": "0.1"}
        ]
        with patch("fundee.datetime") as mock_dt:
            mock_dt.now.return_value.minute = 0
            mock_dt.now.return_value.second = 45
            self.logic.safety_monitor()
        self.interface.run_worker.assert_called()
        self.assertIn("BTCUSDT", self.logic.pending_orders)

    def test_fundee_logic_default_autokill_is_on(self):
        # Fresh instance — default must be enabled (safety-first).
        with patch("fundee.ExchangeInterface") as mock_ei:
            mock_ei.return_value = MagicMock()
            logic = FundeeLogic(MagicMock())
        self.assertTrue(logic.autokill_enabled)

    def test_fundee_logic_autokill_false_via_ctor(self):
        with patch("fundee.ExchangeInterface") as mock_ei:
            mock_ei.return_value = MagicMock()
            logic = FundeeLogic(MagicMock(), autokill=False)
        self.assertFalse(logic.autokill_enabled)

    def test_fundee_logic_volume_filter_defaults_on(self):
        with patch("fundee.ExchangeInterface") as mock_ei:
            mock_ei.return_value = MagicMock()
            logic = FundeeLogic(MagicMock())
        self.assertTrue(logic.volume_filter_enabled)

    def test_fundee_logic_volume_filter_off_via_ctor(self):
        with patch("fundee.ExchangeInterface") as mock_ei:
            mock_ei.return_value = MagicMock()
            logic = FundeeLogic(MagicMock(), volume_filter=False)
        self.assertFalse(logic.volume_filter_enabled)

    def test_process_premiums_volume_filter_off_allows_low_vol(self):
        # With volume filter off, low-volume pairs should pass through.
        self.logic.volume_filter_enabled = False
        data = [
            {
                "symbol": "DUSTUSDT",
                "lastFundingRate": "0.001",  # 0.1%
                "nextFundingTime": int((time.time() + 60) * 1000),
            },
        ]
        # DUST has zero volume — would be filtered by volume gate
        self.logic.ticker_map["DUSTUSDT"] = {"bid": 100.0, "ask": 100.05}
        self.logic.ticker_stats_cache["DUSTUSDT"] = {"quoteVolume": 1000}  # tiny
        self.logic.fee_cache["DUSTUSDT"] = {
            "maker": Decimal("0.0001"),
            "taker": Decimal("0.0002"),
        }
        self.logic.process_premiums(data)
        viable_syms = [x["symbol"] for x in self.logic.viable_pairs]
        self.assertIn("DUSTUSDT", viable_syms)

    def test_process_premiums_volume_filter_on_blocks_low_vol(self):
        # With volume filter on (default), low-volume pairs are blocked.
        data = [
            {
                "symbol": "DUSTUSDT",
                "lastFundingRate": "0.001",
                "nextFundingTime": int((time.time() + 60) * 1000),
            },
        ]
        self.logic.ticker_map["DUSTUSDT"] = {"bid": 100.0, "ask": 100.05}
        self.logic.ticker_stats_cache["DUSTUSDT"] = {"quoteVolume": 1000}
        self.logic.fee_cache["DUSTUSDT"] = {
            "maker": Decimal("0.0001"),
            "taker": Decimal("0.0002"),
        }
        self.logic.process_premiums(data)
        viable_syms = [x["symbol"] for x in self.logic.viable_pairs]
        self.assertNotIn("DUSTUSDT", viable_syms)

    def tearDown(self):
        self._exchange_patcher.stop()


class TestFundeeAppAutokillToggle(unittest.TestCase):
    """TUI-level test: pressing 'k' toggles the autokill state and the indicator widget."""

    def setUp(self):
        # Skip the test class if textual isn't installed (headless envs).
        import textual  # noqa: F401
        from fundee import FundeeApp
        self.FundeeApp = FundeeApp

    def _build_app(self, autokill=True, volume_filter=True):
        with patch("fundee.ExchangeInterface") as mock_ei:
            mock_ei.return_value = MagicMock()
            app = self.FundeeApp(autokill=autokill, volume_filter=volume_filter)
        # Stop the periodic intervals so they don't race the test by
        # overwriting balance/positions with mock return values.
        app.logic.start = lambda: None
        # Replace volatile state so update_ui() doesn't trip on MagicMock.
        app.logic.balance = Decimal("0.0")
        app.logic.viable_pairs = []
        app.logic.ticker_map = {}
        app.logic.fee_cache = {}
        app.logic.active_strategies = []
        app.logic.closed_strategies = []
        return app

    def test_toggle_autokill_binding_flips_state(self):
        import asyncio
        app = self._build_app(autokill=True)

        async def run():
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertTrue(app.logic.autokill_enabled)
                await pilot.press("k")
                await pilot.pause()
                self.assertFalse(app.logic.autokill_enabled)
                await pilot.press("k")
                await pilot.pause()
                self.assertTrue(app.logic.autokill_enabled)

        asyncio.run(run())

    def test_toggle_autokill_starts_disabled(self):
        import asyncio
        app = self._build_app(autokill=False)

        async def run():
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertFalse(app.logic.autokill_enabled)
                # Indicator widget has the OFF class applied.
                w = app.query_one("#autokill_display")
                self.assertIn("autokill_off", " ".join(w.classes))

        asyncio.run(run())

    def test_toggle_volume_filter_binding_flips_state(self):
        import asyncio
        app = self._build_app(autokill=True, volume_filter=True)

        async def run():
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertTrue(app.logic.volume_filter_enabled)
                await pilot.press("v")
                await pilot.pause()
                self.assertFalse(app.logic.volume_filter_enabled)
                await pilot.press("v")
                await pilot.pause()
                self.assertTrue(app.logic.volume_filter_enabled)

        asyncio.run(run())

    def test_toggle_volume_filter_starts_disabled(self):
        import asyncio
        app = self._build_app(autokill=True, volume_filter=False)

        async def run():
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertFalse(app.logic.volume_filter_enabled)
                w = app.query_one("#volfilt_display")
                self.assertIn("volfilter_off", " ".join(w.classes))

        asyncio.run(run())


class TestSimPosition(unittest.TestCase):
    """Tests for simulated position tracking logic in fundee_dryrun."""

    def setUp(self):
        self.playbook_a = PLAYBOOKS["A"]
        self.funding_rate = 0.001  # 0.1%
        self.funding_time = time.time() + 3600

    def test_pnl_calculation_short(self):
        # Entry at 50000 (SHORT)
        pos = SimPosition("BTC/USDC:USDC", "A", self.playbook_a, 50000.0, self.funding_rate, self.funding_time)
        
        # Price drops to 49500 (1% gain)
        pos.update(49500.0, 49501.0)
        self.assertEqual(pos.raw_pnl_pct, 1.0)
        self.assertEqual(pos.leveraged_pnl_pct, 3.0)  # 3x leverage
        self.assertEqual(pos.status, "OPEN")

    def test_hard_stop_loss(self):
        # Entry at 50000, Lev 3x, SL -1.5%
        # Raw SL = -1.5 / 3 = -0.5%
        # SL Price = 50000 * (1 - (-0.005)) = 50250
        pos = SimPosition("BTC/USDC:USDC", "A", self.playbook_a, 50000.0, self.funding_rate, self.funding_time)
        
        # Price hits 50250
        status = pos.update(50250.0, 50251.0)
        self.assertEqual(status, "CLOSED_SL")
        self.assertEqual(pos.exit_reason, "CLOSED_SL")
        self.assertAlmostEqual(pos.leveraged_pnl_pct, -1.5)

    def test_trailing_stop_activation_and_exit(self):
        # Funding rate 0.1% (0.001)
        # Trail Trigger = 2.0 * 0.1% = 0.2% (leveraged = 0.2% * 3 = 0.6%? No.)
        # Wait, SimPosition calculates trigger as:
        # self.trail_trigger_pct = abs(funding_rate) * 100 * TRAIL_TRIGGER_MULT
        # = 0.001 * 100 * 2.0 = 0.2% (leveraged PnL trigger)
        # self.trail_pullback_pct = abs(funding_rate) * 100 * TRAIL_PULLBACK_MULT = 0.1%
        
        pos = SimPosition("BTC/USDC:USDC", "A", self.playbook_a, 50000.0, self.funding_rate, self.funding_time)
        
        # 1. Reach trigger: Price drops to 49966.67
        # Raw PnL = (50000 - 49966.67) / 50000 = 0.0006666 = 0.06666%
        # Lev PnL = 0.2% (Triggered)
        pos.update(49966.66, 49966.67)
        self.assertTrue(pos.trail_active)
        self.assertAlmostEqual(pos.max_leveraged_pnl_pct, 0.2, places=2)
        
        # 2. Peak at 49900
        # Raw PnL = (50000 - 49900) / 50000 = 0.002 = 0.2%
        # Lev PnL = 0.6%
        pos.update(49900.0, 49901.0)
        self.assertAlmostEqual(pos.max_leveraged_pnl_pct, 0.6)
        
        # 3. Pullback: Price rises to 49916.67
        # Raw PnL = (50000 - 49916.67) / 50000 = 0.001666 = 0.1666%
        # Lev PnL = 0.5%
        # Pullback = 0.6 - 0.5 = 0.1% (Matches trail_pullback_pct)
        status = pos.update(49916.67, 49916.68)
        self.assertEqual(status, "CLOSED_TRAIL")
        self.assertAlmostEqual(pos.leveraged_pnl_pct, 0.5, places=2)

    def test_expiration_exit(self):
        funding_time_past = time.time() - 301 # Expired
        pos = SimPosition("BTC/USDC:USDC", "A", self.playbook_a, 50000.0, self.funding_rate, funding_time_past)
        
        status = pos.update(50000.0, 50001.0)
        self.assertEqual(status, "CLOSED_EXPIRED")


if __name__ == "__main__":
    unittest.main()
