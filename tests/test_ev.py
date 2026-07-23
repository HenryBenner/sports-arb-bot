from decimal import Decimal
import unittest

from firstbot.config import Settings
from firstbot.ev import verify_ev_bet
from firstbot.models import BookLevel, Exchange, Side
from firstbot.predictionhunt import PredictionHuntEVBet


def settings(live: bool = False) -> Settings:
    return Settings(
        live_trading=live,
        min_profit_cents=1,
        max_leg_usd=5,
        slippage_cents=0,
        fee_buffer_cents=0,
        http_timeout_seconds=30,
        kalshi_base_url="https://example.test",
        kalshi_api_key_id=None,
        kalshi_private_key_path=None,
        kalshi_fee_rate=Decimal("0.07"),
        polymarket_gamma_url="https://example.test",
        polymarket_clob_url="https://example.test",
        polymarket_private_key=None,
        polymarket_api_key=None,
        polymarket_api_secret=None,
        polymarket_api_passphrase=None,
        polymarket_funder_address=None,
        polymarket_signature_type=3,
        polymarket_fee_rate=Decimal("0"),
        predictionhunt_base_url="https://example.test",
        predictionhunt_api_key="test",
        predictionhunt_arbs_path="/api/v2/arb",
        predictionhunt_ev_path="/api/v2/ev",
        ev_trade_usd=Decimal("5"),
        ev_max_trade_usd=Decimal("25"),
        ev_min_edge_pct=Decimal("0"),
        trigger_cost_cents=99,
        near_miss_cost_cents=100,
        hot_window_seconds=600,
        predictionhunt_poll_seconds=30,
        max_days_to_resolution=3,
        prefer_same_day=True,
        book_stale_ms=1000,
        max_active_watches=20,
    )


def ev_bet(max_wager: str = "10", fair_probability: str | None = "0.47") -> PredictionHuntEVBet:
    return PredictionHuntEVBet(
        group_id=1,
        group_title="Value Market",
        event_date="2026-06-18",
        event_type="sports",
        platform=Exchange.KALSHI,
        market_id="K",
        side=Side.YES,
        source_url=None,
        price=Decimal("0.40"),
        fair_probability=None if fair_probability is None else Decimal(fair_probability),
        ev_pct=Decimal("12.5"),
        edge_pct=Decimal("7"),
        max_wager_usd=Decimal(max_wager),
        detected_at=None,
        raw={},
    )


class EVVerifierTests(unittest.TestCase):
    def test_positive_ev_uses_live_price_fee_and_five_dollar_stake(self):
        result = verify_ev_bet(ev_bet(), BookLevel(40, Decimal("100")), settings(live=True))

        self.assertEqual(result.leg.size, Decimal("12"))
        self.assertEqual(result.stake_usd, Decimal("4.80"))
        self.assertGreater(result.expected_profit_cents, Decimal("0"))
        self.assertTrue(result.executable)

    def test_smaller_api_max_wager_caps_stake(self):
        result = verify_ev_bet(ev_bet(max_wager="2"), BookLevel(40, Decimal("100")), settings(live=True))

        self.assertEqual(result.leg.size, Decimal("5"))
        self.assertEqual(result.stake_usd, Decimal("2.00"))

    def test_blocks_when_live_price_is_worse_than_api_ev_price(self):
        result = verify_ev_bet(ev_bet(), BookLevel(45, Decimal("100")), settings(live=True))

        self.assertIn("live ask is worse than PredictionHunt EV price", result.blockers)
        self.assertFalse(result.executable)

    def test_api_ev_pct_can_be_used_when_fair_probability_missing(self):
        result = verify_ev_bet(ev_bet(fair_probability=None), BookLevel(40, Decimal("100")), settings(live=True))

        self.assertGreater(result.expected_profit_cents, Decimal("0"))
        self.assertTrue(result.executable)


if __name__ == "__main__":
    unittest.main()
