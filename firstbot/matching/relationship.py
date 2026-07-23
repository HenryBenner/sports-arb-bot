from __future__ import annotations

from decimal import Decimal

from .models import BitsetSettlement, MatchDecision, Relationship, ThresholdSettlement, TradeablePosition


class RelationshipEngine:
    def compare(self, first: TradeablePosition, second: TradeablePosition) -> MatchDecision:
        conflicts: list[str] = []
        reasons: list[str] = []
        if first.event.event_key != second.event.event_key and not _events_can_be_same(first, second):
            return _decision(Relationship.UNRELATED, False, 0.0, ("different_event",), ("event_key_mismatch",))
        if first.event.event_key != second.event.event_key:
            reasons.append("event_key_matched_by_settlement_universe")
        if first.market.family != second.market.family:
            conflicts.append("market_family_mismatch")
        if first.market.scope != second.market.scope:
            conflicts.append("market_scope_mismatch")
        if first.market.period != second.market.period:
            conflicts.append("market_period_mismatch")
        if conflicts:
            return _decision(Relationship.SETTLEMENT_MISMATCH, False, 0.0, tuple(conflicts), tuple(conflicts))
        if isinstance(first.settlement, BitsetSettlement) and isinstance(second.settlement, BitsetSettlement):
            decision = self._compare_bitsets(first.settlement, second.settlement, min(first.confidence, second.confidence))
            return _with_reasons(decision, tuple(reasons))
        if isinstance(first.settlement, ThresholdSettlement) and isinstance(second.settlement, ThresholdSettlement):
            decision = self._compare_thresholds(first.settlement, second.settlement, min(first.confidence, second.confidence))
            return _with_reasons(decision, tuple(reasons))
        return _decision(Relationship.UNKNOWN, False, 0.0, tuple((*reasons, "unsupported_settlement_expression")), ("unknown_settlement",))

    def _compare_bitsets(
        self,
        first: BitsetSettlement,
        second: BitsetSettlement,
        confidence: float,
    ) -> MatchDecision:
        universe = first.universe | second.universe
        if first.universe != second.universe:
            return _decision(
                Relationship.SETTLEMENT_MISMATCH,
                False,
                0.0,
                ("settlement_universe_mismatch",),
                ("settlement_universe_mismatch",),
            )
        if first.winning_states == second.winning_states:
            return _decision(Relationship.SAME_EXPOSURE, False, confidence, ("same_winning_states",), ())
        overlap = first.winning_states & second.winning_states
        covered = first.winning_states | second.winning_states
        if not overlap and covered == universe:
            return _decision(Relationship.EXACT_COMPLEMENTS, True, confidence, ("all_states_covered_once",), ())
        if not overlap:
            return _decision(
                Relationship.MUTUALLY_EXCLUSIVE_NOT_EXHAUSTIVE,
                False,
                confidence,
                ("missing_settlement_states",),
                ("not_exhaustive",),
            )
        return _decision(Relationship.PARTIAL_OVERLAP, False, confidence, ("overlapping_winning_states",), ("overlap",))

    def _compare_thresholds(
        self,
        first: ThresholdSettlement,
        second: ThresholdSettlement,
        confidence: float,
    ) -> MatchDecision:
        if (
            first.variable == second.variable
            and _operator_equivalent(first.operator, first.threshold, second.operator, second.threshold)
        ):
            return _decision(Relationship.SAME_EXPOSURE, False, confidence, ("equivalent_threshold_expression",), ())
        if first.variable == second.variable and _threshold_complements(first, second):
            return _decision(Relationship.EXACT_COMPLEMENTS, True, confidence, ("threshold_complements",), ())
        if {first.variable, second.variable} == {"margin", "opposing_margin"} and _spread_push_safe(first, second):
            return _decision(Relationship.GUARANTEED_COVER, True, confidence, ("spread_complements_with_push_refund",), ())
        return _decision(Relationship.SETTLEMENT_MISMATCH, False, confidence, ("threshold_not_complementary",), ("threshold_mismatch",))


def _decision(
    relationship: Relationship,
    tradable: bool,
    confidence: float,
    reasons: tuple[str, ...],
    conflicts: tuple[str, ...],
) -> MatchDecision:
    return MatchDecision(
        relationship=relationship,
        tradable_as_arb=tradable,
        confidence=confidence,
        reason_codes=reasons,
        hard_conflicts=conflicts,
    )


def _with_reasons(decision: MatchDecision, reasons: tuple[str, ...]) -> MatchDecision:
    if not reasons:
        return decision
    return MatchDecision(
        relationship=decision.relationship,
        tradable_as_arb=decision.tradable_as_arb,
        confidence=decision.confidence,
        reason_codes=tuple(dict.fromkeys((*reasons, *decision.reason_codes))),
        hard_conflicts=decision.hard_conflicts,
    )


def _events_can_be_same(first: TradeablePosition, second: TradeablePosition) -> bool:
    if not _same_event_day(first, second):
        return False
    first_participants = set(first.event.participant_ids)
    second_participants = set(second.event.participant_ids)
    if first_participants and first_participants == second_participants:
        return True
    if isinstance(first.settlement, BitsetSettlement) and isinstance(second.settlement, BitsetSettlement):
        return bool(first.settlement.universe) and first.settlement.universe == second.settlement.universe
    return False


def _same_event_day(first: TradeablePosition, second: TradeablePosition) -> bool:
    first_time = first.event.scheduled_start
    second_time = second.event.scheduled_start
    if first_time is None or second_time is None:
        return True
    return first_time.date() == second_time.date()


def _operator_equivalent(first_op: str, first_threshold: Decimal, second_op: str, second_threshold: Decimal) -> bool:
    if first_op == second_op and first_threshold == second_threshold:
        return True
    if first_op in {">", ">="} and second_op in {">", ">="}:
        return _integer_lower_bound(first_op, first_threshold) == _integer_lower_bound(second_op, second_threshold)
    if first_op in {"<", "<="} and second_op in {"<", "<="}:
        return _integer_upper_bound(first_op, first_threshold) == _integer_upper_bound(second_op, second_threshold)
    return False


def _threshold_complements(first: ThresholdSettlement, second: ThresholdSettlement) -> bool:
    if first.variable != second.variable:
        return False
    lower = _integer_lower_bound(first.operator, first.threshold)
    upper = _integer_upper_bound(second.operator, second.threshold)
    if lower is not None and upper is not None and lower == upper + 1:
        return True
    lower = _integer_lower_bound(second.operator, second.threshold)
    upper = _integer_upper_bound(first.operator, first.threshold)
    return lower is not None and upper is not None and lower == upper + 1


def _integer_lower_bound(operator: str, threshold: Decimal) -> int | None:
    if operator == ">":
        return int(threshold.to_integral_value(rounding="ROUND_FLOOR")) + 1
    if operator == ">=":
        return int(threshold)
    return None


def _integer_upper_bound(operator: str, threshold: Decimal) -> int | None:
    if operator == "<":
        return int(threshold.to_integral_value(rounding="ROUND_CEILING")) - 1
    if operator == "<=":
        return int(threshold)
    return None


def _spread_push_safe(first: ThresholdSettlement, second: ThresholdSettlement) -> bool:
    if first.push_policy != "refund" or second.push_policy != "refund":
        return False
    return first.threshold == -second.threshold
