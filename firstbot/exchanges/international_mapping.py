"""Read-only International -> US mapping. Discovery never authorizes a trade.

There is deliberately no sport allowlist and no price-based identity inference.
Unrecognized market types use exact type/question matching. Rules which cannot
be proven identical are rejected, even when normal-play outcomes look alike.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import re
from threading import RLock
import time
import unicodedata
from typing import Any, Callable
from urllib.parse import quote

from ..http import HttpClient
from ..models import Side


LOG = logging.getLogger(__name__)

# Taxonomy aliases, not a sport allowlist. Unknown leagues still work when their
# metadata supplies the sport explicitly. Gamma often uses a league as `sport`.
LEAGUE_SPORTS = {"mlb": "baseball", "npb": "baseball", "kbo": "baseball",
                 "nfl": "football", "cfb": "football", "nba": "basketball",
                 "wnba": "basketball", "cbb": "basketball", "nhl": "hockey",
                 "atp": "tennis", "wta": "tennis", "ufc": "mma"}
SPORT_ALIASES = {"american football": "football", "ice hockey": "hockey", "mixed martial arts": "mma"}


class MappingRejected(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(f"mapping rejected: {reason}")


def text(value: Any) -> str:
    # Preserve numbers, minus signs, accents, and operators: these carry meaning.
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def values(value: Any) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def required(value: Any, name: str) -> Any:
    if value is None or value == "" or value == () or value == []:
        raise MappingRejected(f"missing critical metadata: {name}")
    return value


def first(data: dict, *names: str) -> Any:
    return next((data[n] for n in names if data.get(n) not in (None, "", [])), None)


def name(value: Any) -> str:
    return text(first(value, "name", "displayName", "slug")) if isinstance(value, dict) else text(value)


def timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise MappingRejected("missing critical metadata: timezone-aware scheduled time") from None


def number(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        if result.is_finite():
            return result
    except InvalidOperation:
        pass
    raise MappingRejected(f"invalid line/threshold: {value!r}")


def entities(event: dict) -> list[dict]:
    result = values(event.get("teams")) or values(event.get("participants"))
    return [x if isinstance(x, dict) else {"name": x} for x in result]


def entity_aliases(entity: dict) -> set[str]:
    return {name(entity), *(text(entity.get(k)) for k in ("alias", "safeName", "abbreviation")),
            *entity.get("_mapping_aliases", [])} - {""}


def align_participants(source: dict, target: dict) -> dict:
    """Use only explicit, uniquely matching aliases within this fixture."""
    left, right = entities(source), entities(target)
    if len(left) != len(right) or not left:
        return target
    mapped, seen = [], set()
    for entity in right:
        matches = [s for s in left if entity_aliases(s) & entity_aliases(entity)]
        if len(matches) != 1 or name(matches[0]) in seen:
            return target
        canonical = name(matches[0])
        mapped.append({**entity, "name": canonical, "_mapping_aliases": sorted(entity_aliases(entity))})
        seen.add(canonical)
    return {**target, "teams": mapped}


def participants(event: dict) -> tuple[str, ...]:
    result = [name(x) for x in entities(event)]
    if not result:
        # Deterministic matchup parsing; this is not fuzzy title similarity.
        result = re.split(r"\s+(?:vs\.?|versus|@)\s+", text(event.get("title")))
        if len(result) != 2:
            result = []
    result = [x for x in result if x]
    if len(set(result)) != len(result):
        raise MappingRejected("ambiguous event participants")
    return tuple(sorted(required(result, "participants")))


def sport_league(event: dict) -> tuple[str, str]:
    tags = [t for t in values(event.get("tags")) if isinstance(t, dict)]
    league = name(event.get("league"))
    if not league:
        leagues = {text(first(t["league"], "slug", "abbreviation", "name")) for t in tags if isinstance(t.get("league"), dict)}
        leagues.update(text(x.get("league")) for x in entities(event) if x.get("league"))
        if len(leagues) == 1:
            league = leagues.pop()
        elif len(leagues) > 1:
            raise MappingRejected("conflicting event leagues")
    if not league:
        league = re.sub(r"-20\d\d$", "", text(event.get("seriesSlug")))
    raw_sport = event.get("sport")
    sport = name(raw_sport)
    is_gamma_league = isinstance(raw_sport, dict) and "sport" in raw_sport and "primaryTagId" in raw_sport
    if not sport or sport == league or is_gamma_league:
        # Gamma may put a league code (e.g. mlb) in `sport`; its sport tag
        # supplies the actual sport, while the league remains a separate field.
        sport = ""
        sports = {name(t["sport"]) for t in tags if isinstance(t.get("sport"), dict)}
        if not sports:
            # Gamma's top-level tags carry the sport and league as plain slugs.
            tag_slugs = {text(t.get("slug")) for t in tags}
            known_sports = tag_slugs & {"baseball", "football", "basketball", "hockey", "tennis", "mma", "soccer", "cricket", "rugby", "volleyball", "boxing", "esports"}
            sports = known_sports or ({LEAGUE_SPORTS[league]} if league in LEAGUE_SPORTS else
                                     tag_slugs - {"", "sports", "esports", "games", league})
        if len(sports) == 1:
            sport = sports.pop()
    return required(SPORT_ALIASES.get(sport, sport), "sport"), required(league, "league/competition")


@dataclass(frozen=True)
class EventFingerprint:
    sport: str
    league: str
    participants: tuple[str, ...]
    scheduled: datetime
    game_number: str
    competition: str
    event_name: str


def event_fingerprint(event: dict, market: dict) -> EventFingerprint:
    sport, league = sport_league(event)
    scheduled = timestamp(required(first(market, "gameStartTime", "scheduledStart") or
                                   first(event, "startTime", "scheduledStart"), "scheduled time"))
    event_start = first(event, "startTime", "scheduledStart")
    if event_start and timestamp(event_start) != scheduled:
        raise MappingRejected("event/market scheduled time conflict")
    teams = participants(event)
    title = required(text(event.get("title")), "event title")
    # Teams and precise start identify a fixture; non-matchup events also need title.
    event_name = "" if len(teams) == 2 and re.search(r"\s(?:vs\.?|versus|@)\s", title) else title
    return EventFingerprint(sport, league, teams, scheduled,
                            text(first(event, "gameNumber", "game_number")),
                            name(event.get("competition")), event_name)


def market_kind(market: dict, sport: str) -> tuple[str, str]:
    raw = required(text(market.get("sportsMarketType")), "sportsMarketType")
    raw = raw.removeprefix("sports_market_type_")
    simple = {"moneyline": "moneyline", "winner": "moneyline", "spread": "spread",
              "spreads": "spread", "total": "total", "totals": "total"}
    if raw in {"tennis_match_winner", "ufc_fight_winner"}:
        return "moneyline", "full_game"
    # Period/type names differ between feeds; preserve the actual period.
    scopes = {"first_half": "first_half", "second_half": "second_half",
              "q1": "first_quarter", "q2": "second_quarter",
              "q3": "third_quarter", "q4": "fourth_quarter"}
    for prefix, period in scopes.items():
        if raw.startswith(prefix + "_") and raw[len(prefix)+1:] in simple:
            return simple[raw[len(prefix)+1:]], period
    if raw in simple:
        period = text(first(market, "period", "marketPeriod"))
        question = text(market.get("question"))
        scope = re.search(r"(?:first|second|third|fourth|\d+(?:st|nd|rd|th)?)[ -]+(?:five[ -]+)?(?:innings?|half|quarter|period|set|map)\b", question)
        if scope:
            if period and period != scope[0]:
                raise MappingRejected("conflicting market period metadata")
            period = scope[0]
        return simple[raw], period or ("full_time" if sport == "soccer" and raw == "moneyline" else "full_game")
    for entity_type in ("team", "game"):
        prefix = sport.replace(" ", "_") + f"_{entity_type}_"
        if raw.startswith(prefix):
            remainder = raw[len(prefix):]
            for suffix, kind in (("_winner", "moneyline"), ("_spread", "spread"), ("_total", "total")):
                if remainder.endswith(suffix):
                    return kind, remainder[:-len(suffix)]
    # Generic contracts retain the complete specific type. No broad PROP collapse.
    return raw, text(first(market, "period", "marketPeriod")) or raw


@dataclass(frozen=True)
class ContractFingerprint:
    event: EventFingerprint
    market_type: str
    period: str
    line: Decimal | None
    subject: str
    question: str
    outcome: str
    outcome_universe: tuple[str, ...]


def outcome_name(label: str, event: dict) -> str:
    label = text(label)
    matches = set()
    for entity in entities(event):
        aliases = entity_aliases(entity)
        if label and label in aliases:
            matches.add(name(entity))
    if len(matches) > 1:
        raise MappingRejected(f"ambiguous outcome alias: {label}")
    return next(iter(matches)) if matches else label


def fingerprint(event: dict, market: dict, label: str, team: str = "") -> ContractFingerprint:
    event_key = event_fingerprint(event, market)
    kind, period = market_kind(market, event_key.sport)
    question = required(text(market.get("question")), "exact market question")
    line = number(market.get("line"))
    subject = name(first(market, "subject", "player", "subjectName"))
    outcome = outcome_name(required(label, "outcome name"), event)
    universe = tuple(sorted(outcome_name(str(x), event) for x in values(market.get("outcomes"))))
    if market.get("marketSides"):
        universe = tuple(sorted(outcome_name(s.get("team") and name(s["team"]) or s.get("description", ""), event)
                                if kind == "spread" else outcome_name(s.get("description", ""), event)
                                for s in values(market["marketSides"])))
    if len(universe) != 2 or len(set(universe)) != 2:
        raise MappingRejected("missing or ambiguous contract outcome universe")
    if kind == "moneyline" and outcome in event_key.participants:
        if team and outcome != outcome_name(team, event):
            raise MappingRejected("US outcome name conflicts with its team metadata")
        # Named winner markets are comparable without identical question wording.
        question = ""
    elif kind == "moneyline" and outcome in {"yes", "no"}:
        winner = re.fullmatch(r"will (?:the )?(.+?) win (?:on|against) .+\?", question)
        winner_name = outcome_name(winner[1], event) if winner else ""
        if winner_name in event_key.participants:
            if team and winner_name != outcome_name(team, event):
                raise MappingRejected("winner proposition conflicts with team metadata")
            subject, question = winner_name, ""
        elif re.fullmatch(r"will .+ end in a draw\?", question):
            subject, question = "draw", ""
    elif kind == "total" and outcome in {"over", "under"}:
        required(line, "total line")
        question = ""
    elif kind == "spread":
        required(line, "spread line")
        outcomes = [outcome_name(str(x), event) for x in values(market.get("outcomes"))]
        if team:
            outcome = outcome_name(team, event)
            side_line = number(label) if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", label) else None
            if side_line is not None:
                line = side_line
            else:
                raise MappingRejected("missing critical metadata: signed US spread outcome line")
        elif outcome in event_key.participants and len(outcomes) == 2 and outcome in outcomes:
            line = line if outcome == outcomes[0] else -line
        else:
            # YES/NO spreads need an identical proposition; do not guess a team.
            return ContractFingerprint(event_key, kind, period, line, subject, question, outcome, universe)
        if outcome not in event_key.participants:
            raise MappingRejected("spread outcome is not an event participant")
        question = ""
    # Generic markets compare exact question AND specific type, subject, line, outcome.
    return ContractFingerprint(event_key, kind, period, line, subject, question, outcome, universe)


def differences(a: Any, b: Any) -> list[str]:
    result = []
    for field in fields(a):
        left, right = getattr(a, field.name), getattr(b, field.name)
        if left != right:
            if field.name == "event":
                result.extend(differences(left, right))
            else:
                result.append(f"{field.name} mismatch international={left} us={right}")
    return result


def verify_rules(international: dict, us: dict) -> None:
    left = required(text(international.get("description")), "International settlement rules")
    right = required(text(us.get("description")), "US settlement rules")
    if left != right:
        if ("fair market price" in left) != ("fair market price" in right):
            raise MappingRejected("settlement rules mismatch: last-fair-market-price treatment differs")
        raise MappingRejected("settlement rules equivalence unverified: descriptions differ")
    # Identical prose cannot override a conflicting structured rule or disclaimer.
    for key in ("resolutionSource", "rules", "rulesDisclaimer", "includesOvertime",
                "includesExtraInnings", "pushPolicy", "voidPolicy", "tiePolicy",
                "postponementPolicy", "cancellationPolicy", "settlementDeadline"):
        if international.get(key) != us.get(key):
            raise MappingRejected(f"settlement rules mismatch: {key}")


def us_outcomes(market: dict) -> list[tuple[Side, str, str]]:
    sides = values(market.get("marketSides"))
    if len(sides) != 2 or any(not isinstance(s, dict) or type(s.get("long")) is not bool for s in sides):
        raise MappingRejected("missing critical metadata: two explicit US long/short marketSides")
    if {s["long"] for s in sides} != {True, False}:
        raise MappingRejected("ambiguous US long/short sides")
    if "spread" in text(market.get("sportsMarketType")):
        line = number(market.get("line"))
        for side in sides:
            label = text(side.get("description"))
            if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", label):
                if line is None or number(label) != (line if side["long"] else -line):
                    raise MappingRejected("US signed outcome line conflicts with market line")
    return [(Side.YES if s["long"] else Side.NO,
             required(text(s.get("description")), "US outcome description"), name(s.get("team"))) for s in sides]


class InternationalMarketMapper:
    def __init__(self, us: Any, gamma_url: str = "https://gamma-api.polymarket.com",
                 http: HttpClient | None = None, clock: Callable[[], float] = time.monotonic,
                 success_ttl: float = 300, failure_ttl: float = 30, max_entries: int = 2048):
        self.us = us
        self.gamma_url = gamma_url.rstrip("/")
        self.http = http or HttpClient(timeout=15, retries=0)
        self.clock = clock
        self.success_ttl, self.failure_ttl = success_ttl, failure_ttl
        self.max_entries = max_entries
        self._cache: OrderedDict[Any, tuple[float, Any, float]] = OrderedDict()
        self._read_times: list[float] = []
        self._lock = RLock()

    def _cached(self, key: Any, ttl: float, fetch: Callable[[], Any]) -> Any:
        cached = self._cache.get(key)
        if cached and cached[0] > self.clock():
            self._cache.move_to_end(key)
            if self._read_times:
                self._read_times[-1] = min(self._read_times[-1], cached[2])
            if isinstance(cached[1], MappingRejected):
                raise cached[1]
            return cached[1]
        self._read_times.append(self.clock())
        try:
            value = fetch()
        except Exception as exc:
            value = exc if isinstance(exc, MappingRejected) else MappingRejected(f"metadata lookup failed: {exc}")
            ttl = self.failure_ttl
        observed_at = self._read_times.pop()
        if self._read_times:
            self._read_times[-1] = min(self._read_times[-1], observed_at)
        # A derived mapping cannot renew the age of the metadata it relied on.
        expires = observed_at + ttl if key[0] == "mapping" and not isinstance(value, MappingRejected) else self.clock() + ttl
        self._cache[key] = (expires, value, observed_at)
        self._cache.move_to_end(key)
        while len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)
        if isinstance(value, MappingRejected):
            raise value
        return value

    def resolve(self, token: str, side: Side) -> str:
        # Side is a PredictionHunt group label; a numeric token already identifies
        # its settlement outcome. For literal YES/NO, conflicting labels fail closed.
        with self._lock:
            return self._cached(("mapping", token, side), self.success_ttl,
                                lambda: self._resolve_logged(token, side))

    def _resolve_logged(self, token: str, side: Side) -> str:
        try:
            result = self._resolve(token, side)
        except Exception as exc:
            LOG.warning("international_token=%s %s", token, exc)
            raise
        LOG.info("mapped international_token=%s -> us_slug=%s outcome=%s confidence=exact",
                 token, *result.rsplit("::", 1))
        return result

    def _source(self, token: str, side: Side) -> tuple[dict, dict, str]:
        raw = self.http.get_json(f"{self.gamma_url}/markets", {"clob_token_ids": token})
        markets = values(raw) if isinstance(raw, list) else values(raw.get("markets")) if isinstance(raw, dict) else []
        matched = [m for m in markets if isinstance(m, dict) and token in [str(x) for x in values(m.get("clobTokenIds"))]]
        market_id_lookup = False
        if not matched:
            raw = self.http.get_json(f"{self.gamma_url}/markets", {"clob_token_ids": token, "closed": "true"})
            rows = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
            matched = [m for m in rows if isinstance(m, dict) and token in [str(x) for x in values(m.get("clobTokenIds"))]]
        if not matched:
            # PredictionHunt also sends Gamma market IDs. Verify the returned ID;
            # a short numeric string is not itself evidence of a token or market.
            if not token.isdigit() or int(token) > 2**63 - 1:
                raise MappingRejected("International outcome token not found in open or closed markets")
            raw = self.http.get_json(f"{self.gamma_url}/markets", {"id": token})
            rows = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
            matched = [m for m in rows if isinstance(m, dict) and str(m.get("id")) == token]
            market_id_lookup = True
        if len(matched) != 1:
            raise MappingRejected(f"International token lookup requires one market, found {len(matched)}")
        market = matched[0]
        tokens = [str(x) for x in values(market.get("clobTokenIds"))]
        outcomes = values(market.get("outcomes"))
        if len(tokens) != 2 or len(set(tokens)) != 2 or len(outcomes) != 2 or len(set(map(text, outcomes))) != 2:
            raise MappingRejected("ambiguous International token/outcome metadata")
        if market_id_lookup:
            normalized = list(map(text, outcomes))
            if set(normalized) != {"yes", "no"}:
                raise MappingRejected("market ID has named outcomes: explicit intended outcome/token required")
            label = normalized[normalized.index(side.value)]
        else:
            label = text(outcomes[tokens.index(token)])
        events = values(market.get("events"))
        if len(events) != 1 or not isinstance(events[0], dict) or not events[0].get("slug"):
            raise MappingRejected("missing or ambiguous International parent event")
        slug = str(events[0]["slug"])
        event = self._cached(("international_event", slug), self.failure_ttl,
                             lambda: self.http.get_json(f"{self.gamma_url}/events/slug/{quote(slug, safe='')}"))
        if not isinstance(event, dict) or str(event.get("slug")) != slug:
            raise MappingRejected("International parent event identity mismatch")
        return event, market, label

    def _search(self, query: str) -> list[dict]:
        events: dict[str, dict] = {}
        # One event search, paginated to exhaustion. Never approve a truncated set.
        for page in range(1, 21):
            response = self.us._client().search.query({"query": query, "limit": 100, "page": page})
            if not isinstance(response, dict) or not isinstance(response.get("events"), list):
                raise MappingRejected("invalid US event search response")
            batch = response["events"]
            for event in batch:
                if not isinstance(event, dict) or not event.get("slug"):
                    raise MappingRejected("US search event missing identity")
                slug = str(event["slug"])
                if slug in events:
                    raise MappingRejected("US event search pagination repeated an event")
                events[slug] = event
            if len(batch) < 100:
                return list(events.values())
        raise MappingRejected("US event search incomplete: pagination limit")

    def inspect(self, token: str, side: Side) -> dict:
        """Read-only identity and rules evidence; this never authorizes an order."""
        with self._lock:
            return self._cached(("inspection", token, side), self.failure_ttl,
                                lambda: self._inspect(token, side))

    def _resolve(self, token: str, side: Side) -> str:
        report = self.inspect(token, side)
        if report["international_closed"]:
            raise MappingRejected("International source market is closed; inspection only")
        candidates = report["candidates"]
        approved = [c for c in candidates if c["rules_status"] == "exact"]
        if len(candidates) != 1 or len(approved) != 1 or report["unresolved_candidates"]:
            reasons = [c["rules_status"] for c in candidates if c["rules_status"] != "exact"] + report["reasons"]
            details = "; ".join(dict.fromkeys(reasons))
            raise MappingRejected(f"verified_matches={len(approved)} identity_matches={len(candidates)} "
                                  f"unresolved_candidates={report['unresolved_candidates']}" +
                                  (f"; {details}" if details else "; no matching US event/contract"))
        return f"{approved[0]['us_slug']}::{approved[0]['us_outcome']}"

    def _inspect(self, token: str, side: Side) -> dict:
        event, market, label = self._source(token, side)
        if label in {"yes", "no"} and label != side.value:
            raise MappingRejected(f"token outcome conflicts with PredictionHunt side: token={label} side={side.value}")
        source = fingerprint(event, market, label)
        names = [str(first(e, "name", "displayName") or "").strip() for e in entities(event)]
        query = (" vs. ".join(names) if len(names) == 2 and all(names) else
                 required(str(event.get("title") or "").strip(), "event discovery name"))
        if market.get("closed") is True:
            # US search currently omits archived events even with status=closed.
            # Inspect historical samples through the documented league/date list.
            candidates = self._cached(("archive", source.event.league, source.event.scheduled.date()),
                                      self.failure_ttl, lambda: self._archived_events(source.event))
        else:
            candidates = self._cached(("search", query), self.failure_ttl, lambda: self._search(query))
        matches: dict[str, dict] = {}
        near_matches: dict[str, dict] = {}
        reasons: list[str] = []
        discovery_reasons: list[str] = []
        located_events: list[str] = []
        same_type_markets = 0
        uncertain = False
        for candidate in candidates:
            candidate = align_participants(event, candidate)
            try:
                candidate_key = event_fingerprint(candidate, {})
                event_errors = differences(source.event, candidate_key)
                if event_errors:
                    discovery_reasons.extend(event_errors)
                    # Show same-fixture candidates with discrepant times for
                    # diagnostics only. They can never enter the approved set.
                    if not (all(e.startswith("scheduled mismatch") for e in event_errors) and
                            source.event.scheduled.date() == candidate_key.scheduled.date()):
                        continue
                    reasons.extend(event_errors)
            except MappingRejected as exc:
                # Incomplete discovery results cannot establish uniqueness.
                uncertain = True
                reasons.append(str(exc))
                continue
            slug = str(candidate["slug"])
            located_events.append(slug)
            full = self._cached(("us_event", slug), self.failure_ttl,
                                lambda: self.us._client().events.retrieve_by_slug(slug))
            if isinstance(full, dict) and isinstance(full.get("event"), dict):
                full = full["event"]
            if not isinstance(full, dict) or str(full.get("slug")) != slug or not isinstance(full.get("markets"), list):
                raise MappingRejected("US parent event response missing identity/markets")
            full = align_participants(event, full)
            if differences(candidate_key, event_fingerprint(full, {})):
                raise MappingRejected("US event metadata changed during lookup")
            for item in full["markets"]:
                if not isinstance(item, dict) or not item.get("slug"):
                    raise MappingRejected("US associated market missing identity")
                us_slug = str(item["slug"])
                detail = item
                if any(not item.get(k) for k in ("question", "description", "sportsMarketType", "marketSides", "gameStartTime")):
                    detail = self._cached(("us_market", us_slug), self.failure_ttl,
                                          lambda: self.us.get_market_by_slug(us_slug))
                if str(detail.get("slug")) != us_slug:
                    raise MappingRejected("US market identity changed during lookup")
                # Definite type/line conflicts can eliminate incomplete candidates.
                try:
                    kind, period = market_kind(detail, source.event.sport)
                    if (kind, period) != (source.market_type, source.period):
                        reasons.append(f"market_type/period mismatch international={source.market_type}/{source.period} us={kind}/{period}")
                        continue
                    same_type_markets += 1
                    for us_side, us_label, team in us_outcomes(detail):
                        target = fingerprint(full, detail, us_label, team)
                        errors = differences(source, target)
                        if errors:
                            reasons.extend(errors)
                            if event_errors and errors == event_errors:
                                near_matches[f"{us_slug}::{us_side.value}"] = {
                                    "us_slug": us_slug, "us_outcome": us_side.value, "mismatches": errors,
                                    "us_question": detail.get("question"),
                                }
                            continue
                        try:
                            verify_rules(market, detail)
                            rules_status = "exact"
                        except MappingRejected as exc:
                            rules_status = str(exc)
                        matches[f"{us_slug}::{us_side.value}"] = {
                            "us_slug": us_slug, "us_outcome": us_side.value,
                            "rules_status": rules_status, "us_question": detail.get("question"),
                            "international_rules": market.get("description"), "us_rules": detail.get("description"),
                        }
                except MappingRejected as exc:
                    uncertain = True
                    reasons.append(str(exc))
        if not located_events:
            reasons.extend(discovery_reasons)
        elif same_type_markets == 0:
            reasons = [f"no US market with type={source.market_type} period={source.period} in located event"] + reasons
        else:
            reasons = [r for r in reasons if not r.startswith("market_type/period mismatch")]
        unique = list(dict.fromkeys(reasons))
        unique.sort(key=lambda r: ("settlement" not in r, "line mismatch" not in r))
        return {"international_id": token, "international_question": market.get("question"),
                "international_closed": market.get("closed") is True,
                "fingerprint": asdict(source), "discovery_query": query,
                "located_events": located_events, "same_type_markets": same_type_markets,
                "identity_matches": len(matches), "candidates": list(matches.values()),
                "near_matches": list(near_matches.values()),
                "unresolved_candidates": uncertain, "reasons": unique[:12], "diagnostic_only": True}

    def _archived_events(self, event: EventFingerprint) -> list[dict]:
        start = event.scheduled.replace(hour=0, minute=0, second=0, microsecond=0)
        found = {}
        for page in range(20):
            response = self.us.get_events(closed=True, tagSlug=event.league, limit=100, offset=page * 100,
                                          startTimeMin=start.isoformat(), startTimeMax=(start + timedelta(days=1)).isoformat())
            if not isinstance(response, dict) or not isinstance(response.get("events"), list):
                raise MappingRejected("invalid US archived event response")
            batch = response["events"]
            for item in batch:
                if not isinstance(item, dict) or not item.get("slug") or item["slug"] in found:
                    raise MappingRejected("incomplete or repeated archived event pagination")
                found[item["slug"]] = item
            if len(batch) < 100:
                return list(found.values())
        raise MappingRejected("US archived event search incomplete")
