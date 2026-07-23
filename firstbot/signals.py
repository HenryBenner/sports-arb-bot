from __future__ import annotations

import asyncio
import csv
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path
from typing import Any, AsyncIterable, Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .executor import TradeExecutor
from .fees import leg_fee_cents_per_contract
from .models import ArbLeg, BookLevel, EVOpportunity, Exchange, Side


DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVENT_DATE_ZONE = ZoneInfo("America/New_York")
EVENT_DATE_END_OF_DAY = datetime_time(23, 59, 59)
ONE_HUNDRED = Decimal("100")


class SignalRejectReason:
    SIDE_UNCLEAR = "Signal side is unclear"
    NOT_BUY_NO = SIDE_UNCLEAR
    MARKET_UNMATCHED = "Market could not be matched"
    RESOLUTION_TOO_FAR = "Resolution is more than 72 hours away"
    MARKET_INACTIVE = "Market is inactive"
    NO_PRICE_TOO_LOW = "NO price is too low"
    NO_PRICE_TOO_HIGH = "NO price is too high"
    CHASE = "Price moved too far from signal"
    LIQUIDITY_LOW = "Liquidity is too low"
    SPREAD_WIDE = "Spread is too wide"
    ORDERBOOK_DEPTH_LOW = "Orderbook depth is too low"
    SCORE_LOW = "Signal score is too low"
    EV_LOW = "Estimated EV is too low"
    RISK_LIMIT = "Risk limit exceeded"
    DUPLICATE = "Duplicate recent trade"
    UNCLEAR_RULES = "Market resolution rules are unclear"
    CONFLICTING_EXPOSURE = "Existing repo bot already has conflicting exposure"
    ZERO_STAKE_CONFIG = "zero_stake_config"
    MARKET_LOOKUP_FAILED = "market_lookup_failed"
    UNSUPPORTED_MARKET_TYPE = "unsupported_market_type"
    OUTCOME_TOKEN_NOT_FOUND = "outcome_token_not_found"
    MISSING_ORDERBOOK = "missing_orderbook"
    API_VALIDATION_ERROR = "api_validation_error"
    INSUFFICIENT_FILLABLE_DEPTH = "insufficient_fillable_depth"


@dataclass(frozen=True)
class SignalEvent:
    channel: str
    market_id: str | None
    platform: Exchange | None
    side: Side | None
    outcome: str | None
    price_cents: int | None
    amount_usd: Decimal
    wallet_pnl_usd: Decimal
    profitable_wallet_count: int
    losing_wallet_count: int
    losing_side: Side | None
    resolution_date: datetime | None
    detected_at: datetime
    group_id: str | None
    title: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class SignalEvaluation:
    signal: SignalEvent
    exchange: Exchange | None
    market_id: str | None
    no_price_cents: int
    signal_price_cents: int | None
    chase_cents: int | None
    spread_cents: int | None
    depth_contracts: Decimal
    depth_usd: Decimal
    score: int
    estimated_probability: Decimal
    expected_profit_cents: Decimal
    stake_usd: Decimal
    contracts: Decimal
    blockers: tuple[str, ...]
    required_depth_usd: Decimal = Decimal("0")
    depth_pass: bool = False
    decision_tier: str = "log_only"
    paper_allowed: bool = False
    live_candidate: bool = False
    reject_category: str | None = None
    resolver_error: str | None = None
    paper_blockers: tuple[str, ...] = ()
    live_blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignalDecision:
    action: str
    mode: str
    message: str
    accepted: bool
    evaluation: SignalEvaluation


@dataclass(frozen=True)
class SignalMarket:
    exchange: Exchange
    market_id: str
    no_ask: BookLevel | None
    yes_ask: BookLevel | None
    no_market_id: str | None = None
    yes_market_id: str | None = None
    active: bool = True

    def ask(self, side: Side) -> BookLevel | None:
        return self.no_ask if side is Side.NO else self.yes_ask

    def market_id_for(self, side: Side) -> str:
        if side is Side.NO:
            return self.no_market_id or self.market_id
        return self.yes_market_id or self.market_id

    def spread_cents(self, side: Side) -> int | None:
        if self.no_ask is None or self.yes_ask is None:
            return None
        if side is Side.NO:
            bid_cents = 100 - self.yes_ask.price_cents
            return max(0, self.no_ask.price_cents - bid_cents)
        bid_cents = 100 - self.no_ask.price_cents
        return max(0, self.yes_ask.price_cents - bid_cents)


class SignalNormalizer:
    def normalize(self, raw: dict[str, Any], now: datetime | None = None) -> SignalEvent:
        now = now or datetime.now(timezone.utc)
        payload = _payload(raw)
        channel = str(_first(payload, "channel", "type", "signal_type", "source") or "").lower()
        if channel not in {"smart_money", "fade_finder"}:
            channel = str(_first(raw, "channel", "type", "signal_type", "source") or channel).lower()
        market_id = _optional_str(
            _first(payload, "market_id", "marketId", "ticker", "slug", "market_slug", "marketSlug", "token_id", "asset_id")
        )
        outcome = _optional_str(_first(payload, "outcome", "direction", "position", "trade_side"))
        action = _first(payload, "side", "action", "trade_action")
        side = _trade_side(action=action, outcome=outcome)
        losing_side = _side(_first(payload, "losing_side", "loser_side", "weak_side", "opposite_side"))
        platform = _platform(_first(payload, "platform", "exchange", "venue", "platform_buy", "platformBuy"))
        price_cents = _price_cents(_first(payload, "price", "signal_price", "trade_price", "fill_price"))
        detected_at = parse_datetime(_first(payload, "detected_at", "timestamp", "created_at", "time")) or now
        resolution_date = parse_datetime(
            _first(payload, "resolution_date", "event_date", "end_date", "close_time", "resolve_time")
        ) or _date_from_text(market_id) or _date_from_text(str(_first(payload, "title", "market_title", "question", "group_title") or ""))
        return SignalEvent(
            channel=channel,
            market_id=market_id,
            platform=platform,
            side=side,
            outcome=outcome,
            price_cents=price_cents,
            amount_usd=_decimal(_first(payload, "amount_usd", "amount", "size_usd", "trade_size", "notional")),
            wallet_pnl_usd=_decimal(_first(payload, "wallet_pnl", "wallet_pnl_usd", "pnl", "profit", "historical_pnl", "pnl_to_date")),
            profitable_wallet_count=_int(_first(payload, "profitable_wallet_count", "smart_wallets", "winner_count"), 0),
            losing_wallet_count=_int(_first(payload, "losing_wallet_count", "losing_wallets", "loser_count"), 0),
            losing_side=losing_side,
            resolution_date=resolution_date,
            detected_at=detected_at,
            group_id=_optional_str(_first(payload, "group_id", "event_id", "market_group_id")),
            title=str(_first(payload, "title", "market_title", "question", "group_title") or ""),
            raw=raw,
        )


class SignalMarketResolver:
    def __init__(self, kalshi, polymarket) -> None:
        self.kalshi = kalshi
        self.polymarket = polymarket

    def resolve(self, signal: SignalEvent) -> SignalMarket | None:
        if not signal.market_id:
            return None
        platform = signal.platform or self._infer_platform(signal.market_id)
        if platform is Exchange.KALSHI:
            return self._kalshi_market(signal.market_id)
        if platform is Exchange.POLYMARKET:
            return self._polymarket_market(signal)
        return None

    def _kalshi_market(self, market_id: str) -> SignalMarket:
        book = self.kalshi.get_orderbook(market_id)
        return SignalMarket(
            exchange=Exchange.KALSHI,
            market_id=market_id,
            no_ask=book.best_ask(Side.NO),
            yes_ask=book.best_ask(Side.YES),
            no_market_id=market_id,
            yes_market_id=market_id,
        )

    def _polymarket_market(self, signal: SignalEvent) -> SignalMarket:
        assert signal.market_id is not None
        market_id = signal.market_id
        trade_side = signal.side or Side.NO
        outcome = signal.outcome if signal.outcome and _side(signal.outcome) is None else None
        try:
            target_token = self.polymarket.resolve_clob_token_id_for_outcome(
                market_id,
                outcome,
                trade_side,
            )
        except RuntimeError as exc:
            reason = _resolver_reject_reason(str(exc))
            if trade_side is Side.NO and reason == SignalRejectReason.UNSUPPORTED_MARKET_TYPE:
                raise RuntimeError(f"{SignalRejectReason.UNSUPPORTED_MARKET_TYPE}: {exc}") from exc
            raise

        no_token = target_token if trade_side is Side.NO else None
        yes_token = target_token if trade_side is Side.YES else None
        no_ask = None
        yes_ask = None
        opposite_token = None
        try:
            opposite_side = _opposite(trade_side)
            opposite_token = self.polymarket.resolve_clob_token_id_for_outcome(
                market_id,
                None,
                opposite_side,
            )
            if trade_side is Side.NO:
                yes_token = opposite_token
            else:
                no_token = opposite_token
            book = self.polymarket.get_orderbook(yes_token, no_token, market_id=target_token)
            yes_ask = book.best_ask(Side.YES)
            no_ask = book.best_ask(Side.NO)
        except RuntimeError:
            target_ask = self.polymarket.get_token_best_ask(target_token)
            if trade_side is Side.NO:
                no_ask = target_ask
            else:
                yes_ask = target_ask
        return SignalMarket(
            exchange=Exchange.POLYMARKET,
            market_id=target_token,
            no_ask=no_ask,
            yes_ask=yes_ask,
            no_market_id=no_token,
            yes_market_id=yes_token,
        )

    def _infer_platform(self, market_id: str) -> Exchange | None:
        if market_id.upper() == market_id and any(char.isalpha() for char in market_id):
            return Exchange.KALSHI
        return Exchange.POLYMARKET


class SignalRiskStore:
    def __init__(
        self,
        log_dir: str | Path,
        cooldown_seconds: int,
        daily_loss_limit_usd: Decimal,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.cooldown_seconds = cooldown_seconds
        self.daily_loss_limit_usd = daily_loss_limit_usd
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.last_trade_by_market: dict[str, datetime] = {}
        self.exposed_markets: set[str] = set()
        self.exposed_groups: set[str] = set()
        self.paper_signal_keys: set[str] = set()
        self._load_existing_logs()

    def blockers(
        self,
        signal: SignalEvent,
        market_id: str | None,
        stake_usd: Decimal,
        enforce_daily_loss_limit: bool = True,
    ) -> list[str]:
        blockers: list[str] = []
        if market_id:
            last = self.last_trade_by_market.get(market_id)
            if last and self.clock() - last < timedelta(seconds=self.cooldown_seconds):
                blockers.append(SignalRejectReason.DUPLICATE)
            if market_id in self.exposed_markets:
                blockers.append(SignalRejectReason.CONFLICTING_EXPOSURE)
        if signal.group_id and signal.group_id in self.exposed_groups:
            blockers.append(SignalRejectReason.RISK_LIMIT)
        if enforce_daily_loss_limit and stake_usd > self.daily_loss_limit_usd:
            blockers.append(SignalRejectReason.RISK_LIMIT)
        return blockers

    def mark(self, signal: SignalEvent, market_id: str) -> None:
        now = self.clock()
        self.last_trade_by_market[market_id] = now
        self.exposed_markets.add(market_id)
        self.paper_signal_keys.add(_signal_trade_key(signal, market_id))
        if signal.group_id:
            self.exposed_groups.add(signal.group_id)

    def paper_duplicate_blockers(self, signal: SignalEvent, market_id: str | None) -> list[str]:
        if market_id and _signal_trade_key(signal, market_id) in self.paper_signal_keys:
            return [SignalRejectReason.DUPLICATE]
        return []

    def _load_existing_logs(self) -> None:
        for filename in (
            "signal_paper_trades.jsonl",
            "signal_live_trades.jsonl",
            "hot_paper_trades.jsonl",
            "hot_live_trades.jsonl",
            "ev_paper_trades.jsonl",
            "ev_live_trades.jsonl",
        ):
            path = self.log_dir / filename
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._load_record(record)

    def _load_record(self, record: dict[str, Any]) -> None:
        timestamp = parse_datetime(record.get("timestamp")) or self.clock()
        for market_id in _market_ids_from_record(record):
            self.exposed_markets.add(market_id)
            self.last_trade_by_market.setdefault(market_id, timestamp)
        key = _signal_trade_key_from_record(record)
        if key:
            self.paper_signal_keys.add(key)
        predictionhunt = record.get("predictionhunt")
        if isinstance(predictionhunt, dict) and predictionhunt.get("group_id") is not None:
            self.exposed_groups.add(str(predictionhunt["group_id"]))


class SignalEngine:
    def __init__(self, settings: Settings, risk_store: SignalRiskStore | None = None) -> None:
        self.settings = settings
        self.risk_store = risk_store

    def evaluate(
        self,
        signal: SignalEvent,
        market: SignalMarket | None,
        now: datetime,
        intended_stake_usd: Decimal | None = None,
        enforce_daily_loss_limit: bool = False,
        enforce_risk_limits: bool = False,
    ) -> SignalEvaluation:
        blockers: list[str] = []
        if signal.side is None:
            blockers.append(SignalRejectReason.SIDE_UNCLEAR)
        if market is None:
            blockers.append(SignalRejectReason.MARKET_UNMATCHED)
            return self._evaluation(signal, None, blockers, intended_stake_usd)
        if not market.active:
            blockers.append(SignalRejectReason.MARKET_INACTIVE)
        if signal.resolution_date is None:
            blockers.append(SignalRejectReason.UNCLEAR_RULES)
        else:
            if signal.resolution_date < now:
                blockers.append(SignalRejectReason.MARKET_INACTIVE)
            if signal.resolution_date > now + timedelta(hours=72):
                blockers.append(SignalRejectReason.RESOLUTION_TOO_FAR)
        trade_side = signal.side or Side.NO
        target_ask = market.ask(trade_side)
        if target_ask is None:
            blockers.append(SignalRejectReason.MISSING_ORDERBOOK)
            return self._evaluation(signal, market, blockers, intended_stake_usd)

        trade_price = target_ask.price_cents
        depth_contracts = target_ask.size.to_integral_value(rounding=ROUND_FLOOR)
        depth_usd = (Decimal(trade_price) * depth_contracts / ONE_HUNDRED).quantize(Decimal("0.01"))
        spread = market.spread_cents(trade_side)
        chase = None if signal.price_cents is None else trade_price - signal.price_cents
        score = self._score(signal, trade_price, depth_usd, spread, chase, now)
        intended_stake_usd = (
            intended_stake_usd
            if intended_stake_usd is not None
            else self._paper_target_stake(depth_usd)
        )
        contracts, stake_usd = self._contracts(trade_price, target_ask.size, intended_stake_usd)
        required_depth_usd = (intended_stake_usd * self.settings.signal_min_depth_multiple).quantize(Decimal("0.01"))
        depth_pass = intended_stake_usd > 0 and contracts >= Decimal("1") and depth_usd >= required_depth_usd
        trade_market_id = market.market_id_for(trade_side)
        leg = ArbLeg(market.exchange, trade_market_id, trade_side, trade_price, max(contracts, Decimal("1")))
        estimated_probability = self._estimated_probability(trade_price, score)
        fee_cents = leg_fee_cents_per_contract(leg, self.settings)
        buffers = Decimal(self.settings.slippage_cents + self.settings.fee_buffer_cents)
        expected_profit_cents = (estimated_probability * ONE_HUNDRED) - Decimal(trade_price) - fee_cents - buffers

        if intended_stake_usd <= 0:
            blockers.append(SignalRejectReason.ZERO_STAKE_CONFIG)
        elif not depth_pass:
            blockers.append(SignalRejectReason.INSUFFICIENT_FILLABLE_DEPTH)
        if spread is None:
            blockers.append(SignalRejectReason.SPREAD_WIDE)
        if chase is not None and chase > self.settings.signal_paper_max_chase_cents:
            blockers.append(SignalRejectReason.CHASE)
        if spread is not None and spread > self.settings.signal_paper_max_spread_cents:
            blockers.append(SignalRejectReason.SPREAD_WIDE)
        if score < self.settings.signal_paper_min_score:
            blockers.append(SignalRejectReason.SCORE_LOW)
        if expected_profit_cents < self.settings.signal_paper_min_ev_cents:
            blockers.append(SignalRejectReason.EV_LOW)
        if self.risk_store and enforce_risk_limits:
            blockers.extend(
                self.risk_store.blockers(
                    signal,
                    market.market_id,
                    stake_usd,
                    enforce_daily_loss_limit=enforce_daily_loss_limit,
                )
            )
        paper_blockers = _dedupe(blockers)

        live_blockers = list(blockers)
        if depth_usd < self.settings.signal_min_depth_usd:
            live_blockers.append(SignalRejectReason.LIQUIDITY_LOW)
        if trade_price < self.settings.signal_no_min_cents:
            live_blockers.append(SignalRejectReason.NO_PRICE_TOO_LOW)
        if trade_price > self.settings.signal_no_max_cents:
            live_blockers.append(SignalRejectReason.NO_PRICE_TOO_HIGH)
        if chase is not None and chase > self.settings.signal_max_chase_cents:
            live_blockers.append(SignalRejectReason.CHASE)
        if spread is not None and spread > self.settings.signal_max_spread_cents:
            live_blockers.append(SignalRejectReason.SPREAD_WIDE)
        if score < self.settings.signal_min_score:
            live_blockers.append(SignalRejectReason.SCORE_LOW)
        if expected_profit_cents < self.settings.signal_min_ev_cents:
            live_blockers.append(SignalRejectReason.EV_LOW)

        return SignalEvaluation(
            signal=signal,
            exchange=market.exchange,
            market_id=trade_market_id,
            no_price_cents=trade_price,
            signal_price_cents=signal.price_cents,
            chase_cents=chase,
            spread_cents=spread,
            depth_contracts=depth_contracts,
            depth_usd=depth_usd,
            score=score,
            estimated_probability=estimated_probability,
            expected_profit_cents=expected_profit_cents,
            stake_usd=stake_usd,
            contracts=contracts,
            blockers=tuple(_dedupe(live_blockers)),
            required_depth_usd=required_depth_usd,
            depth_pass=depth_pass,
            decision_tier=_decision_tier(score),
            paper_allowed=len(paper_blockers) == 0,
            live_candidate=len(_dedupe(live_blockers)) == 0,
            reject_category=_reject_category(paper_blockers),
            paper_blockers=paper_blockers,
            live_blockers=_dedupe(live_blockers),
        )

    def opportunity(self, evaluation: SignalEvaluation, for_paper: bool = False) -> EVOpportunity:
        exchange = evaluation.exchange or Exchange.KALSHI
        market_id = evaluation.market_id or ""
        leg = ArbLeg(
            exchange=exchange,
            market_id=market_id,
            side=evaluation.signal.side or Side.NO,
            price_cents=evaluation.no_price_cents,
            size=evaluation.contracts,
        )
        blockers = [] if for_paper and evaluation.paper_allowed else list(evaluation.blockers)
        if not for_paper and not self.settings.live_trading:
            blockers.append("live trading disabled")
        return EVOpportunity(
            name=evaluation.signal.title or market_id,
            leg=leg,
            live_price_cents=evaluation.no_price_cents,
            fair_value_cents=evaluation.estimated_probability * ONE_HUNDRED,
            edge_cents=(evaluation.estimated_probability * ONE_HUNDRED) - Decimal(evaluation.no_price_cents),
            ev_pct=_profit_pct(evaluation.expected_profit_cents, evaluation.no_price_cents),
            expected_profit_cents=evaluation.expected_profit_cents,
            expected_profit_usd=(evaluation.expected_profit_cents * evaluation.contracts / ONE_HUNDRED),
            stake_usd=evaluation.stake_usd,
            executable=len(blockers) == 0,
            blockers=tuple(blockers),
        )

    def _evaluation(
        self,
        signal: SignalEvent,
        market: SignalMarket | None,
        blockers: list[str],
        intended_stake_usd: Decimal | None = None,
    ) -> SignalEvaluation:
        intended_stake_usd = intended_stake_usd if intended_stake_usd is not None else self.settings.signal_paper_min_trade_usd
        return SignalEvaluation(
            signal=signal,
            exchange=None if market is None else market.exchange,
            market_id=None if market is None or signal.side is None else market.market_id_for(signal.side),
            no_price_cents=0 if market is None or signal.side is None or market.ask(signal.side) is None else market.ask(signal.side).price_cents,
            signal_price_cents=signal.price_cents,
            chase_cents=None,
            spread_cents=None if market is None or signal.side is None else market.spread_cents(signal.side),
            depth_contracts=Decimal("0"),
            depth_usd=Decimal("0"),
            score=0,
            estimated_probability=Decimal("0"),
            expected_profit_cents=Decimal("-100"),
            stake_usd=Decimal("0"),
            contracts=Decimal("0"),
            blockers=tuple(blockers),
            required_depth_usd=(intended_stake_usd * self.settings.signal_min_depth_multiple).quantize(Decimal("0.01")),
            depth_pass=False,
            decision_tier="log_only",
            paper_allowed=False,
            live_candidate=False,
            reject_category=_reject_category(blockers),
            paper_blockers=tuple(blockers),
            live_blockers=tuple(blockers),
        )

    def _score(
        self,
        signal: SignalEvent,
        no_price: int,
        depth_usd: Decimal,
        spread: int | None,
        chase: int | None,
        now: datetime,
    ) -> int:
        score = 45 if signal.channel == "fade_finder" else 35
        score += min(15, int(max(signal.wallet_pnl_usd, Decimal("0")) / Decimal("1000")))
        score += min(10, int(max(signal.amount_usd, Decimal("0")) / Decimal("1000")))
        score += min(10, signal.profitable_wallet_count * 3)
        if signal.channel == "fade_finder" and signal.side is not None and signal.losing_side is _opposite(signal.side):
            score += 10
        score += min(8, signal.losing_wallet_count * 2)
        if self.settings.signal_no_min_cents <= no_price <= self.settings.signal_no_max_cents:
            score += 8
        if depth_usd >= self.settings.signal_min_depth_usd * Decimal("2"):
            score += 5
        if spread is not None and spread <= max(1, self.settings.signal_max_spread_cents // 2):
            score += 5
        age_seconds = max(0, (now - signal.detected_at).total_seconds())
        if age_seconds <= 300:
            score += 4
        if signal.resolution_date:
            hours = (signal.resolution_date - now).total_seconds() / 3600
            if 0 <= hours <= 24:
                score += 5
            elif 24 < hours <= 72:
                score += 2
        if chase is not None and chase > 0:
            score -= min(20, chase * 4)
        if spread is not None and spread > self.settings.signal_max_spread_cents:
            score -= min(20, spread * 2)
        if no_price > self.settings.signal_no_max_cents:
            score -= min(20, no_price - self.settings.signal_no_max_cents)
        return max(0, min(100, score))

    def _estimated_probability(self, no_price: int, score: int) -> Decimal:
        market_probability = Decimal(no_price) / ONE_HUNDRED
        score_edge = Decimal(max(0, score - 50)) / Decimal("1000")
        return min(Decimal("0.95"), market_probability + score_edge)

    def _contracts(self, price_cents: int, displayed_size: Decimal, target_usd: Decimal) -> tuple[Decimal, Decimal]:
        if price_cents <= 0 or target_usd <= 0:
            return Decimal("0"), Decimal("0")
        contracts = (target_usd / (Decimal(price_cents) / ONE_HUNDRED)).to_integral_value(rounding=ROUND_FLOOR)
        contracts = min(contracts, displayed_size.to_integral_value(rounding=ROUND_FLOOR))
        stake_usd = (contracts * Decimal(price_cents) / ONE_HUNDRED).quantize(Decimal("0.01"))
        return contracts, stake_usd

    def _paper_target_stake(self, depth_usd: Decimal) -> Decimal:
        max_stake = min(self.settings.signal_paper_trade_usd, self.settings.signal_paper_max_trade_usd)
        if max_stake <= 0:
            return Decimal("0")
        min_stake = max(Decimal("0"), self.settings.signal_paper_min_trade_usd)
        multiple = self.settings.signal_min_depth_multiple
        depth_supported_stake = depth_usd if multiple <= 0 else depth_usd / multiple
        if depth_supported_stake < min_stake:
            return min_stake.quantize(Decimal("0.01"))
        return min(max_stake, depth_supported_stake).quantize(Decimal("0.01"))


class SignalBotRunner:
    def __init__(
        self,
        predictionhunt,
        kalshi,
        polymarket,
        settings: Settings,
        log_dir: str | Path = "logs",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.predictionhunt = predictionhunt
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.settings = settings
        self.log_dir = Path(log_dir)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.normalizer = SignalNormalizer()
        self.resolver = SignalMarketResolver(kalshi, polymarket)
        self.risk = SignalRiskStore(
            self.log_dir,
            settings.signal_cooldown_seconds,
            settings.signal_daily_loss_limit_usd,
            clock=self.clock,
        )
        self.engine = SignalEngine(settings, self.risk)
        self.executor = TradeExecutor(kalshi, polymarket, allowed_workflow="run-signal-bot")

    async def run(self, execute: bool, once: bool = False) -> None:
        if not self.settings.predictionhunt_ws_url:
            raise RuntimeError("PREDICTIONHUNT_WS_URL is required for run-signal-bot")
        retry_seconds = 1
        while True:
            try:
                stream = self.predictionhunt.listen_signal_channels(
                    self.settings.predictionhunt_ws_url,
                    self.settings.predictionhunt_signal_channels,
                )
                await self.consume(stream, execute=execute, once=once)
                return
            except Exception as exc:
                self._write_jsonl(
                    "signal_stream_errors.jsonl",
                    {
                        "timestamp": self.clock().isoformat(),
                        "action": "stream_error",
                        "message": str(exc),
                        "retry_seconds": retry_seconds,
                    },
                )
                print(f"signal stream error: {exc}; reconnecting in {retry_seconds}s")
                if once:
                    return
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 60)

    async def consume(
        self,
        stream: AsyncIterable[dict[str, Any]],
        execute: bool,
        once: bool = False,
    ) -> None:
        async for raw in stream:
            self._write_jsonl("signal_raw.jsonl", {"timestamp": self.clock().isoformat(), "raw": raw})
            if _is_control_message(raw):
                print(f"signal stream: {raw.get('type', raw.get('action', 'message'))} {raw.get('message', '')}")
                if once:
                    return
                continue
            decision = await asyncio.to_thread(self.process_raw, raw, execute)
            print(
                f"signal {decision.action}: {decision.evaluation.signal.channel} "
                f"{decision.evaluation.market_id or 'unmatched'} "
                f"score={decision.evaluation.score} ev={_decimal_str(decision.evaluation.expected_profit_cents)}c "
                f"{decision.message}"
            )
            if once:
                return

    def process_raw(self, raw: dict[str, Any], execute: bool) -> SignalDecision:
        now = self.clock()
        signal = self.normalizer.normalize(raw, now)
        resolver_error = None
        try:
            market = self.resolver.resolve(signal)
        except Exception as exc:
            resolver_error = str(exc)
            market = None
        target_stake_usd = self.settings.signal_live_trade_usd if execute else None
        evaluation = self.engine.evaluate(
            signal,
            market,
            now,
            target_stake_usd,
            enforce_daily_loss_limit=execute,
            enforce_risk_limits=execute or self.settings.signal_paper_enforce_cooldown,
        )
        if resolver_error:
            category = _resolver_reject_reason(resolver_error)
            evaluation = _with_resolver_error(evaluation, category, resolver_error)
        if not execute and evaluation.market_id:
            duplicate_blockers = self.risk.paper_duplicate_blockers(signal, evaluation.market_id)
            if duplicate_blockers:
                paper_blockers = _dedupe(list(evaluation.paper_blockers) + duplicate_blockers)
                live_blockers = _dedupe(list(evaluation.live_blockers) + duplicate_blockers)
                evaluation = replace(
                    evaluation,
                    blockers=live_blockers,
                    paper_blockers=paper_blockers,
                    live_blockers=live_blockers,
                    paper_allowed=False,
                    live_candidate=False,
                    reject_category=_reject_category(paper_blockers),
                )
        accepted = evaluation.live_candidate if execute else evaluation.paper_allowed
        action = "accepted" if accepted else "rejected"
        active_blockers = evaluation.live_blockers if execute else evaluation.paper_blockers
        message = "paper signal" if accepted and not execute else "; ".join(active_blockers)
        if resolver_error and not accepted:
            message = f"{message}: {resolver_error}"
        mode = "live" if execute else "paper"
        self._log_candidate(evaluation, action, message, resolver_error)
        if not accepted:
            return SignalDecision(action, mode, message, False, evaluation)

        opportunity = self.engine.opportunity(evaluation, for_paper=not execute)
        submitted = False
        trade_message = "paper signal"
        if execute:
            submitted, trade_message = self.executor.execute_ev(opportunity, workflow="run-signal-bot")
        self._log_trade(evaluation, opportunity, mode, "executed" if submitted else "paper", trade_message)
        self._log_profit_row(evaluation, opportunity, mode, "executed" if submitted else "paper")
        if not execute:
            self._log_paper_analysis_row(evaluation, opportunity, trade_message)
        if not execute or submitted:
            assert evaluation.market_id is not None
            self.risk.mark(signal, evaluation.market_id)
        return SignalDecision("accepted", mode, trade_message, True, evaluation)

    def _log_candidate(
        self,
        evaluation: SignalEvaluation,
        action: str,
        message: str,
        resolver_error: str | None = None,
    ) -> None:
        self._write_jsonl(
            "signal_candidates.jsonl",
            {
                "timestamp": self.clock().isoformat(),
                "action": action,
                "message": message,
                "resolver_error": resolver_error,
                "signal": _signal_record(evaluation.signal),
                "evaluation": _evaluation_record(evaluation),
            },
        )

    def _log_trade(
        self,
        evaluation: SignalEvaluation,
        opportunity: EVOpportunity,
        mode: str,
        action: str,
        message: str,
    ) -> None:
        self._write_jsonl(
            "signal_live_trades.jsonl" if mode == "live" else "signal_paper_trades.jsonl",
            {
                "timestamp": self.clock().isoformat(),
                "mode": mode,
                "action": action,
                "message": message,
                "signal": _signal_record(evaluation.signal),
                "evaluation": _evaluation_record(evaluation),
                "verified": _opportunity_record(opportunity),
            },
        )

    def _log_profit_row(
        self,
        evaluation: SignalEvaluation,
        opportunity: EVOpportunity,
        mode: str,
        action: str,
    ) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / "trade_profit.csv"
        headers = [
            "timestamp",
            "mode",
            "strategy",
            "action",
            "bet",
            "resolution_time",
            "ev_exchange",
            "ev_side",
            "ev_contracts",
            "ev_price_cents",
            "ev_cost_usd",
            "percent_gain",
            "total_profit_usd",
            "profit_cents_per_contract",
            "fees_and_buffers_cents_per_contract",
        ]
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(
                {
                    "timestamp": self.clock().isoformat(),
                    "mode": mode,
                    "strategy": "signal",
                    "action": action,
                    "bet": opportunity.name,
                    "resolution_time": None
                    if evaluation.signal.resolution_date is None
                    else evaluation.signal.resolution_date.isoformat(),
                    "ev_exchange": opportunity.leg.exchange.value,
                    "ev_side": opportunity.leg.side.value,
                    "ev_contracts": _decimal_str(opportunity.leg.size),
                    "ev_price_cents": opportunity.leg.price_cents,
                    "ev_cost_usd": _decimal_str(opportunity.stake_usd),
                    "percent_gain": _decimal_str(opportunity.ev_pct),
                    "total_profit_usd": _decimal_str(opportunity.expected_profit_usd),
                    "profit_cents_per_contract": _decimal_str(opportunity.expected_profit_cents),
                    "fees_and_buffers_cents_per_contract": "",
                }
            )

    def _log_paper_analysis_row(
        self,
        evaluation: SignalEvaluation,
        opportunity: EVOpportunity,
        message: str,
    ) -> None:
        self._write_csv_row(
            "signal_paper_trades.csv",
            _signal_paper_headers(),
            _signal_paper_row(self.clock().isoformat(), evaluation, opportunity, message),
        )

    def _write_csv_row(self, filename: str, headers: list[str], row: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / filename
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    def _write_jsonl(self, filename: str, record: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with (self.log_dir / filename).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value)
    if DATE_ONLY_RE.match(text):
        try:
            parsed_date = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None
        return datetime.combine(parsed_date, EVENT_DATE_END_OF_DAY, EVENT_DATE_ZONE).astimezone(timezone.utc)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payload(raw: dict[str, Any]) -> dict[str, Any]:
    merged = dict(raw)
    current = raw
    for key in ("data", "payload", "signal", "trade"):
        value = current.get(key) if isinstance(current, dict) else None
        if not isinstance(value, dict):
            continue
        merged.update(value)
        current = value
        nested = value.get("data")
        if isinstance(nested, dict):
            merged.update(nested)
            current = nested
    return merged


def _is_control_message(raw: dict[str, Any]) -> bool:
    message_type = str(raw.get("type") or raw.get("action") or "").lower()
    if message_type in {"error", "subscribed", "subscription", "ack", "pong", "heartbeat"}:
        return True
    return bool(raw.get("error")) and not any(
        key in raw for key in ("market_id", "ticker", "slug", "side", "outcome", "payload", "data")
    )


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _optional_str(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _side(value: Any) -> Side | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"no", "buy_no", "buy no", "no_token"}:
        return Side.NO
    if normalized in {"yes", "buy_yes", "buy yes", "yes_token"}:
        return Side.YES
    return None


def _trade_side(action: Any, outcome: Any) -> Side | None:
    outcome_side = _side(outcome)
    action_text = "" if action is None else str(action).strip().lower()
    action_side = _side(action)
    if action_text == "buy":
        return outcome_side or Side.YES
    if action_text == "sell" and outcome_side is not None:
        return _opposite(outcome_side)
    return outcome_side or action_side


def _platform(value: Any) -> Exchange | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized == "kalshi":
        return Exchange.KALSHI
    if normalized == "polymarket":
        return Exchange.POLYMARKET
    return None


def _date_from_text(value: str | None) -> datetime | None:
    if not value:
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if not match:
        return None
    return parse_datetime(match.group(0))


def _opposite(side: Side) -> Side:
    return Side.NO if side is Side.YES else Side.YES


def _price_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if price <= 1:
        price *= ONE_HUNDRED
    return int(price.to_integral_value())


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _profit_pct(profit_cents: Decimal, cost_cents: int) -> Decimal:
    if cost_cents <= 0:
        return Decimal("0")
    return ((profit_cents / Decimal(cost_cents)) * ONE_HUNDRED).quantize(Decimal("0.01"))


def _decimal_str(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _dedupe(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _decision_tier(score: int) -> str:
    if score >= 80:
        return "strong_paper"
    if score >= 70:
        return "paper_candidate"
    if score >= 60:
        return "log_only"
    return "low_quality"


def _reject_category(blockers: tuple[str, ...] | list[str]) -> str | None:
    for blocker in blockers:
        if blocker in {
            SignalRejectReason.MARKET_UNMATCHED,
            SignalRejectReason.MARKET_LOOKUP_FAILED,
            SignalRejectReason.UNSUPPORTED_MARKET_TYPE,
            SignalRejectReason.OUTCOME_TOKEN_NOT_FOUND,
            SignalRejectReason.MISSING_ORDERBOOK,
            SignalRejectReason.API_VALIDATION_ERROR,
        }:
            return blocker
        if blocker in {SignalRejectReason.DUPLICATE, SignalRejectReason.CONFLICTING_EXPOSURE, SignalRejectReason.RISK_LIMIT}:
            return "risk"
        if blocker == SignalRejectReason.ZERO_STAKE_CONFIG:
            return blocker
    return blockers[0] if blockers else None


def _resolver_reject_reason(error: str) -> str:
    text = error.lower()
    if "outcome" in text and ("no clob token" in text or "token" in text):
        return SignalRejectReason.OUTCOME_TOKEN_NOT_FOUND
    if "unsupported_market_type" in text or "no no clob token" in text or "no clob token" in text:
        return SignalRejectReason.UNSUPPORTED_MARKET_TYPE
    if "validation_error" in text or "wrong value" in text or "422" in text:
        return SignalRejectReason.API_VALIDATION_ERROR
    if "gamma market not found" in text or "not found" in text:
        return SignalRejectReason.MARKET_LOOKUP_FAILED
    if "orderbook" in text or "/book" in text:
        return SignalRejectReason.MISSING_ORDERBOOK
    return SignalRejectReason.MARKET_LOOKUP_FAILED


def _with_resolver_error(evaluation: SignalEvaluation, category: str, error: str) -> SignalEvaluation:
    blockers = _dedupe([category if item == SignalRejectReason.MARKET_UNMATCHED else item for item in evaluation.blockers])
    return replace(
        evaluation,
        blockers=blockers,
        paper_allowed=False,
        live_candidate=False,
        reject_category=category,
        resolver_error=error,
        paper_blockers=blockers,
        live_blockers=blockers,
    )


def _signal_record(signal: SignalEvent) -> dict[str, Any]:
    return {
        "channel": signal.channel,
        "market_id": signal.market_id,
        "platform": None if signal.platform is None else signal.platform.value,
        "side": None if signal.side is None else signal.side.value,
        "outcome": signal.outcome,
        "price_cents": signal.price_cents,
        "amount_usd": str(signal.amount_usd),
        "wallet_pnl_usd": str(signal.wallet_pnl_usd),
        "profitable_wallet_count": signal.profitable_wallet_count,
        "losing_wallet_count": signal.losing_wallet_count,
        "losing_side": None if signal.losing_side is None else signal.losing_side.value,
        "resolution_date": None if signal.resolution_date is None else signal.resolution_date.isoformat(),
        "detected_at": signal.detected_at.isoformat(),
        "group_id": signal.group_id,
        "title": signal.title,
    }


def _evaluation_record(evaluation: SignalEvaluation) -> dict[str, Any]:
    return {
        "exchange": None if evaluation.exchange is None else evaluation.exchange.value,
        "market_id": evaluation.market_id,
        "no_price_cents": evaluation.no_price_cents,
        "signal_price_cents": evaluation.signal_price_cents,
        "chase_cents": evaluation.chase_cents,
        "spread_cents": evaluation.spread_cents,
        "depth_contracts": str(evaluation.depth_contracts),
        "depth_usd": str(evaluation.depth_usd),
        "score": evaluation.score,
        "estimated_probability": str(evaluation.estimated_probability),
        "expected_profit_cents": str(evaluation.expected_profit_cents),
        "stake_usd": str(evaluation.stake_usd),
        "contracts": str(evaluation.contracts),
        "blockers": list(evaluation.blockers),
        "required_depth_usd": str(evaluation.required_depth_usd),
        "depth_pass": evaluation.depth_pass,
        "decision_tier": evaluation.decision_tier,
        "paper_allowed": evaluation.paper_allowed,
        "live_candidate": evaluation.live_candidate,
        "reject_category": evaluation.reject_category,
        "resolver_error": evaluation.resolver_error,
        "paper_blockers": list(evaluation.paper_blockers),
        "live_blockers": list(evaluation.live_blockers),
    }


def _opportunity_record(opportunity: EVOpportunity) -> dict[str, Any]:
    return {
        "name": opportunity.name,
        "exchange": opportunity.leg.exchange.value,
        "market_id": opportunity.leg.market_id,
        "side": opportunity.leg.side.value,
        "live_price_cents": opportunity.live_price_cents,
        "fair_value_cents": str(opportunity.fair_value_cents),
        "edge_cents": str(opportunity.edge_cents),
        "expected_profit_cents": str(opportunity.expected_profit_cents),
        "expected_profit_usd": str(opportunity.expected_profit_usd),
        "stake_usd": str(opportunity.stake_usd),
        "executable": opportunity.executable,
        "blockers": list(opportunity.blockers),
    }


def _signal_paper_headers() -> list[str]:
    return [
        "paper_trade_id",
        "timestamp",
        "status",
        "message",
        "decision_tier",
        "paper_allowed",
        "live_candidate",
        "required_depth_usd",
        "depth_pass",
        "reject_category",
        "resolver_error",
        "channel",
        "title",
        "group_id",
        "source_platform",
        "exchange",
        "market_id",
        "side",
        "signal_price_cents",
        "entry_price_cents",
        "chase_cents",
        "spread_cents",
        "depth_contracts",
        "depth_usd",
        "contracts",
        "stake_usd",
        "score",
        "estimated_probability_pct",
        "fair_value_cents",
        "edge_cents",
        "expected_profit_cents_per_contract",
        "expected_profit_usd",
        "ev_pct",
        "amount_usd",
        "wallet_pnl_usd",
        "profitable_wallet_count",
        "losing_wallet_count",
        "losing_side",
        "detected_at",
        "resolution_date",
        "hours_to_resolution",
        "price_bucket",
        "score_bucket",
        "wallet_pnl_bucket",
        "trade_size_bucket",
        "liquidity_bucket",
        "spread_bucket",
        "result_status",
        "resolved_outcome",
        "exit_value_usd",
        "realized_pnl_usd",
        "notes",
    ]


def _signal_paper_row(
    timestamp: str,
    evaluation: SignalEvaluation,
    opportunity: EVOpportunity,
    message: str,
) -> dict[str, Any]:
    signal = evaluation.signal
    hours_to_resolution = ""
    if signal.resolution_date is not None:
        detected_at = signal.detected_at
        hours = (signal.resolution_date - detected_at).total_seconds() / 3600
        hours_to_resolution = str(Decimal(str(max(0, hours))).quantize(Decimal("0.01")))
    return {
        "paper_trade_id": _paper_trade_id(signal, evaluation),
        "timestamp": timestamp,
        "status": "open",
        "message": message,
        "decision_tier": evaluation.decision_tier,
        "paper_allowed": evaluation.paper_allowed,
        "live_candidate": evaluation.live_candidate,
        "required_depth_usd": _decimal_str(evaluation.required_depth_usd),
        "depth_pass": evaluation.depth_pass,
        "reject_category": evaluation.reject_category or "",
        "resolver_error": evaluation.resolver_error or "",
        "channel": signal.channel,
        "title": signal.title,
        "group_id": signal.group_id or "",
        "source_platform": "" if signal.platform is None else signal.platform.value,
        "exchange": opportunity.leg.exchange.value,
        "market_id": opportunity.leg.market_id,
        "side": opportunity.leg.side.value,
        "signal_price_cents": "" if evaluation.signal_price_cents is None else evaluation.signal_price_cents,
        "entry_price_cents": opportunity.leg.price_cents,
        "chase_cents": "" if evaluation.chase_cents is None else evaluation.chase_cents,
        "spread_cents": "" if evaluation.spread_cents is None else evaluation.spread_cents,
        "depth_contracts": _decimal_str(evaluation.depth_contracts),
        "depth_usd": _decimal_str(evaluation.depth_usd),
        "contracts": _decimal_str(opportunity.leg.size),
        "stake_usd": _decimal_str(opportunity.stake_usd),
        "score": evaluation.score,
        "estimated_probability_pct": _decimal_str(evaluation.estimated_probability * ONE_HUNDRED),
        "fair_value_cents": "" if opportunity.fair_value_cents is None else _decimal_str(opportunity.fair_value_cents),
        "edge_cents": "" if opportunity.edge_cents is None else _decimal_str(opportunity.edge_cents),
        "expected_profit_cents_per_contract": _decimal_str(opportunity.expected_profit_cents),
        "expected_profit_usd": _decimal_str(opportunity.expected_profit_usd),
        "ev_pct": _decimal_str(opportunity.ev_pct),
        "amount_usd": _decimal_str(signal.amount_usd),
        "wallet_pnl_usd": _decimal_str(signal.wallet_pnl_usd),
        "profitable_wallet_count": signal.profitable_wallet_count,
        "losing_wallet_count": signal.losing_wallet_count,
        "losing_side": "" if signal.losing_side is None else signal.losing_side.value,
        "detected_at": signal.detected_at.isoformat(),
        "resolution_date": "" if signal.resolution_date is None else signal.resolution_date.isoformat(),
        "hours_to_resolution": hours_to_resolution,
        "price_bucket": _cents_bucket(opportunity.leg.price_cents),
        "score_bucket": _score_bucket(evaluation.score),
        "wallet_pnl_bucket": _usd_bucket(signal.wallet_pnl_usd),
        "trade_size_bucket": _usd_bucket(signal.amount_usd),
        "liquidity_bucket": _usd_bucket(evaluation.depth_usd),
        "spread_bucket": _spread_bucket(evaluation.spread_cents),
        "result_status": "pending",
        "resolved_outcome": "",
        "exit_value_usd": "",
        "realized_pnl_usd": "",
        "notes": "",
    }


def _paper_trade_id(signal: SignalEvent, evaluation: SignalEvaluation) -> str:
    parts = [
        signal.detected_at.isoformat(),
        signal.channel,
        evaluation.market_id or signal.market_id or "",
        "" if signal.side is None else signal.side.value,
        str(evaluation.no_price_cents),
    ]
    return "|".join(parts)


def _cents_bucket(value: int) -> str:
    lower = (value // 5) * 5
    upper = lower + 4
    return f"{lower}-{upper}c"


def _score_bucket(value: int) -> str:
    lower = (value // 10) * 10
    upper = min(100, lower + 9)
    return f"{lower}-{upper}"


def _usd_bucket(value: Decimal) -> str:
    if value < Decimal("100"):
        return "<$100"
    if value < Decimal("500"):
        return "$100-$499"
    if value < Decimal("1000"):
        return "$500-$999"
    if value < Decimal("5000"):
        return "$1k-$4.9k"
    if value < Decimal("10000"):
        return "$5k-$9.9k"
    return "$10k+"


def _spread_bucket(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value <= 1:
        return "0-1c"
    if value <= 3:
        return "2-3c"
    if value <= 5:
        return "4-5c"
    return "6c+"


def _market_ids_from_record(record: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("evaluation", "verified"):
        value = record.get(key)
        if isinstance(value, dict) and value.get("market_id"):
            ids.append(str(value["market_id"]))
    predictionhunt = record.get("predictionhunt")
    if isinstance(predictionhunt, dict):
        for leg in predictionhunt.get("legs", []):
            if isinstance(leg, dict) and leg.get("market_id"):
                ids.append(str(leg["market_id"]))
    return ids


def _signal_trade_key(signal: SignalEvent, market_id: str) -> str:
    side = "" if signal.side is None else signal.side.value
    return f"{market_id}|{side}".lower()


def _signal_trade_key_from_record(record: dict[str, Any]) -> str | None:
    evaluation = record.get("evaluation")
    signal = record.get("signal")
    if not isinstance(evaluation, dict) or not isinstance(signal, dict):
        return None
    market_id = evaluation.get("market_id")
    side = signal.get("side") or evaluation.get("side")
    if not market_id or not side:
        return None
    return f"{market_id}|{side}".lower()
