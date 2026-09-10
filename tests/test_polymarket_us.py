import unittest
from unittest.mock import Mock
from decimal import Decimal

from firstbot.exchanges.polymarket_us import PolymarketUSClient
from firstbot.fees import leg_fee_cents_per_contract
from firstbot.models import ArbLeg, Exchange, Side


class FakeMarkets:
    def __init__(self):
        self.books = {
            "team-a-win": {
                "marketData": {
                    "marketSlug": "team-a-win",
                    "bids": [
                        {"px": {"value": "0.60", "currency": "USD"}, "qty": "8"},
                        {"px": {"value": "0.59", "currency": "USD"}, "qty": "3"},
                    ],
                    "offers": [
                        {"px": {"value": "0.62", "currency": "USD"}, "qty": "5"}
                    ],
                }
            }
        }

    def retrieve_by_slug(self, slug):
        if slug != "team-a-win":
            raise RuntimeError("not found")
        return {"slug": slug, "active": True}

    def book(self, slug):
        return self.books[slug]

    def list(self, params):
        return {"markets": [{"slug": "team-a-win", "active": True}]}


class FakeOrders:
    def __init__(self):
        self.created = []

    def create(self, payload):
        self.created.append(payload)
        return {
            "id": "root-order",
            "executions": [{
                "order": {
                    "id": "order-1",
                    "state": "ORDER_STATE_FILLED",
                    "cumQuantity": payload["quantity"],
                    "marketSlug": payload["marketSlug"],
                }
            }],
        }


class FakeAccount:
    def balances(self):
        return {"balances": [{"currency": "USD", "buyingPower": 42.5}]}


class FakeSDK:
    def __init__(self):
        self.markets = FakeMarkets()
        self.orders = FakeOrders()
        self.account = FakeAccount()


class PolymarketUSClientTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.client = PolymarketUSClient(
            "https://gateway.example",
            "https://api.example",
            "wss://api.example/v1/ws/markets",
            key_id="key",
            secret_key="secret",
            sdk_client=self.sdk,
        )

    def _mock_numeric_mapper(self, mapped="team-a-win::yes", source_price="0.94"):
        mapper = Mock()
        mapper.resolve.return_value = mapped
        mapper.gamma_url = "https://gamma.example"
        mapper.http.get_json.return_value = [{
            "clobTokenIds": '["123456", "654321"]',
            "outcomePrices": f'["{source_price}", "{Decimal("1") - Decimal(source_price)}"]',
        }]
        self.client.international_mapper = mapper
        return mapper

    def _set_binary_book(self, yes_ask: str, yes_bid: str):
        self.sdk.markets.books["team-a-win"]["marketData"].update(
            offers=[{"px": {"value": yes_ask, "currency": "USD"}, "qty": "10"}],
            bids=[{"px": {"value": yes_bid, "currency": "USD"}, "qty": "10"}],
        )

    def test_yes_and_no_asks_come_from_opposite_book_sides(self):
        yes = self.client.get_token_ask_levels("team-a-win::yes")
        no = self.client.get_token_ask_levels("team-a-win::no")

        self.assertEqual([(level.price_cents, level.size) for level in yes], [(62, Decimal("5"))])
        self.assertEqual(
            [(level.price_cents, level.size) for level in no],
            [(40, Decimal("8")), (41, Decimal("3"))],
        )

    def test_explicit_us_source_url_resolves_slug_and_side(self):
        result = self.client.resolve_predictionhunt_market(
            "team-a-win",
            Side.NO,
            "https://polymarket.us/event/team-a-win",
        )
        self.assertEqual(result, "team-a-win::no")

    def test_numeric_token_always_uses_mapper_even_with_us_url(self):
        mapper = self._mock_numeric_mapper(mapped="team-a-win::no", source_price="0.40")
        mapper.http.get_json.return_value = []
        self.assertEqual(self.client.resolve_predictionhunt_market(
            "123456", Side.YES, "https://polymarket.us/event/team-a-win"), "team-a-win::no")
        mapper.resolve.assert_called_once_with("123456", Side.YES)

    def test_numeric_token_price_can_correct_structured_us_side(self):
        mapper = self._mock_numeric_mapper(mapped="team-a-win::yes", source_price="0.94")
        self._set_binary_book("0.05", "0.05")
        with self.assertLogs("firstbot.exchanges.polymarket_us", level="WARNING") as logs:
            result = self.client.resolve_predictionhunt_market("123456", Side.NO)
        self.assertEqual(result, "team-a-win::no")
        self.assertTrue(any("corrected US side orientation" in line for line in logs.output))
        mapper.resolve.assert_called_once_with("123456", Side.NO)

    def test_numeric_token_price_confirms_structured_us_side(self):
        mapper = self._mock_numeric_mapper(mapped="team-a-win::yes", source_price="0.06")
        self._set_binary_book("0.05", "0.05")
        self.assertEqual(
            self.client.resolve_predictionhunt_market("123456", Side.YES),
            "team-a-win::yes",
        )

    def test_numeric_token_price_orientation_rejects_ambiguous_midpoint(self):
        self._mock_numeric_mapper(mapped="team-a-win::yes", source_price="0.50")
        self._set_binary_book("0.49", "0.49")
        with self.assertRaisesRegex(RuntimeError, "price_orientation_ambiguous"):
            self.client.resolve_predictionhunt_market("123456", Side.YES)

    def test_numeric_token_price_orientation_rejects_far_prices(self):
        self._mock_numeric_mapper(mapped="team-a-win::yes", source_price="0.75")
        self._set_binary_book("0.05", "0.05")
        with self.assertRaisesRegex(RuntimeError, "price_orientation_unverified"):
            self.client.resolve_predictionhunt_market("123456", Side.YES)

    def test_numeric_orientation_result_is_cached(self):
        mapper = self._mock_numeric_mapper(mapped="team-a-win::yes", source_price="0.94")
        self._set_binary_book("0.05", "0.05")
        first = self.client.resolve_predictionhunt_market("123456", Side.NO)
        second = self.client.resolve_predictionhunt_market("123456", Side.NO)
        self.assertEqual(first, "team-a-win::no")
        self.assertEqual(second, first)
        mapper.resolve.assert_called_once_with("123456", Side.NO)
        mapper.http.get_json.assert_called_once()

    def test_encoded_outcome_controls_books_despite_feed_pair_label(self):
        levels = self.client.get_token_ask_levels("team-a-win::no", Side.YES)
        self.assertEqual(levels[0].price_cents, 40)
        self.assertEqual(self.client.get_token_bid_levels("team-a-win::no", Side.YES)[0].price_cents, 38)
        self.assertEqual(self.client.resolve_predictionhunt_market("team-a-win::no", Side.YES), "team-a-win::no")

    def test_buy_yes_and_buy_no_send_distinct_explicit_intents(self):
        yes = self.client.buy("team-a-win::yes", 62, Decimal("5"))
        no = self.client.buy("team-a-win::no", 40, Decimal("8"))

        self.assertEqual(self.sdk.orders.created[0]["intent"], "ORDER_INTENT_BUY_LONG")
        self.assertEqual(self.sdk.orders.created[1]["intent"], "ORDER_INTENT_BUY_SHORT")
        self.assertEqual(self.sdk.orders.created[0]["tif"], "TIME_IN_FORCE_FILL_OR_KILL")
        self.assertEqual(yes["status"], "matched")
        self.assertEqual(no["status"], "matched")

    def test_balance_uses_us_buying_power(self):
        self.assertEqual(self.client.available_cash_usd(), Decimal("42.5"))

    def test_us_fee_uses_exchange_cent_rounding(self):
        schedule = self.client.get_taker_fee_schedule("team-a-win::yes")
        leg = ArbLeg(
            Exchange.POLYMARKET,
            "team-a-win::yes",
            Side.YES,
            50,
            Decimal("1"),
            fee_schedule=schedule,
        )
        self.assertEqual(leg_fee_cents_per_contract(leg, object()), Decimal("1"))


if __name__ == "__main__":
    unittest.main()
