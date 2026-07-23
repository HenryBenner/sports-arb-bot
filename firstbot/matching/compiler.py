from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..models import Exchange, Side
from ..predictionhunt import PredictionHuntLeg, PredictionHuntOpportunity
from .entity_registry import EntityRegistry, normalize_text
from .metadata import KalshiEventBundle, PolymarketEventBundle
from .models import BitsetSettlement, CanonicalEvent, CanonicalMarket, ThresholdSettlement, TradeablePosition
from .utils import decimal_or_none, first_value, json_list, parse_datetime, stable_key


KALSHI_DATED_MATCHUP_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?(?P<matchup>[A-Z]+)$")
YES_NO = {"yes", "no"}


@dataclass(frozen=True)
class CompiledMarketSet:
    event: CanonicalEvent
    positions: tuple[TradeablePosition, ...]
    reason_codes: tuple[str, ...]


class MarketCompiler:
    def __init__(self, registry: EntityRegistry | None = None) -> None:
        self.registry = registry or EntityRegistry()

    def compile_kalshi(
        self,
        bundle: KalshiEventBundle,
        opportunity: PredictionHuntOpportunity,
    ) -> CompiledMarketSet:
        event = self._canonical_event_from_kalshi(bundle, opportunity)
        positions: list[TradeablePosition] = []
        for market in bundle.markets:
            positions.extend(self._kalshi_positions(market, event, bundle.raw_event))
        return CompiledMarketSet(event, tuple(positions), ("kalshi_event_expanded",))

    def compile_polymarket(
        self,
        bundle: PolymarketEventBundle,
        opportunity: PredictionHuntOpportunity,
    ) -> CompiledMarketSet:
        event = self._canonical_event_from_polymarket(bundle, opportunity)
        positions: list[TradeablePosition] = []
        for market in bundle.markets:
            positions.extend(self._polymarket_positions(market, event, bundle.raw_event))
        return CompiledMarketSet(event, tuple(positions), ("polymarket_event_expanded",))

    def _canonical_event_from_kalshi(
        self,
        bundle: KalshiEventBundle,
        opportunity: PredictionHuntOpportunity,
    ) -> CanonicalEvent:
        raw = _merge(bundle.raw_event, bundle.source_market, opportunity.raw)
        sport, league = _sport_league(raw, opportunity)
        participants = self._kalshi_participants(bundle.source_market, bundle.raw_event, sport, league)
        scheduled = _event_time(raw, opportunity.event_date)
        event_key = _event_key(sport, league, participants, scheduled, opportunity.group_id, opportunity.group_title)
        return CanonicalEvent(
            event_key=event_key,
            domain=_domain(sport, raw),
            sport=sport,
            league=league,
            competition=None,
            season=None,
            participant_ids=participants,
            scheduled_start=scheduled,
            game_number=_game_number(raw),
            series_scope=_series_scope(raw),
            best_of=_best_of(raw),
        )

    def _canonical_event_from_polymarket(
        self,
        bundle: PolymarketEventBundle,
        opportunity: PredictionHuntOpportunity,
    ) -> CanonicalEvent:
        raw = _merge(bundle.raw_event, bundle.source_market, opportunity.raw)
        sport, league = _sport_league(raw, opportunity)
        participants = self._polymarket_participants(bundle.source_market, sport, league)
        scheduled = _event_time(raw, opportunity.event_date)
        event_key = _event_key(sport, league, participants, scheduled, opportunity.group_id, opportunity.group_title)
        return CanonicalEvent(
            event_key=event_key,
            domain=_domain(sport, raw),
            sport=sport,
            league=league,
            competition=None,
            season=None,
            participant_ids=participants,
            scheduled_start=scheduled,
            game_number=_game_number(raw),
            series_scope=_series_scope(raw),
            best_of=_best_of(raw),
        )

    def _kalshi_positions(
        self,
        market: dict[str, Any],
        event: CanonicalEvent,
        raw_event: dict[str, Any],
    ) -> tuple[TradeablePosition, ...]:
        title = _market_text(market, raw_event)
        family, scope, period = _market_family_scope(title, market)
        subject, opponent = self._kalshi_subject_opponent(market, event)
        canonical_market = _canonical_market(market, event, family, scope, period, subject, opponent)
        settlement_yes, settlement_no, reasons = _binary_settlements(event, canonical_market, subject, opponent)
        ticker = str(first_value(market, "ticker", "market_ticker", "marketTicker") or "").strip()
        if not ticker or settlement_yes is None or settlement_no is None:
            return ()
        return (
            TradeablePosition(
                venue=Exchange.KALSHI.value,
                market_id=ticker,
                instrument_id=ticker,
                instrument_outcome=subject or "yes",
                order_action="BUY",
                side=Side.YES,
                event=event,
                market=canonical_market,
                settlement=settlement_yes,
                confidence=0.9 if subject and opponent else 0.45,
                reason_codes=tuple(reasons),
                raw={"market": market},
            ),
            TradeablePosition(
                venue=Exchange.KALSHI.value,
                market_id=ticker,
                instrument_id=ticker,
                instrument_outcome=opponent or "no",
                order_action="BUY",
                side=Side.NO,
                event=event,
                market=canonical_market,
                settlement=settlement_no,
                confidence=0.9 if subject and opponent else 0.45,
                reason_codes=tuple(reasons),
                raw={"market": market},
            ),
        )

    def _polymarket_positions(
        self,
        market: dict[str, Any],
        event: CanonicalEvent,
        raw_event: dict[str, Any],
    ) -> tuple[TradeablePosition, ...]:
        title = _market_text(market, raw_event)
        family, scope, period = _market_family_scope(title, market)
        outcomes = [str(item).strip() for item in json_list(first_value(market, "outcomes", "shortOutcomes"))]
        token_ids = _polymarket_token_ids(market)
        if not token_ids:
            return ()
        if len(outcomes) != len(token_ids):
            outcomes = outcomes[: len(token_ids)]
        participants = tuple(event.participant_ids)
        canonical_market = _canonical_market(market, event, family, scope, period, None, None)
        positions: list[TradeablePosition] = []
        if len(outcomes) >= 2 and {normalize_text(outcome) for outcome in outcomes} != YES_NO:
            universe = frozenset(self.registry.resolve(outcome, event.sport, event.league, participants, "polymarket") for outcome in outcomes)
            for outcome, token_id in zip(outcomes, token_ids):
                canonical = self.registry.resolve(outcome, event.sport, event.league, participants, "polymarket")
                if canonical is None:
                    continue
                for side in (Side.YES, Side.NO):
                    positions.append(
                        TradeablePosition(
                            venue=Exchange.POLYMARKET.value,
                            market_id=str(first_value(market, "id", "slug", "market_slug") or token_id),
                            instrument_id=token_id,
                            instrument_outcome=canonical,
                            order_action="BUY",
                            side=side,
                            event=event,
                            market=_canonical_market(market, event, family, scope, period, canonical, None),
                            settlement=BitsetSettlement(
                                universe=frozenset(state for state in universe if state),
                                winning_states=frozenset({canonical}),
                            ),
                            confidence=0.9,
                            reason_codes=("named_outcome_token", "side_label_not_settlement"),
                            raw={"market": market, "outcome": outcome},
                        )
                    )
            return tuple(positions)
        subject = _subject_from_question(title, event.participant_ids)
        opponent = _other_participant(subject, event.participant_ids)
        yes_settlement, no_settlement, reasons = _binary_settlements(event, canonical_market, subject, opponent)
        if yes_settlement is None or no_settlement is None:
            return ()
        sides = (Side.YES, Side.NO)
        settlements = (yes_settlement, no_settlement)
        labels = (subject or "yes", opponent or "no")
        return tuple(
            TradeablePosition(
                venue=Exchange.POLYMARKET.value,
                market_id=str(first_value(market, "id", "slug", "market_slug") or token_id),
                instrument_id=token_id,
                instrument_outcome=label,
                order_action="BUY",
                side=side,
                event=event,
                market=_canonical_market(market, event, family, scope, period, subject, opponent),
                settlement=settlement,
                confidence=0.8 if subject and opponent else 0.4,
                reason_codes=tuple(reasons),
                raw={"market": market},
            )
            for token_id, side, settlement, label in zip(token_ids, sides, settlements, labels)
        )

    def _kalshi_participants(
        self,
        market: dict[str, Any],
        raw_event: dict[str, Any],
        sport: str | None,
        league: str | None,
    ) -> tuple[str, ...]:
        subject, opponent = self._kalshi_subject_opponent(market, None, sport=sport, league=league)
        participants = tuple(item for item in (subject, opponent) if item)
        if participants:
            return _dedupe(participants)
        text = _market_text(market, raw_event)
        return _participants_from_text(text, self.registry, sport, league)

    def _polymarket_participants(
        self,
        market: dict[str, Any],
        sport: str | None,
        league: str | None,
    ) -> tuple[str, ...]:
        outcomes = [str(item).strip() for item in json_list(first_value(market, "outcomes", "shortOutcomes"))]
        if len(outcomes) >= 2 and {normalize_text(item) for item in outcomes} != YES_NO:
            return _dedupe(
                tuple(
                    self.registry.resolve(outcome, sport, league, platform="polymarket")
                    for outcome in outcomes
                    if outcome
                )
            )
        return _participants_from_text(_market_text(market, {}), self.registry, sport, league)

    def _kalshi_subject_opponent(
        self,
        market: dict[str, Any],
        event: CanonicalEvent | None = None,
        sport: str | None = None,
        league: str | None = None,
    ) -> tuple[str | None, str | None]:
        sport = sport or (event.sport if event else None)
        league = league or (event.league if event else None)
        participants = event.participant_ids if event else ()
        yes_label = str(first_value(market, "yes_sub_title", "yesSubtitle", "yes_title", "yesTitle") or "").strip()
        no_label = str(first_value(market, "no_sub_title", "noSubtitle", "no_title", "noTitle") or "").strip()
        subject = self.registry.resolve(yes_label, sport, league, participants, "kalshi") if yes_label else None
        opponent = self.registry.resolve(no_label, sport, league, participants, "kalshi") if no_label and normalize_text(no_label) != "no" else None
        ticker_subject, ticker_opponent = _kalshi_subject_opponent_from_ticker(
            str(first_value(market, "ticker", "market_ticker", "marketTicker") or ""),
            self.registry,
            sport,
            league,
        )
        subject = subject or ticker_subject
        opponent = opponent or ticker_opponent
        if not opponent and subject and participants:
            opponent = _other_participant(subject, participants)
        return subject, opponent


def _binary_settlements(
    event: CanonicalEvent,
    market: CanonicalMarket,
    subject: str | None,
    opponent: str | None,
) -> tuple[BitsetSettlement | None, BitsetSettlement | None, list[str]]:
    if not subject or not opponent:
        return None, None, ["missing_binary_participants"]
    if market.family not in {"two_way_winner", "binary"}:
        return None, None, [f"unsupported_family:{market.family}"]
    universe = frozenset({subject, opponent})
    return (
        BitsetSettlement(universe=universe, winning_states=frozenset({subject})),
        BitsetSettlement(universe=universe, winning_states=frozenset({opponent})),
        ["binary_two_state_market"],
    )


def _canonical_market(
    market: dict[str, Any],
    event: CanonicalEvent,
    family: str,
    scope: str,
    period: str,
    subject: str | None,
    opponent: str | None,
) -> CanonicalMarket:
    return CanonicalMarket(
        event_key=event.event_key,
        family=family,
        scope=scope,
        period=period,
        subject_id=subject,
        opponent_id=opponent,
        metric="winner" if family == "two_way_winner" else family,
        operator=None,
        threshold=decimal_or_none(first_value(market, "strike_value", "line", "threshold")),
        unit=None,
        target_value=None,
        includes_overtime=None,
        includes_extra_innings=True if event.sport == "baseball" else None,
        push_policy="refund",
        void_policy="refund",
        settlement_deadline=_event_time(market, None),
    )


def threshold_position(
    *,
    venue: Exchange,
    market_id: str,
    instrument_id: str,
    side: Side,
    event: CanonicalEvent,
    market: CanonicalMarket,
    expression: ThresholdSettlement,
) -> TradeablePosition:
    return TradeablePosition(
        venue=venue.value,
        market_id=market_id,
        instrument_id=instrument_id,
        instrument_outcome=f"{expression.variable}:{expression.operator}{expression.threshold}",
        order_action="BUY",
        side=side,
        event=event,
        market=market,
        settlement=expression,
        confidence=0.9,
        reason_codes=("threshold_expression",),
        raw={},
    )


def _kalshi_subject_opponent_from_ticker(
    ticker: str,
    registry: EntityRegistry,
    sport: str | None,
    league: str | None,
) -> tuple[str | None, str | None]:
    parts = str(ticker or "").upper().split("-")
    if len(parts) < 3:
        return None, None
    suffix = parts[-1]
    date_match = KALSHI_DATED_MATCHUP_RE.match(parts[-2])
    matchup = date_match.group("matchup") if date_match else parts[-2]
    subject = registry.resolve(suffix, sport, league, platform="kalshi")
    if not suffix:
        return subject, None
    if matchup.endswith(suffix):
        opponent_code = matchup[: -len(suffix)]
    elif matchup.startswith(suffix):
        opponent_code = matchup[len(suffix) :]
    else:
        return subject, None
    subject = registry.resolve(suffix, sport, league, platform="kalshi")
    opponent = registry.resolve(opponent_code, sport, league, platform="kalshi")
    return subject, opponent


def _participants_from_text(
    text: str,
    registry: EntityRegistry,
    sport: str | None,
    league: str | None,
) -> tuple[str, ...]:
    parts = re.split(r"\s+(?:vs\.?|v\.?|versus)\s+", text, flags=re.IGNORECASE)
    if len(parts) < 2:
        return ()
    first = registry.resolve(parts[0].split(":")[-1].strip(), sport, league)
    second = registry.resolve(parts[1].split("-")[0].strip(), sport, league)
    return _dedupe(tuple(item for item in (first, second) if item))


def _polymarket_token_ids(market: dict[str, Any]) -> tuple[str, ...]:
    token_ids = [str(item).strip() for item in json_list(market.get("clobTokenIds")) if str(item).strip()]
    token_ids.extend(str(item).strip() for item in json_list(market.get("clob_token_ids")) if str(item).strip())
    for token in json_list(market.get("tokens")):
        if isinstance(token, dict):
            for key in ("token_id", "tokenId", "clobTokenId", "id"):
                value = str(token.get(key) or "").strip()
                if value:
                    token_ids.append(value)
        else:
            value = str(token).strip()
            if value:
                token_ids.append(value)
    return tuple(dict.fromkeys(token_ids))


def _sport_league(raw: dict[str, Any], opportunity: PredictionHuntOpportunity) -> tuple[str | None, str | None]:
    text = normalize_text(" ".join(str(first_value(raw, key) or "") for key in ("title", "sub_title", "category", "event_type", "series_ticker")))
    text = f"{text} {normalize_text(opportunity.group_title)} {normalize_text(opportunity.event_type)}"
    if "mlb" in text or "baseball" in text or "xmalb" in text or "xmlb" in text:
        return "baseball", "mlb"
    if "soccer" in text or "draw" in text:
        return "soccer", None
    if "lol" in text or "league of legends" in text:
        return "esports", "lol"
    if "cs2" in text or "counter strike" in text:
        return "esports", "cs2"
    return opportunity.event_type, None


def _domain(sport: str | None, raw: dict[str, Any]) -> str:
    if sport in {"baseball", "soccer", "esports"}:
        return "sports"
    category = normalize_text(first_value(raw, "category", "event_type"))
    return category or "unknown"


def _market_family_scope(text: str, market: dict[str, Any]) -> tuple[str, str, str]:
    normalized = normalize_text(text)
    if "draw" in normalized or "three way" in normalized or "3 way" in normalized:
        return "three_way_winner", "match", "full_game"
    if "map 1" in normalized or "map one" in normalized or "game 1" in normalized:
        return "two_way_winner", "map_1", "full_game"
    if "map 2" in normalized or "game 2" in normalized:
        return "two_way_winner", "map_2", "full_game"
    if "winner" in normalized or "win" in normalized or "game" in normalized or " vs " in f" {normalized} ":
        return "two_way_winner", "match", "full_game"
    return "binary", "market", "market"


def _market_text(market: dict[str, Any], raw_event: dict[str, Any]) -> str:
    values = [
        first_value(raw_event, "title", "sub_title", "event_title"),
        first_value(market, "title", "question", "sub_title", "yes_sub_title"),
    ]
    return " ".join(str(value) for value in values if value)


def _event_time(raw: dict[str, Any], fallback: str | None) -> datetime | None:
    for key in (
        "event_date",
        "start_time",
        "scheduled_start",
        "close_time",
        "end_date",
        "expiration_time",
        "latest_expiration_time",
    ):
        parsed = parse_datetime(raw.get(key))
        if parsed:
            return parsed
    return parse_datetime(fallback)


def _event_key(
    sport: str | None,
    league: str | None,
    participants: tuple[str, ...],
    scheduled: datetime | None,
    group_id: int | None,
    title: str,
) -> str:
    if participants:
        date_part = scheduled.date().isoformat() if scheduled else "unknown_date"
        return stable_key("event", sport or "unknown", league or "unknown", date_part, *sorted(participants))
    if group_id is not None:
        return stable_key("event", group_id)
    return stable_key("event", title)


def _subject_from_question(text: str, participants: tuple[str, ...]) -> str | None:
    normalized = normalize_text(text)
    for participant in participants:
        tail = participant.split(":")[-1]
        if tail and tail in normalized.replace(" ", "_"):
            return participant
        if normalize_text(participant).replace(" ", "_") in normalized.replace(" ", "_"):
            return participant
    return participants[0] if len(participants) == 2 else None


def _other_participant(subject: str | None, participants: tuple[str, ...]) -> str | None:
    if subject is None:
        return None
    others = [participant for participant in participants if participant != subject]
    return others[0] if len(others) == 1 else None


def _merge(*items: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict):
            merged.update(item)
    return merged


def _dedupe(values: tuple[str | None, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _game_number(raw: dict[str, Any]) -> int | None:
    value = first_value(raw, "game_number", "gameNumber")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _series_scope(raw: dict[str, Any]) -> str | None:
    text = normalize_text(first_value(raw, "series", "sub_title", "title"))
    if "map" in text:
        return "map"
    if "game" in text:
        return "game"
    return None


def _best_of(raw: dict[str, Any]) -> int | None:
    text = normalize_text(first_value(raw, "title", "sub_title"))
    match = re.search(r"best of (\d+)|bo(\d+)", text)
    if not match:
        return None
    return int(match.group(1) or match.group(2))
