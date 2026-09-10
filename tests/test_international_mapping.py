from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from firstbot.exchanges.international_mapping import InternationalMarketMapper, MappingRejected
from firstbot.exchanges.polymarket_us import PolymarketUSClient
from firstbot.models import Exchange, Side
from firstbot.predictionhunt import PredictionHuntLeg
from firstbot.websockets import PolymarketOrderbookStream


RULES = "The official result including extra innings determines settlement. Cancellation settles 50-50."


def fixture(sport="baseball", league="mlb", kind="totals", line="9.5", labels=("Over", "Under")):
    event = {"slug": "international-event", "title": "Houston Astros vs. Philadelphia Phillies",
             "sport": sport, "league": league, "startTime": "2026-09-10T17:05:00Z",
             "teams": [{"name": "Houston Astros", "alias": "Astros"},
                       {"name": "Philadelphia Phillies", "alias": "Phillies"}]}
    source = {"slug": "international-market", "question": "Houston Astros vs. Philadelphia Phillies: O/U 9.5",
              "sportsMarketType": kind, "gameStartTime": event["startTime"], "line": line,
              "outcomes": list(labels), "clobTokenIds": ["111", "222"], "description": RULES,
              "events": [{"slug": event["slug"]}]}
    target = {k: deepcopy(v) for k, v in source.items() if k not in ("events", "clobTokenIds")}
    target["slug"] = "us-market"
    target["marketSides"] = [{"long": True, "description": labels[0]}, {"long": False, "description": labels[1]}]
    us_event = {**deepcopy(event), "slug": "us-event", "markets": [target]}
    return event, source, us_event


class MappingTests(unittest.TestCase):
    def setup_mapper(self, data=None):
        self.event, self.source, self.us_event = data or fixture()
        self.now = 0
        self.http = Mock()
        def fetch(url, params=None):
            if url.endswith("/markets"):
                return [deepcopy(self.source)]
            return deepcopy(self.event)
        self.http.get_json.side_effect = fetch
        self.sdk = SimpleNamespace(search=Mock(), events=Mock())
        self.sdk.search.query.side_effect = lambda params: {"events": [deepcopy(self.us_event)]}
        self.sdk.events.retrieve_by_slug.side_effect = lambda slug: deepcopy(self.us_event)
        self.us = Mock()
        self.us._client.return_value = self.sdk
        self.us.get_market_by_slug.side_effect = lambda slug: deepcopy(next(m for m in self.us_event["markets"] if m["slug"] == slug))
        self.mapper = InternationalMarketMapper(self.us, http=self.http, clock=lambda: self.now)
        return self.mapper

    def test_exact_total_and_event_discovery_query(self):
        mapper = self.setup_mapper()
        self.us_event["markets"][0]["sportsMarketType"] = "baseball_team_full_game_total"
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")
        self.sdk.search.query.assert_called_once_with({"query": self.event["title"], "limit": 100, "page": 1})
        self.us.get_market_by_slug.assert_not_called()

    def test_all_sports_have_no_allowlist(self):
        for sport in ("baseball", "basketball", "soccer", "tennis", "cricket", "ice hockey", "dota2", "new sport"):
            with self.subTest(sport=sport):
                mapper = self.setup_mapper(fixture(sport=sport, league="competition"))
                self.assertEqual(mapper.resolve("222", Side.NO), "us-market::no")

    def test_gamma_sport_can_be_league_code(self):
        mapper = self.setup_mapper()
        self.event["sport"] = "mlb"
        self.event["tags"] = [{"slug": tag} for tag in ("sports", "games", "mlb", "baseball")]
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_specific_props_need_same_type_subject_question_and_line(self):
        data = fixture(kind="player_strikeouts", line="6.5", labels=("Yes", "No"))
        data[1]["question"] = data[2]["markets"][0]["question"] = "Will Player A record over 6.5 strikeouts?"
        data[1]["subject"] = data[2]["markets"][0]["subject"] = {"name": "Player A"}
        mapper = self.setup_mapper(data)
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")
        self.now = 301
        self.us_event["markets"][0]["subject"] = {"name": "Player B"}
        with self.assertRaisesRegex(MappingRejected, "subject mismatch"):
            mapper.resolve("111", Side.YES)

    def test_named_team_maps_to_short_without_using_prices(self):
        labels = ("Houston Astros", "Philadelphia Phillies")
        mapper = self.setup_mapper(fixture(kind="moneyline", line=None, labels=labels))
        target = self.us_event["markets"][0]
        target["sportsMarketType"] = "baseball_team_full_game_winner"
        target["marketSides"].reverse()
        target["marketSides"][0]["long"] = True
        target["marketSides"][1]["long"] = False
        target["outcomes"] = list(reversed(labels))
        target["question"] = "Who will win?"
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::no")

    def test_spread_signed_line_and_named_team(self):
        mapper = self.setup_mapper(fixture(kind="spreads", line="-1.5", labels=("Houston Astros", "Philadelphia Phillies")))
        target = self.us_event["markets"][0]
        target.update(sportsMarketType="baseball_team_full_game_spread", outcomes=["-1.50", "+1.50"])
        target["marketSides"] = [{"long": True, "description": "-1.50", "team": {"name": "Houston Astros"}},
                                 {"long": False, "description": "+1.50", "team": {"name": "Philadelphia Phillies"}}]
        self.assertEqual(mapper.resolve("222", Side.NO), "us-market::no")

    def test_line_mismatch_never_overridden_by_title(self):
        mapper = self.setup_mapper()
        self.us_event["markets"][0]["line"] = "8.5"
        with self.assertRaisesRegex(MappingRejected, "line mismatch international=9.5 us=8.5"):
            mapper.resolve("111", Side.YES)

    def test_period_mismatch(self):
        mapper = self.setup_mapper()
        self.us_event["markets"][0]["sportsMarketType"] = "baseball_team_first_five_innings_total"
        with self.assertRaisesRegex(MappingRejected, "period mismatch"):
            mapper.resolve("111", Side.YES)

    def test_same_day_different_start_rejected(self):
        mapper = self.setup_mapper()
        self.us_event["startTime"] = "2026-09-10T23:05:00Z"
        with self.assertRaisesRegex(MappingRejected, "scheduled mismatch"):
            mapper.resolve("111", Side.YES)

    def test_timezone_equivalence(self):
        mapper = self.setup_mapper()
        self.us_event["startTime"] = "2026-09-10T13:05:00-04:00"
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_missing_metadata_rejected(self):
        for key in ("sport", "league", "startTime"):
            with self.subTest(key=key):
                mapper = self.setup_mapper()
                self.event.pop(key)
                if key == "sport":
                    self.event["league"] = "unknown-league"
                if key == "startTime":
                    self.source.pop("gameStartTime")
                with self.assertRaisesRegex(MappingRejected, "missing critical metadata"):
                    mapper.resolve("111", Side.YES)

    def test_outcome_token_conflict(self):
        mapper = self.setup_mapper(fixture(kind="custom_prop", labels=("Yes", "No")))
        with self.assertRaisesRegex(MappingRejected, "token outcome conflicts"):
            mapper.resolve("222", Side.YES)

    def test_no_event_match(self):
        mapper = self.setup_mapper()
        self.sdk.search.query.side_effect = lambda params: {"events": []}
        with self.assertRaisesRegex(MappingRejected, "verified_matches=0"):
            mapper.resolve("111", Side.YES)

    def test_two_exact_matches_rejected(self):
        mapper = self.setup_mapper()
        duplicate = deepcopy(self.us_event["markets"][0])
        duplicate["slug"] = "other-us-market"
        self.us_event["markets"].append(duplicate)
        with self.assertRaisesRegex(MappingRejected, "verified_matches=2"):
            mapper.resolve("111", Side.YES)

    def test_edge_case_settlement_mismatch_is_warning_only(self):
        mapper = self.setup_mapper()
        self.us_event["markets"][0]["description"] += " Postponement settles at last fair market price."
        report = mapper.inspect("111", Side.YES)
        self.assertEqual(report["identity_matches"], 1)
        self.assertTrue(report["candidates"][0]["rules_status"].startswith("warning:"))
        self.assertIn("last-fair-market-price", report["candidates"][0]["rules_status"])
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_explicit_normal_play_rule_conflicts_are_rejected(self):
        for key in ("includesOvertime", "includesExtraInnings", "pushPolicy", "tiePolicy"):
            with self.subTest(key=key):
                mapper = self.setup_mapper()
                self.source[key] = "source-policy"
                self.us_event["markets"][0][key] = "different-policy"
                with self.assertRaisesRegex(MappingRejected, key):
                    mapper.resolve("111", Side.YES)

    def test_edge_case_rule_fields_are_warning_only(self):
        for key in ("resolutionSource", "rules", "rulesDisclaimer", "voidPolicy",
                    "postponementPolicy", "cancellationPolicy", "settlementDeadline"):
            with self.subTest(key=key):
                mapper = self.setup_mapper()
                self.us_event["markets"][0][key] = "different"
                report = mapper.inspect("111", Side.YES)
                self.assertTrue(report["candidates"][0]["rules_status"].startswith("warning:"))
                self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_success_cache_avoids_all_network_calls_and_expires(self):
        mapper = self.setup_mapper()
        mapper.resolve("111", Side.YES)
        calls = self.http.get_json.call_count
        for _ in range(10):
            self.now += 3
            mapper.resolve("111", Side.YES)
        self.assertEqual(self.http.get_json.call_count, calls)
        self.sdk.search.query.assert_called_once()
        self.now = 301
        self.us_event["markets"][0]["line"] = "8.5"
        with self.assertRaisesRegex(MappingRejected, "line mismatch"):
            mapper.resolve("111", Side.YES)
        self.assertGreater(self.http.get_json.call_count, calls)

    def test_negative_cache_retries_and_recovers(self):
        mapper = self.setup_mapper()
        self.us_event["markets"][0]["line"] = "8.5"
        for _ in range(2):
            with self.assertRaises(MappingRejected):
                mapper.resolve("111", Side.YES)
        self.sdk.search.query.assert_called_once()
        self.us_event["markets"][0]["line"] = "9.5"
        self.now = 31
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_wrong_token_returned_by_gamma_is_rejected(self):
        mapper = self.setup_mapper()
        self.source["clobTokenIds"] = ["333", "444"]
        with self.assertRaisesRegex(MappingRejected, "token lookup requires one market"):
            mapper.resolve("111", Side.YES)

    def test_success_logs_exact_mapping(self):
        mapper = self.setup_mapper()
        with self.assertLogs("firstbot.exchanges.international_mapping", level="INFO") as logs:
            mapper.resolve("111", Side.YES)
        self.assertIn("confidence=exact", logs.output[0])

    def test_gamma_market_id_resolves_literal_side_without_token_guessing(self):
        mapper = self.setup_mapper(fixture(kind="custom_prop", labels=("No", "Yes")))
        self.source["id"] = "999"
        self.assertEqual(mapper.resolve("999", Side.YES), "us-market::no")
        self.assertIn({"id": "999"}, [call.args[1] for call in self.http.get_json.call_args_list if len(call.args) > 1])

    def test_market_id_with_named_outcomes_needs_explicit_intent(self):
        mapper = self.setup_mapper()
        self.source["id"] = "999"
        with self.assertRaisesRegex(MappingRejected, "explicit intended outcome/token required"):
            mapper.resolve("999", Side.YES)

    def test_sport_league_participants_and_game_number_conflicts(self):
        changes = (("sport", "cricket"), ("league", "minor league"),
                   ("gameNumber", 2), ("teams", [{"name": "Other Team"}, {"name": "Philadelphia Phillies"}]))
        for key, value in changes:
            with self.subTest(key=key):
                mapper = self.setup_mapper()
                self.us_event[key] = value
                with self.assertRaisesRegex(MappingRejected, "mismatch"):
                    mapper.resolve("111", Side.YES)

    def test_incomplete_plausible_candidate_blocks_an_otherwise_exact_match(self):
        mapper = self.setup_mapper()
        incomplete = deepcopy(self.us_event["markets"][0])
        incomplete["slug"] = "unverified"
        incomplete.pop("description")
        self.us_event["markets"].append(incomplete)
        with self.assertRaisesRegex(MappingRejected, "identity_matches=2"):
            mapper.resolve("111", Side.YES)

    def test_reversed_or_duplicate_us_outcome_flags_are_not_array_indices(self):
        mapper = self.setup_mapper()
        self.us_event["markets"][0]["marketSides"][1]["long"] = True
        with self.assertRaisesRegex(MappingRejected, "ambiguous US long/short"):
            mapper.resolve("111", Side.YES)

    def test_spread_side_cannot_override_conflicting_structured_line(self):
        mapper = self.setup_mapper(fixture(kind="spreads", line="-1.5", labels=("Houston Astros", "Philadelphia Phillies")))
        target = self.us_event["markets"][0]
        target.update(line="-2.5", sportsMarketType="baseball_team_full_game_spread")
        target["marketSides"] = [{"long": True, "description": "-1.50", "team": {"name": "Houston Astros"}},
                                 {"long": False, "description": "+1.50", "team": {"name": "Philadelphia Phillies"}}]
        with self.assertRaisesRegex(MappingRejected, "outcome line conflicts"):
            mapper.resolve("111", Side.YES)

    def test_search_pagination_failure_cannot_authorize_first_page(self):
        mapper = self.setup_mapper()
        page = [{**deepcopy(self.us_event), "slug": f"event-{i}"} for i in range(100)]
        self.sdk.search.query.side_effect = [{"events": page}, RuntimeError("page unavailable")]
        with self.assertRaisesRegex(MappingRejected, "page unavailable"):
            mapper.resolve("111", Side.YES)

    def test_search_reads_all_pages(self):
        mapper = self.setup_mapper()
        page = [{**deepcopy(self.us_event), "slug": f"event-{i}"} for i in range(100)]
        self.sdk.search.query.side_effect = [{"events": page}, {"events": [self.us_event]}]
        self.assertEqual(len(mapper._search("fixture")), 101)
        self.assertEqual(self.sdk.search.query.call_args.args[0]["page"], 2)

    def test_negative_transport_cache_recovers(self):
        mapper = self.setup_mapper()
        fetch = self.http.get_json.side_effect
        self.http.get_json.side_effect = RuntimeError("unavailable")
        for _ in range(2):
            with self.assertRaisesRegex(MappingRejected, "metadata lookup failed"):
                mapper.resolve("111", Side.YES)
        self.http.get_json.assert_called_once()
        self.http.get_json.side_effect = fetch
        self.now = 31
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_cached_event_does_not_extend_mapping_metadata_age(self):
        mapper = self.setup_mapper()
        mapper.resolve("111", Side.YES)
        self.now = 20
        mapper.resolve("222", Side.NO)
        self.assertEqual(mapper._cache[("mapping", "222", Side.NO)][0], 300)

    def test_inspection_keeps_settlement_warning_without_blocking_identity(self):
        mapper = self.setup_mapper()
        self.us_event["markets"][0]["description"] += " Different rules."
        report = mapper.inspect("111", Side.YES)
        self.assertEqual(report["identity_matches"], 1)
        self.assertTrue(report["candidates"][0]["rules_status"].startswith("warning:"))
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_same_day_time_difference_is_only_a_near_match(self):
        mapper = self.setup_mapper()
        self.us_event["startTime"] = self.us_event["markets"][0]["gameStartTime"] = "2026-09-10T17:10:00Z"
        report = mapper.inspect("111", Side.YES)
        self.assertEqual(report["identity_matches"], 0)
        self.assertEqual(len(report["near_matches"]), 1)
        with self.assertRaisesRegex(MappingRejected, "scheduled mismatch"):
            mapper.resolve("111", Side.YES)

    def test_game_first_half_total_type_matches_without_becoming_team_total(self):
        mapper = self.setup_mapper(fixture(sport="football", league="nfl", kind="first_half_totals"))
        self.us_event["markets"][0]["sportsMarketType"] = "football_game_first_half_total"
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")
        self.now = 301
        self.us_event["markets"][0]["sportsMarketType"] = "football_team_points_full_game_total"
        with self.assertRaisesRegex(MappingRejected, "period mismatch"):
            mapper.resolve("111", Side.YES)

    def test_explicit_unique_aliases_align_participants(self):
        mapper = self.setup_mapper(fixture(kind="moneyline", line=None, labels=("Houston Astros", "Philadelphia Phillies")))
        self.us_event["teams"][0]["name"] = "Astros"
        self.us_event["markets"][0]["marketSides"][0]["description"] = "Astros"
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")

    def test_soccer_binary_winner_has_explicit_subject(self):
        mapper = self.setup_mapper(fixture(sport="soccer", league="epl", kind="moneyline", line=None, labels=("Yes", "No")))
        self.source["question"] = "Will Houston Astros win on 2026-09-10?"
        target = self.us_event["markets"][0]
        target["sportsMarketType"] = "soccer_team_full_time_winner"
        target["question"] = "Will Houston Astros win against Philadelphia Phillies in the match scheduled for Sep 10, 2026?"
        self.assertEqual(mapper.resolve("111", Side.YES), "us-market::yes")
        self.now = 301
        target["question"] = "Will Philadelphia Phillies win against Houston Astros in the match scheduled for Sep 10, 2026?"
        with self.assertRaisesRegex(MappingRejected, "subject mismatch"):
            mapper.resolve("111", Side.YES)

    def test_huge_unknown_token_is_not_sent_to_integer_market_id_endpoint(self):
        mapper = self.setup_mapper()
        self.http.get_json.side_effect = lambda *_args: []
        with self.assertRaisesRegex(MappingRejected, "not found in open or closed"):
            mapper.resolve("9" * 77, Side.YES)
        self.assertEqual(self.http.get_json.call_count, 2)

    def test_closed_market_can_be_inspected_but_never_resolved_for_trading(self):
        mapper = self.setup_mapper()
        self.source["closed"] = True
        self.us.get_events.return_value = {"events": [self.us_event]}
        self.assertEqual(mapper.inspect("111", Side.YES)["identity_matches"], 1)
        with self.assertRaisesRegex(MappingRejected, "closed; inspection only"):
            mapper.resolve("111", Side.YES)
        self.sdk.search.query.assert_not_called()


class USStreamOrientationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_uses_encoded_side_and_preserves_group_label(self):
        class Socket:
            def on(self, event, callback):
                self.callback = callback
            async def connect(self):
                pass
            async def subscribe_market_data(self, *_):
                self.callback({"marketData": {"marketSlug": "us-market", "bids": [
                    {"px": {"value": "0.60"}, "qty": "10"}], "offers": [
                    {"px": {"value": "0.62"}, "qty": "10"}]}})
            async def close(self):
                pass
        client = PolymarketUSClient("https://example", "https://example", "wss://example")
        client.market_websocket = lambda: Socket()
        leg = PredictionHuntLeg(Side.YES, Exchange.POLYMARKET, "us-market::no", None,
                                Decimal("0.4"), Decimal("10"), Decimal("0"))
        stream = PolymarketOrderbookStream(client, (leg,))._listen_us_until(datetime.now(timezone.utc) + timedelta(seconds=2))
        try:
            update = await anext(stream)
            self.assertEqual(update.side, Side.YES)
            self.assertEqual(update.best_ask.price_cents, 40)
        finally:
            await stream.aclose()
