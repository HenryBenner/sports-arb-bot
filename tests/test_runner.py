from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from io import StringIO

from firstbot.config import Settings
from firstbot.models import BookLevel, Exchange, Side
from firstbot.predictionhunt import PredictionHuntLeg, PredictionHuntOpportunity
from firstbot.runner import PredictionHuntRunner, _parse_datetime


NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


class FakePredictionHunt:
    def __init__(self, opportunities):
        self.opportunities = opportunities
        self.calls = 0

    def get_arbitrage_opportunities(self, **kwargs):
        self.calls += 1
        return self.opportunities


class FakeKalshi:
    def get_best_ask(self, ticker, side):
        return BookLevel(price_cents=45, size=Decimal("10"))


class FakePolymarket:
    def get_token_best_ask(self, token_id):
        return BookLevel(price_cents=45, size=Decimal("10"))


def settings(live: bool = False) -> Settings:
    return Settings(
        live_trading=live,
        min_profit_cents=5,
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
        trigger_cost_cents=96,
        near_miss_cost_cents=100,
        hot_window_seconds=600,
        predictionhunt_poll_seconds=30,
        max_days_to_resolution=3,
        prefer_same_day=True,
        book_stale_ms=1000,
        max_active_watches=20,
    )


def opportunity(event_date: datetime | None = None, group_id: int = 1) -> PredictionHuntOpportunity:
    event_date = event_date or (NOW + timedelta(days=2))
    return PredictionHuntOpportunity(
        group_id=group_id,
        group_title="Team A vs Team B",
        event_date=event_date.isoformat(),
        event_type="sports",
        roi_pct=Decimal("6.0"),
        total_cost=Decimal("0.90"),
        max_wager_usd=Decimal("5"),
        detected_at=NOW.isoformat(),
        legs=(
            PredictionHuntLeg(
                side=Side.YES,
                platform=Exchange.POLYMARKET,
                market_id="poly-token",
                source_url=None,
                price=Decimal("0.45"),
                liquidity_usd=Decimal("10"),
                fee_usd=Decimal("0"),
            ),
            PredictionHuntLeg(
                side=Side.NO,
                platform=Exchange.KALSHI,
                market_id="KALSHI-TICKER",
                source_url=None,
                price=Decimal("0.45"),
                liquidity_usd=Decimal("10"),
                fee_usd=Decimal("0"),
            ),
        ),
        raw={},
    )


def opportunity_with_date_string(event_date: str, group_id: int = 1) -> PredictionHuntOpportunity:
    item = opportunity(group_id=group_id)
    return PredictionHuntOpportunity(
        group_id=item.group_id,
        group_title=item.group_title,
        event_date=event_date,
        event_type=item.event_type,
        roi_pct=item.roi_pct,
        total_cost=item.total_cost,
        max_wager_usd=item.max_wager_usd,
        detected_at=item.detected_at,
        legs=item.legs,
        raw=item.raw,
    )


class PredictionHuntRunnerTests(unittest.TestCase):
    def test_poll_once_paper_logs_verified_opportunity(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PredictionHuntRunner(
                predictionhunt=FakePredictionHunt([opportunity()]),
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            with redirect_stdout(StringIO()):
                summary = runner.poll_once(category="sports", limit=10, execute=False)

            self.assertEqual(summary["fetched"], 1)
            self.assertEqual(summary["eligible"], 1)
            self.assertEqual(summary["verified"], 1)
            path = Path(tmp) / "paper_trades.jsonl"
            record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["verified"]["net_profit_cents"], "8.20")
            self.assertIn("live trading disabled", record["verified"]["blockers"])

    def test_dedupe_skips_repeated_unchanged_opportunity(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PredictionHuntRunner(
                predictionhunt=FakePredictionHunt([opportunity()]),
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            with redirect_stdout(StringIO()):
                first = runner.poll_once(category="sports", limit=10, execute=False)
                second = runner.poll_once(category="sports", limit=10, execute=False)

            self.assertEqual(first["eligible"], 1)
            self.assertEqual(second["eligible"], 0)
            lines = (Path(tmp) / "paper_trades.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)

    def test_resolution_window_blocks_far_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = PredictionHuntRunner(
                predictionhunt=FakePredictionHunt([opportunity(NOW + timedelta(days=5))]),
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            with redirect_stdout(StringIO()):
                summary = runner.poll_once(category="sports", limit=10, execute=False)

            self.assertEqual(summary["eligible"], 0)
            record = json.loads((Path(tmp) / "paper_trades.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["action"], "skipped")
            self.assertIn("more than 3 days away", record["message"])

    def test_date_only_same_day_stays_eligible_until_eastern_day_ends(self):
        evening_utc = datetime(2026, 6, 17, 0, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            runner = PredictionHuntRunner(
                predictionhunt=FakePredictionHunt([opportunity_with_date_string("2026-06-16")]),
                kalshi=FakeKalshi(),
                polymarket=FakePolymarket(),
                settings=settings(),
                log_dir=tmp,
                clock=lambda: evening_utc,
            )

            with redirect_stdout(StringIO()):
                summary = runner.poll_once(category="sports", limit=10, execute=False)

            self.assertEqual(summary["eligible"], 1)
            self.assertGreater(_parse_datetime("2026-06-16"), evening_utc)


if __name__ == "__main__":
    unittest.main()
