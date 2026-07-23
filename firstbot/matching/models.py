from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ..models import Exchange, Side


class Relationship(str, Enum):
    SAME_EXPOSURE = "SAME_EXPOSURE"
    EXACT_COMPLEMENTS = "EXACT_COMPLEMENTS"
    GUARANTEED_COVER = "GUARANTEED_COVER"
    PARTIAL_OVERLAP = "PARTIAL_OVERLAP"
    MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE = "MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE"
    SETTLEMENT_MISMATCH = "SETTLEMENT_MISMATCH"
    UNRELATED = "UNRELATED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CanonicalEvent:
    event_key: str
    domain: str
    sport: str | None
    league: str | None
    competition: str | None
    season: str | None
    participant_ids: tuple[str, ...]
    scheduled_start: datetime | None
    game_number: int | None
    series_scope: str | None
    best_of: int | None


@dataclass(frozen=True)
class CanonicalMarket:
    event_key: str
    family: str
    scope: str
    period: str
    subject_id: str | None
    opponent_id: str | None
    metric: str
    operator: str | None
    threshold: Decimal | None
    unit: str | None
    target_value: str | None
    includes_overtime: bool | None
    includes_extra_innings: bool | None
    push_policy: str
    void_policy: str
    settlement_deadline: datetime | None


@dataclass(frozen=True)
class BitsetSettlement:
    universe: frozenset[str]
    winning_states: frozenset[str]
    void_states: frozenset[str] = frozenset({"VOID"})
    push_states: frozenset[str] = frozenset()
    refund_on_void: bool = True
    refund_on_push: bool = True


@dataclass(frozen=True)
class ThresholdSettlement:
    variable: str
    operator: str
    threshold: Decimal
    subject_id: str | None
    opponent_id: str | None
    push_policy: str
    integer_valued: bool = True


SettlementExpression = BitsetSettlement | ThresholdSettlement


@dataclass(frozen=True)
class TradeablePosition:
    venue: str
    market_id: str
    instrument_id: str
    instrument_outcome: str
    order_action: str
    side: Side
    event: CanonicalEvent
    market: CanonicalMarket
    settlement: SettlementExpression | None
    confidence: float
    reason_codes: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def leg_key(self) -> tuple[Exchange, str, Side]:
        return (Exchange(self.venue), self.instrument_id, self.side)


@dataclass(frozen=True)
class MatchDecision:
    relationship: Relationship
    tradable_as_arb: bool
    confidence: float
    reason_codes: tuple[str, ...]
    hard_conflicts: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedArbitrageStructure:
    event_key: str
    legs: tuple[tuple[Exchange, str, Side], ...]
    settlement_states: tuple[str, ...]
    confidence: float
    reason_codes: tuple[str, ...]
    positions: tuple[TradeablePosition, ...]
    decisions: tuple[tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side], MatchDecision], ...]
    approved_pairs: frozenset[tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]]
    outcome_keys: dict[tuple[Exchange, str, Side], str]
