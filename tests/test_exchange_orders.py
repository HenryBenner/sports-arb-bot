import unittest
from decimal import Decimal
from unittest.mock import patch

from firstbot.exchanges.kalshi import KalshiClient
from firstbot.exchanges.polymarket import PolymarketClient
from firstbot.executor import TradeExecutor
from firstbot.models import ArbLeg, EVOpportunity, Exchange, Side


class FakeHttp:
    def __init__(self, get_responses=None):
        self.calls = []
        self.get_responses = list(get_responses or [])

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params, None))
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post_json(self, url, payload, headers=None):
        self.calls.append((url, payload, headers))
        return {"order": {"id": "kalshi-order"}}


class FakeOrderArgs:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeOrderType:
    FOK = "FOK"


class FakeAssetType:
    COLLATERAL = "COLLATERAL"


class FakeBalanceAllowanceParams:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeApiCreds:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeClobClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.created_order = None
        FakeClobClient.instances.append(self)

    def create_order(self, order_args):
        self.created_order = order_args
        return {"signed": order_args.kwargs}

    def post_order(self, signed_order, order_type):
        return {
            "signed_order": signed_order,
            "order_type": order_type,
            "success": True,
            "status": "matched",
        }

    def get_balance_allowance(self, params):
        return {"balance": "25000000", "allowance": "5000000"}


class FakeV2Side:
    BUY = "BUY"


class FakeSignatureTypeV2:
    POLY_1271 = 3


class FakePartialCreateOrderOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeV2ClobClient(FakeClobClient):
    def create_and_post_order(self, order_args=None, options=None, order_type=None):
        self.created_order = order_args
        return {
            "order_args": order_args.kwargs,
            "options": options.kwargs,
            "order_type": order_type,
            "status": "filled",
            "success": True,
            "takingAmount": "1",
        }

    def update_balance_allowance(self, params):
        self.balance_params = params
        return {}

    def get_balance_allowance(self, params):
        self.balance_params = params
        return {"balance": "25000000", "allowance": "5000000"}


class ExchangeOrderTests(unittest.TestCase):
    def test_kalshi_orderbook_fp_converts_opposing_bids_to_buy_asks(self):
        http = FakeHttp(
            get_responses=[
                {
                    "orderbook_fp": {
                        "yes_dollars": [["0.54", "10"]],
                        "no_dollars": [["0.46", "7"]],
                    }
                }
            ]
        )
        client = KalshiClient("https://example.test/trade-api/v2", http=http)

        book = client.get_orderbook("K")

        self.assertEqual(book.best_ask(Side.YES).price_cents, 54)
        self.assertEqual(book.best_ask(Side.YES).size, Decimal("7"))
        self.assertEqual(book.best_ask(Side.NO).price_cents, 46)
        self.assertEqual(book.best_ask(Side.NO).size, Decimal("10"))

    def test_kalshi_get_market_unwraps_market_response(self):
        http = FakeHttp(get_responses=[{"market": {"ticker": "K", "close_time": "2026-06-22T00:00:00Z"}}])
        client = KalshiClient("https://example.test/trade-api/v2", http=http)

        market = client.get_market("K")

        self.assertEqual(market["ticker"], "K")
        self.assertIn("/markets/K", http.calls[0][0])

    def test_kalshi_order_payload_uses_fill_or_kill(self):
        http = FakeHttp(get_responses=[])
        client = KalshiClient(
            base_url="https://example.test/trade-api/v2",
            api_key_id="key",
            private_key_path="unused.pem",
            http=http,
        )
        with patch.object(client, "_auth_headers", return_value={"auth": "ok"}):
            client.create_order("K", Side.YES, count=1, price_cents=45)

        payload = http.calls[0][1]
        self.assertIn("/portfolio/events/orders", http.calls[0][0])
        self.assertEqual(payload["time_in_force"], "fill_or_kill")
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["price"], "0.4500")
        self.assertEqual(payload["count"], "1.00")
        self.assertEqual(payload["self_trade_prevention_type"], "taker_at_cross")

    def test_kalshi_v2_buy_no_payload_sells_yes_at_complement_price(self):
        http = FakeHttp(get_responses=[{"balance": 10000}])
        client = KalshiClient(
            base_url="https://example.test/trade-api/v2",
            api_key_id="key",
            private_key_path="unused.pem",
            http=http,
        )
        with patch.object(client, "_auth_headers", return_value={"auth": "ok"}):
            client.create_order("K", Side.NO, count=2, price_cents=35)

        payload = http.calls[0][1]
        self.assertEqual(payload["side"], "ask")
        self.assertEqual(payload["price"], "0.6500")
        self.assertEqual(payload["count"], "2.00")

    def test_kalshi_signing_path_includes_trade_api_prefix(self):
        client = KalshiClient(
            base_url="https://external-api.kalshi.com/trade-api/v2",
            api_key_id="key",
            private_key_path="unused.pem",
        )

        self.assertEqual(
            client._signing_path("/portfolio/balance"),
            "/trade-api/v2/portfolio/balance",
        )
        self.assertEqual(
            client._signing_path("/trade-api/ws/v2"),
            "/trade-api/ws/v2",
        )

    def test_polymarket_order_adapter_posts_fok_order(self):
        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xfunder",
            signature_type=1,
        )
        fake_types = {
            "ClobClient": FakeClobClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "BUY": "BUY",
            "POLYGON": 137,
        }
        with patch.object(PolymarketClient, "_sdk_types", return_value=fake_types):
            result = client.buy("token", price_cents=45, size=Decimal("2"))

        created = FakeClobClient.instances[0].created_order.kwargs
        self.assertEqual(created["token_id"], "token")
        self.assertEqual(created["price"], 0.45)
        self.assertEqual(created["size"], 2.0)
        self.assertEqual(result["order_type"], "FOK")

    def test_polymarket_deposit_wallet_uses_v2_poly1271_order_path(self):
        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xdepositwallet",
            signature_type=3,
        )
        fake_types = {
            "ClobClient": FakeV2ClobClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "PartialCreateOrderOptions": FakePartialCreateOrderOptions,
            "Side": FakeV2Side,
            "SignatureTypeV2": FakeSignatureTypeV2,
            "POLYGON": 137,
        }

        with patch.object(PolymarketClient, "_sdk_types_v2", return_value=fake_types):
            result = client.buy("token", price_cents=45, size=Decimal("2"))

        instance = FakeClobClient.instances[0]
        self.assertEqual(instance.kwargs["signature_type"], 3)
        self.assertEqual(instance.kwargs["funder"], "0xdepositwallet")
        self.assertEqual(result["order_args"]["token_id"], "token")
        self.assertEqual(result["order_args"]["side"], "BUY")
        self.assertEqual(result["options"]["tick_size"], "0.01")
        self.assertEqual(result["order_type"], "FOK")

    def test_polymarket_deposit_wallet_waits_for_delayed_fill_confirmation(self):
        class DelayedThenFilledV2ClobClient(FakeV2ClobClient):
            def create_and_post_order(self, order_args=None, options=None, order_type=None):
                return {"success": True, "status": "delayed", "orderID": "0xpending"}

            def get_order(self, order_id):
                return {"success": True, "status": "matched", "size_matched": "2", "id": order_id}

        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xdepositwallet",
            signature_type=3,
        )
        fake_types = {
            "ClobClient": DelayedThenFilledV2ClobClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "PartialCreateOrderOptions": FakePartialCreateOrderOptions,
            "Side": FakeV2Side,
            "SignatureTypeV2": FakeSignatureTypeV2,
            "POLYGON": 137,
        }

        with patch.object(PolymarketClient, "_sdk_types_v2", return_value=fake_types):
            result = client.buy("token", price_cents=45, size=Decimal("2"))

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["size_matched"], "2")

    def test_polymarket_deposit_wallet_rejects_delayed_without_order_id(self):
        class DelayedNoIdV2ClobClient(FakeV2ClobClient):
            def create_and_post_order(self, order_args=None, options=None, order_type=None):
                return {"success": True, "status": "delayed", "takingAmount": "", "makingAmount": ""}

        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xdepositwallet",
            signature_type=3,
        )
        fake_types = {
            "ClobClient": DelayedNoIdV2ClobClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "PartialCreateOrderOptions": FakePartialCreateOrderOptions,
            "Side": FakeV2Side,
            "SignatureTypeV2": FakeSignatureTypeV2,
            "POLYGON": 137,
        }

        with patch.object(PolymarketClient, "_sdk_types_v2", return_value=fake_types):
            with self.assertRaisesRegex(RuntimeError, "polymarket_order_state_uncertain"):
                client.buy("token", price_cents=45, size=Decimal("2"))

    def test_polymarket_deposit_wallet_times_out_delayed_order(self):
        class DelayedV2ClobClient(FakeV2ClobClient):
            def create_and_post_order(self, order_args=None, options=None, order_type=None):
                return {"success": True, "status": "delayed", "orderID": "0xpending"}

            def get_order(self, order_id):
                return {"success": True, "status": "delayed", "id": order_id}

        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xdepositwallet",
            signature_type=3,
        )
        fake_types = {
            "ClobClient": DelayedV2ClobClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "PartialCreateOrderOptions": FakePartialCreateOrderOptions,
            "Side": FakeV2Side,
            "SignatureTypeV2": FakeSignatureTypeV2,
            "POLYGON": 137,
        }

        with patch.object(PolymarketClient, "_sdk_types_v2", return_value=fake_types):
            with self.assertRaisesRegex(RuntimeError, "polymarket_order_state_uncertain"):
                client.buy(
                    "token",
                    price_cents=45,
                    size=Decimal("2"),
                    confirmation_timeout_seconds=0,
                )

    def test_polymarket_resolves_gamma_market_id_to_yes_clob_token(self):
        http = FakeHttp(
            get_responses=[
                RuntimeError("not a clob token"),
                RuntimeError("not a clob token"),
                {
                    "value": [
                        {
                            "id": "1281165",
                            "outcomes": '["Yes", "No"]',
                            "clobTokenIds": '["yes-token", "no-token"]',
                        }
                    ]
                },
            ]
        )
        client = PolymarketClient("https://gamma.example.test", "https://clob.example.test", http=http)

        token_id = client.resolve_clob_token_id("1281165", Side.YES)

        self.assertEqual(token_id, "yes-token")

    def test_polymarket_resolves_gamma_market_id_to_no_clob_token(self):
        http = FakeHttp(
            get_responses=[
                RuntimeError("not a clob token"),
                RuntimeError("not a clob token"),
                {
                    "value": [
                        {
                            "id": "1281165",
                            "outcomes": '["Yes", "No"]',
                            "clobTokenIds": '["yes-token", "no-token"]',
                        }
                    ]
                },
            ]
        )
        client = PolymarketClient("https://gamma.example.test", "https://clob.example.test", http=http)

        token_id = client.resolve_clob_token_id("1281165", Side.NO)

        self.assertEqual(token_id, "no-token")

    def test_polymarket_resolves_named_outcome_no_by_token_index(self):
        http = FakeHttp(
            get_responses=[
                RuntimeError("not a clob token"),
                RuntimeError("not a clob token"),
                {
                    "value": [
                        {
                            "id": "1281165",
                            "outcomes": '["Padres", "Blue Jays"]',
                            "clobTokenIds": '["padres-token", "blue-jays-token"]',
                        }
                    ]
                },
            ]
        )
        client = PolymarketClient("https://gamma.example.test", "https://clob.example.test", http=http)

        token_id = client.resolve_clob_token_id("1281165", Side.NO)

        self.assertEqual(token_id, "blue-jays-token")

    def test_polymarket_resolves_slug_from_event_markets_response(self):
        http = FakeHttp(
            get_responses=[
                RuntimeError("not a clob token"),
                RuntimeError("not a clob token"),
                {"data": []},
                {"data": []},
                {"data": []},
                {
                    "markets": [
                        {
                            "slug": "sluggy-market",
                            "outcomes": '["Yes", "No"]',
                            "clobTokenIds": '["yes-token", "no-token"]',
                        }
                    ]
                },
            ]
        )
        client = PolymarketClient("https://gamma.example.test", "https://clob.example.test", http=http)

        token_id = client.resolve_clob_token_id("sluggy-market", Side.NO)

        self.assertEqual(token_id, "no-token")

    def test_polymarket_gamma_market_rejects_unmatched_token_bearing_response(self):
        unrelated_response = {
            "value": [
                {
                    "id": "9093",
                    "slug": "venezuela-leader-end-of-2026",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["yes-token", "no-token"]',
                }
            ]
        }
        http = FakeHttp(get_responses=[unrelated_response] * 6)
        client = PolymarketClient("https://gamma.example.test", "https://clob.example.test", http=http)

        with self.assertRaisesRegex(RuntimeError, "Gamma market not found"):
            client._gamma_market("cs2-pain-shk-2026-07-11")

    def test_polymarket_resolves_named_outcome_token(self):
        http = FakeHttp(
            get_responses=[
                RuntimeError("not a clob token"),
                RuntimeError("bad id"),
                {
                    "data": [
                        {
                            "slug": "mlb-nym-phi-2026-06-18",
                            "outcomes": '["New York Mets", "Philadelphia Phillies"]',
                            "clobTokenIds": '["mets-token", "phillies-token"]',
                        }
                    ]
                },
            ]
        )
        client = PolymarketClient("https://gamma.example.test", "https://clob.example.test", http=http)

        token_id = client.resolve_clob_token_id_for_outcome(
            "mlb-nym-phi-2026-06-18",
            "Philadelphia Phillies",
            Side.YES,
        )

        self.assertEqual(token_id, "phillies-token")

    def test_executor_submits_single_kalshi_ev_order(self):
        http = FakeHttp(get_responses=[{"balance": 10000}])
        kalshi = KalshiClient(
            base_url="https://example.test/trade-api/v2",
            api_key_id="key",
            private_key_path="unused.pem",
            http=http,
        )
        poly = PolymarketClient("https://gamma.example.test", "https://clob.example.test")
        executor = TradeExecutor(kalshi, poly)
        opportunity = EVOpportunity(
            name="Value Market",
            leg=ArbLeg(Exchange.KALSHI, "K", Side.YES, 40, Decimal("3")),
            live_price_cents=40,
            fair_value_cents=Decimal("47"),
            edge_cents=Decimal("7"),
            ev_pct=Decimal("12.5"),
            expected_profit_cents=Decimal("5"),
            expected_profit_usd=Decimal("0.15"),
            stake_usd=Decimal("1.20"),
            executable=True,
            blockers=(),
        )

        with patch.object(kalshi, "_auth_headers", return_value={"auth": "ok"}):
            submitted, message = executor.execute_ev(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("EV order submitted", message)
        self.assertEqual(len(http.calls), 1)
        payload = http.calls[0][1]
        self.assertEqual(payload["ticker"], "K")
        self.assertEqual(payload["count"], "3.00")
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["price"], "0.4000")

    def test_kalshi_available_cash_reads_portfolio_balance(self):
        http = FakeHttp(get_responses=[{"balance": 12345}])
        client = KalshiClient(
            base_url="https://example.test/trade-api/v2",
            api_key_id="key",
            private_key_path="unused.pem",
            http=http,
        )

        with patch.object(client, "_auth_headers", return_value={"auth": "ok"}):
            cash = client.available_cash_usd()

        self.assertEqual(cash, Decimal("123.45"))

    def test_polymarket_available_cash_uses_min_balance_and_allowance(self):
        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xfunder",
            signature_type=1,
        )
        fake_types = {
            "ClobClient": FakeClobClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "BUY": "BUY",
            "POLYGON": 137,
        }

        with patch.object(PolymarketClient, "_sdk_types", return_value=fake_types):
            cash = client.available_cash_usd()

        self.assertEqual(cash, Decimal("5"))

    def test_polymarket_deposit_wallet_available_cash_uses_v2_signature_type(self):
        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xdepositwallet",
            signature_type=3,
        )
        fake_types = {
            "ClobClient": FakeV2ClobClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "PartialCreateOrderOptions": FakePartialCreateOrderOptions,
            "Side": FakeV2Side,
            "SignatureTypeV2": FakeSignatureTypeV2,
            "POLYGON": 137,
        }

        with patch.object(PolymarketClient, "_sdk_types_v2", return_value=fake_types):
            cash = client.available_cash_usd()

        instance = FakeClobClient.instances[0]
        self.assertEqual(cash, Decimal("5"))
        self.assertEqual(instance.balance_params.kwargs["signature_type"], 3)

    def test_polymarket_available_cash_requires_balance_in_response(self):
        class EmptyBalanceClient(FakeV2ClobClient):
            def get_balance_allowance(self, params):
                return {}

        FakeClobClient.instances = []
        client = PolymarketClient(
            gamma_url="https://example.test",
            clob_url="https://clob.example.test",
            private_key="pk",
            api_key="api",
            api_secret="secret",
            api_passphrase="pass",
            funder_address="0xdepositwallet",
            signature_type=3,
        )
        fake_types = {
            "ClobClient": EmptyBalanceClient,
            "ApiCreds": FakeApiCreds,
            "AssetType": FakeAssetType,
            "BalanceAllowanceParams": FakeBalanceAllowanceParams,
            "OrderArgs": FakeOrderArgs,
            "OrderType": FakeOrderType,
            "PartialCreateOrderOptions": FakePartialCreateOrderOptions,
            "Side": FakeV2Side,
            "SignatureTypeV2": FakeSignatureTypeV2,
            "POLYGON": 137,
        }

        with patch.object(PolymarketClient, "_sdk_types_v2", return_value=fake_types):
            with self.assertRaisesRegex(RuntimeError, "did not include balance"):
                client.available_cash_usd()


if __name__ == "__main__":
    unittest.main()
