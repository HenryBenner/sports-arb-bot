from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from firstbot.matching import MarketMatchingEngine
from firstbot.matching.compiler import threshold_position
from firstbot.matching.models import BitsetSettlement, CanonicalEvent, CanonicalMarket, Relationship, ThresholdSettlement
from firstbot.matching.relationship import RelationshipEngine
from firstbot.models import BookLevel, Exchange, Side
from firstbot.predictionhunt import PredictionHuntLeg, PredictionHuntOpportunity
from firstbot.hot import HotTriggerEngine, HotWatchManager, LiveLegBook
from firstbot.config import Settings


NOW = datetime(2026, 7, 11, 18, 0, tzinfo=timezone.utc)


class FakeKalshi:
    def get_market(self, ticker):
        return {
            "ticker": ticker,
            "event_ticker": "KXMLBGAME-26JUL112040TORSD",
            "title": "San Diego Padres vs Toronto Blue Jays",
            "yes_sub_title": "San Diego Padres",
            "no_sub_title": "Toronto Blue Jays",
            "category": "Sports",
            "close_time": NOW.isoformat(),
        }

    def get_event(self, event_ticker, with_nested_markets=True):
        return {
            "event": {
                "event_ticker": event_ticker,
                "title": "San Diego Padres vs Toronto Blue Jays",
                "category": "Sports",
                "markets": [self.get_market("KXMLBGAME-26JUL112040TORSD-SD")],
            }
        }


class FakeHttp:
    def get_json(self, url, params=None):
        return {
            "id": "pm-event",
            "slug": "mlb-tor-sd-2026-07-11",
            "title": "Toronto Blue Jays vs San Diego Padres",
            "markets": [
                {
                    "id": "pm-market",
                    "slug": "mlb-tor-sd-2026-07-11",
                    "question": "Toronto Blue Jays vs San Diego Padres",
                    "outcomes": '["Toronto Blue Jays", "San Diego Padres"]',
                    "clobTokenIds": '["poly-tor", "poly-sd"]',
                }
            ],
        }


class FakePolymarket:
    gamma_url = "https://gamma.example"
    http = FakeHttp()

    def _gamma_market(self, market_id):
        return self.http.get_json("", {})["markets"][0]


POLY_LIU_TOKEN = "107033931580389488030857623719184152074318531303266118430007316167882072249128"
POLY_IPEK_TOKEN = "38838252182550798319764305389525401368436654913351353489929139018956870630385"


class FakeKalshiTennis:
    def get_market(self, ticker):
        return {
            "ticker": ticker,
            "event_ticker": "KXWTAMATCH-26JUL13LIUIPE",
            "title": "Claire Liu vs Ipek Oz",
            "yes_sub_title": "Claire Liu",
            "no_sub_title": "Ipek Oz",
            "category": "Sports",
            "close_time": NOW.isoformat(),
        }

    def get_event(self, event_ticker, with_nested_markets=True):
        return {
            "event": {
                "event_ticker": event_ticker,
                "title": "Claire Liu vs Ipek Oz",
                "category": "Sports",
                "markets": [self.get_market("KXWTAMATCH-26JUL13LIUIPE-LIU")],
            }
        }


class FakeTokenFirstHttp:
    def get_json(self, url, params=None):
        if "markets-by-token" in url:
            return {
                "condition_id": "0xtenniscondition",
                "primary_token_id": POLY_LIU_TOKEN,
                "secondary_token_id": POLY_IPEK_TOKEN,
            }
        if url.endswith("/markets") and params and any(key in params for key in ("condition_id", "conditionId", "condition_ids", "conditionIds")):
            return [
                {
                    "id": "pm-tennis-market",
                    "condition_id": "0xtenniscondition",
                    "slug": "wta-liu-ipek-2026-07-13",
                    "category": ["Sports"],
                    "question": "Claire Liu vs Ipek Oz",
                    "outcomes": '["Claire Liu", "Ipek Oz"]',
                    "clobTokenIds": f'["{POLY_LIU_TOKEN}", "{POLY_IPEK_TOKEN}"]',
                }
            ]
        return {
            "id": "wrong-event",
            "slug": "wrong-event",
            "title": "Different event",
            "markets": [
                {
                    "id": "wrong-market",
                    "question": "Wrong Player vs Other Player",
                    "outcomes": '["Wrong Player", "Other Player"]',
                    "clobTokenIds": '["wrong-token-a", "wrong-token-b"]',
                }
            ],
        }


class FakePolymarketTokenFirst:
    gamma_url = "https://gamma.example"
    clob_url = "https://clob.example"
    http = FakeTokenFirstHttp()

    def _gamma_market(self, market_id):
        return self.http.get_json(f"{self.gamma_url}/markets", {"condition_id": market_id})[0]


class FakeKalshiPrefixEsports:
    def get_market(self, ticker):
        return {
            "ticker": ticker,
            "event_ticker": "KXCS2GAME-26JUL131430LPHENJOY",
            "title": "LPH Gaming vs ENJOY",
            "yes_sub_title": "LPH Gaming",
            "no_sub_title": "No",
            "category": "Sports",
            "close_time": NOW.isoformat(),
        }

    def get_event(self, event_ticker, with_nested_markets=True):
        return {
            "event": {
                "event_ticker": event_ticker,
                "title": "CS2 LPH Gaming vs ENJOY",
                "category": "Sports",
                "markets": [self.get_market("KXCS2GAME-26JUL131430LPHENJOY-LPH")],
            }
        }


class FakePolymarketEsports:
    gamma_url = "https://gamma.example"

    class Http:
        def get_json(self, url, params=None):
            return {
                "id": "pm-cs2-event",
                "slug": "cs2-enjoy-lph-2026-07-13",
                "title": "CS2 ENJOY vs LPH Gaming",
                "markets": [
                    {
                        "id": "pm-cs2-market",
                        "slug": "cs2-enjoy-lph-2026-07-13",
                        "question": "ENJOY vs LPH Gaming",
                        "outcomes": '["ENJOY", "LPH Gaming"]',
                        "clobTokenIds": '["poly-enjoy", "poly-lph"]',
                    }
                ],
            }

    http = Http()

    def _gamma_market(self, market_id):
        return self.http.get_json("", {})["markets"][0]


def expanded_padres_opportunity():
    return PredictionHuntOpportunity(
        group_id=248305,
        group_title="2026-07-11 San Diego Padres",
        event_date=NOW.isoformat(),
        event_type="sports",
        roi_pct=Decimal("5.87"),
        total_cost=Decimal("0.92"),
        max_wager_usd=Decimal("10"),
        detected_at=NOW.isoformat(),
        raw={},
        legs=(
            PredictionHuntLeg(Side.YES, Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", None, Decimal("0.37"), Decimal("10"), Decimal("0")),
            PredictionHuntLeg(Side.NO, Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", None, Decimal("0.63"), Decimal("10"), Decimal("0")),
            PredictionHuntLeg(Side.YES, Exchange.POLYMARKET, "poly-tor", "https://polymarket.com/event/mlb-tor-sd-2026-07-11", Decimal("0.58"), Decimal("10"), Decimal("0")),
            PredictionHuntLeg(Side.NO, Exchange.POLYMARKET, "poly-sd", "https://polymarket.com/event/mlb-tor-sd-2026-07-11", Decimal("0.42"), Decimal("10"), Decimal("0")),
        ),
    )


class MarketMatchingEngineTests(unittest.TestCase):
    def test_padres_blue_jays_blocks_same_exposure_and_allows_complements(self):
        with tempfile.TemporaryDirectory() as tmp:
            verified = MarketMatchingEngine(FakeKalshi(), FakePolymarket(), clock=lambda: NOW, log_dir=tmp).verify_predictionhunt_opportunity(
                expanded_padres_opportunity()
            )

        self.assertIn(
            (
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.YES),
                (Exchange.POLYMARKET, "poly-tor", Side.YES),
            ),
            verified.approved_pairs,
        )
        self.assertIn(
            (
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO),
                (Exchange.POLYMARKET, "poly-sd", Side.NO),
            ),
            verified.approved_pairs,
        )
        self.assertNotIn(
            (
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO),
                (Exchange.POLYMARKET, "poly-tor", Side.YES),
            ),
            verified.approved_pairs,
        )

    def test_trigger_can_execute_same_label_exact_complement(self):
        opportunity = expanded_padres_opportunity()
        with tempfile.TemporaryDirectory() as tmp:
            verified = MarketMatchingEngine(FakeKalshi(), FakePolymarket(), clock=lambda: NOW, log_dir=tmp).verify_predictionhunt_opportunity(opportunity)
        watch = HotWatchManager(600, 3, 20, True, clock=lambda: NOW).add_or_refresh(
            opportunity,
            outcome_keys=verified.outcome_keys,
            allowed_pairs=set(verified.approved_pairs),
        )[0]
        assert watch is not None
        watch.books = {
            (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.YES): LiveLegBook(
                Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.YES, BookLevel(37, Decimal("5")), NOW, True, True
            ),
            (Exchange.POLYMARKET, "poly-tor", Side.YES): LiveLegBook(
                Exchange.POLYMARKET, "poly-tor", Side.YES, BookLevel(58, Decimal("5")), NOW, True, True
            ),
        }

        result = HotTriggerEngine(settings(live=True), 99, 100, 1000).evaluate(watch, NOW)

        self.assertTrue(result.executable)
        self.assertEqual(result.gross_cost_cents, 95)
        self.assertEqual({result.buy_yes.exchange, result.buy_no.exchange}, {Exchange.KALSHI, Exchange.POLYMARKET})
        self.assertTrue(all(leg.side is Side.YES for leg in (result.buy_yes, result.buy_no)))

    def test_named_polymarket_token_can_arrive_with_yes_side_label(self):
        opportunity = PredictionHuntOpportunity(
            group_id=248305,
            group_title="2026-07-11 San Diego Padres",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("5.87"),
            total_cost=Decimal("0.92"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            raw={},
            legs=(
                PredictionHuntLeg(Side.NO, Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", None, Decimal("0.37"), Decimal("10"), Decimal("0")),
                PredictionHuntLeg(Side.YES, Exchange.POLYMARKET, "poly-sd", "https://polymarket.com/event/mlb-tor-sd-2026-07-11", Decimal("0.58"), Decimal("10"), Decimal("0")),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            verified = MarketMatchingEngine(FakeKalshi(), FakePolymarket(), clock=lambda: NOW, log_dir=tmp).verify_predictionhunt_opportunity(opportunity)

        self.assertIn(
            (
                (Exchange.KALSHI, "KXMLBGAME-26JUL112040TORSD-SD", Side.NO),
                (Exchange.POLYMARKET, "poly-sd", Side.YES),
            ),
            verified.approved_pairs,
        )

    def test_numeric_polymarket_token_verifies_parent_market_when_source_event_is_wrong(self):
        opportunity = PredictionHuntOpportunity(
            group_id=267051,
            group_title="2026-07-13 Claire Liu",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("7.91"),
            total_cost=Decimal("0.92"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            raw={},
            legs=(
                PredictionHuntLeg(Side.NO, Exchange.KALSHI, "KXWTAMATCH-26JUL13LIUIPE-LIU", None, Decimal("0.40"), Decimal("10"), Decimal("0")),
                PredictionHuntLeg(Side.YES, Exchange.POLYMARKET, POLY_LIU_TOKEN, "https://polymarket.com/event/not-the-source-market", Decimal("0.58"), Decimal("10"), Decimal("0")),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            verified = MarketMatchingEngine(
                FakeKalshiTennis(),
                FakePolymarketTokenFirst(),
                clock=lambda: NOW,
                log_dir=tmp,
            ).verify_predictionhunt_opportunity(opportunity)

        self.assertNotIn(
            f"polymarket_metadata_failed:polymarket_token_not_in_source_event: {POLY_LIU_TOKEN}",
            verified.reason_codes,
        )
        self.assertIn(
            (
                (Exchange.KALSHI, "KXWTAMATCH-26JUL13LIUIPE-LIU", Side.NO),
                (Exchange.POLYMARKET, POLY_LIU_TOKEN, Side.YES),
            ),
            verified.approved_pairs,
        )

    def test_named_polymarket_no_label_does_not_invert_token_outcome(self):
        opportunity = PredictionHuntOpportunity(
            group_id=280437,
            group_title="2026-07-18 Claire Liu",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("170.27"),
            total_cost=Decimal("0.37"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            raw={},
            legs=(
                PredictionHuntLeg(
                    Side.YES,
                    Exchange.KALSHI,
                    "KXWTAMATCH-26JUL13LIUIPE-LIU",
                    None,
                    Decimal("0.18"),
                    Decimal("10"),
                    Decimal("0"),
                ),
                PredictionHuntLeg(
                    Side.NO,
                    Exchange.POLYMARKET,
                    POLY_LIU_TOKEN,
                    "https://polymarket.com/event/wta-liu-ipek-2026-07-13",
                    Decimal("0.19"),
                    Decimal("10"),
                    Decimal("0"),
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            verified = MarketMatchingEngine(
                FakeKalshiTennis(),
                FakePolymarketTokenFirst(),
                clock=lambda: NOW,
                log_dir=tmp,
            ).verify_predictionhunt_opportunity(opportunity)

        exact_pair = (
            (Exchange.KALSHI, "KXWTAMATCH-26JUL13LIUIPE-LIU", Side.YES),
            (Exchange.POLYMARKET, POLY_LIU_TOKEN, Side.NO),
        )
        self.assertNotIn(exact_pair, verified.approved_pairs)
        exact_positions = [
            position
            for position in verified.positions
            if position.leg_key in exact_pair
        ]
        self.assertEqual(
            {position.instrument_outcome for position in exact_positions},
            {"unknown:claire_liu"},
        )

    def test_kalshi_prefix_matchup_ticker_infers_opponent_for_esports(self):
        opportunity = PredictionHuntOpportunity(
            group_id=269221,
            group_title="2026-07-13 LPH Gaming",
            event_date=NOW.isoformat(),
            event_type="sports",
            roi_pct=Decimal("5.11"),
            total_cost=Decimal("0.92"),
            max_wager_usd=Decimal("10"),
            detected_at=NOW.isoformat(),
            raw={},
            legs=(
                PredictionHuntLeg(Side.YES, Exchange.POLYMARKET, "poly-lph", "https://polymarket.com/event/cs2-enjoy-lph-2026-07-13", Decimal("0.52"), Decimal("10"), Decimal("0")),
                PredictionHuntLeg(Side.NO, Exchange.KALSHI, "KXCS2GAME-26JUL131430LPHENJOY-LPH", None, Decimal("0.41"), Decimal("10"), Decimal("0")),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            verified = MarketMatchingEngine(
                FakeKalshiPrefixEsports(),
                FakePolymarketEsports(),
                clock=lambda: NOW,
                log_dir=tmp,
            ).verify_predictionhunt_opportunity(opportunity)

        self.assertIn(
            (
                (Exchange.KALSHI, "KXCS2GAME-26JUL131430LPHENJOY-LPH", Side.NO),
                (Exchange.POLYMARKET, "poly-lph", Side.YES),
            ),
            verified.approved_pairs,
        )


class RelationshipRegressionTests(unittest.TestCase):
    def test_soccer_three_way_draw_is_not_exact_opposite(self):
        event = canonical_event(("home", "draw", "away"), sport="soccer")
        market = canonical_market(event, "three_way_winner")
        home_no = position(event, market, "home-no", BitsetSettlement(frozenset({"home", "draw", "away"}), frozenset({"draw", "away"})))
        away_yes = position(event, market, "away-yes", BitsetSettlement(frozenset({"home", "draw", "away"}), frozenset({"away"})))

        decision = RelationshipEngine().compare(home_no, away_yes)

        self.assertEqual(decision.relationship, Relationship.PARTIAL_OVERLAP)
        self.assertFalse(decision.tradable_as_arb)

    def test_future_no_one_candidate_and_yes_other_candidate_is_partial_overlap(self):
        event = canonical_event(("france", "spain", "germany", "other"), sport="soccer")
        market = canonical_market(event, "future_winner")
        france_no = position(event, market, "france-no", BitsetSettlement(frozenset(event.participant_ids), frozenset({"spain", "germany", "other"})))
        spain_yes = position(event, market, "spain-yes", BitsetSettlement(frozenset(event.participant_ids), frozenset({"spain"})))

        decision = RelationshipEngine().compare(france_no, spain_yes)

        self.assertEqual(decision.relationship, Relationship.PARTIAL_OVERLAP)
        self.assertFalse(decision.tradable_as_arb)

    def test_total_over_eight_and_half_matches_at_least_nine(self):
        event = canonical_event(("home", "away"))
        market = canonical_market(event, "total", metric="total_runs")
        over_8_5 = threshold_position(
            venue=Exchange.KALSHI,
            market_id="K-TOTAL",
            instrument_id="K-TOTAL",
            side=Side.YES,
            event=event,
            market=market,
            expression=ThresholdSettlement("total_runs", ">", Decimal("8.5"), None, None, "refund"),
        )
        at_least_9 = threshold_position(
            venue=Exchange.POLYMARKET,
            market_id="PM-TOTAL",
            instrument_id="PM-TOTAL",
            side=Side.YES,
            event=event,
            market=market,
            expression=ThresholdSettlement("total_runs", ">=", Decimal("9"), None, None, "refund"),
        )

        decision = RelationshipEngine().compare(over_8_5, at_least_9)

        self.assertEqual(decision.relationship, Relationship.SAME_EXPOSURE)
        self.assertFalse(decision.tradable_as_arb)

    def test_spread_push_is_only_allowed_as_guaranteed_cover(self):
        event = canonical_event(("team-a", "team-b"))
        market = canonical_market(event, "spread", metric="margin")
        team_a_minus_one = threshold_position(
            venue=Exchange.KALSHI,
            market_id="K-SPREAD",
            instrument_id="K-SPREAD",
            side=Side.YES,
            event=event,
            market=market,
            expression=ThresholdSettlement("margin", ">", Decimal("-1"), "team-a", "team-b", "refund"),
        )
        team_b_plus_one = threshold_position(
            venue=Exchange.POLYMARKET,
            market_id="PM-SPREAD",
            instrument_id="PM-SPREAD",
            side=Side.YES,
            event=event,
            market=market,
            expression=ThresholdSettlement("opposing_margin", ">", Decimal("1"), "team-b", "team-a", "refund"),
        )

        decision = RelationshipEngine().compare(team_a_minus_one, team_b_plus_one)

        self.assertEqual(decision.relationship, Relationship.GUARANTEED_COVER)
        self.assertTrue(decision.tradable_as_arb)

    def test_esports_map_and_match_scope_mismatch_blocks(self):
        event = canonical_event(("team-a", "team-b"), sport="esports", league="cs2")
        match_market = canonical_market(event, "two_way_winner", scope="match")
        map_market = canonical_market(event, "two_way_winner", scope="map_1")
        match_pos = position(event, match_market, "match-team-a", BitsetSettlement(frozenset({"team-a", "team-b"}), frozenset({"team-a"})))
        map_pos = position(event, map_market, "map-team-b", BitsetSettlement(frozenset({"team-a", "team-b"}), frozenset({"team-b"})))

        decision = RelationshipEngine().compare(match_pos, map_pos)

        self.assertEqual(decision.relationship, Relationship.SETTLEMENT_MISMATCH)
        self.assertIn("market_scope_mismatch", decision.hard_conflicts)


def canonical_event(participants, sport="baseball", league="mlb"):
    return CanonicalEvent(
        event_key=f"{sport}:{league}:{':'.join(sorted(participants))}",
        domain="sports",
        sport=sport,
        league=league,
        competition=None,
        season=None,
        participant_ids=tuple(participants),
        scheduled_start=NOW,
        game_number=None,
        series_scope=None,
        best_of=None,
    )


def canonical_market(event, family, scope="match", metric="winner"):
    return CanonicalMarket(
        event_key=event.event_key,
        family=family,
        scope=scope,
        period="full_game",
        subject_id=None,
        opponent_id=None,
        metric=metric,
        operator=None,
        threshold=None,
        unit=None,
        target_value=None,
        includes_overtime=None,
        includes_extra_innings=None,
        push_policy="refund",
        void_policy="refund",
        settlement_deadline=NOW,
    )


def position(event, market, instrument_id, settlement):
    return threshold_position(
        venue=Exchange.KALSHI if instrument_id.startswith("home") or instrument_id.startswith("france") or instrument_id.startswith("match") else Exchange.POLYMARKET,
        market_id=instrument_id,
        instrument_id=instrument_id,
        side=Side.YES,
        event=event,
        market=market,
        expression=settlement,
    ) if isinstance(settlement, ThresholdSettlement) else _bitset_position(event, market, instrument_id, settlement)


def _bitset_position(event, market, instrument_id, settlement):
    return __import__("firstbot.matching.models", fromlist=["TradeablePosition"]).TradeablePosition(
        venue=(Exchange.KALSHI if instrument_id.startswith(("home", "france", "match")) else Exchange.POLYMARKET).value,
        market_id=instrument_id,
        instrument_id=instrument_id,
        instrument_outcome=instrument_id,
        order_action="BUY",
        side=Side.YES,
        event=event,
        market=market,
        settlement=settlement,
        confidence=0.9,
        reason_codes=(),
        raw={},
    )


def settings(live=False):
    return Settings(
        live_trading=live,
        min_profit_cents=1,
        max_leg_usd=10,
        slippage_cents=0,
        fee_buffer_cents=0,
        http_timeout_seconds=30,
        kalshi_base_url="https://kalshi.example",
        kalshi_api_key_id=None,
        kalshi_private_key_path=None,
        kalshi_fee_rate=Decimal("0"),
        polymarket_gamma_url="https://gamma.example",
        polymarket_clob_url="https://clob.example",
        polymarket_private_key=None,
        polymarket_api_key=None,
        polymarket_api_secret=None,
        polymarket_api_passphrase=None,
        polymarket_funder_address=None,
        polymarket_signature_type=3,
        polymarket_fee_rate=Decimal("0"),
        predictionhunt_base_url="https://predictionhunt.example",
        predictionhunt_api_key="key",
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


if __name__ == "__main__":
    unittest.main()
