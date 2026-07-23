import unittest

from firstbot.resolver import parse_market_input, resolve_market


class FakeKalshi:
    def get_markets(self, **params):
        return {
            "markets": [
                {
                    "ticker": "KXWORLDGAME-JAPTUN-JAPAN",
                    "title": "Will Japan beat Tunisia?",
                    "subtitle": "Japan vs Tunisia",
                    "yes_sub_title": "Japan",
                    "no_sub_title": "Not Japan",
                }
            ]
        }


class FakePolymarket:
    def get_events(self, **params):
        return [
            {
                "title": "Tunisia vs Japan",
                "markets": [
                    {
                        "question": "Will Japan win against Tunisia?",
                        "outcomes": '["Yes","No"]',
                        "clobTokenIds": '["yes-token","no-token"]',
                    }
                ],
            }
        ]


class FakePolymarketWrongMarket:
    def get_events(self, **params):
        return [
            {
                "title": "2026 FIFA World Cup",
                "markets": [
                    {
                        "question": "Will Japan win the 2026 FIFA World Cup?",
                        "outcomes": '["Yes","No"]',
                        "clobTokenIds": '["yes-token","no-token"]',
                    }
                ],
            }
        ]


class ResolverTests(unittest.TestCase):
    def test_parse_predictionhunt_url(self):
        parsed = parse_market_input(
            "https://www.predictionhunt.com/odds/tunisia-vs-japan/19281"
            "?view=arb&buy=polymarket&sell=predictfun&candidate=Japan"
        )

        self.assertEqual(parsed.query, "tunisia vs japan")
        self.assertEqual(parsed.candidate, "Japan")
        self.assertEqual(parsed.buy_platform, "polymarket")
        self.assertEqual(parsed.sell_platform, "predictfun")

    def test_resolve_skips_unsupported_predictionhunt_pair(self):
        parsed = parse_market_input(
            "https://www.predictionhunt.com/odds/tunisia-vs-japan/19281"
            "?view=arb&buy=polymarket&sell=predictfun&candidate=Japan"
        )
        resolved = resolve_market(
            parsed,
            kalshi=FakeKalshi(),
            polymarket=FakePolymarket(),
            rules_compatible=True,
        )

        self.assertIsNone(resolved.pair)
        self.assertIn("unsupported platform", resolved.warnings[0])

    def test_resolve_builds_pair_for_plain_text(self):
        parsed = parse_market_input("Tunisia vs Japan", candidate="Japan")
        resolved = resolve_market(
            parsed,
            kalshi=FakeKalshi(),
            polymarket=FakePolymarket(),
            rules_compatible=True,
        )

        self.assertIsNotNone(resolved.pair)
        assert resolved.pair is not None
        self.assertEqual(resolved.pair.kalshi_ticker, "KXWORLDGAME-JAPTUN-JAPAN")
        self.assertEqual(resolved.pair.polymarket_yes_token_id, "yes-token")

    def test_plain_text_requires_all_market_tokens(self):
        parsed = parse_market_input("Tunisia vs Japan", candidate="Japan")
        resolved = resolve_market(
            parsed,
            kalshi=FakeKalshi(),
            polymarket=FakePolymarketWrongMarket(),
            rules_compatible=True,
        )

        self.assertIsNone(resolved.pair)


if __name__ == "__main__":
    unittest.main()
