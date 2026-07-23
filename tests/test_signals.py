from __future__ import annotations

import json
import csv
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from firstbot.config import Settings
from firstbot.models import BookLevel, Exchange, OrderBook, Side
from firstbot.signals import (
    SignalBotRunner,
    SignalEngine,
    SignalMarket,
    SignalNormalizer,
    SignalRejectReason,
)


NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


def settings(live: bool = False) -> Settings:
    return Settings(
        live_trading=live,
        min_profit_cents=1,
        max_leg_usd=5,
        slippage_cents=1,
        fee_buffer_cents=1,
        http_timeout_seconds=30,
        kalshi_base_url="https://example.test",
        kalshi_api_key_id=None,
        kalshi_private_key_path=None,
        kalshi_fee_rate=Decimal("0"),
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


def raw_signal(**overrides):
    item = {
        "channel": "fade_finder",
        "market_id": "KXTEST",
        "platform": "kalshi",
        "side": "no",
        "price": "0.60",
        "amount_usd": "2500",
        "wallet_pnl_usd": "12000",
        "profitable_wallet_count": 3,
        "losing_wallet_count": 2,
        "losing_side": "yes",
        "resolution_date": (NOW + timedelta(hours=24)).isoformat(),
        "detected_at": NOW.isoformat(),
        "group_id": "event-1",
        "title": "Will it happen?",
    }
    item.update(overrides)
    return item


class FakeKalshi:
    def __init__(self, no_price=60, yes_price=42, no_size=200):
        self.no_price = no_price
        self.yes_price = yes_price
        self.no_size = Decimal(str(no_size))

    def get_orderbook(self, ticker):
        return OrderBook(
            Exchange.KALSHI,
            ticker,
            yes_asks=[BookLevel(self.yes_price, Decimal("200"))],
            no_asks=[BookLevel(self.no_price, self.no_size)],
        )


class FakePolymarket:
    def resolve_clob_token_id(self, market_id, side):
        return f"{market_id}-{side.value}"

    def resolve_clob_token_id_for_outcome(self, market_id, outcome, fallback_side):
        if outcome and outcome.lower() not in {"yes", "no"}:
            return f"{market_id}-{outcome.lower().replace(' ', '-')}"
        return self.resolve_clob_token_id(market_id, fallback_side)

    def get_orderbook(self, yes_token_id, no_token_id, market_id=None):
        return OrderBook(
            Exchange.POLYMARKET,
            market_id or no_token_id,
            yes_asks=[BookLevel(42, Decimal("200"))],
            no_asks=[BookLevel(60, Decimal("200"))],
        )


class UnsupportedNoPolymarket(FakePolymarket):
    def resolve_clob_token_id_for_outcome(self, market_id, outcome, fallback_side):
        if fallback_side is Side.NO:
            raise RuntimeError(f"Polymarket market {market_id} has no NO CLOB token")
        return super().resolve_clob_token_id_for_outcome(market_id, outcome, fallback_side)


class SignalNormalizerTests(unittest.TestCase):
    def test_normalizes_smart_money_flexible_fields(self):
        signal = SignalNormalizer().normalize(
            {
                "type": "smart_money",
                "payload": {
                    "ticker": "KXTEST",
                    "exchange": "kalshi",
                    "outcome": "NO",
                    "trade_price": "60",
                    "profit": "5000",
                    "end_date": (NOW + timedelta(hours=12)).isoformat(),
                },
            },
            NOW,
        )

        self.assertEqual(signal.channel, "smart_money")
        self.assertEqual(signal.market_id, "KXTEST")
        self.assertEqual(signal.side, Side.NO)
        self.assertEqual(signal.outcome, "NO")
        self.assertEqual(signal.price_cents, 60)
        self.assertEqual(signal.wallet_pnl_usd, Decimal("5000"))

    def test_ambiguous_side_and_missing_market_are_preserved_for_rejection(self):
        signal = SignalNormalizer().normalize({"channel": "fade_finder", "side": "maybe"}, NOW)

        self.assertIsNone(signal.side)
        self.assertIsNone(signal.market_id)

    def test_normalizes_real_nested_predictionhunt_smart_money_buy_no(self):
        signal = SignalNormalizer().normalize(
            {
                "channel": "smart_money",
                "type": "smart_money_update",
                "data": {
                    "created_at": "2026-06-19T00:39:40.610860+00:00",
                    "event_id": 21397,
                    "group_id": 214840,
                    "market_slug": "fifwc-tur-par-2026-06-19-draw",
                    "platform_buy": "polymarket",
                    "title": "Will Turkiye vs. Paraguay end in a draw?",
                    "data": {
                        "amount": 122.41,
                        "marketSlug": "fifwc-tur-par-2026-06-19-draw",
                        "outcome": "No",
                        "pnl_to_date": 607898.3,
                        "price": 0.71,
                        "side": "BUY",
                    },
                },
            },
            NOW,
        )

        self.assertEqual(signal.channel, "smart_money")
        self.assertEqual(signal.market_id, "fifwc-tur-par-2026-06-19-draw")
        self.assertEqual(signal.platform, Exchange.POLYMARKET)
        self.assertEqual(signal.side, Side.NO)
        self.assertEqual(signal.price_cents, 71)
        self.assertEqual(signal.amount_usd, Decimal("122.41"))
        self.assertEqual(signal.wallet_pnl_usd, Decimal("607898.3"))
        self.assertIsNotNone(signal.resolution_date)

    def test_sell_yes_supports_buy_no(self):
        signal = SignalNormalizer().normalize(
            {
                "channel": "smart_money",
                "data": {
                    "market_slug": "fifwc-sco-mar-2026-06-19-mar",
                    "platform_buy": "polymarket",
                    "data": {"outcome": "Yes", "side": "SELL", "price": 0.56},
                },
            },
            NOW,
        )

        self.assertEqual(signal.side, Side.NO)

    def test_buy_named_outcome_supports_that_polymarket_token(self):
        signal = SignalNormalizer().normalize(
            {
                "channel": "smart_money",
                "data": {
                    "market_slug": "mlb-nym-phi-2026-06-18",
                    "platform_buy": "polymarket",
                    "data": {"outcome": "Philadelphia Phillies", "side": "BUY", "price": 0.62},
                },
            },
            NOW,
        )
        market = FakePolymarket()

        self.assertEqual(signal.side, Side.YES)
        self.assertEqual(signal.outcome, "Philadelphia Phillies")
        self.assertEqual(
            market.resolve_clob_token_id_for_outcome(signal.market_id, signal.outcome, signal.side),
            "mlb-nym-phi-2026-06-18-philadelphia-phillies",
        )


class SignalEngineTests(unittest.TestCase):
    def test_accepts_high_quality_fade_finder_buy_no(self):
        signal = SignalNormalizer().normalize(raw_signal(), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("200")), BookLevel(42, Decimal("200")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertEqual(result.blockers, ())
        self.assertGreaterEqual(result.score, 70)
        self.assertGreaterEqual(result.expected_profit_cents, Decimal("1"))

    def test_fade_finder_scores_above_equivalent_smart_money(self):
        normalizer = SignalNormalizer()
        fade = normalizer.normalize(raw_signal(channel="fade_finder"), NOW)
        smart = normalizer.normalize(raw_signal(channel="smart_money"), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("200")), BookLevel(42, Decimal("200")))
        engine = SignalEngine(settings())

        fade_result = engine.evaluate(fade, market, NOW)
        smart_result = engine.evaluate(smart, market, NOW)

        self.assertGreater(fade_result.score, smart_result.score)

    def test_rejects_core_hard_filter_reasons(self):
        engine = SignalEngine(settings())
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(80, Decimal("1")), BookLevel(50, Decimal("20")))
        signal = SignalNormalizer().normalize(
            raw_signal(side="maybe", price="0.60", resolution_date=(NOW + timedelta(days=4)).isoformat()),
            NOW,
        )

        result = engine.evaluate(signal, market, NOW)

        self.assertIn(SignalRejectReason.SIDE_UNCLEAR, result.blockers)
        self.assertIn(SignalRejectReason.RESOLUTION_TOO_FAR, result.blockers)
        self.assertIn(SignalRejectReason.NO_PRICE_TOO_HIGH, result.blockers)
        self.assertIn(SignalRejectReason.CHASE, result.blockers)
        self.assertIn(SignalRejectReason.LIQUIDITY_LOW, result.blockers)
        self.assertIn(SignalRejectReason.SPREAD_WIDE, result.blockers)

    def test_rejects_missing_market_low_price_score_and_ev(self):
        engine = SignalEngine(settings())
        signal = SignalNormalizer().normalize(
            raw_signal(
                channel="smart_money",
                market_id=None,
                side="no",
                wallet_pnl_usd="0",
                amount_usd="0",
                profitable_wallet_count=0,
                losing_wallet_count=0,
                losing_side=None,
                detected_at=(NOW - timedelta(hours=2)).isoformat(),
            ),
            NOW,
        )

        unmatched = engine.evaluate(signal, None, NOW)
        low_quality = engine.evaluate(
            signal,
            SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(40, Decimal("20")), BookLevel(62, Decimal("20"))),
            NOW,
        )

        self.assertIn(SignalRejectReason.MARKET_UNMATCHED, unmatched.blockers)
        self.assertIn(SignalRejectReason.NO_PRICE_TOO_LOW, low_quality.blockers)
        self.assertIn(SignalRejectReason.SCORE_LOW, low_quality.blockers)
        self.assertIn(SignalRejectReason.EV_LOW, low_quality.blockers)

    def test_accepts_high_quality_buy_yes_signal_too(self):
        signal = SignalNormalizer().normalize(raw_signal(side="yes", price="0.60"), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(42, Decimal("200")), BookLevel(60, Decimal("200")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)
        opportunity = SignalEngine(settings()).opportunity(result)

        self.assertEqual(result.blockers, ())
        self.assertEqual(opportunity.leg.side, Side.YES)
        self.assertEqual(opportunity.leg.price_cents, 60)

    def test_strong_signal_with_huge_depth_creates_paper_trade(self):
        signal = SignalNormalizer().normalize(
            raw_signal(
                channel="smart_money",
                amount_usd="58483.07",
                wallet_pnl_usd="122442.95",
                profitable_wallet_count=3,
                losing_wallet_count=2,
                losing_side="yes",
                price="0.65",
            ),
            NOW,
        )
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(65, Decimal("522698")), BookLevel(36, Decimal("522698")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertTrue(result.paper_allowed)
        self.assertGreaterEqual(result.score, 87)
        self.assertEqual(result.spread_cents, 1)
        self.assertEqual(result.chase_cents, 0)
        self.assertTrue(result.depth_pass)
        self.assertNotIn(SignalRejectReason.INSUFFICIENT_FILLABLE_DEPTH, result.blockers)

    def test_score_70_with_slightly_negative_ev_creates_paper_trade(self):
        signal = SignalNormalizer().normalize(
            raw_signal(
                channel="smart_money",
                amount_usd="8000",
                wallet_pnl_usd="0",
                profitable_wallet_count=0,
                losing_wallet_count=0,
                losing_side=None,
                price="0.60",
            ),
            NOW,
        )
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("200")), BookLevel(42, Decimal("200")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertEqual(result.score, 70)
        self.assertGreaterEqual(result.expected_profit_cents, Decimal("-1.5"))
        self.assertTrue(result.paper_allowed)

    def test_score_62_is_allowed_for_exploratory_paper(self):
        signal = SignalNormalizer().normalize(
            raw_signal(
                channel="smart_money",
                amount_usd="0",
                wallet_pnl_usd="0",
                profitable_wallet_count=0,
                losing_wallet_count=0,
                losing_side=None,
                price="0.60",
            ),
            NOW,
        )
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("200")), BookLevel(42, Decimal("200")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertEqual(result.score, 62)
        self.assertTrue(result.paper_allowed)

    def test_paper_duplicate_market_side_is_blocked_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = SignalBotRunner(
                predictionhunt=None,
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            first = runner.process_raw(raw_signal(), execute=False)
            second = runner.process_raw(raw_signal(), execute=False)

            self.assertTrue(first.accepted)
            self.assertFalse(second.accepted)
            self.assertIn(SignalRejectReason.DUPLICATE, second.evaluation.paper_blockers)

    def test_score_below_60_logs_only(self):
        signal = SignalNormalizer().normalize(
            raw_signal(
                channel="smart_money",
                amount_usd="0",
                wallet_pnl_usd="0",
                profitable_wallet_count=0,
                losing_wallet_count=0,
                losing_side=None,
                price="0.60",
                detected_at=(NOW - timedelta(hours=2)).isoformat(),
                resolution_date=(NOW + timedelta(hours=48)).isoformat(),
            ),
            NOW,
        )
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("200")), BookLevel(42, Decimal("200")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertLess(result.score, 60)
        self.assertFalse(result.paper_allowed)
        self.assertIn(SignalRejectReason.SCORE_LOW, result.blockers)

    def test_zero_stake_config_gets_specific_rejection(self):
        signal = SignalNormalizer().normalize(raw_signal(), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("200")), BookLevel(42, Decimal("200")))
        zero_settings = replace(settings(), signal_paper_trade_usd=Decimal("0"))

        result = SignalEngine(zero_settings).evaluate(signal, market, NOW)

        self.assertFalse(result.paper_allowed)
        self.assertIn(SignalRejectReason.ZERO_STAKE_CONFIG, result.blockers)
        self.assertEqual(result.reject_category, SignalRejectReason.ZERO_STAKE_CONFIG)
        self.assertNotIn(SignalRejectReason.INSUFFICIENT_FILLABLE_DEPTH, result.blockers)

    def test_huge_depth_never_produces_depth_failure(self):
        signal = SignalNormalizer().normalize(raw_signal(price="0.65"), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(65, Decimal("522698")), BookLevel(36, Decimal("522698")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertGreater(result.depth_usd, Decimal("300000"))
        self.assertTrue(result.depth_pass)
        self.assertNotIn(SignalRejectReason.INSUFFICIENT_FILLABLE_DEPTH, result.blockers)

    def test_paper_trade_sizes_down_to_thin_book_above_one_dollar(self):
        signal = SignalNormalizer().normalize(raw_signal(price="0.60"), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("15")), BookLevel(42, Decimal("200")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertTrue(result.paper_allowed)
        self.assertEqual(result.depth_usd, Decimal("9.00"))
        self.assertEqual(result.required_depth_usd, Decimal("9.00"))
        self.assertEqual(result.stake_usd, Decimal("3.00"))
        self.assertTrue(result.depth_pass)

    def test_paper_trade_rejects_when_book_cannot_support_one_dollar(self):
        signal = SignalNormalizer().normalize(raw_signal(price="0.60"), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("2")), BookLevel(42, Decimal("200")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertFalse(result.paper_allowed)
        self.assertEqual(result.required_depth_usd, Decimal("3.00"))
        self.assertIn(SignalRejectReason.INSUFFICIENT_FILLABLE_DEPTH, result.blockers)

    def test_paper_trade_caps_at_one_hundred_dollars(self):
        signal = SignalNormalizer().normalize(raw_signal(price="0.60"), NOW)
        market = SignalMarket(Exchange.KALSHI, "KXTEST", BookLevel(60, Decimal("1000")), BookLevel(42, Decimal("1000")))

        result = SignalEngine(settings()).evaluate(signal, market, NOW)

        self.assertTrue(result.paper_allowed)
        self.assertEqual(result.required_depth_usd, Decimal("300.00"))
        self.assertLessEqual(result.stake_usd, Decimal("100.00"))


class SignalRunnerTests(unittest.TestCase):
    def test_process_raw_logs_candidate_trade_and_profit_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = SignalBotRunner(
                predictionhunt=None,
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            decision = runner.process_raw(raw_signal(), execute=False)

            self.assertTrue(decision.accepted)
            candidate = json.loads((Path(tmp) / "signal_candidates.jsonl").read_text(encoding="utf-8"))
            trade = json.loads((Path(tmp) / "signal_paper_trades.jsonl").read_text(encoding="utf-8"))
            profit = (Path(tmp) / "trade_profit.csv").read_text(encoding="utf-8")
            with (Path(tmp) / "signal_paper_trades.csv").open(encoding="utf-8", newline="") as handle:
                paper_rows = list(csv.DictReader(handle))
            self.assertEqual(candidate["action"], "accepted")
            self.assertEqual(trade["verified"]["side"], "no")
            self.assertIn("signal", profit)
            self.assertEqual(len(paper_rows), 1)
            self.assertEqual(paper_rows[0]["channel"], "fade_finder")
            self.assertEqual(paper_rows[0]["side"], "no")
            self.assertEqual(paper_rows[0]["entry_price_cents"], "60")
            self.assertEqual(paper_rows[0]["result_status"], "pending")
            self.assertEqual(paper_rows[0]["paper_allowed"], "True")
            self.assertEqual(paper_rows[0]["required_depth_usd"], "120.00")
            self.assertEqual(paper_rows[0]["depth_pass"], "True")
            self.assertIn(paper_rows[0]["decision_tier"], {"paper_candidate", "strong_paper"})

    def test_cooldown_blocks_duplicate_recent_trade_when_enabled_for_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = SignalBotRunner(
                predictionhunt=None,
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=replace(settings(), signal_paper_enforce_cooldown=True),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            first = runner.process_raw(raw_signal(), execute=False)
            second = runner.process_raw(raw_signal(), execute=False)

            self.assertTrue(first.accepted)
            self.assertFalse(second.accepted)
            self.assertIn(SignalRejectReason.DUPLICATE, second.evaluation.blockers)

    def test_named_polymarket_sports_outcome_resolves_to_named_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = SignalBotRunner(
                predictionhunt=None,
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            decision = runner.process_raw(
                raw_signal(
                    platform="polymarket",
                    market_id="mlb-nym-phi-2026-06-18",
                    side="buy",
                    outcome="Philadelphia Phillies",
                    price="0.42",
                ),
                execute=False,
            )

            self.assertTrue(decision.accepted)
            self.assertEqual(decision.evaluation.market_id, "mlb-nym-phi-2026-06-18-philadelphia-phillies")

    def test_missing_no_token_is_unsupported_market_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = SignalBotRunner(
                predictionhunt=None,
                kalshi=FakeKalshi(),
                polymarket=UnsupportedNoPolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            decision = runner.process_raw(
                raw_signal(platform="polymarket", market_id="mlb-nym-phi-2026-06-18", side="no"),
                execute=False,
            )

            self.assertFalse(decision.accepted)
            self.assertEqual(decision.evaluation.reject_category, SignalRejectReason.UNSUPPORTED_MARKET_TYPE)
            self.assertIn(SignalRejectReason.UNSUPPORTED_MARKET_TYPE, decision.evaluation.blockers)


if __name__ == "__main__":
    unittest.main()
