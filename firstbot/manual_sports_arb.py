from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .executor import TradeExecutor
from .fees import leg_fee_cents_per_contract
from .models import ArbLeg, ArbOpportunity, BookLevel, Exchange, OrderBook, Side

ONE_HUNDRED = Decimal("100")


class ManualArbRejectReason:
    NO_ARBITRAGE = "no_arbitrage"
    FEES_KILL_EDGE = "fees_kill_edge"
    ORDERBOOK_STALE = "orderbook_stale"
    INSUFFICIENT_DEPTH = "insufficient_depth"
    PRICE_MOVED = "price_moved"
    UNSAFE_MARKET_PAIR = "unsafe_market_pair"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    SPREAD_TOO_WIDE = "spread_too_wide"
    GAME_NOT_LIVE = "game_not_live"
    MARKET_CLOSED = "market_closed"
    ONE_VENUE_HALTED = "one_venue_halted"
    DUPLICATE_PENDING_TRADE = "duplicate_pending_trade"
    EXISTING_UNHEDGED_EXPOSURE = "existing_unhedged_exposure"
    TRADE_SIZE_TOO_SMALL = "trade_size_too_small"
    LIVE_MODE_DISABLED = "live_mode_disabled"


@dataclass(frozen=True)
class ManualPairInput:
    polymarket_url: str
    kalshi_url: str
    sport: str | None = None
    event_label: str | None = None
    safe_to_trade: bool = False
    mapping: dict[str, Any] | None = None


@dataclass(frozen=True)
class ResolvedManualPair:
    polymarket_url: str
    kalshi_url: str
    polymarket_slug: str
    kalshi_ticker: str
    polymarket_yes_token_id: str
    polymarket_no_token_id: str
    polymarket_title: str
    kalshi_title: str
    sport: str | None
    event_label: str
    polymarket_market: dict[str, Any]
    kalshi_market: dict[str, Any]


@dataclass(frozen=True)
class SafetyReview:
    safe: bool
    reason: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class WeightedFill:
    side: Side
    requested_contracts: Decimal
    contracts: Decimal
    avg_price_cents: Decimal
    max_price_cents: int
    cost_usd: Decimal
    depth_usd: Decimal
    filled: bool


@dataclass(frozen=True)
class DirectionEvaluation:
    name: str
    polymarket_side: Side
    kalshi_side: Side
    polymarket_fill: WeightedFill
    kalshi_fill: WeightedFill
    contracts: Decimal
    gross_cost_cents: Decimal
    gross_edge_cents: Decimal
    fees_cents: Decimal
    net_edge_cents: Decimal
    max_safe_contracts: Decimal
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class ManualArbDecision:
    timestamp: datetime
    pair: ResolvedManualPair
    safety: SafetyReview
    polymarket_book: OrderBook
    kalshi_book: OrderBook
    direction_a: DirectionEvaluation
    direction_b: DirectionEvaluation
    best_direction: DirectionEvaluation
    mode: str
    decision: str
    rejection_reason: str | None
    freshness_ms: int


@dataclass
class ManualArbSessionState:
    opportunities_seen: int = 0
    scan_only_arbs: int = 0
    paper_arbs: int = 0
    live_attempts: int = 0
    missed_arbs: int = 0
    failed_orders: int = 0
    partial_fills: int = 0
    unhedged_exposure_usd: Decimal = Decimal("0")
    pending_trade: bool = False


def parse_kalshi_url(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ("op_market_ticker", "ticker", "market_ticker", "market"):
        if params.get(key):
            return params[key][0].strip()
    parts = [part for part in parsed.path.split("/") if part]
    for part in parts:
        cleaned = part.strip()
        if re.match(r"^K[A-Za-z0-9_.-]+$", cleaned):
            return cleaned.upper()
    for part in reversed(parts):
        cleaned = part.strip()
        if cleaned and re.match(r"^[A-Za-z0-9_.-]+$", cleaned):
            return cleaned.upper()
    if url and not parsed.scheme:
        return url.strip().upper()
    raise RuntimeError("Could not parse Kalshi market ticker from URL")


def parse_polymarket_url(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ("id", "slug", "market", "condition_id", "token_id", "asset_id"):
        if params.get(key):
            return params[key][0].strip()
    parts = [part for part in parsed.path.split("/") if part]
    if parts:
        return parts[-1].strip()
    if url and not parsed.scheme:
        return url.strip()
    raise RuntimeError("Could not parse Polymarket market slug/id from URL")


class ManualSportsArbResolver:
    def __init__(self, kalshi, polymarket) -> None:
        self.kalshi = kalshi
        self.polymarket = polymarket

    def resolve(self, pair_input: ManualPairInput) -> ResolvedManualPair:
        kalshi_ticker = parse_kalshi_url(pair_input.kalshi_url)
        polymarket_slug = parse_polymarket_url(pair_input.polymarket_url)
        kalshi_market = self._kalshi_market(kalshi_ticker)
        polymarket_market = self.polymarket._gamma_market(polymarket_slug)
        yes_token, no_token = self._polymarket_tokens(polymarket_slug, polymarket_market, pair_input)
        title_poly = _poly_title(polymarket_market)
        title_kalshi = _kalshi_title(kalshi_market, kalshi_ticker)
        event_label = pair_input.event_label or _event_label(title_poly, title_kalshi)
        return ResolvedManualPair(
            polymarket_url=pair_input.polymarket_url,
            kalshi_url=pair_input.kalshi_url,
            polymarket_slug=polymarket_slug,
            kalshi_ticker=kalshi_ticker,
            polymarket_yes_token_id=yes_token,
            polymarket_no_token_id=no_token,
            polymarket_title=title_poly,
            kalshi_title=title_kalshi,
            sport=pair_input.sport or _optional_str(polymarket_market.get("category")),
            event_label=event_label,
            polymarket_market=polymarket_market,
            kalshi_market=kalshi_market,
        )

    def _kalshi_market(self, ticker: str) -> dict[str, Any]:
        try:
            response = self.kalshi.get_markets(ticker=ticker, limit=1)
        except Exception:
            response = self.kalshi.get_markets(status="open", limit=1000)
        markets = response.get("markets", []) if isinstance(response, dict) else []
        for market in markets:
            if str(market.get("ticker") or "").upper() == ticker.upper():
                return market
        return {"ticker": ticker}

    def _polymarket_tokens(
        self,
        market_id: str,
        market: dict[str, Any],
        pair_input: ManualPairInput,
    ) -> tuple[str, str]:
        try:
            return (
                self.polymarket.resolve_clob_token_id(market_id, Side.YES),
                self.polymarket.resolve_clob_token_id(market_id, Side.NO),
            )
        except RuntimeError:
            pass
        mapping = pair_input.mapping or {}
        outcomes = _json_list(market.get("outcomes"))
        token_ids = [str(token) for token in _json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))]
        if len(outcomes) != 2 or len(token_ids) != 2:
            raise RuntimeError("Polymarket market is not a supported binary or named two-outcome market")
        yes_hint = _optional_str(mapping.get("polymarket_yes_outcome")) or _infer_yes_outcome(pair_input.kalshi_url, outcomes)
        if not yes_hint:
            raise RuntimeError("Polymarket named-outcome market requires polymarket_yes_outcome in mapping file")
        yes_index = _match_outcome_index(outcomes, yes_hint)
        no_index = 1 - yes_index
        return token_ids[yes_index], token_ids[no_index]


class ManualPairSafetyChecker:
    def review(self, pair: ResolvedManualPair, pair_input: ManualPairInput) -> SafetyReview:
        warnings: list[str] = []
        mapping = pair_input.mapping or {}
        mapping_confirms = bool(mapping.get("safe_to_trade") or mapping.get("rules_compatible"))
        safe_flag = pair_input.safe_to_trade or mapping_confirms
        if _market_closed(pair.polymarket_market) or _market_closed(pair.kalshi_market):
            return SafetyReview(False, ManualArbRejectReason.MARKET_CLOSED, tuple(warnings))
        if _dangerous_draw_market(pair, pair_input):
            warnings.append("draw/three-way language requires explicit manual mapping")
            if not mapping_confirms:
                return SafetyReview(False, ManualArbRejectReason.MANUAL_REVIEW_REQUIRED, tuple(warnings))
        if not _looks_like_same_event(pair, pair_input):
            warnings.append("titles/event labels do not confidently match")
            return SafetyReview(False, ManualArbRejectReason.UNSAFE_MARKET_PAIR, tuple(warnings))
        if not safe_flag:
            warnings.append("manual --safe-to-trade confirmation is required")
            return SafetyReview(False, ManualArbRejectReason.MANUAL_REVIEW_REQUIRED, tuple(warnings))
        return SafetyReview(True, None, tuple(warnings))


class ManualSportsArbEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        pair: ResolvedManualPair,
        safety: SafetyReview,
        polymarket_book: OrderBook,
        kalshi_book: OrderBook,
        mode: str,
        now: datetime,
        state: ManualArbSessionState | None = None,
    ) -> ManualArbDecision:
        freshness_ms = _freshness_ms((polymarket_book, kalshi_book), now)
        direction_a = self._direction(
            "polymarket_yes_kalshi_no",
            polymarket_book,
            kalshi_book,
            Side.YES,
            Side.NO,
            now,
            freshness_ms,
        )
        direction_b = self._direction(
            "polymarket_no_kalshi_yes",
            polymarket_book,
            kalshi_book,
            Side.NO,
            Side.YES,
            now,
            freshness_ms,
        )
        best = max((direction_a, direction_b), key=lambda item: item.net_edge_cents)
        blockers = list(best.blockers)
        if not safety.safe and safety.reason:
            blockers.append(safety.reason)
        if state and state.pending_trade:
            blockers.append(ManualArbRejectReason.DUPLICATE_PENDING_TRADE)
        if state and state.unhedged_exposure_usd > self.settings.manual_arb_max_unhedged_usd:
            blockers.append(ManualArbRejectReason.EXISTING_UNHEDGED_EXPOSURE)
        decision = self._decision(mode, best, blockers)
        rejection = None if decision in {"scan_opportunity", "paper_trade"} else _first_blocker(blockers)
        return ManualArbDecision(
            timestamp=now,
            pair=pair,
            safety=safety,
            polymarket_book=polymarket_book,
            kalshi_book=kalshi_book,
            direction_a=direction_a,
            direction_b=direction_b,
            best_direction=best,
            mode=mode,
            decision=decision,
            rejection_reason=rejection,
            freshness_ms=freshness_ms,
        )

    def _direction(
        self,
        name: str,
        polymarket_book: OrderBook,
        kalshi_book: OrderBook,
        polymarket_side: Side,
        kalshi_side: Side,
        now: datetime,
        freshness_ms: int,
    ) -> DirectionEvaluation:
        blockers: list[str] = []
        pm_levels = _levels_for_side(polymarket_book, polymarket_side)
        k_levels = _levels_for_side(kalshi_book, kalshi_side)
        if not pm_levels or not k_levels:
            blockers.append(ManualArbRejectReason.INSUFFICIENT_DEPTH)
        if freshness_ms > self.settings.book_stale_ms:
            blockers.append(ManualArbRejectReason.ORDERBOOK_STALE)
        if _spread_cents(polymarket_book) > self.settings.manual_arb_max_spread_cents:
            blockers.append(ManualArbRejectReason.SPREAD_TOO_WIDE)
        if _spread_cents(kalshi_book) > self.settings.manual_arb_max_spread_cents:
            blockers.append(ManualArbRejectReason.SPREAD_TOO_WIDE)

        max_contracts = Decimal(self.settings.manual_arb_max_contracts).to_integral_value(rounding=ROUND_FLOOR)
        best_pm = _empty_fill(polymarket_side)
        best_k = _empty_fill(kalshi_side)
        best_contracts = Decimal("0")
        best_gross_cost = Decimal("0")
        best_gross_edge = Decimal("-100")
        best_fees = Decimal("0")
        best_net = Decimal("-100")

        for count in range(1, int(max_contracts) + 1):
            contracts = Decimal(count)
            pm_fill = weighted_fill(pm_levels, polymarket_side, contracts)
            k_fill = weighted_fill(k_levels, kalshi_side, contracts)
            if not pm_fill.filled or not k_fill.filled:
                break
            total_cost_usd = pm_fill.cost_usd + k_fill.cost_usd
            if total_cost_usd > self.settings.manual_arb_max_usd:
                break
            gross_cost = pm_fill.avg_price_cents + k_fill.avg_price_cents
            gross_edge = ONE_HUNDRED - gross_cost
            pm_leg = ArbLeg(Exchange.POLYMARKET, polymarket_book.market_id, polymarket_side, int(pm_fill.avg_price_cents.to_integral_value(rounding=ROUND_CEILING)), contracts)
            k_leg = ArbLeg(Exchange.KALSHI, kalshi_book.market_id, kalshi_side, int(k_fill.avg_price_cents.to_integral_value(rounding=ROUND_CEILING)), contracts)
            fees = leg_fee_cents_per_contract(pm_leg, self.settings) + leg_fee_cents_per_contract(k_leg, self.settings)
            net = gross_edge - fees
            if net >= best_net:
                best_pm = pm_fill
                best_k = k_fill
                best_contracts = contracts
                best_gross_cost = gross_cost
                best_gross_edge = gross_edge
                best_fees = fees
                best_net = net

        if best_contracts < Decimal("1"):
            blockers.append(ManualArbRejectReason.TRADE_SIZE_TOO_SMALL)
        elif best_gross_edge <= 0:
            blockers.append(ManualArbRejectReason.NO_ARBITRAGE)
        elif best_net <= 0:
            blockers.append(ManualArbRejectReason.FEES_KILL_EDGE)
        elif best_net < self.settings.manual_arb_min_net_edge_cents:
            blockers.append(ManualArbRejectReason.FEES_KILL_EDGE)

        return DirectionEvaluation(
            name=name,
            polymarket_side=polymarket_side,
            kalshi_side=kalshi_side,
            polymarket_fill=best_pm,
            kalshi_fill=best_k,
            contracts=best_contracts,
            gross_cost_cents=best_gross_cost,
            gross_edge_cents=best_gross_edge,
            fees_cents=best_fees,
            net_edge_cents=best_net,
            max_safe_contracts=best_contracts,
            blockers=tuple(_dedupe(blockers)),
        )

    def _decision(self, mode: str, best: DirectionEvaluation, blockers: list[str]) -> str:
        actionable = not blockers and best.net_edge_cents > 0
        if mode == "scan":
            return "scan_opportunity" if best.gross_edge_cents > 0 else "rejected"
        if mode == "paper":
            return "paper_trade" if actionable else "rejected"
        if mode == "live":
            return "blocked"
        return "rejected"


class ManualSportsArbRunner:
    def __init__(
        self,
        kalshi,
        polymarket,
        settings: Settings,
        log_dir: str | Path = "logs",
        clock=None,
    ) -> None:
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.settings = settings
        self.log_dir = Path(log_dir)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.resolver = ManualSportsArbResolver(kalshi, polymarket)
        self.safety = ManualPairSafetyChecker()
        self.engine = ManualSportsArbEngine(settings)
        self.state = ManualArbSessionState()
        self.executor = TradeExecutor(kalshi, polymarket, allowed_workflow="run-manual-sports-arb")

    def resolve_pair(self, pair_input: ManualPairInput) -> tuple[ResolvedManualPair, SafetyReview]:
        pair = self.resolver.resolve(pair_input)
        return pair, self.safety.review(pair, pair_input)

    def tick(self, pair: ResolvedManualPair, safety: SafetyReview, mode: str) -> ManualArbDecision:
        now = self.clock()
        polymarket_book = self.polymarket.get_orderbook(
            pair.polymarket_yes_token_id,
            pair.polymarket_no_token_id,
            market_id=pair.polymarket_slug,
        )
        kalshi_book = self.kalshi.get_orderbook(pair.kalshi_ticker)
        polymarket_book = _with_timestamp(polymarket_book, now)
        kalshi_book = _with_timestamp(kalshi_book, now)
        decision = self.engine.evaluate(pair, safety, polymarket_book, kalshi_book, mode, now, self.state)
        self._record(decision)
        return decision

    def live_attempt(self, decision: ManualArbDecision) -> tuple[bool, str]:
        opportunity = _opportunity_from_decision(decision, executable=False)
        message = ManualArbRejectReason.LIVE_MODE_DISABLED
        self._write_jsonl("manual_arb_live_attempts.jsonl", {"timestamp": self.clock().isoformat(), "message": message, "opportunity": _arb_opportunity_record(opportunity)})
        return False, message

    def _record(self, decision: ManualArbDecision) -> None:
        record = _decision_record(decision)
        self._write_jsonl("manual_arb_calculations.jsonl", record)
        if decision.best_direction.gross_edge_cents > 0 or decision.best_direction.net_edge_cents > 0:
            self.state.opportunities_seen += 1
            self._write_jsonl("manual_arb_opportunities.jsonl", record)
        if decision.decision == "paper_trade":
            self.state.paper_arbs += 1
            self._write_jsonl("manual_arb_paper_trades.jsonl", record)
            self._write_csv_row("manual_arb_trades.csv", _csv_headers(), _csv_row(decision))
        elif decision.mode == "scan" and decision.decision == "scan_opportunity":
            self.state.scan_only_arbs += 1

    def _write_jsonl(self, filename: str, record: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _write_csv_row(self, filename: str, headers: list[str], row: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / filename
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)


def weighted_fill(levels: list[BookLevel], side: Side, contracts: Decimal) -> WeightedFill:
    remaining = contracts
    filled = Decimal("0")
    total_cents = Decimal("0")
    max_price = 0
    for level in sorted(levels, key=lambda item: item.price_cents):
        if remaining <= 0:
            break
        take = min(remaining, level.size)
        filled += take
        remaining -= take
        total_cents += Decimal(level.price_cents) * take
        max_price = max(max_price, level.price_cents)
    if filled <= 0:
        return _empty_fill(side, contracts)
    avg = (total_cents / filled).quantize(Decimal("0.0001"))
    cost = (total_cents / ONE_HUNDRED).quantize(Decimal("0.01"))
    return WeightedFill(
        side=side,
        requested_contracts=contracts,
        contracts=filled,
        avg_price_cents=avg,
        max_price_cents=max_price,
        cost_usd=cost,
        depth_usd=cost,
        filled=filled >= contracts,
    )


def load_mapping(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _with_timestamp(book: OrderBook, now: datetime) -> OrderBook:
    return OrderBook(book.exchange, book.market_id, book.yes_asks, book.no_asks, book.timestamp or now.isoformat())


def _levels_for_side(book: OrderBook, side: Side) -> list[BookLevel]:
    return book.yes_asks if side is Side.YES else book.no_asks


def _empty_fill(side: Side, requested: Decimal = Decimal("0")) -> WeightedFill:
    return WeightedFill(side, requested, Decimal("0"), Decimal("0"), 0, Decimal("0"), Decimal("0"), False)


def _freshness_ms(books: tuple[OrderBook, OrderBook], now: datetime) -> int:
    ages = []
    for book in books:
        parsed = _parse_datetime(book.timestamp)
        if parsed is None:
            ages.append(10**9)
        else:
            ages.append(int(max(0, (now - parsed).total_seconds() * 1000)))
    return max(ages)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.isdigit():
        timestamp = Decimal(text)
        if timestamp > Decimal("100000000000"):
            timestamp = timestamp / Decimal("1000")
        return datetime.fromtimestamp(float(timestamp), timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _spread_cents(book: OrderBook) -> int:
    yes = book.best_ask(Side.YES)
    no = book.best_ask(Side.NO)
    if yes is None or no is None:
        return 100
    return max(0, yes.price_cents + no.price_cents - 100)


def _opportunity_from_decision(decision: ManualArbDecision, executable: bool) -> ArbOpportunity:
    best = decision.best_direction
    pm_market = (
        decision.pair.polymarket_yes_token_id
        if best.polymarket_side is Side.YES
        else decision.pair.polymarket_no_token_id
    )
    poly_leg = ArbLeg(Exchange.POLYMARKET, pm_market, best.polymarket_side, best.polymarket_fill.max_price_cents, best.contracts)
    kalshi_leg = ArbLeg(Exchange.KALSHI, decision.pair.kalshi_ticker, best.kalshi_side, best.kalshi_fill.max_price_cents, best.contracts)
    yes_leg = poly_leg if poly_leg.side is Side.YES else kalshi_leg
    no_leg = poly_leg if poly_leg.side is Side.NO else kalshi_leg
    blockers = () if executable else (ManualArbRejectReason.LIVE_MODE_DISABLED,)
    return ArbOpportunity(
        pair_name=decision.pair.event_label,
        buy_yes=yes_leg,
        buy_no=no_leg,
        gross_cost_cents=int(best.gross_cost_cents.to_integral_value(rounding=ROUND_CEILING)),
        buffers_cents=best.fees_cents,
        net_profit_cents=best.net_edge_cents,
        executable=executable,
        blockers=blockers,
    )


def _decision_record(decision: ManualArbDecision) -> dict[str, Any]:
    return {
        "timestamp": decision.timestamp.isoformat(),
        "mode": decision.mode,
        "decision": decision.decision,
        "rejection_reason": decision.rejection_reason,
        "sport": decision.pair.sport,
        "event_label": decision.pair.event_label,
        "polymarket_url": decision.pair.polymarket_url,
        "kalshi_url": decision.pair.kalshi_url,
        "polymarket_market_id": decision.pair.polymarket_slug,
        "kalshi_market_id": decision.pair.kalshi_ticker,
        "polymarket_yes_ask": _ask_price(decision.polymarket_book, Side.YES),
        "polymarket_no_ask": _ask_price(decision.polymarket_book, Side.NO),
        "kalshi_yes_ask": _ask_price(decision.kalshi_book, Side.YES),
        "kalshi_no_ask": _ask_price(decision.kalshi_book, Side.NO),
        "direction_a": _direction_record(decision.direction_a),
        "direction_b": _direction_record(decision.direction_b),
        "best_direction": decision.best_direction.name,
        "freshness_ms": decision.freshness_ms,
        "safe_to_trade": decision.safety.safe,
        "safety_warnings": list(decision.safety.warnings),
    }


def _direction_record(direction: DirectionEvaluation) -> dict[str, Any]:
    return {
        "name": direction.name,
        "polymarket_side": direction.polymarket_side.value,
        "kalshi_side": direction.kalshi_side.value,
        "contracts": str(direction.contracts),
        "gross_cost_cents": str(direction.gross_cost_cents),
        "gross_edge_cents": str(direction.gross_edge_cents),
        "fees_cents": str(direction.fees_cents),
        "net_edge_cents": str(direction.net_edge_cents),
        "polymarket_avg_price_cents": str(direction.polymarket_fill.avg_price_cents),
        "kalshi_avg_price_cents": str(direction.kalshi_fill.avg_price_cents),
        "max_safe_contracts": str(direction.max_safe_contracts),
        "blockers": list(direction.blockers),
    }


def _arb_opportunity_record(opportunity: ArbOpportunity) -> dict[str, Any]:
    return {
        "pair_name": opportunity.pair_name,
        "gross_cost_cents": opportunity.gross_cost_cents,
        "fees_cents": str(opportunity.buffers_cents),
        "net_profit_cents": str(opportunity.net_profit_cents),
        "executable": opportunity.executable,
        "blockers": list(opportunity.blockers),
    }


def _csv_headers() -> list[str]:
    return [
        "timestamp", "sport", "event", "polymarket_url", "kalshi_url",
        "polymarket_market_id", "kalshi_market_id", "polymarket_yes_ask",
        "polymarket_no_ask", "kalshi_yes_ask", "kalshi_no_ask",
        "direction_a_gross_edge", "direction_a_fees", "direction_a_net_edge",
        "direction_b_gross_edge", "direction_b_fees", "direction_b_net_edge",
        "best_direction", "available_depth_contracts", "max_safe_trade_size",
        "freshness_ms", "mode", "decision", "rejection_reason",
        "paper_pnl_status", "paper_pnl_usd",
    ]


def _csv_row(decision: ManualArbDecision) -> dict[str, Any]:
    return {
        "timestamp": decision.timestamp.isoformat(),
        "sport": decision.pair.sport or "",
        "event": decision.pair.event_label,
        "polymarket_url": decision.pair.polymarket_url,
        "kalshi_url": decision.pair.kalshi_url,
        "polymarket_market_id": decision.pair.polymarket_slug,
        "kalshi_market_id": decision.pair.kalshi_ticker,
        "polymarket_yes_ask": _ask_price(decision.polymarket_book, Side.YES),
        "polymarket_no_ask": _ask_price(decision.polymarket_book, Side.NO),
        "kalshi_yes_ask": _ask_price(decision.kalshi_book, Side.YES),
        "kalshi_no_ask": _ask_price(decision.kalshi_book, Side.NO),
        "direction_a_gross_edge": _dstr(decision.direction_a.gross_edge_cents),
        "direction_a_fees": _dstr(decision.direction_a.fees_cents),
        "direction_a_net_edge": _dstr(decision.direction_a.net_edge_cents),
        "direction_b_gross_edge": _dstr(decision.direction_b.gross_edge_cents),
        "direction_b_fees": _dstr(decision.direction_b.fees_cents),
        "direction_b_net_edge": _dstr(decision.direction_b.net_edge_cents),
        "best_direction": decision.best_direction.name,
        "available_depth_contracts": _dstr(decision.best_direction.max_safe_contracts),
        "max_safe_trade_size": _dstr(decision.best_direction.max_safe_contracts),
        "freshness_ms": decision.freshness_ms,
        "mode": decision.mode,
        "decision": decision.decision,
        "rejection_reason": decision.rejection_reason or "",
        "paper_pnl_status": "pending",
        "paper_pnl_usd": "",
    }


def _ask_price(book: OrderBook, side: Side) -> int | None:
    ask = book.best_ask(side)
    return None if ask is None else ask.price_cents


def _poly_title(market: dict[str, Any]) -> str:
    return str(market.get("question") or market.get("title") or market.get("slug") or "")


def _kalshi_title(market: dict[str, Any], fallback: str) -> str:
    return str(market.get("title") or market.get("subtitle") or market.get("event_title") or fallback)


def _event_label(poly_title: str, kalshi_title: str) -> str:
    return poly_title or kalshi_title or "manual sports arb"


def _optional_str(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _infer_yes_outcome(kalshi_url: str, outcomes: list[Any]) -> str | None:
    ticker = parse_kalshi_url(kalshi_url)
    suffix = ticker.rsplit("-", 1)[-1].lower()
    if len(suffix) < 2:
        return None
    aliases = {
        "chc": ("chicago cubs", "cubs"),
        "tor": ("toronto blue jays", "blue jays"),
        "nyy": ("new york yankees", "yankees"),
        "nym": ("new york mets", "mets"),
        "lad": ("los angeles dodgers", "dodgers"),
        "laa": ("los angeles angels", "angels"),
        "bos": ("boston red sox", "red sox"),
        "cws": ("chicago white sox", "white sox"),
        "det": ("detroit tigers", "tigers"),
        "cin": ("cincinnati reds", "reds"),
        "pit": ("pittsburgh pirates", "pirates"),
        "col": ("colorado rockies", "rockies"),
        "bal": ("baltimore orioles", "orioles"),
    }
    candidates = aliases.get(suffix, (suffix,))
    normalized_outcomes = [(str(outcome), _norm(str(outcome))) for outcome in outcomes]
    for candidate in candidates:
        needle = _norm(candidate)
        for original, normalized in normalized_outcomes:
            if needle and needle in normalized:
                return original
    return None


def _match_outcome_index(outcomes: list[Any], hint: str) -> int:
    needle = _norm(hint)
    for index, outcome in enumerate(outcomes):
        normalized = _norm(str(outcome))
        if needle == normalized or needle in normalized:
            return index
    raise RuntimeError(f"Could not match Polymarket outcome {hint!r}")


def _market_closed(market: dict[str, Any]) -> bool:
    status = str(market.get("status") or "").lower()
    return bool(market.get("closed") or market.get("archived") or status in {"closed", "settled", "halted"})


def _dangerous_draw_market(pair: ResolvedManualPair, pair_input: ManualPairInput) -> bool:
    text = " ".join([pair.polymarket_title, pair.kalshi_title, pair_input.sport or pair.sport or ""]).lower()
    return any(term in text for term in ("soccer", "draw", "tie", "three-way", "3-way"))


def _looks_like_same_event(pair: ResolvedManualPair, pair_input: ManualPairInput) -> bool:
    if pair_input.event_label:
        label = _norm(pair_input.event_label)
        return label in _norm(pair.polymarket_title) or label in _norm(pair.kalshi_title)
    poly_tokens = set(_norm(pair.polymarket_title).split())
    kalshi_tokens = set(_norm(pair.kalshi_title).split())
    stopwords = {
        "will", "beat", "beats", "win", "wins", "the", "and", "for", "game",
        "market", "team", "against", "versus", "vs",
    }
    meaningful = {token for token in poly_tokens & kalshi_tokens if len(token) > 2 and token not in stopwords}
    return len(meaningful) >= 1


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _first_blocker(blockers: list[str]) -> str | None:
    return blockers[0] if blockers else None


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _dstr(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))
