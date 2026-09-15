"""Read-only International -> US mapping. Discovery never authorizes a trade.

There is deliberately no sport allowlist and no price-based identity inference.
Unrecognized market types use exact type/question matching. Contract identity
must be unique and exact; edge-case settlement-policy differences are warnings
unless explicit structured metadata shows a normal-play semantic conflict.
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
LEAGUE_ALIASES = {
    "ncaa football": "cfb", "ncaa-football": "cfb",
    "ncaa basketball": "cbb", "ncaa-basketball": "cbb",
    "itfme": "itf men", "itf-men": "itf men", "itf men": "itf men",
    "itfwo": "itf women", "itf-women": "itf women", "itf women": "itf women",
    "atp challenger": "challenger", "atp-challenger": "challenger",
    # Deterministic soccer feed aliases. These deliberately do not include cups,
    # lower divisions, women's, reserve, or youth competitions.
    "por": "ligpor", "ligpor": "ligpor",
    "den": "sld", "sld": "sld",
}
LEAGUE_SPORTS.update({"itf": "tennis", "itf men": "tennis", "itf women": "tennis",
                      "challenger": "tennis", "ligpor": "soccer", "sld": "soccer"})
MOVABLE_START_SPORTS = {"tennis", "cricket", "mma", "boxing"}
SPORT_ALIASES = {"american football": "football", "ice hockey": "hockey", "ice-hockey": "hockey", "american-football": "football", "mixed martial arts": "mma"}
ITF_LEAGUES = {"itf", "itf men", "itf women"}


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


def participant_name(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", text(value))
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s+-]", " ", normalized).split())


def name(value: Any) -> str:
    return participant_name(first(value, "name", "displayName", "slug")) if isinstance(value, dict) else participant_name(value)


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
    return {name(entity), *(participant_name(entity.get(k)) for k in ("alias", "safeName", "abbreviation")),
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
    league = LEAGUE_ALIASES.get(league, league)
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


def leagues_compatible(left: str, right: str, sport: str) -> bool:
    """Return deterministic league compatibility without erasing ITF gender."""
    if left == right:
        return True
    if sport != "tennis" or left not in ITF_LEAGUES or right not in ITF_LEAGUES:
        return False
    # A generic label may be completed by the other feed. Explicit men/women
    # labels remain incompatible with each other.
    return "itf" in {left, right}


@dataclass(frozen=True)
class EventFingerprint:
    sport: str
    league: str
    participants: tuple[str, ...]
    scheduled: datetime
    game_number: str
    competition: str
    event_name: str
    round: str


def event_fingerprint(event: dict, market: dict) -> EventFingerprint:
    sport, league = sport_league(event)
    scheduled = timestamp(required(first(market, "gameStartTime", "scheduledStart") or
                                   first(event, "startTime", "scheduledStart"), "scheduled time"))
    event_start = first(event, "startTime", "scheduledStart")
    if event_start and timestamp(event_start) != scheduled and not (
            sport in MOVABLE_START_SPORTS and timestamp(event_start).date() == scheduled.date()):
        raise MappingRejected("event/market scheduled time conflict")
    for field, alias in (("round", "roundNumber"), ("gameNumber", "game_number")):
        event_value, market_value = first(event, field, alias), first(market, field, alias)
        if event_value is not None and market_value is not None and text(event_value) != text(market_value):
            raise MappingRejected(f"event/market {field} conflict")
    teams = participants(event)
    title = required(text(event.get("title")), "event title")
    # Teams and precise start identify a fixture; non-matchup events also need title.
    event_name = "" if len(teams) == 2 and re.search(r"\s(?:vs\.?|versus|@)\s", title) else title
    return EventFingerprint(sport, league, teams, scheduled,
                            text(first(market, "gameNumber", "game_number") or first(event, "gameNumber", "game_number")),
                            name(first(event, "competition", "tournament", "promotion", "card")), event_name,
                            text(first(market, "round", "roundNumber") or first(event, "round", "roundNumber")))


def canonical_period(value: Any) -> str:
    value = text(value).replace("-", "_").replace(" ", "_")
    aliases = {"full_time": "full_time", "full_game": "full_game", "match": "full_game",
               "first_5": "first_five_innings", "first_5_innings": "first_five_innings",
               "first_five": "first_five_innings", "f5": "first_five_innings",
               "1st_half": "first_half", "2nd_half": "second_half"}
    for n, ordinal in enumerate(("first", "second", "third", "fourth"), 1):
        aliases[f"q{n}"] = f"{ordinal}_quarter"
        aliases[f"{n}_quarter"] = f"{ordinal}_quarter"
        aliases[f"{ordinal}_quarter"] = f"{ordinal}_quarter"
        aliases[f"{n}_period"] = f"{ordinal}_period"
        aliases[f"{n}_set"] = f"{ordinal}_set"
    return aliases.get(value, value)


def _market_kind(market: dict, sport: str) -> tuple[str, str]:
    raw = required(text(market.get("sportsMarketType")), "sportsMarketType")
    raw = raw.removeprefix("sports_market_type_")
    simple = {"moneyline": "moneyline", "winner": "moneyline", "spread": "spread",
              "spreads": "spread", "run_line": "spread", "puck_line": "spread",
              "handicap": "spread", "total": "total", "totals": "total"}
    if raw in {"tennis_match_winner", "ufc_fight_winner"}:
        return "moneyline", "full_game"
    # Period/type names differ between feeds; preserve the actual period.
    scopes = {"first_five_innings": "first_five_innings", "first_5_innings": "first_five_innings", "f5": "first_five_innings",
              "first_half": "first_half", "second_half": "second_half",
              "q1": "first_quarter", "q2": "second_quarter",
              "q3": "third_quarter", "q4": "fourth_quarter"}
    for prefix, period in scopes.items():
        if raw.startswith(prefix + "_") and raw[len(prefix)+1:] in simple:
            return simple[raw[len(prefix)+1:]], period
    if raw in simple:
        period = canonical_period(first(market, "period", "marketPeriod"))
        question = text(market.get("question"))
        scope = re.search(r"(?:first|second|third|fourth|\d+(?:st|nd|rd|th)?)[ -]+(?:five[ -]+)?(?:innings?|half|quarter|period|set|map)\b", question)
        if scope:
            if period and period != canonical_period(scope[0]):
                raise MappingRejected("conflicting market period metadata")
            period = canonical_period(scope[0])
        return simple[raw], period or ("full_time" if sport == "soccer" and raw == "moneyline" else "full_game")
    for entity_type in ("team", "game"):
        prefix = sport.replace(" ", "_") + f"_{entity_type}_"
        if raw.startswith(prefix):
            remainder = raw[len(prefix):]
            for suffix, kind in (("_winner", "moneyline"), ("_spread", "spread"), ("_total", "total")):
                if remainder.endswith(suffix):
                    return kind, canonical_period(remainder[:-len(suffix)])
    # Generic contracts retain the complete specific type. No broad PROP collapse.
    return raw, text(first(market, "period", "marketPeriod")) or raw


def market_kind(market: dict, sport: str) -> tuple[str, str]:
    kind, period = _market_kind(market, sport)
    explicit = canonical_period(first(market, "period", "marketPeriod"))
    if explicit and explicit != period:
        raise MappingRejected("conflicting market period metadata")
    return kind, period


def spread_label(label: str, event: dict) -> tuple[str, Decimal | None]:
    match = re.fullmatch(r"(.+?)\s+([+-]\d+(?:\.\d+)?)", text(label))
    if not match:
        return outcome_name(label, event), None
    participant = outcome_name(match[1], event)
    if participant not in participants(event):
        raise MappingRejected("spread outcome is not an event participant")
    return participant, number(match[2])


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
    raw_label = text(label)
    label = participant_name(label)
    matches = set()
    for entity in entities(event):
        aliases = entity_aliases(entity)
        if label and label in aliases:
            matches.add(name(entity))
    if len(matches) > 1:
        raise MappingRejected(f"ambiguous outcome alias: {label}")
    return next(iter(matches)) if matches else raw_label


def fingerprint(event: dict, market: dict, label: str, team: str = "") -> ContractFingerprint:
    event_key = event_fingerprint(event, market)
    kind, period = market_kind(market, event_key.sport)
    question = required(text(market.get("question")), "exact market question")
    line = number(market.get("line"))
    subject = name(first(market, "subject", "player", "subjectName"))
    def semantic_outcome(value: str) -> str:
        if kind == "spread":
            return spread_label(value, event)[0]
        if kind == "total":
            total = re.fullmatch(r"(over|under)\s+([+-]?\d+(?:\.\d+)?)", text(value))
            if total:
                if number(total[2]) != line:
                    raise MappingRejected("total outcome line conflicts with market line")
                return total[1]
        return outcome_name(value, event)

    outcome = semantic_outcome(required(label, "outcome name"))
    signed_label_line = spread_label(label, event)[1] if kind == "spread" else None
    universe = tuple(sorted(semantic_outcome(str(x)) for x in values(market.get("outcomes"))))
    if market.get("marketSides"):
        universe = tuple(sorted(semantic_outcome(
            name(s["team"]) if kind == "spread" and s.get("team") else s.get("description", "")
        ) for s in values(market["marketSides"])))
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
        outcomes = [spread_label(str(x), event)[0] for x in values(market.get("outcomes"))]
        if team:
            outcome = outcome_name(team, event)
            side_line = signed_label_line
            if side_line is None and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", label):
                side_line = number(label)
            if side_line is not None:
                line = side_line
            else:
                raise MappingRejected("missing critical metadata: signed US spread outcome line")
        elif outcome in event_key.participants and len(outcomes) == 2 and outcome in outcomes:
            expected_line = line if outcome == outcomes[0] else -line
            if signed_label_line is not None and signed_label_line != expected_line:
                raise MappingRejected("signed spread outcome line conflicts with market line")
            line = expected_line
        else:
            # YES/NO spreads need an identical proposition; do not guess a team.
            return ContractFingerprint(event_key, kind, period, line, subject, question, outcome, universe)
        if outcome not in event_key.participants:
            raise MappingRejected("spread outcome is not an event participant")
        question = ""
    # Generic markets compare exact question AND specific type, subject, line, outcome.
    return ContractFingerprint(event_key, kind, period, line, subject, question, outcome, universe)


def differences(a: Any, b: Any) -> list[str]:
    if isinstance(a, EventFingerprint) and isinstance(b, EventFingerprint):
        result: list[str] = []
        if a.sport != b.sport:
            result.append(f"reason=sport_conflict sport mismatch international={a.sport} us={b.sport}")
        if not leagues_compatible(a.league, b.league, a.sport) or a.sport != b.sport:
            result.append(f"reason=league_conflict league mismatch international={a.league} us={b.league}")
        if a.participants != b.participants:
            result.append(
                f"reason=participant_mismatch participants mismatch international={a.participants} us={b.participants}"
            )

        if a.scheduled != b.scheduled:
            delta = abs((a.scheduled - b.scheduled).total_seconds())
            same_date = a.scheduled.date() == b.scheduled.date()
            tennis_fixture = (
                a.sport == b.sport == "tennis"
                and a.participants == b.participants
                and leagues_compatible(a.league, b.league, "tennis")
            )
            movable = a.sport == b.sport and a.sport in MOVABLE_START_SPORTS
            if not (
                (tennis_fixture and same_date and delta <= 18 * 3600)
                or (movable and same_date and a.competition and a.competition == b.competition and delta <= 18 * 3600)
                or (not movable and same_date and delta <= 30 * 60)
            ):
                result.append(
                    f"reason=scheduled_time_conflict scheduled mismatch international={a.scheduled} us={b.scheduled}"
                )

        for field_name, reason in (
            ("game_number", "game_number_conflict"),
            ("competition", "tournament_conflict"),
            ("event_name", "event_name_conflict"),
            ("round", "round_conflict"),
        ):
            left, right = getattr(a, field_name), getattr(b, field_name)
            # Tennis tournament and round metadata are corroborating fields:
            # absence is allowed, but an explicit disagreement is never allowed.
            if a.sport == b.sport == "tennis" and field_name in {"competition", "round"}:
                mismatch = bool(left and right and left != right)
            else:
                mismatch = left != right
            if mismatch:
                result.append(
                    f"reason={reason} {field_name} mismatch international={left} us={right}"
                )
        return result

    result = []
    for field in fields(a):
        left, right = getattr(a, field.name), getattr(b, field.name)
        if left != right:
            if field.name == "scheduled" and isinstance(a, EventFingerprint):
                delta = abs((left - right).total_seconds())
                movable = a.sport == b.sport and a.sport in MOVABLE_START_SPORTS
                if left.date() == right.date() and (
                        (movable and a.competition and a.competition == b.competition and delta <= 18 * 3600)
                        or (not movable and delta <= 30 * 60)):
                    continue
            if field.name == "event":
                result.extend(differences(left, right))
            else:
                result.append(f"{field.name} mismatch international={left} us={right}")
    return result


def verify_rules(international: dict, us: dict) -> str:
    """Classify settlement differences after exact contract identity is established.

    Cancellation/postponement/LFMP and other administrative settlement differences
    are warnings only. Explicit conflicts that can change a normally played game's
    outcome remain hard rejects.
    """
    warnings: list[str] = []
    left = text(international.get("description"))
    right = text(us.get("description"))
    if left != right:
        if ("fair market price" in left) != ("fair market price" in right):
            warnings.append("last-fair-market-price treatment differs")
        elif left and right:
            warnings.append("settlement descriptions differ")
        else:
            warnings.append("settlement description missing on one venue")

    # These can change payout after a normally played game, so an explicit
    # disagreement remains a hard safety failure. Missing metadata is only a
    # warning because exact event/type/period/line/outcome identity is already
    # required independently by the fingerprint matcher.
    hard_fields = ("includesOvertime", "includesExtraInnings", "pushPolicy", "tiePolicy")
    for key in hard_fields:
        left_value, right_value = international.get(key), us.get(key)
        if left_value not in (None, "") and right_value not in (None, ""):
            if left_value != right_value:
                raise MappingRejected(f"settlement semantics mismatch: {key}")
        elif left_value != right_value:
            warnings.append(f"settlement metadata differs: {key}")

    # These primarily govern exceptional, administrative, or fallback settlement.
    # They are retained in diagnostics but do not veto an otherwise exact mapping.
    warning_fields = ("resolutionSource", "rules", "rulesDisclaimer", "voidPolicy",
                      "postponementPolicy", "cancellationPolicy", "settlementDeadline")
    for key in warning_fields:
        if international.get(key) != us.get(key):
            warnings.append(f"settlement policy differs: {key}")

    warnings = list(dict.fromkeys(warnings))
    return "exact" if not warnings else "warning: " + "; ".join(warnings)


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
        # its settlement outcome. The feed group label cannot override that token.
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
            response = self._us_read(
                "search.query",
                lambda page=page: self.us._client().search.query(
                    {"query": query, "limit": 100, "page": page}
                ),
            )
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

    def _us_read(self, operation: str, request: Callable[[], Any]) -> Any:
        """Use the venue's bounded read retry when the concrete client provides it."""
        if callable(getattr(type(self.us), "_read_only", None)):
            return self.us._read_only(operation, request)
        return request()

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
        approved = [c for c in candidates if c["rules_status"] == "exact" or
                    c["rules_status"].startswith("warning:")]
        if len(candidates) != 1 or len(approved) != 1 or report["unresolved_candidates"]:
            reasons = [c["rules_status"] for c in candidates
                       if c["rules_status"] != "exact" and not c["rules_status"].startswith("warning:")] + report["reasons"]
            if len(candidates) > 1 or len(approved) > 1:
                reasons.insert(0, "reason=ambiguous_fixture")
            details = "; ".join(dict.fromkeys(reasons))
            raise MappingRejected(f"verified_matches={len(approved)} identity_matches={len(candidates)} "
                                  f"unresolved_candidates={report['unresolved_candidates']}" +
                                  (f"; {details}" if details else "; no matching US event/contract"))
        selected = approved[0]
        if selected["rules_status"].startswith("warning:"):
            LOG.warning("international_token=%s exact identity accepted with settlement warning: %s",
                        token, selected["rules_status"].removeprefix("warning: "))
        return f"{selected['us_slug']}::{selected['us_outcome']}"

    def _inspect(self, token: str, side: Side) -> dict:
        event, market, label = self._source(token, side)
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
                    if not (all("scheduled mismatch" in e for e in event_errors) and
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
                                lambda: self._us_read(
                                    "events.retrieve_by_slug",
                                    lambda: self.us._client().events.retrieve_by_slug(slug),
                                ))
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
                            rules_status = verify_rules(market, detail)
                        except MappingRejected as exc:
                            rules_status = str(exc)
                        matches[f"{us_slug}::{us_side.value}"] = {
                            "us_slug": us_slug, "us_outcome": us_side.value,
                            "semantic_outcome": target.outcome,
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
        if len(located_events) > 1:
            uncertain = True
            reasons.insert(0, "reason=ambiguous_fixture")
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
