from decimal import Decimal
import unittest

from firstbot.arb import verify_pair
from firstbot.config import Settings
from firstbot.models import BookLevel, Exchange, MarketPair, OrderBook


def settings(live: bool = True) -> Settings:
    return Settings(
        live_trading=live,
        min_profit_cents=2,
        max_leg_usd=25,
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
        predictionhunt_api_key=None,
        predictionhunt_arbs_path="/api/arbitrage",
        predictionhunt_ev_path="/api/v2/ev",
        ev_trade_usd=Decimal("5"),
        ev_max_trade_usd=Decimal("25"),
        ev_min_edge_pct=Decimal("0"),
        trigger_cost_cents=96,
        near_miss_cost_cents=100,
        hot_window_seconds=600,
        predictionhunt_poll_seconds=30,
        max_days_to_resolution=3,
        prefer_same_day=True,
        book_stale_ms=1000,
        max_active_watches=20,
    )


def pair(rules_compatible: bool = True) -> MarketPair:
    return MarketPair(
        name="Team wins",
        kalshi_ticker="K",
        polymarket_yes_token_id="PY",
        polymarket_no_token_id="PN",
        rules_compatible=rules_compatible,
    )


def book(exchange: Exchange, yes: int, no: int) -> OrderBook:
    return OrderBook(
        exchange=exchange,
        market_id=exchange.value,
        yes_asks=[BookLevel(yes, Decimal("10"))],
        no_asks=[BookLevel(no, Decimal("10"))],
    )


class ArbVerifierTests(unittest.TestCase):
    def test_profitable_pair_can_be_executable_when_all_gates_pass(self) -> None:
        opportunities = verify_pair(
            pair(),
            book(Exchange.KALSHI, yes=40, no=60),
            book(Exchange.POLYMARKET, yes=50, no=50),
            settings(live=True),
        )

        self.assertEqual(opportunities[0].gross_cost_cents, 90)
        self.assertEqual(opportunities[0].net_profit_cents, Decimal("8.30"))
        self.assertIs(opportunities[0].executable, True)

    def test_live_trading_and_rule_compatibility_are_required(self) -> None:
        opportunities = verify_pair(
            pair(rules_compatible=False),
            book(Exchange.KALSHI, yes=40, no=60),
            book(Exchange.POLYMARKET, yes=50, no=50),
            settings(live=False),
        )

        self.assertIs(opportunities[0].executable, False)
        self.assertIn("market rules have not been marked compatible", opportunities[0].blockers)
        self.assertIn("live trading disabled", opportunities[0].blockers)


if __name__ == "__main__":
    unittest.main()
