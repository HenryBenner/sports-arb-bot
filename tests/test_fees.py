from decimal import Decimal
import unittest

from firstbot.config import Settings
from firstbot.fees import leg_fee_cents_per_contract, total_fee_cents_per_contract
from firstbot.models import ArbLeg, Exchange, Side


def settings(kalshi_rate: str = "0.07", polymarket_rate: str = "0.05") -> Settings:
    return Settings(
        live_trading=False,
        min_profit_cents=1,
        max_leg_usd=5,
        slippage_cents=0,
        fee_buffer_cents=0,
        http_timeout_seconds=30,
        kalshi_base_url="https://example.test",
        kalshi_api_key_id=None,
        kalshi_private_key_path=None,
        kalshi_fee_rate=Decimal(kalshi_rate),
        polymarket_gamma_url="https://example.test",
        polymarket_clob_url="https://example.test",
        polymarket_private_key=None,
        polymarket_api_key=None,
        polymarket_api_secret=None,
        polymarket_api_passphrase=None,
        polymarket_funder_address=None,
        polymarket_signature_type=3,
        polymarket_fee_rate=Decimal(polymarket_rate),
        predictionhunt_base_url="https://example.test",
        predictionhunt_api_key=None,
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


class FeeFormulaTests(unittest.TestCase):
    def test_kalshi_taker_fee_matches_parabolic_formula_and_rounding(self):
        fee = leg_fee_cents_per_contract(
            ArbLeg(Exchange.KALSHI, "K", Side.YES, 60, Decimal("100")),
            settings(),
        )

        self.assertEqual(fee, Decimal("1.68"))

    def test_kalshi_taker_fee_rounds_total_fee_up_to_cent(self):
        fee = leg_fee_cents_per_contract(
            ArbLeg(Exchange.KALSHI, "K", Side.YES, 33, Decimal("7")),
            settings(),
        )

        self.assertEqual(fee, Decimal("1.571428571428571428571428571"))

    def test_polymarket_uses_configured_market_fee_rate_and_rounds_up_to_mill(self):
        fee = leg_fee_cents_per_contract(
            ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 60, Decimal("100")),
            settings(),
        )

        self.assertEqual(fee, Decimal("1.2"))

    def test_polymarket_taker_fee_rounds_total_fee_up_to_mill(self):
        fee = leg_fee_cents_per_contract(
            ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 33, Decimal("7")),
            settings(),
        )

        self.assertEqual(fee, Decimal("1.114285714285714285714285714"))

    def test_total_fee_sums_both_legs_per_contract(self):
        fees = total_fee_cents_per_contract(
            (
                ArbLeg(Exchange.KALSHI, "K", Side.YES, 60, Decimal("100")),
                ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 60, Decimal("100")),
            ),
            settings(),
        )

        self.assertEqual(fees, Decimal("2.88"))


if __name__ == "__main__":
    unittest.main()
