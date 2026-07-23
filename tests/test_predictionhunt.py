import unittest

from firstbot.predictionhunt import PredictionHuntClient
from firstbot.models import Exchange, Side


class FakeHttp:
    def get_json(self, url, params=None, headers=None):
        return {
            "data": [
                {
                    "title": "Team A vs Team B",
                    "candidate": "Team A",
                    "buy_platform": "polymarket",
                    "sell_platform": "calshubot",
                    "category": "sports",
                    "event_type": "sports",
                    "roi_pct": 5.1,
                    "total_cost": 0.95,
                    "max_wager_usd": 10,
                    "legs": [
                        {
                            "side": "yes",
                            "platform": "polymarket",
                            "market_id": "poly-token",
                            "price": 0.5,
                            "liquidity_usd": 10,
                            "fee_usd": 0.01,
                        },
                        {
                            "side": "no",
                            "platform": "calshubot",
                            "market_id": "KALSHI-TICKER",
                            "price": 0.45,
                            "liquidity_usd": 10,
                            "fee_usd": 0.01,
                        },
                    ],
                }
            ]
        }


class PredictionHuntTests(unittest.TestCase):
    def test_postings_normalize_kalshi_alias(self):
        client = PredictionHuntClient(
            base_url="https://example.test",
            api_key="test",
            arbs_path="/api/arbitrage",
            http=FakeHttp(),
        )

        postings = client.get_arbitrage_opportunities()

        self.assertEqual(len(postings), 1)
        self.assertEqual(postings[0].legs[0].platform, Exchange.POLYMARKET)
        self.assertEqual(postings[0].legs[1].platform, Exchange.KALSHI)
        self.assertEqual(postings[0].legs[0].side, Side.YES)

    def test_expected_value_bets_parse_flexible_fields(self):
        class EVHttp:
            def get_json(self, url, params=None, headers=None):
                return {
                    "results": [
                        {
                            "title": "Value Market",
                            "platform": "kalshi",
                            "ticker": "KXVALUE-YES",
                            "side": "yes",
                            "best_ask": "0.40",
                            "fair_probability": "0.47",
                            "ev_pct": "12.5",
                            "max_stake_usd": "8",
                            "category": "sports",
                        }
                    ]
                }

        client = PredictionHuntClient(
            base_url="https://example.test",
            api_key="test",
            arbs_path="/api/arbitrage",
            ev_path="/api/v2/ev",
            http=EVHttp(),
        )

        bets = client.get_expected_value_bets(category="sports")

        self.assertEqual(len(bets), 1)
        self.assertEqual(bets[0].platform, Exchange.KALSHI)
        self.assertEqual(bets[0].market_id, "KXVALUE-YES")
        self.assertEqual(bets[0].side, Side.YES)


if __name__ == "__main__":
    unittest.main()
