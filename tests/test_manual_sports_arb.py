from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from firstbot.config import Settings
from firstbot.manual_sports_arb import (
    ManualArbRejectReason,
    ManualPairInput,
    ManualPairSafetyChecker,
    ManualSportsArbEngine,
    ManualSportsArbResolver,
    ManualSportsArbRunner,
    parse_kalshi_url,
    parse_polymarket_url,
    weighted_fill,
)
from firstbot.models import BookLevel, Exchange, OrderBook, Side


NOW = datetime(2026, 6, 19, 12, 0, tzinfo=timezone.utc)


def settings() -> Settings:
    return Settings(
        live_trading=True,
        min_profit_cents=1,
        max_leg_usd=5,
        slippage_cents=0,
        fee_buffer_cents=0,
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


class FakeKalshi:
    def __init__(self, yes=51, no=49, title="Will Yankees beat Phillies?") -> None:
        self.yes = yes
        self.no = no
        self.title = title

    def get_markets(self, **params):
        return {"markets": [{"ticker": "KXYY", "title": self.title, "status": "open"}]}

    def get_orderbook(self, ticker):
        return OrderBook(
            Exchange.KALSHI,
            ticker,
            yes_asks=[BookLevel(self.yes, Decimal("10"))],
            no_asks=[BookLevel(self.no, Decimal("10"))],
            timestamp=NOW.isoformat(),
        )


class FakePolymarket:
    def __init__(self, yes=48, no=52, title="Will Yankees beat Phillies?", named_outcomes=False) -> None:
        self.yes = yes
        self.no = no
        self.title = title
        self.named_outcomes = named_outcomes

    def _gamma_market(self, market_id):
        outcomes = '["Toronto Blue Jays", "Chicago Cubs"]' if self.named_outcomes else '["Yes","No"]'
        return {
            "slug": market_id,
            "question": self.title,
            "outcomes": outcomes,
            "clobTokenIds": '["PY","PN"]',
            "closed": False,
        }

    def resolve_clob_token_id(self, market_id, side):
        if self.named_outcomes:
            desired = "YES" if side is Side.YES else "NO"
            raise RuntimeError(f"Polymarket market {market_id} has no {desired} CLOB token")
        return "PY" if side is Side.YES else "PN"

    def get_orderbook(self, yes_token_id, no_token_id, market_id=None):
        return OrderBook(
            Exchange.POLYMARKET,
            market_id or "poly-market",
            yes_asks=[BookLevel(self.yes, Decimal("10"))],
            no_asks=[BookLevel(self.no, Decimal("10"))],
            timestamp=NOW.isoformat(),
        )


class ManualSportsArbTests(unittest.TestCase):
    def test_url_parsing_for_kalshi_and_polymarket(self):
        self.assertEqual(parse_kalshi_url("https://kalshi.com/markets/KXYY/yankees"), "KXYY")
        self.assertEqual(parse_kalshi_url("https://kalshi.com/event?market_ticker=KXYY"), "KXYY")
        self.assertEqual(
            parse_kalshi_url("https://kalshi.com/markets/kxmlbgame/game?k=bad&op_market_ticker=KXMLBGAME-26JUN191420TORCHC-CHC"),
            "KXMLBGAME-26JUN191420TORCHC-CHC",
        )
        self.assertEqual(parse_polymarket_url("https://polymarket.com/event/mlb-yankees-phillies"), "mlb-yankees-phillies")
        self.assertEqual(parse_polymarket_url("https://polymarket.com/market?id=123"), "123")

    def test_named_polymarket_outcomes_map_from_kalshi_team_suffix(self):
        pair_input = ManualPairInput(
            "https://polymarket.com/sports/mlb/mlb-tor-chc-2026-06-19",
            "https://kalshi.com/markets/kxmlbgame/game?op_market_ticker=KXMLBGAME-26JUN191420TORCHC-CHC",
            safe_to_trade=True,
        )
        resolver = ManualSportsArbResolver(
            FakeKalshi(title="Will the Chicago Cubs win?"),
            FakePolymarket(title="Toronto Blue Jays vs. Chicago Cubs", named_outcomes=True),
        )

        pair = resolver.resolve(pair_input)

        self.assertEqual(pair.kalshi_ticker, "KXMLBGAME-26JUN191420TORCHC-CHC")
        self.assertEqual(pair.polymarket_yes_token_id, "PN")
        self.assertEqual(pair.polymarket_no_token_id, "PY")

    def test_safety_requires_manual_confirmation_and_allows_confirmed_pair(self):
        pair_input = ManualPairInput(
            "https://polymarket.com/event/mlb-yankees-phillies",
            "https://kalshi.com/markets/KXYY",
            safe_to_trade=False,
        )
        pair = ManualSportsArbResolver(FakeKalshi(), FakePolymarket()).resolve(pair_input)
        blocked = ManualPairSafetyChecker().review(pair, pair_input)
        allowed = ManualPairSafetyChecker().review(
            pair,
            ManualPairInput(pair_input.polymarket_url, pair_input.kalshi_url, safe_to_trade=True),
        )

        self.assertFalse(blocked.safe)
        self.assertEqual(blocked.reason, ManualArbRejectReason.MANUAL_REVIEW_REQUIRED)
        self.assertTrue(allowed.safe)

    def test_soccer_draw_market_requires_mapping_review(self):
        pair_input = ManualPairInput(
            "https://polymarket.com/event/soccer-draw",
            "https://kalshi.com/markets/KDRAW",
            sport="soccer",
            safe_to_trade=True,
        )
        pair = ManualSportsArbResolver(
            FakeKalshi(title="Will Team A beat Team B?"),
            FakePolymarket(title="Will Team A vs Team B end in a draw?"),
        ).resolve(pair_input)

        review = ManualPairSafetyChecker().review(pair, pair_input)

        self.assertFalse(review.safe)
        self.assertEqual(review.reason, ManualArbRejectReason.MANUAL_REVIEW_REQUIRED)

    def test_mismatched_event_rejects_as_unsafe_pair(self):
        pair_input = ManualPairInput(
            "https://polymarket.com/event/lakers-celtics",
            "https://kalshi.com/markets/KXYY",
            safe_to_trade=True,
        )
        pair = ManualSportsArbResolver(
            FakeKalshi(title="Will Yankees beat Phillies?"),
            FakePolymarket(title="Will Lakers beat Celtics?"),
        ).resolve(pair_input)

        review = ManualPairSafetyChecker().review(pair, pair_input)

        self.assertFalse(review.safe)
        self.assertEqual(review.reason, ManualArbRejectReason.UNSAFE_MARKET_PAIR)

    def test_weighted_fill_uses_multiple_levels(self):
        fill = weighted_fill(
            [BookLevel(48, Decimal("2")), BookLevel(49, Decimal("3"))],
            Side.YES,
            Decimal("5"),
        )

        self.assertTrue(fill.filled)
        self.assertEqual(fill.avg_price_cents, Decimal("48.6000"))
        self.assertEqual(fill.max_price_cents, 49)

    def test_direction_a_and_b_math_choose_best_valid_arb(self):
        pair_input = ManualPairInput(
            "https://polymarket.com/event/mlb-yankees-phillies",
            "https://kalshi.com/markets/KXYY",
            safe_to_trade=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            runner = ManualSportsArbRunner(
                FakeKalshi(yes=45, no=51),
                FakePolymarket(yes=48, no=40),
                settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )
            pair, safety = runner.resolve_pair(pair_input)

            decision = runner.tick(pair, safety, "paper")

        self.assertEqual(decision.direction_a.gross_edge_cents, Decimal("1.0000"))
        self.assertEqual(decision.direction_b.gross_edge_cents, Decimal("15.0000"))
        self.assertEqual(decision.best_direction.name, "polymarket_no_kalshi_yes")
        self.assertEqual(decision.decision, "paper_trade")

    def test_fees_can_kill_edge(self):
        cfg = settings()
        cfg = cfg.__class__(**{**cfg.__dict__, "kalshi_fee_rate": Decimal("0.50"), "polymarket_fee_rate": Decimal("0.50")})
        pair_input = ManualPairInput(
            "https://polymarket.com/event/mlb-yankees-phillies",
            "https://kalshi.com/markets/KXYY",
            safe_to_trade=True,
        )
        runner = ManualSportsArbRunner(FakeKalshi(yes=55, no=49), FakePolymarket(yes=50, no=45), cfg, clock=lambda: NOW)
        pair, safety = runner.resolve_pair(pair_input)

        decision = runner.tick(pair, safety, "paper")

        self.assertEqual(decision.rejection_reason, ManualArbRejectReason.FEES_KILL_EDGE)

    def test_stale_books_reject(self):
        cfg = settings()
        engine = ManualSportsArbEngine(cfg)
        pair_input = ManualPairInput(
            "https://polymarket.com/event/mlb-yankees-phillies",
            "https://kalshi.com/markets/KXYY",
            safe_to_trade=True,
        )
        pair = ManualSportsArbResolver(FakeKalshi(), FakePolymarket()).resolve(pair_input)
        safety = ManualPairSafetyChecker().review(pair, pair_input)
        stale = (NOW - timedelta(seconds=10)).isoformat()
        poly_book = OrderBook(Exchange.POLYMARKET, "poly", [BookLevel(48, Decimal("10"))], [BookLevel(52, Decimal("10"))], stale)
        kalshi_book = OrderBook(Exchange.KALSHI, "KXYY", [BookLevel(51, Decimal("10"))], [BookLevel(49, Decimal("10"))], stale)

        decision = engine.evaluate(pair, safety, poly_book, kalshi_book, "paper", NOW)

        self.assertIn(ManualArbRejectReason.ORDERBOOK_STALE, decision.best_direction.blockers)

    def test_epoch_millisecond_timestamp_is_fresh(self):
        cfg = settings()
        engine = ManualSportsArbEngine(cfg)
        pair_input = ManualPairInput(
            "https://polymarket.com/event/mlb-yankees-phillies",
            "https://kalshi.com/markets/KXYY",
            safe_to_trade=True,
        )
        pair = ManualSportsArbResolver(FakeKalshi(), FakePolymarket()).resolve(pair_input)
        safety = ManualPairSafetyChecker().review(pair, pair_input)
        timestamp_ms = str(int(NOW.timestamp() * 1000))
        poly_book = OrderBook(Exchange.POLYMARKET, "poly", [BookLevel(48, Decimal("10"))], [BookLevel(52, Decimal("10"))], timestamp_ms)
        kalshi_book = OrderBook(Exchange.KALSHI, "KXYY", [BookLevel(51, Decimal("10"))], [BookLevel(49, Decimal("10"))], NOW.isoformat())

        decision = engine.evaluate(pair, safety, poly_book, kalshi_book, "paper", NOW)

        self.assertNotIn(ManualArbRejectReason.ORDERBOOK_STALE, decision.best_direction.blockers)

    def test_scan_logs_without_trade_and_paper_writes_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = ManualSportsArbRunner(
                FakeKalshi(yes=45, no=51),
                FakePolymarket(yes=48, no=40),
                settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )
            pair_input = ManualPairInput(
                "https://polymarket.com/event/mlb-yankees-phillies",
                "https://kalshi.com/markets/KXYY",
                safe_to_trade=True,
            )
            pair, safety = runner.resolve_pair(pair_input)

            scan = runner.tick(pair, safety, "scan")
            paper = runner.tick(pair, safety, "paper")

            self.assertEqual(scan.decision, "scan_opportunity")
            self.assertEqual(paper.decision, "paper_trade")
            self.assertTrue((Path(tmp) / "manual_arb_calculations.jsonl").exists())
            with (Path(tmp) / "manual_arb_trades.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["decision"], "paper_trade")

    def test_live_mode_is_stub_blocked(self):
        runner = ManualSportsArbRunner(FakeKalshi(yes=45, no=51), FakePolymarket(yes=48, no=40), settings(), clock=lambda: NOW)
        pair_input = ManualPairInput(
            "https://polymarket.com/event/mlb-yankees-phillies",
            "https://kalshi.com/markets/KXYY",
            safe_to_trade=True,
        )
        pair, safety = runner.resolve_pair(pair_input)
        decision = runner.tick(pair, safety, "live")

        submitted, message = runner.live_attempt(decision)

        self.assertEqual(decision.decision, "blocked")
        self.assertFalse(submitted)
        self.assertEqual(message, ManualArbRejectReason.LIVE_MODE_DISABLED)


if __name__ == "__main__":
    unittest.main()
