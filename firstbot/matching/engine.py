from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..models import Exchange, Side
from ..predictionhunt import PredictionHuntLeg, PredictionHuntOpportunity
from .compiler import CompiledMarketSet, MarketCompiler
from .entity_registry import EntityRegistry
from .metadata import KalshiMetadataResolver, PolymarketMetadataResolver
from .models import BitsetSettlement, MatchDecision, Relationship, TradeablePosition, VerifiedArbitrageStructure
from .relationship import RelationshipEngine


class MarketMatchingEngine:
    def __init__(
        self,
        kalshi: Any,
        polymarket: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        log_dir: str | Path = "logs",
        registry: EntityRegistry | None = None,
    ) -> None:
        self.kalshi_resolver = KalshiMetadataResolver(kalshi)
        self.polymarket_resolver = PolymarketMetadataResolver(polymarket)
        self.compiler = MarketCompiler(registry)
        self.relationships = RelationshipEngine()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log_dir = Path(log_dir)

    def verify_predictionhunt_opportunity(
        self,
        opportunity: PredictionHuntOpportunity,
    ) -> VerifiedArbitrageStructure:
        compiled: list[CompiledMarketSet] = []
        errors: list[str] = []
        kalshi_leg = next((leg for leg in opportunity.legs if leg.platform is Exchange.KALSHI), None)
        polymarket_leg = next((leg for leg in opportunity.legs if leg.platform is Exchange.POLYMARKET), None)
        if kalshi_leg is not None:
            try:
                compiled.append(
                    self.compiler.compile_kalshi(self.kalshi_resolver.resolve(kalshi_leg.market_id), opportunity)
                )
            except Exception as exc:
                errors.append(f"kalshi_metadata_failed:{exc}")
        if polymarket_leg is not None:
            try:
                compiled.append(
                    self.compiler.compile_polymarket(
                        self.polymarket_resolver.resolve(polymarket_leg),
                        opportunity,
                    )
                )
            except Exception as exc:
                errors.append(f"polymarket_metadata_failed:{exc}")
        positions = tuple(
            position
            for item in compiled
            for position in item.positions
            if _position_matches_any_leg(position, opportunity.legs)
        )
        decisions: list[tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side], MatchDecision]] = []
        approved: set[tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]] = set()
        outcome_keys: dict[tuple[Exchange, str, Side], str] = {}
        for position in positions:
            outcome_keys[position.leg_key] = _settlement_key(position)
        for first in positions:
            for second in positions:
                if first.venue == second.venue:
                    continue
                if Exchange(first.venue) is not Exchange.POLYMARKET:
                    continue
                decision = self.relationships.compare(first, second)
                pair = _pair_key(first.leg_key, second.leg_key)
                decisions.append((first.leg_key, second.leg_key, decision))
                if decision.tradable_as_arb and decision.confidence >= 0.75 and not decision.hard_conflicts:
                    approved.add(pair)
        reason_codes = tuple(dict.fromkeys([*errors, *[reason for item in compiled for reason in item.reason_codes]]))
        event_key = _single_event_key(positions) or f"unmatched:{opportunity.group_id or opportunity.group_title}"
        verified = VerifiedArbitrageStructure(
            event_key=event_key,
            legs=tuple(_leg_key(leg) for leg in opportunity.legs),
            settlement_states=tuple(sorted({key for key in outcome_keys.values() if key})),
            confidence=max((decision.confidence for _, _, decision in decisions), default=0.0),
            reason_codes=reason_codes,
            positions=positions,
            decisions=tuple(decisions),
            approved_pairs=frozenset(approved),
            outcome_keys=outcome_keys,
        )
        self.write_audit(opportunity, verified)
        return verified

    def write_audit(
        self,
        opportunity: PredictionHuntOpportunity,
        verified: VerifiedArbitrageStructure,
    ) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / "hot_match_decisions.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(_audit_record(self.clock().isoformat(), opportunity, verified), sort_keys=True) + "\n")
        except OSError:
            return


def _position_matches_any_leg(position: TradeablePosition, legs: tuple[PredictionHuntLeg, ...]) -> bool:
    for leg in legs:
        if Exchange(position.venue) is not leg.platform:
            continue
        if position.instrument_id == leg.market_id and position.side is leg.side:
            return True
        if Exchange(position.venue) is Exchange.KALSHI and position.market_id == leg.market_id and position.side is leg.side:
            return True
    return False


def _settlement_key(position: TradeablePosition) -> str:
    settlement = position.settlement
    if isinstance(settlement, BitsetSettlement):
        return "|".join(sorted(settlement.winning_states))
    return position.instrument_outcome


def _single_event_key(positions: tuple[TradeablePosition, ...]) -> str | None:
    keys = {position.event.event_key for position in positions}
    return next(iter(keys)) if len(keys) == 1 else None


def _leg_key(leg: PredictionHuntLeg) -> tuple[Exchange, str, Side]:
    return leg.platform, leg.market_id, leg.side


def _pair_key(
    first: tuple[Exchange, str, Side],
    second: tuple[Exchange, str, Side],
) -> tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]:
    return tuple(
        sorted(
            (first, second),
            key=lambda item: (item[0].value, item[1], item[2].value),
        )
    )


def _audit_record(
    timestamp: str,
    opportunity: PredictionHuntOpportunity,
    verified: VerifiedArbitrageStructure,
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "group_id": opportunity.group_id,
        "group_title": opportunity.group_title,
        "event_date": opportunity.event_date,
        "event_type": opportunity.event_type,
        "event_key": verified.event_key,
        "confidence": verified.confidence,
        "approved_pairs": [_pair_record(pair) for pair in verified.approved_pairs],
        "reason_codes": list(verified.reason_codes),
        "legs": [
            {"exchange": leg[0].value, "market_id": leg[1], "side": leg[2].value}
            for leg in verified.legs
        ],
        "positions": [_position_record(position) for position in verified.positions],
        "decisions": [
            {
                "first": _leg_record(first),
                "second": _leg_record(second),
                "relationship": decision.relationship.value,
                "tradable_as_arb": decision.tradable_as_arb,
                "confidence": decision.confidence,
                "reason_codes": list(decision.reason_codes),
                "hard_conflicts": list(decision.hard_conflicts),
            }
            for first, second, decision in verified.decisions
        ],
    }


def _position_record(position: TradeablePosition) -> dict[str, Any]:
    return {
        "venue": position.venue,
        "market_id": position.market_id,
        "instrument_id": position.instrument_id,
        "instrument_outcome": position.instrument_outcome,
        "side": position.side.value,
        "event_key": position.event.event_key,
        "family": position.market.family,
        "scope": position.market.scope,
        "period": position.market.period,
        "confidence": position.confidence,
        "reason_codes": list(position.reason_codes),
    }


def _pair_record(pair: tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]) -> list[dict[str, str]]:
    return [_leg_record(leg) for leg in pair]


def _leg_record(leg: tuple[Exchange, str, Side]) -> dict[str, str]:
    return {"exchange": leg[0].value, "market_id": leg[1], "side": leg[2].value}
