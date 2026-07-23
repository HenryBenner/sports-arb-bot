from __future__ import annotations

import json
import asyncio
import csv
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from firstbot.config import Settings
from firstbot.hot import (
    HotArbRunner,
    HotTriggerEngine,
    HotWatchManager,
    LiveLegBook,
    _arb_record,
    _log_dir_path,
    _refresh_live_book_timestamps,
    _market_resolution_safety_reason,
    _poll_error_summary,
    _poll_retry_seconds,
    _predictionhunt_trusted_outcome_keys,
    _requires_live_halt,
    parse_datetime,
)
from firstbot.models import BookLevel, Exchange, Side
from firstbot.predictionhunt import PredictionHuntLeg, PredictionHuntOpportunity
from firstbot.websockets import _remap_polymarket_token_side, parse_kalshi_message, parse_polymarket_message


NOW = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


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


def ph_opportunity(
    event_date: datetime | None = None,
    event_type: str = "sports",
    roi: str = "4.0",
    group_id: int = 1,
    yes_price: str = "0.48",
    no_price: str = "0.48",
) -> PredictionHuntOpportunity:
    event_date = event_date or NOW
    return PredictionHuntOpportunity(
        group_id=group_id,
        group_title=f"Market {group_id}",
        event_date=event_date.isoformat(),
        event_type=event_type,
        roi_pct=Decimal(roi),
        total_cost=Decimal("0.96"),
        max_wager_usd=Decimal("5"),
        detected_at=NOW.isoformat(),
        legs=(
            PredictionHuntLeg(
                side=Side.YES,
                platform=Exchange.POLYMARKET,
                market_id=f"poly-{group_id}",
                source_url=None,
                price=Decimal(yes_price),
                liquidity_usd=Decimal("10"),
                fee_usd=Decimal("0"),
            ),
            PredictionHuntLeg(
                side=Side.NO,
                platform=Exchange.KALSHI,
                market_id=f"KALSHI-{group_id}",
                source_url=None,
                price=Decimal(no_price),
                liquidity_usd=Decimal("10"),
                fee_usd=Decimal("0"),
            ),
        ),
        raw={},
    )


def ph_outcome_keys(group_id: int = 1) -> dict[tuple[Exchange, str, Side], str]:
    return {
        (Exchange.POLYMARKET, f"poly-{group_id}", Side.YES): "a",
        (Exchange.KALSHI, f"KALSHI-{group_id}", Side.NO): "b",
    }


class HotWatchManagerTests(unittest.TestCase):
    def test_categoryless_watch_accepts_non_sports_and_refreshes_duplicate(self):
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: NOW)
        first, first_status = manager.add_or_refresh(ph_opportunity(event_type="politics"))
        second, second_status = manager.add_or_refresh(ph_opportunity(event_type="politics"))

        self.assertIsNotNone(first)
        self.assertIs(first, second)
        self.assertEqual(first_status, "added")
        self.assertEqual(second_status, "refreshed")
        self.assertEqual(len(manager.active()), 1)

    def test_far_resolution_is_skipped(self):
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: NOW)
        watch, status = manager.add_or_refresh(ph_opportunity(event_date=NOW + timedelta(days=4)))

        self.assertIsNone(watch)
        self.assertIn("more than 3 days away", status)

    def test_date_only_same_day_stays_eligible_until_eastern_day_ends(self):
        evening_utc = datetime(2026, 6, 17, 0, 5, tzinfo=timezone.utc)
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: evening_utc)
        opportunity = ph_opportunity()
        opportunity = PredictionHuntOpportunity(
            group_id=opportunity.group_id,
            group_title=opportunity.group_title,
            event_date="2026-06-16",
            event_type=opportunity.event_type,
            roi_pct=opportunity.roi_pct,
            total_cost=opportunity.total_cost,
            max_wager_usd=opportunity.max_wager_usd,
            detected_at=opportunity.detected_at,
            legs=opportunity.legs,
            raw=opportunity.raw,
        )

        watch, status = manager.add_or_refresh(opportunity)

        self.assertIsNotNone(watch)
        self.assertEqual(status, "added")
        self.assertGreater(parse_datetime("2026-06-16"), evening_utc)

    def test_date_only_previous_day_is_skipped_after_eastern_day_ends(self):
        evening_utc = datetime(2026, 6, 17, 0, 5, tzinfo=timezone.utc)
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: evening_utc)
        opportunity = ph_opportunity()
        opportunity = PredictionHuntOpportunity(
            group_id=opportunity.group_id,
            group_title=opportunity.group_title,
            event_date="2026-06-15",
            event_type=opportunity.event_type,
            roi_pct=opportunity.roi_pct,
            total_cost=opportunity.total_cost,
            max_wager_usd=opportunity.max_wager_usd,
            detected_at=opportunity.detected_at,
            legs=opportunity.legs,
            raw=opportunity.raw,
        )

        watch, status = manager.add_or_refresh(opportunity)

        self.assertIsNone(watch)
        self.assertEqual(status, "event_date is in the past")

    def test_same_day_priority_evicts_farther_lower_priority_watch(self):
        manager = HotWatchManager(600, 3, 1, True, clock=lambda: NOW)
        far, _ = manager.add_or_refresh(
            ph_opportunity(event_date=NOW + timedelta(days=2), roi="10", group_id=1)
        )
        same_day, _ = manager.add_or_refresh(ph_opportunity(event_date=NOW, roi="1", group_id=2))

        active = manager.active()
        self.assertEqual(len(active), 1)
        self.assertIsNotNone(far)
        self.assertIsNotNone(same_day)
        self.assertEqual(active[0].opportunity.group_id, 2)


class WebSocketParserTests(unittest.TestCase):
    def test_kalshi_snapshot_and_delta_update_best_ask(self):
        books = {}
        snapshot = parse_kalshi_message(
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "K",
                    "yes_dollars_fp": [["0.4800", "2"], ["0.5200", "5"]],
                    "no_dollars_fp": [["0.4900", "3"]],
                },
            },
            books,
            now=NOW,
        )
        delta = parse_kalshi_message(
            {
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": "K",
                    "side": "yes",
                    "price_dollars": "0.5200",
                    "delta_fp": "-5",
                },
            },
            books,
            now=NOW,
        )

        self.assertEqual(snapshot[0].side, Side.YES)
        self.assertEqual(snapshot[0].best_ask.price_cents, 51)
        self.assertEqual(snapshot[1].side, Side.NO)
        self.assertEqual(snapshot[1].best_ask.price_cents, 48)
        self.assertEqual(delta[0].side, Side.NO)
        self.assertEqual(delta[0].best_ask.price_cents, 52)

    def test_kalshi_real_snapshot_converts_bid_levels_to_buy_asks(self):
        updates = parse_kalshi_message(
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "KXHOUSERACE-CA34-26-D",
                    "no_dollars_fp": [["0.0010", "1000.00"], ["0.0020", "340.00"], ["0.0030", "1000.00"]],
                    "yes_dollars_fp": [
                        ["0.0010", "201000.00"],
                        ["0.0020", "1010101.00"],
                        ["0.1500", "15151.00"],
                        ["0.1600", "51.00"],
                        ["0.1800", "33.00"],
                        ["0.6000", "50.00"],
                        ["0.6200", "1033.00"],
                        ["0.9600", "500.00"],
                        ["0.9750", "250.00"],
                        ["0.9760", "74.00"],
                    ],
                },
            },
            {},
            now=NOW,
        )

        yes_update = next(update for update in updates if update.side is Side.YES)
        no_update = next(update for update in updates if update.side is Side.NO)
        self.assertEqual(yes_update.best_ask.price_cents, 100)
        self.assertEqual(no_update.best_ask.price_cents, 3)

    def test_polymarket_snapshot_and_price_change_update_best_ask(self):
        books = {}
        snapshot = parse_polymarket_message(
            {"event_type": "book", "asset_id": "poly", "asks": [{"price": "0.48", "size": "2"}]},
            books,
            now=NOW,
        )
        change = parse_polymarket_message(
            {
                "event_type": "price_change",
                "asset_id": "poly",
                "changes": [{"asset_id": "poly", "side": "sell", "price": "0.47", "size": "3"}],
            },
            books,
            now=NOW,
        )

        self.assertEqual(snapshot[0].best_ask.price_cents, 48)
        self.assertEqual(change[0].best_ask.price_cents, 47)

    def test_polymarket_token_update_is_remapped_to_predictionhunt_side(self):
        update = parse_polymarket_message(
            {"event_type": "book", "asset_id": "no-token", "asks": [{"price": "0.92", "size": "2"}]},
            {},
            now=NOW,
        )[0]

        remapped = _remap_polymarket_token_side(update, {"no-token": Side.NO})

        self.assertEqual(update.side, Side.YES)
        self.assertEqual(remapped.side, Side.NO)
        self.assertEqual(remapped.best_ask.price_cents, 92)

    def test_polymarket_malformed_price_change_is_ignored(self):
        books = {}
        snapshot = parse_polymarket_message(
            {"event_type": "book", "asset_id": "poly", "asks": [{"price": "0.96", "size": "36.2"}]},
            books,
            now=NOW,
        )
        malformed = parse_polymarket_message(
            {
                "event_type": "price_change",
                "asset_id": "poly",
                "changes": [{"asset_id": "poly", "side": "sell", "price": None, "size": None}],
            },
            books,
            now=NOW,
        )

        self.assertEqual(snapshot[0].best_ask.price_cents, 96)
        self.assertEqual(malformed, [])


class HotTriggerTests(unittest.TestCase):
    def test_live_halt_message_detection(self):
        self.assertTrue(
            _requires_live_halt(
                "first leg failed before paired order submission: "
                "polymarket_order_state_uncertain: delayed Polymarket FOK order "
                "0xpending was not confirmed filled within 3.5s"
            )
        )
        self.assertTrue(
            _requires_live_halt(
                "second leg failed after first leg polymarket response=...; "
                "manual_review_required: kalshi rejected FOK"
            )
        )
        self.assertFalse(_requires_live_halt("order could not be fully filled; FOK killed"))
        self.assertTrue(
            _requires_live_halt(
                "PolyApiException status_code=403: Trading restricted in your region"
            )
        )

    def test_live_safety_allows_named_market_token_without_name_mapping(self):
        opportunity = PredictionHuntOpportunity(
            group_id=1,
            group_title="2026-06-21 Hamish Stewart",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("10"),
            total_cost=Decimal("0.90"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXATPMATCH-26JUN21STEVUK-STE",
                    source_url="https://kalshi.com/markets/KXATPMATCH/KXATPMATCH-26JUN21STEVUK-STE",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.POLYMARKET,
                    market_id="11777111362990187134696021544338578790065523690734648614240087196046475903706",
                    source_url="https://polymarket.com/sports/tennis/atp-eastbourne/stewart-vs-vukic",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )
        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Hamish Stewart", "Aleksandar Vukic"]',
                    "clobTokenIds": '["11777111362990187134696021544338578790065523690734648614240087196046475903706", "2"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        reason = runner._unsafe_hot_arb_reason(opportunity)

        self.assertIsNone(reason)

    def test_live_safety_trusts_exact_predictionhunt_token_without_name_remapping(self):
        opportunity = PredictionHuntOpportunity(
            group_id=1,
            group_title="2026-06-21 Hamish Stewart",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("10"),
            total_cost=Decimal("0.90"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXATPMATCH-26JUN21STEVUK-STE",
                    source_url="https://kalshi.com/markets/KXATPMATCH/KXATPMATCH-26JUN21STEVUK-STE",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.POLYMARKET,
                    market_id="9999999999999999999999999999999999999999999999999999999999999999",
                    source_url="https://polymarket.com/market/atp-stewart-vukic-2026-06-21",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Hamish Stewart", "Aleksandar Vukic"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        reason = runner._unsafe_hot_arb_reason(opportunity)

        self.assertIsNone(reason)

    def test_live_safety_allows_verified_named_outcome_opposite_token(self):
        opportunity = PredictionHuntOpportunity(
            group_id=1,
            group_title="2026-06-21 Hamish Stewart",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("10"),
            total_cost=Decimal("0.90"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXATPMATCH-26JUN21STEVUK-STE",
                    source_url="https://kalshi.com/markets/KXATPMATCH/KXATPMATCH-26JUN21STEVUK-STE",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.POLYMARKET,
                    market_id="2222222222222222222222222222222222222222222222222222222222222222",
                    source_url="https://polymarket.com/market/atp-stewart-vukic-2026-06-21",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Hamish Stewart", "Aleksandar Vukic"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_live_safety_allows_named_token_without_last_name_mapping(self):
        opportunity = PredictionHuntOpportunity(
            group_id=1,
            group_title="2026-06-21 Aleksandar Vukic",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("10"),
            total_cost=Decimal("0.90"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXATPMATCH-26JUN21STEVUK-VUKIC",
                    source_url="https://kalshi.com/markets/KXATPMATCH/KXATPMATCH-26JUN21STEVUK-VUKIC",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.POLYMARKET,
                    market_id="1111111111111111111111111111111111111111111111111111111111111111",
                    source_url="https://polymarket.com/market/atp-stewart-vukic-2026-06-21",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Hamish Stewart", "Aleksandar Vukic"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_live_safety_allows_named_tennis_market_without_suffix_mapping(self):
        opportunity = PredictionHuntOpportunity(
            group_id=1,
            group_title="2026-07-01 Fabian Marozsan",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("10"),
            total_cost=Decimal("0.90"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXATPMATCH-26JUL01DAVMAR-MAR",
                    source_url="https://kalshi.com/markets/KXATPMATCH/KXATPMATCH-26JUL01DAVMAR",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.POLYMARKET,
                    market_id="1111111111111111111111111111111111111111111111111111111111111111",
                    source_url="https://polymarket.com/event/atp-fokina-marozsa-2026-07-01/atp-fokina-marozsa-2026-07-01",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Alejandro Davidovich Fokina", "Fabian Marozsan"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_live_safety_allows_named_team_market_without_suffix_mapping(self):
        opportunity = PredictionHuntOpportunity(
            group_id=1,
            group_title="2026-07-03 San Francisco Giants",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("10"),
            total_cost=Decimal("0.90"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KXMLBGAME-26JUL032010SFCOL-SF",
                    source_url="https://kalshi.com/markets/KXMLBGAME/KXMLBGAME-26JUL032010SFCOL",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="1111111111111111111111111111111111111111111111111111111111111111",
                    source_url="https://polymarket.com/event/mlb-sf-col-2026-07-03/mlb-sf-col-2026-07-03",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["San Francisco Giants", "Colorado Rockies"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_live_safety_allows_named_market_when_kalshi_suffix_is_ambiguous(self):
        opportunity = PredictionHuntOpportunity(
            group_id=1,
            group_title="2026-06-21 Smith match",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("10"),
            total_cost=Decimal("0.90"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXTEST-26JUN21-SMITH",
                    source_url="https://kalshi.com/markets/KXTEST/KXTEST-26JUN21-SMITH",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.POLYMARKET,
                    market_id="1111111111111111111111111111111111111111111111111111111111111111",
                    source_url="https://polymarket.com/market/smith-vs-smith-2026-06-21",
                    price=Decimal("0.40"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["John Smith", "Adam Smith"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        reason = runner._unsafe_hot_arb_reason(opportunity)

        self.assertIsNone(reason)

    def test_live_safety_allows_kansas_city_without_acronym_alias(self):
        opportunity = PredictionHuntOpportunity(
            group_id=242178,
            group_title="2026-07-08 Kansas City Royals",
            event_date="2026-07-08",
            event_type="sports",
            roi_pct=Decimal("7.39"),
            total_cost=Decimal("0.92"),
            max_wager_usd=Decimal("0"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="46789336376906124205914468821518644525738614318340541934888892430507937133940",
                    source_url="https://polymarket.com/event/mlb-kc-nym-2026-07-08/mlb-kc-nym-2026-07-08?r=predictionhunt",
                    price=Decimal("0.03"),
                    liquidity_usd=Decimal("966.99"),
                    fee_usd=Decimal("0.002"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KXMLBGAME-26JUL081910KCNYM-KC",
                    source_url="https://kalshi.com/markets/KXMLBGAME/KXMLBGAME-26JUL081910KCNYM",
                    price=Decimal("0.89"),
                    liquidity_usd=Decimal("0"),
                    fee_usd=Decimal("0.01"),
                ),
            ),
            raw={},
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Kansas City Royals", "New York Mets"]',
                    "clobTokenIds": '["46789336376906124205914468821518644525738614318340541934888892430507937133940", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_hot_runner_preserves_only_exact_predictionhunt_basket_direction(self):
        opportunity = ph_opportunity()
        opportunity = opportunity.__class__(
            **{
                **opportunity.__dict__,
                "legs": (
                    PredictionHuntLeg(
                        side=Side.YES,
                        platform=Exchange.POLYMARKET,
                        market_id="1111111111111111111111111111111111111111111111111111111111111111",
                        source_url="https://polymarket.com/event/mlb-kc-nym-2026-07-08/mlb-kc-nym-2026-07-08",
                        price=Decimal("0.03"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                    PredictionHuntLeg(
                        side=Side.NO,
                        platform=Exchange.KALSHI,
                        market_id="KXMLBGAME-26JUL081910KCNYM-KC",
                        source_url="https://kalshi.com/markets/KXMLBGAME/KXMLBGAME-26JUL081910KCNYM",
                        price=Decimal("0.89"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                ),
            }
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Kansas City Royals", "New York Mets"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        prepared = runner._resolve_hot_arb_legs(opportunity)

        leg_keys = {(leg.platform, leg.market_id, leg.side) for leg in prepared.legs}
        self.assertEqual(len(leg_keys), 2)
        self.assertIn((Exchange.POLYMARKET, "1111111111111111111111111111111111111111111111111111111111111111", Side.YES), leg_keys)
        self.assertIn((Exchange.KALSHI, "KXMLBGAME-26JUL081910KCNYM-KC", Side.NO), leg_keys)

    def test_polymarket_event_verification_selects_market_containing_token(self):
        token = "1111111111111111111111111111111111111111111111111111111111111111"
        opportunity = PredictionHuntOpportunity(
            group_id=77,
            group_title="Multi market event",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("4"),
            total_cost=Decimal("0.96"),
            max_wager_usd=Decimal("5"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id=token,
                    source_url="https://polymarket.com/event/multi-market-event",
                    price=Decimal("0.45"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KALSHI-1",
                    source_url=None,
                    price=Decimal("0.45"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakeHttp:
            def get_json(self, url, params=None):
                return {
                    "markets": [
                        {
                            "slug": "wrong-market",
                            "outcomes": '["Wrong A", "Wrong B"]',
                            "clobTokenIds": '["2222222222222222222222222222222222222222222222222222222222222222", "3333333333333333333333333333333333333333333333333333333333333333"]',
                        },
                        {
                            "slug": "right-market",
                            "outcomes": '["Right A", "Right B"]',
                            "clobTokenIds": f'["{token}", "4444444444444444444444444444444444444444444444444444444444444444"]',
                        },
                    ]
                }

        class FakePolymarket:
            gamma_url = "https://gamma.example"
            http = FakeHttp()

            def _gamma_market(self, slug):
                return {
                    "slug": "wrong-market",
                    "outcomes": '["Wrong A", "Wrong B"]',
                    "clobTokenIds": '["2222222222222222222222222222222222222222222222222222222222222222", "3333333333333333333333333333333333333333333333333333333333333333"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_polymarket_event_verification_is_diagnostic_only_for_exact_token(self):
        token = "1111111111111111111111111111111111111111111111111111111111111111"
        opportunity = PredictionHuntOpportunity(
            group_id=78,
            group_title="2026-07-11 Sharks",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("4"),
            total_cost=Decimal("0.96"),
            max_wager_usd=Decimal("5"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.POLYMARKET,
                    market_id=token,
                    source_url="https://polymarket.com/event/cs2-pain-shk-2026-07-11",
                    price=Decimal("0.45"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXCS2GAME-26JUL111900SHKPAIN-SHK",
                    source_url=None,
                    price=Decimal("0.45"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        class FakeHttp:
            def get_json(self, url, params=None):
                return {
                    "markets": [
                        {
                            "slug": "cs2-pain-shk-2026-07-11",
                            "outcomes": '["paiN", "Sharks"]',
                            "clobTokenIds": '["2222222222222222222222222222222222222222222222222222222222222222", "3333333333333333333333333333333333333333333333333333333333333333"]',
                        }
                    ]
                }

        class FakePolymarket:
            gamma_url = "https://gamma.example"
            http = FakeHttp()

            def _gamma_market(self, slug):
                return {
                    "slug": "venezuela-leader-end-of-2026",
                    "outcomes": '["Delcy Rodriguez", "Other"]',
                    "clobTokenIds": f'["{token}", "4444444444444444444444444444444444444444444444444444444444444444"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        reason = runner._unsafe_hot_arb_reason(opportunity)

        self.assertIsNone(reason)

    def test_hot_trigger_evaluates_all_expanded_live_baskets(self):
        opportunity = ph_opportunity()
        opportunity = opportunity.__class__(
            **{
                **opportunity.__dict__,
                "legs": (
                    PredictionHuntLeg(
                        side=Side.YES,
                        platform=Exchange.POLYMARKET,
                        market_id="poly-yes",
                        source_url=None,
                        price=Decimal("0.60"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                    PredictionHuntLeg(
                        side=Side.NO,
                        platform=Exchange.POLYMARKET,
                        market_id="poly-no",
                        source_url=None,
                        price=Decimal("0.40"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                    PredictionHuntLeg(
                        side=Side.NO,
                        platform=Exchange.KALSHI,
                        market_id="K",
                        source_url=None,
                        price=Decimal("0.60"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                    PredictionHuntLeg(
                        side=Side.YES,
                        platform=Exchange.KALSHI,
                        market_id="K",
                        source_url=None,
                        price=Decimal("0.45"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                ),
            }
        )
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            opportunity,
            outcome_keys={
                (Exchange.POLYMARKET, "poly-yes", Side.YES): "a",
                (Exchange.POLYMARKET, "poly-no", Side.NO): "b",
                (Exchange.KALSHI, "K", Side.NO): "b",
                (Exchange.KALSHI, "K", Side.YES): "a",
            },
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-yes", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-yes", Side.YES, BookLevel(60, Decimal("5")), NOW, True, True
            ),
            (Exchange.POLYMARKET, "poly-no", Side.NO): LiveLegBook(
                Exchange.POLYMARKET, "poly-no", Side.NO, BookLevel(40, Decimal("5")), NOW, True, True
            ),
            (Exchange.KALSHI, "K", Side.NO): LiveLegBook(
                Exchange.KALSHI, "K", Side.NO, BookLevel(60, Decimal("5")), NOW, True, True
            ),
            (Exchange.KALSHI, "K", Side.YES): LiveLegBook(
                Exchange.KALSHI, "K", Side.YES, BookLevel(45, Decimal("5")), NOW, True, True
            ),
        }

        result = HotTriggerEngine(settings(live=True), 96, 100, 1000).evaluate(watch, NOW)

        self.assertEqual(result.gross_cost_cents, 85)
        self.assertEqual(result.buy_yes.exchange, Exchange.KALSHI)
        self.assertEqual(result.buy_yes.price_cents, 45)
        self.assertEqual(result.buy_no.exchange, Exchange.POLYMARKET)
        self.assertEqual(result.buy_no.market_id, "poly-no")

    def test_hot_trigger_blocks_named_same_outcome_even_when_sides_are_opposite(self):
        opportunity = PredictionHuntOpportunity(
            group_id=248305,
            group_title="2026-07-11 San Diego Padres",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("5.87"),
            total_cost=Decimal("0.26"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KXMLBGAME-26JUL112040TORSD-SD",
                    source_url="https://kalshi.com/markets/KXMLBGAME/KXMLBGAME-26JUL112040TORSD",
                    price=Decimal("0.13"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="poly-tor",
                    source_url="https://polymarket.com/event/mlb-tor-sd-2026-07-11/mlb-tor-sd-2026-07-11",
                    price=Decimal("0.13"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: NOW)
        outcome_keys = {
            (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO): "tor",
            (Exchange.POLYMARKET, "poly-tor", Side.YES): "tor",
        }
        watch = manager.add_or_refresh(opportunity, outcome_keys)[0]
        assert watch is not None
        watch.books = {
            (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO): LiveLegBook(
                Exchange.KALSHI,
                "KXMLBGAME-26JUL112040TORSD-SD",
                Side.NO,
                BookLevel(13, Decimal("100")),
                NOW,
                True,
                True,
            ),
            (Exchange.POLYMARKET, "poly-tor", Side.YES): LiveLegBook(
                Exchange.POLYMARKET,
                "poly-tor",
                Side.YES,
                BookLevel(13, Decimal("100")),
                NOW,
                True,
                True,
            ),
        }

        result = HotTriggerEngine(settings(live=True), 96, 100, 1000).evaluate(watch, NOW)

        self.assertFalse(result.executable)
        self.assertIn("true-opposite basket", "; ".join(result.blockers))

    def test_live_hot_trigger_blocks_missing_real_outcome_mapping(self):
        opportunity = PredictionHuntOpportunity(
            group_id=248305,
            group_title="2026-07-11 San Diego Padres",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("5.87"),
            total_cost=Decimal("0.26"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KXMLBGAME-26JUL112040TORSD-SD",
                    source_url="https://kalshi.com/markets/KXMLBGAME/KXMLBGAME-26JUL112040TORSD",
                    price=Decimal("0.13"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="poly-tor",
                    source_url="https://polymarket.com/event/mlb-tor-sd-2026-07-11/mlb-tor-sd-2026-07-11",
                    price=Decimal("0.13"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: NOW)
        watch = manager.add_or_refresh(
            opportunity,
            outcome_keys={(Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO): "tor"},
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO): LiveLegBook(
                Exchange.KALSHI,
                "KXMLBGAME-26JUL112040TORSD-SD",
                Side.NO,
                BookLevel(13, Decimal("100")),
                NOW,
                True,
                True,
            ),
            (Exchange.POLYMARKET, "poly-tor", Side.YES): LiveLegBook(
                Exchange.POLYMARKET,
                "poly-tor",
                Side.YES,
                BookLevel(13, Decimal("100")),
                NOW,
                True,
                True,
            ),
        }

        result = HotTriggerEngine(settings(live=True), 96, 100, 1000).evaluate(watch, NOW)

        self.assertFalse(result.executable)
        self.assertIn("true-opposite basket", "; ".join(result.blockers))

    def test_predictionhunt_trusted_pair_can_execute_without_independent_outcome_names(self):
        opportunity = ph_opportunity(
            event_type="sports",
            yes_price="0.40",
            no_price="0.55",
        )
        pair = (
            (Exchange.KALSHI, "KALSHI-1", Side.NO),
            (Exchange.POLYMARKET, "poly-1", Side.YES),
        )
        trusted_pairs = {pair}
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: NOW)
        watch = manager.add_or_refresh(
            opportunity,
            outcome_keys=_predictionhunt_trusted_outcome_keys({}, trusted_pairs),
            allowed_pairs=trusted_pairs,
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(40, Decimal("100")), NOW, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(55, Decimal("100")), NOW, True, True
            ),
        }

        result = HotTriggerEngine(settings(live=True), 99, 100, 1000).evaluate(watch, NOW)

        self.assertTrue(result.executable)
        self.assertEqual(result.gross_cost_cents, 95)
        self.assertEqual(result.buy_yes.exchange, Exchange.POLYMARKET)
        self.assertEqual(result.buy_no.exchange, Exchange.KALSHI)

    def test_predictionhunt_trusted_pair_is_blocked_when_both_prices_are_below_fifty(self):
        opportunity = ph_opportunity(
            event_type="sports",
            yes_price="0.19",
            no_price="0.18",
        )
        trusted_pairs = {
            (
                (Exchange.KALSHI, "KALSHI-1", Side.NO),
                (Exchange.POLYMARKET, "poly-1", Side.YES),
            )
        }
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            opportunity,
            outcome_keys=_predictionhunt_trusted_outcome_keys({}, trusted_pairs),
            allowed_pairs=trusted_pairs,
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(19, Decimal("100")), NOW, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(18, Decimal("100")), NOW, True, True
            ),
        }

        result = HotTriggerEngine(settings(live=True), 99, 100, 1000).evaluate(watch, NOW)

        self.assertFalse(result.executable)
        self.assertIn("opposite sides of 50c", "; ".join(result.blockers))

    def test_hot_allowed_pair_uses_only_exact_predictionhunt_legs(self):
        runner = HotArbRunner(None, None, None, settings(), clock=lambda: NOW)
        opportunity = ph_opportunity()

        pairs = runner._hot_allowed_pair_keys(opportunity)

        self.assertEqual(
            pairs,
            {
                (
                    (Exchange.KALSHI, "KALSHI-1", Side.NO),
                    (Exchange.POLYMARKET, "poly-1", Side.YES),
                )
            },
        )

    def test_hot_trigger_uses_structural_pairs_not_cheapest_cross_pair(self):
        opportunity = PredictionHuntOpportunity(
            group_id=248305,
            group_title="2026-07-11 San Diego Padres",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("5.87"),
            total_cost=Decimal("0.92"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.KALSHI,
                    market_id="KXMLBGAME-26JUL112040TORSD-SD",
                    source_url="https://kalshi.com/markets/KXMLBGAME/KXMLBGAME-26JUL112040TORSD",
                    price=Decimal("0.88"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="poly-tor",
                    source_url="https://polymarket.com/event/mlb-tor-sd-2026-07-11/mlb-tor-sd-2026-07-11",
                    price=Decimal("0.13"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )
        expanded = opportunity.__class__(
            **{
                **opportunity.__dict__,
                "legs": (
                    *opportunity.legs,
                    PredictionHuntLeg(
                        side=Side.NO,
                        platform=Exchange.KALSHI,
                        market_id="KXMLBGAME-26JUL112040TORSD-SD",
                        source_url="https://kalshi.com/markets/KXMLBGAME/KXMLBGAME-26JUL112040TORSD",
                        price=Decimal("0.13"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                    PredictionHuntLeg(
                        side=Side.NO,
                        platform=Exchange.POLYMARKET,
                        market_id="poly-sd",
                        source_url="https://polymarket.com/event/mlb-tor-sd-2026-07-11/mlb-tor-sd-2026-07-11",
                        price=Decimal("0.88"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                ),
            }
        )
        allowed_pairs = {
            (
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.YES),
                (Exchange.POLYMARKET, "poly-tor", Side.YES),
            ),
            (
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO),
                (Exchange.POLYMARKET, "poly-sd", Side.NO),
            ),
        }
        manager = HotWatchManager(600, 3, 20, True, clock=lambda: NOW)
        watch = manager.add_or_refresh(
            expanded,
            outcome_keys={
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.YES): "sd",
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO): "tor",
                (Exchange.POLYMARKET, "poly-tor", Side.YES): "tor",
                (Exchange.POLYMARKET, "poly-sd", Side.NO): "sd",
            },
            allowed_pairs=allowed_pairs,
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.YES): LiveLegBook(
                Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.YES, BookLevel(88, Decimal("100")), NOW, True, True
            ),
            (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO, BookLevel(13, Decimal("100")), NOW, True, True
            ),
            (Exchange.POLYMARKET, "poly-tor", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-tor", Side.YES, BookLevel(13, Decimal("100")), NOW, True, True
            ),
            (Exchange.POLYMARKET, "poly-sd", Side.NO): LiveLegBook(
                Exchange.POLYMARKET, "poly-sd", Side.NO, BookLevel(88, Decimal("100")), NOW, True, True
            ),
        }

        result = HotTriggerEngine(settings(live=True), 96, 100, 1000).evaluate(watch, NOW)

        self.assertEqual(result.gross_cost_cents, 101)
        self.assertLessEqual(result.net_profit_cents, Decimal("0"))

    def test_live_filter_blocks_non_sports_before_market_checks(self):
        runner = HotArbRunner(None, None, None, settings(), clock=lambda: NOW)
        reason = runner._event_type_block_reason(ph_opportunity(event_type="election"))

        self.assertIn("event_type=election is not allowed", reason)

    def test_live_filter_allows_sports_and_esports(self):
        runner = HotArbRunner(None, None, None, settings(), clock=lambda: NOW)

        self.assertIsNone(
            runner._event_type_block_reason(ph_opportunity(event_type="Sports"))
        )
        self.assertIsNone(
            runner._event_type_block_reason(ph_opportunity(event_type="Esports"))
        )

    def test_exact_predictionhunt_pair_rejects_same_side(self):
        opportunity = ph_opportunity()
        opportunity = opportunity.__class__(
            **{
                **opportunity.__dict__,
                "legs": (
                    opportunity.legs[0],
                    PredictionHuntLeg(
                        side=Side.YES,
                        platform=Exchange.KALSHI,
                        market_id="KALSHI-1",
                        source_url=None,
                        price=Decimal("0.48"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                ),
            }
        )
        runner = HotArbRunner(None, None, None, settings(), clock=lambda: NOW)

        reason = runner._exact_predictionhunt_pair_reason(opportunity)

        self.assertIn("one BUY YES and one BUY NO", reason)

    def test_delayed_recovery_system_is_absent(self):
        runner = HotArbRunner(None, None, None, settings(live=True), clock=lambda: NOW)

        self.assertFalse(hasattr(runner, "_active_orphan_order_ids"))
        self.assertFalse(hasattr(runner, "_candidate_orphan_block_reason"))
        self.assertFalse(hasattr(runner, "_orphan_trade_block_reason"))

    def test_live_filter_blocks_mentions_market_by_category(self):
        opportunity = PredictionHuntOpportunity(
            group_id=267721,
            group_title="Gaming",
            event_date=NOW.isoformat(),
            event_type="Mentions",
            roi_pct=Decimal("13.84"),
            total_cost=Decimal("0.86"),
            max_wager_usd=Decimal("86"),
            detected_at=NOW.isoformat(),
            raw={},
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="44489368150267527358376237758398701376767583228387260929462544330608903414668",
                    source_url=(
                        "https://polymarket.com/event/"
                        "what-will-netflix-say-during-their-next-earnings-call/"
                        "will-netflix-say-cloud-gaming-during-earnings-call"
                    ),
                    price=Decimal("0.70"),
                    liquidity_usd=Decimal("70"),
                    fee_usd=Decimal("0.011"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KXEARNINGSMENTIONNFLX-26JUL02-GAME",
                    source_url="https://kalshi.com/markets/KXEARNINGSMENTIONNFLX/KXEARNINGSMENTIONNFLX-26JUL02",
                    price=Decimal("0.16"),
                    liquidity_usd=Decimal("29.71"),
                    fee_usd=Decimal("0.01"),
                ),
            ),
        )
        runner = HotArbRunner(None, None, None, settings(), clock=lambda: NOW)

        reason = runner._event_type_block_reason(opportunity)

        self.assertIn("event_type=Mentions is not allowed", reason)

    def test_live_safety_allows_verified_binary_polymarket_no_token(self):
        opportunity = ph_opportunity(event_type="Elections")
        opportunity = opportunity.__class__(
            **{
                **opportunity.__dict__,
                "legs": (
                    PredictionHuntLeg(
                        side=Side.YES,
                        platform=Exchange.KALSHI,
                        market_id="KXFAST-R",
                        source_url=None,
                        price=Decimal("0.40"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                    PredictionHuntLeg(
                        side=Side.NO,
                        platform=Exchange.POLYMARKET,
                        market_id="2222222222222222222222222222222222222222222222222222222222222222",
                        source_url="https://polymarket.com/market/will-fast-election-resolve-soon",
                        price=Decimal("0.40"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                ),
            }
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_live_safety_allows_verified_named_election_outcome(self):
        opportunity = ph_opportunity(event_type="Elections")
        opportunity = opportunity.__class__(
            **{
                **opportunity.__dict__,
                "legs": (
                    PredictionHuntLeg(
                        side=Side.YES,
                        platform=Exchange.KALSHI,
                        market_id="KXHOUSERACE-CT02-26-R",
                        source_url="https://kalshi.com/markets/KXHOUSERACE/KXHOUSERACE-CT02-26-R",
                        price=Decimal("0.40"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                    PredictionHuntLeg(
                        side=Side.NO,
                        platform=Exchange.POLYMARKET,
                        market_id="2222222222222222222222222222222222222222222222222222222222222222",
                        source_url="https://polymarket.com/market/will-the-republican-party-win-the-ct-02-house-seat",
                        price=Decimal("0.40"),
                        liquidity_usd=Decimal("10"),
                        fee_usd=Decimal("0"),
                    ),
                ),
            }
        )

        class FakePolymarket:
            def _gamma_market(self, slug):
                return {
                    "outcomes": '["Republican Party", "Democratic Party"]',
                    "clobTokenIds": '["1111111111111111111111111111111111111111111111111111111111111111", "2222222222222222222222222222222222222222222222222222222222222222"]',
                }

        runner = HotArbRunner(None, None, FakePolymarket(), settings(), clock=lambda: NOW)

        self.assertIsNone(runner._unsafe_hot_arb_reason(opportunity))

    def test_venue_resolution_blocks_far_future_election(self):
        market = {"ticker": "KXCOLOMBIAPRES-26-AESP", "close_time": (NOW + timedelta(days=365)).isoformat()}

        reason = _market_resolution_safety_reason(market, 3, NOW, "live trade blocked")

        self.assertIn("more than 3 days away", reason)

    def test_venue_resolution_allows_near_term_election(self):
        market = {"ticker": "KXFAST-ELECTION", "close_time": (NOW + timedelta(days=2)).isoformat()}

        reason = _market_resolution_safety_reason(market, 3, NOW, "live trade blocked")

        self.assertIsNone(reason)

    def test_venue_resolution_uses_near_kalshi_ticker_date_when_close_time_is_far(self):
        market = {
            "ticker": "KXLOLGAME-26JUN161600BLGHLE-BLG",
            "close_time": (NOW + timedelta(days=120)).isoformat(),
        }

        reason = _market_resolution_safety_reason(market, 3, NOW, "live trade blocked")

        self.assertIsNone(reason)

    def test_venue_resolution_allows_same_day_mets_kalshi_game_ticker(self):
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        market = {
            "ticker": "KXMLBGAME-26JUL091310KCNYM-NYM",
            "close_time": (now + timedelta(days=120)).isoformat(),
        }

        reason = _market_resolution_safety_reason(market, 3, now, "live trade blocked")

        self.assertIsNone(reason)

    def test_venue_resolution_allows_same_day_hanwha_kalshi_game_ticker(self):
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        market = {
            "ticker": "KXLOLGAME-26JUL090400BLGHLE-HLE",
            "close_time": (now + timedelta(days=120)).isoformat(),
        }

        reason = _market_resolution_safety_reason(market, 3, now, "live trade blocked")

        self.assertIsNone(reason)

    def test_venue_resolution_allows_same_day_metanoia_ticker_when_kalshi_metadata_fails(self):
        now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
        opportunity = PredictionHuntOpportunity(
            group_id=248048,
            group_title="2026-07-09 METANOIA WOLVES",
            event_date="2026-07-09",
            event_type="sports",
            roi_pct=Decimal("0.82"),
            total_cost=Decimal("0.98"),
            max_wager_usd=Decimal("5"),
            detected_at=None,
            raw={},
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="26472134307571900331572160012849699303058803920814148295376410097419287267659",
                    source_url="https://polymarket.com/event/cs2-pain-mw-2026-07-09",
                    price=Decimal("0.03"),
                    liquidity_usd=Decimal("112.32"),
                    fee_usd=Decimal("0.002"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KXCS2GAME-26JUL091300MWPAIN-MW",
                    source_url="https://kalshi.com/markets/KXCS2GAME/KXCS2GAME-26JUL091300MWPAIN",
                    price=Decimal("0.95"),
                    liquidity_usd=Decimal("1576.35"),
                    fee_usd=Decimal("0.01"),
                ),
            ),
        )

        class FailingKalshi:
            def get_market(self, ticker):
                raise RuntimeError("service_unavailable")

        runner = HotArbRunner(None, FailingKalshi(), None, settings(live=True), clock=lambda: now)

        reason = runner._venue_resolution_safety_reason(opportunity)

        self.assertIsNone(reason)

    def test_venue_resolution_blocks_far_kalshi_ticker_date(self):
        market = {"ticker": "KXNEWZEALANDPARLI-26DEC31-NAT"}

        reason = _market_resolution_safety_reason(market, 3, NOW, "live trade blocked")

        self.assertIn("more than 3 days away", reason)

    def test_trigger_fires_when_basket_is_at_threshold_and_depth_sufficient(self):
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            ph_opportunity(), ph_outcome_keys()
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(48, Decimal("5")), NOW, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(48, Decimal("5")), NOW, True, True
            ),
        }
        engine = HotTriggerEngine(settings(), trigger_cost_cents=99, near_miss_cost_cents=100, stale_ms=1000)

        result = engine.evaluate(watch, NOW)

        self.assertEqual(result.gross_cost_cents, 96)
        self.assertIn("live trading disabled", result.blockers)

    def test_hot_trigger_sizes_by_dollar_budget_not_contract_count(self):
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            ph_opportunity(), ph_outcome_keys()
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(19, Decimal("100")), NOW, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(19, Decimal("100")), NOW, True, True
            ),
        }

        result = HotTriggerEngine(settings(live=True), 96, 100, 1000).evaluate(watch, NOW)

        self.assertEqual(result.buy_yes.size, Decimal("26"))
        self.assertEqual(result.buy_no.size, Decimal("26"))

    def test_hot_trigger_does_not_cap_final_size_to_top_book_depth(self):
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            ph_opportunity(), ph_outcome_keys()
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(19, Decimal("1")), NOW, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(19, Decimal("1")), NOW, True, True
            ),
        }

        result = HotTriggerEngine(settings(live=True), 96, 100, 1000).evaluate(watch, NOW)

        self.assertEqual(result.buy_yes.size, Decimal("26"))
        self.assertEqual(result.buy_no.size, Decimal("26"))

    def test_fast_path_uses_live_blended_levels_without_rest_refresh(self):
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            ph_opportunity(yes_price="0.40", no_price="0.55"), ph_outcome_keys()
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET,
                "poly-1",
                Side.YES,
                BookLevel(40, Decimal("1")),
                NOW,
                True,
                True,
                ask_levels=(BookLevel(40, Decimal("1")), BookLevel(42, Decimal("20"))),
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI,
                "KALSHI-1",
                Side.NO,
                BookLevel(55, Decimal("1")),
                NOW,
                True,
                True,
                ask_levels=(BookLevel(55, Decimal("1")), BookLevel(55, Decimal("20"))),
            ),
        }

        class FakeExecutor:
            def __init__(self):
                self.fast_opportunity = None

            def execute_fast(self, opportunity, workflow):
                self.fast_opportunity = opportunity
                return True, "fast"

            def execute(self, opportunity, workflow):
                raise AssertionError("fast path should not fall back to REST refresh")

        fake_executor = FakeExecutor()
        engine = HotTriggerEngine(settings(live=True), 96, 100, 1000, executor=fake_executor)
        evaluation = engine.evaluate(watch, NOW)

        submitted, message = engine.execute_if_allowed(evaluation, True, watch=watch, now=NOW)

        self.assertTrue(submitted)
        self.assertEqual(message, "fast")
        self.assertIsNotNone(fake_executor.fast_opportunity)
        self.assertEqual(fake_executor.fast_opportunity.buy_yes.price_cents, 42)
        self.assertGreater(fake_executor.fast_opportunity.buy_yes.size, Decimal("1"))

    def test_hot_runner_ignores_far_future_before_market_checks(self):
        class FakePredictionHunt:
            def get_arbitrage_opportunities(self, *args):
                return [ph_opportunity(event_date=NOW + timedelta(days=30))]

        class ExplodingVenue:
            def __getattr__(self, name):
                raise AssertionError("far-future candidates should not hit venue checks")

        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(
                FakePredictionHunt(),
                ExplodingVenue(),
                ExplodingVenue(),
                settings(live=True),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            asyncio.run(
                runner.run(
                    category=None,
                    limit=25,
                    predictionhunt_poll_seconds=30,
                    hot_window_seconds=600,
                    max_days_to_resolution=3,
                    prefer_same_day=True,
                    trigger_cost_cents=96,
                    near_miss_cost_cents=100,
                    stale_ms=1000,
                    max_active_watches=20,
                    execute=True,
                    once=True,
                )
            )

            record = json.loads((Path(tmp) / "hot_candidates.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["action"], "skipped")
            self.assertIn("event_date is more than 3 days away", record["message"])

    def test_trigger_blocks_stale_book(self):
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            ph_opportunity(), ph_outcome_keys()
        )[0]
        assert watch is not None
        stale = NOW - timedelta(seconds=5)
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(48, Decimal("5")), stale, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(48, Decimal("5")), NOW, True, True
            ),
        }
        engine = HotTriggerEngine(settings(), trigger_cost_cents=99, near_miss_cost_cents=100, stale_ms=1000)

        result = engine.evaluate(watch, NOW)

        self.assertIn("stale polymarket yes book", result.blockers)

    def test_websocket_state_refresh_keeps_ready_books_evaluable_on_new_update(self):
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            ph_opportunity(), ph_outcome_keys()
        )[0]
        assert watch is not None
        old = NOW - timedelta(seconds=5)
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(96, Decimal("5")), old, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(2, Decimal("5")), NOW, True, True
            ),
        }
        engine = HotTriggerEngine(settings(), trigger_cost_cents=99, near_miss_cost_cents=100, stale_ms=1000)

        _refresh_live_book_timestamps(watch.books, NOW)
        result = engine.evaluate(watch, NOW)

        self.assertEqual(result.gross_cost_cents, 98)
        self.assertNotIn("net profit 1c is not positive after buffers", result.blockers)
        self.assertNotIn("stale polymarket yes book", result.blockers)

    def test_paper_trigger_log_contains_books_and_verified_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(None, None, None, settings(), log_dir=tmp, clock=lambda: NOW)
            watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
                ph_opportunity(), ph_outcome_keys()
            )[0]
            assert watch is not None
            watch.books = {
                (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                    Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(48, Decimal("5")), NOW, True, True
                )
            }
            result = HotTriggerEngine(settings(), 96, 100, 1000).evaluate(watch, NOW)

            runner._log_trigger(watch, result, "paper", "blocked", "paper trigger")

            record = json.loads((Path(tmp) / "hot_paper_trades.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["mode"], "paper")
            self.assertEqual(record["verified"]["gross_cost_cents"], 0)
            self.assertEqual(len(record["books"]), 1)

    def test_jsonl_writer_serializes_decimal_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(None, None, None, settings(), log_dir=tmp, clock=lambda: NOW)

            runner._write_jsonl(
                "hot_candidates.jsonl",
                {
                    "action": "test",
                    "gross_cost_cents": Decimal("53.32285714285714285714285714"),
                    "nested": {"checked_at": NOW, "net_profit_cents": Decimal("42.67714285714285714285714286")},
                },
            )

            record = json.loads((Path(tmp) / "hot_candidates.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["gross_cost_cents"], "53.32285714285714285714285714")
            self.assertEqual(record["nested"]["net_profit_cents"], "42.67714285714285714285714286")
            self.assertEqual(record["nested"]["checked_at"], NOW.isoformat())

    def test_quoted_log_dir_is_normalized_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(None, None, None, settings(), log_dir=f'"{tmp}"', clock=lambda: NOW)

            runner._log_candidate(ph_opportunity(), "skipped", "test")

            self.assertEqual(runner.log_dir, Path(tmp))
            self.assertTrue((Path(tmp) / "hot_candidates.jsonl").exists())

    def test_unwritable_existing_jsonl_is_deleted_and_recreated(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            original = log_dir / "hot_candidates.jsonl"
            original.write_text("old useless log\n", encoding="utf-8")
            runner = HotArbRunner(None, None, None, settings(), log_dir=tmp, clock=lambda: NOW)
            calls = 0

            def flaky_append(path, line):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError(22, "Invalid argument")
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line)

            with patch("firstbot.hot._append_text", flaky_append):
                runner._log_candidate(ph_opportunity(), "skipped", "test")

            records = original.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0])["message"], "test")

    def test_bad_existing_jsonl_falls_back_when_original_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            (log_dir / "hot_candidates.jsonl").mkdir()
            runner = HotArbRunner(None, None, None, settings(), log_dir=tmp, clock=lambda: NOW)

            runner._log_candidate(ph_opportunity(), "skipped", "test")
            runner._log_candidate(ph_opportunity(group_id=2), "skipped", "test again")

            recovered = log_dir / "hot_candidates.recovered-20260616-120000.jsonl"
            self.assertTrue(recovered.exists())
            records = recovered.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(records), 2)
            self.assertEqual(json.loads(records[1])["group_id"], 2)

    def test_empty_log_dir_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "--log-dir must not be empty"):
            _log_dir_path('""')

    def test_paper_trigger_writes_profit_spreadsheet_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(None, None, None, settings(), log_dir=tmp, clock=lambda: NOW)
            watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
                ph_opportunity(), ph_outcome_keys()
            )[0]
            assert watch is not None
            watch.books = {
                (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                    Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(45, Decimal("5")), NOW, True, True
                ),
                (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                    Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(45, Decimal("5")), NOW, True, True
                ),
            }
            result = HotTriggerEngine(settings(), 96, 100, 1000).evaluate(watch, NOW)

            runner._log_trigger(watch, result, "paper", "blocked", "paper trigger")

            with (Path(tmp) / "trade_profit.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            record = rows[0]
            self.assertEqual(record["bet"], "Market 1")
            self.assertEqual(record["strategy"], "arbitrage")
            self.assertEqual(record["resolution_time"], NOW.isoformat())
            self.assertEqual(record["yes_contracts"], "5.00")
            self.assertEqual(record["no_contracts"], "5.00")
            self.assertEqual(record["yes_cost_usd"], "2.25")
            self.assertEqual(record["no_cost_usd"], "2.25")
            self.assertEqual(record["percent_gain"], "9.11")
            self.assertEqual(record["total_profit_usd"], "0.41")

    def test_trigger_record_tracks_profit_percentages(self):
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            ph_opportunity(), ph_outcome_keys()
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(45, Decimal("5")), NOW, True, True
            ),
            (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(45, Decimal("5")), NOW, True, True
            ),
        }
        result = HotTriggerEngine(settings(live=True), 96, 100, 1000).evaluate(watch, NOW)

        record = _arb_record(result)

        self.assertEqual(record["gross_cost_cents"], 90)
        self.assertEqual(record["net_profit_cents"], "8.18")
        self.assertEqual(record["gross_profit_pct"], "11.11")
        self.assertEqual(record["guaranteed_profit_pct"], "9.09")

    def test_near_miss_log_is_separate_from_paper_trades(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(None, None, None, settings(), log_dir=tmp, clock=lambda: NOW)
            watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
                ph_opportunity(), ph_outcome_keys()
            )[0]
            assert watch is not None
            watch.books = {
                (Exchange.POLYMARKET, "poly-1", Side.YES): LiveLegBook(
                    Exchange.POLYMARKET, "poly-1", Side.YES, BookLevel(49, Decimal("5")), NOW, True, True
                ),
                (Exchange.KALSHI, "KALSHI-1", Side.NO): LiveLegBook(
                    Exchange.KALSHI, "KALSHI-1", Side.NO, BookLevel(50, Decimal("5")), NOW, True, True
                ),
            }
            result = HotTriggerEngine(settings(), 96, 100, 1000).evaluate(watch, NOW)

            runner._log_near_miss(watch, result)

            near_miss = Path(tmp) / "hot_near_misses.jsonl"
            paper = Path(tmp) / "hot_paper_trades.jsonl"
            self.assertTrue(near_miss.exists())
            self.assertFalse(paper.exists())
            record = json.loads(near_miss.read_text(encoding="utf-8"))
            self.assertEqual(record["action"], "near_miss")
            self.assertEqual(record["verified"]["gross_cost_cents"], 99)


class HotArbRunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_poll_retry_backoff_reaches_five_minutes(self):
        self.assertEqual(_poll_retry_seconds(3, 1), 3)
        self.assertEqual(_poll_retry_seconds(3, 5), 15)
        self.assertEqual(_poll_retry_seconds(3, 100), 300)
        self.assertEqual(_poll_retry_seconds(3, 250), 300)

    def test_poll_error_summary_classifies_dns_failure(self):
        message = "curl: (6) Could not resolve host: www.predictionhunt.com"

        self.assertEqual(_poll_error_summary(message), "predictionhunt_dns_unresolved")

    async def test_hot_runner_does_not_fetch_ev_bets(self):
        class FakePredictionHunt:
            def get_arbitrage_opportunities(self, *args):
                return []

            def get_expected_value_bets(self, *args):
                raise AssertionError("EV endpoint should not be called by run-hot-arb")

        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(
                FakePredictionHunt(),
                None,
                None,
                settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            await runner.run(
                category=None,
                limit=25,
                predictionhunt_poll_seconds=30,
                hot_window_seconds=600,
                max_days_to_resolution=3,
                prefer_same_day=True,
                trigger_cost_cents=96,
                near_miss_cost_cents=100,
                stale_ms=1000,
                max_active_watches=20,
                execute=False,
                once=True,
            )

    async def test_non_sports_candidate_is_rejected_before_venue_checks(self):
        class FakePredictionHunt:
            def get_arbitrage_opportunities(self, *args):
                return [ph_opportunity(event_type="Entertainment")]

        class ExplodingVenue:
            def __getattr__(self, name):
                raise AssertionError("non-sports candidate must not reach venue checks")

        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(
                FakePredictionHunt(),
                ExplodingVenue(),
                ExplodingVenue(),
                settings(live=True),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            with redirect_stdout(StringIO()) as stdout:
                await runner.run(
                    category=None,
                    limit=25,
                    predictionhunt_poll_seconds=30,
                    hot_window_seconds=600,
                    max_days_to_resolution=3,
                    prefer_same_day=True,
                    trigger_cost_cents=96,
                    near_miss_cost_cents=100,
                    stale_ms=1000,
                    max_active_watches=20,
                    execute=True,
                    once=True,
                )

            record = json.loads(
                (Path(tmp) / "hot_candidates.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(record["action"], "skipped")
            self.assertIn("event_type=Entertainment is not allowed", record["message"])

    async def test_live_candidate_is_not_blocked_by_local_same_exposure_diagnostic(self):
        opportunity = ph_opportunity()

        class FakePredictionHunt:
            def get_arbitrage_opportunities(self, *args):
                return [opportunity]

        class FakePolymarket:
            def resolve_clob_token_id(self, market_id, side):
                return market_id

        class SameOutcomeMatcher:
            def verify_predictionhunt_opportunity(self, candidate):
                positions = tuple(
                    SimpleNamespace(
                        leg_key=(leg.platform, leg.market_id, leg.side),
                        instrument_outcome="player:tereza_krejcova",
                    )
                    for leg in candidate.legs
                )
                return SimpleNamespace(
                    approved_pairs=frozenset(),
                    outcome_keys={},
                    positions=positions,
                    decisions=(),
                    reason_codes=("same_exposure",),
                )

        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(
                FakePredictionHunt(),
                None,
                FakePolymarket(),
                settings(live=True),
                log_dir=tmp,
                clock=lambda: NOW,
            )
            runner.matcher = SameOutcomeMatcher()
            runner._venue_resolution_safety_reason = lambda candidate: None

            with patch("firstbot.hot.preflight_hot_candidate", return_value=None):
                with redirect_stdout(StringIO()) as stdout:
                    await runner.run(
                        category=None,
                        limit=25,
                        predictionhunt_poll_seconds=30,
                        hot_window_seconds=600,
                        max_days_to_resolution=3,
                        prefer_same_day=True,
                        trigger_cost_cents=96,
                        near_miss_cost_cents=100,
                        stale_ms=1000,
                        max_active_watches=20,
                        execute=True,
                        once=True,
                    )

            records = [
                json.loads(line)
                for line in (Path(tmp) / "hot_candidates.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(records[-1]["action"], "added")
            self.assertEqual(records[-1]["message"], "hot watch active")
            self.assertEqual(stdout.getvalue().count("fetched expiring candidate:"), 1)
            self.assertNotIn("expiring candidate blocked:", stdout.getvalue())
            self.assertEqual(stdout.getvalue().count("watching expiring market:"), 1)

    async def test_hot_runner_prints_fetched_markets_inside_resolution_window(self):
        class FakePredictionHunt:
            def get_arbitrage_opportunities(self, *args):
                watched = ph_opportunity(
                    event_date=NOW + timedelta(days=2, hours=3),
                    group_id=1,
                )
                return [
                    watched,
                    watched,
                    ph_opportunity(event_date=NOW + timedelta(days=4), group_id=2),
                ]

        class FakePolymarket:
            def resolve_clob_token_id(self, market_id, side):
                return market_id

        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(
                FakePredictionHunt(),
                None,
                FakePolymarket(),
                settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            with redirect_stdout(StringIO()) as stdout:
                await runner.run(
                    category=None,
                    limit=25,
                    predictionhunt_poll_seconds=30,
                    hot_window_seconds=600,
                    max_days_to_resolution=3,
                    prefer_same_day=True,
                    trigger_cost_cents=96,
                    near_miss_cost_cents=100,
                    stale_ms=1000,
                    max_active_watches=20,
                    execute=False,
                    once=True,
                )

            output = stdout.getvalue()
            self.assertIn("fetched expiring candidate: expires_in=2d 3h", output)
            self.assertEqual(output.count("fetched expiring candidate:"), 1)
            self.assertEqual(output.count("watching expiring market:"), 1)
            self.assertIn("group_id=1", output)
            self.assertIn("title=Market 1", output)
            self.assertNotIn("group_id=2", output)

    async def test_predictionhunt_poll_error_logs_and_does_not_raise(self):
        class FailingPredictionHunt:
            def get_arbitrage_opportunities(self, *args):
                raise RuntimeError("temporary PredictionHunt 500")

        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(
                FailingPredictionHunt(),
                None,
                None,
                settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )

            with redirect_stdout(StringIO()) as stdout:
                await runner.run(
                    category=None,
                    limit=25,
                    predictionhunt_poll_seconds=30,
                    hot_window_seconds=600,
                    max_days_to_resolution=3,
                    prefer_same_day=True,
                    trigger_cost_cents=96,
                    near_miss_cost_cents=100,
                    stale_ms=1000,
                    max_active_watches=20,
                    execute=False,
                    once=True,
                )

            self.assertEqual(stdout.getvalue(), "")
            record = json.loads((Path(tmp) / "hot_poll_errors.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["action"], "poll_error")
            self.assertIn("temporary PredictionHunt 500", record["message"])

    async def test_stream_task_exception_is_logged_as_stream_error(self):
        class FakeKalshiClient:
            base_url = "https://example.test"

        class FakePolymarketClient:
            clob_url = "https://example.test"

        class FailingStream:
            async def listen_until(self, expires_at):
                raise RuntimeError("stream failed visibly")
                yield

        with tempfile.TemporaryDirectory() as tmp:
            runner = HotArbRunner(
                None,
                FakeKalshiClient(),
                FakePolymarketClient(),
                settings(),
                log_dir=tmp,
                clock=lambda: NOW,
            )
            watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
                ph_opportunity(), ph_outcome_keys()
            )[0]
            assert watch is not None
            engine = HotTriggerEngine(settings(), 96, 100, 1000)

            from firstbot import hot

            original_merge_streams = hot.merge_streams

            async def failing_merge_streams(streams, expires_at, clock):
                async for update in original_merge_streams([FailingStream()], expires_at, clock):
                    yield update

            hot.merge_streams = failing_merge_streams
            try:
                await runner._watch_market(watch, engine, execute=False)
            finally:
                hot.merge_streams = original_merge_streams

            records = (Path(tmp) / "hot_candidates.jsonl").read_text(encoding="utf-8")
            self.assertIn("stream_error", records)
            self.assertIn("stream failed visibly", records)

    def test_used_contract_guard_blocks_future_candidates_with_either_leg(self):
        runner = HotArbRunner(None, None, None, settings(), clock=lambda: NOW)
        first = ph_opportunity(group_id=1)
        second = PredictionHuntOpportunity(
            group_id=2,
            group_title="Shares one contract",
            event_date=NOW.isoformat(),
            event_type="politics",
            roi_pct=Decimal("4.0"),
            total_cost=Decimal("0.96"),
            max_wager_usd=Decimal("5"),
            detected_at=NOW.isoformat(),
            legs=(
                PredictionHuntLeg(
                    side=Side.YES,
                    platform=Exchange.POLYMARKET,
                    market_id="poly-1",
                    source_url=None,
                    price=Decimal("0.48"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
                PredictionHuntLeg(
                    side=Side.NO,
                    platform=Exchange.KALSHI,
                    market_id="KALSHI-2",
                    source_url=None,
                    price=Decimal("0.48"),
                    liquidity_usd=Decimal("10"),
                    fee_usd=Decimal("0"),
                ),
            ),
            raw={},
        )

        runner._mark_contracts_used(first)

        self.assertTrue(runner._uses_previously_triggered_contract(second))


if __name__ == "__main__":
    unittest.main()
