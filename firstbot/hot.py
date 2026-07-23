from __future__ import annotations

import asyncio
import csv
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from .config import Settings
from .executor import (
    TradeExecutor,
    _cross_50_block_reason,
    _largest_profitable_blended_fill,
    _source_price_alignment_block_reason,
)
from .fees import total_cost_adjustment_cents
from .exchanges import KalshiClient, PolymarketClient
from .matching import MarketMatchingEngine
from .models import ArbLeg, ArbOpportunity, BookLevel, Exchange, Side
from .predictionhunt import PredictionHuntClient, PredictionHuntLeg, PredictionHuntOpportunity
from .readiness import preflight_hot_candidate


DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KALSHI_TICKER_DATE_RE = re.compile(
    r"(?:^|-)(?P<year>\d{2})(?P<month>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<day>\d{2})",
    re.IGNORECASE,
)
KALSHI_MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
VENUE_DATE_FIELDS = (
    "close_time",
    "expiration_time",
    "latest_expiration_time",
    "expected_expiration_time",
    "settlement_timer",
    "last_trading_time",
    "end_date",
    "event_date",
)
EVENT_DATE_ZONE = ZoneInfo("America/New_York")
EVENT_DATE_END_OF_DAY = time(23, 59, 59)


@dataclass(frozen=True)
class LiveLegBook:
    exchange: Exchange
    market_id: str
    side: Side
    best_ask: BookLevel | None
    updated_at: datetime | None
    connected: bool = True
    snapshot_ready: bool = False
    ask_levels: tuple[BookLevel, ...] = ()

    def is_fresh(self, now: datetime, stale_ms: int) -> bool:
        if not self.connected or not self.snapshot_ready or self.updated_at is None:
            return False
        age_ms = (now - self.updated_at).total_seconds() * 1000
        return age_ms <= stale_ms


@dataclass
class HotWatch:
    key: str
    opportunity: PredictionHuntOpportunity
    expires_at: datetime
    event_date: datetime
    outcome_keys: dict[tuple[Exchange, str, Side], str] = field(default_factory=dict)
    allowed_pairs: set[
        tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]
    ] = field(default_factory=set)
    triggered: bool = False
    books: dict[tuple[Exchange, str, Side], LiveLegBook] | None = None
    near_misses_logged: set[int] = field(default_factory=set)

    def refresh(
        self,
        opportunity: PredictionHuntOpportunity,
        expires_at: datetime,
        outcome_keys: dict[tuple[Exchange, str, Side], str] | None = None,
        allowed_pairs: set[
            tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]
        ] | None = None,
    ) -> None:
        self.opportunity = opportunity
        self.expires_at = expires_at
        if outcome_keys is not None:
            self.outcome_keys = outcome_keys
        if allowed_pairs is not None:
            self.allowed_pairs = allowed_pairs


class HotWatchManager:
    def __init__(
        self,
        hot_window_seconds: int,
        max_days_to_resolution: int,
        max_active_watches: int,
        prefer_same_day: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.hot_window_seconds = hot_window_seconds
        self.max_days_to_resolution = max_days_to_resolution
        self.max_active_watches = max_active_watches
        self.prefer_same_day = prefer_same_day
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.watches: dict[str, HotWatch] = {}

    def add_or_refresh(
        self,
        opportunity: PredictionHuntOpportunity,
        outcome_keys: dict[tuple[Exchange, str, Side], str] | None = None,
        allowed_pairs: set[
            tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]
        ] | None = None,
    ) -> tuple[HotWatch | None, str]:
        skip_reason = self.skip_reason(opportunity)
        if skip_reason:
            return None, skip_reason
        now = self.clock()
        event_date = parse_datetime(opportunity.event_date)
        assert event_date is not None
        key = watch_key(opportunity)
        expires_at = now + timedelta(seconds=self.hot_window_seconds)
        if key in self.watches:
            self.watches[key].refresh(opportunity, expires_at, outcome_keys, allowed_pairs)
            return self.watches[key], "refreshed"
        watch = HotWatch(
            key=key,
            opportunity=opportunity,
            expires_at=expires_at,
            event_date=event_date,
            outcome_keys=outcome_keys or {},
            allowed_pairs=allowed_pairs or set(),
        )
        self.watches[key] = watch
        self._evict_if_needed()
        if key not in self.watches:
            return None, "evicted by higher-priority watches"
        return watch, "added"

    def active(self) -> list[HotWatch]:
        now = self.clock()
        expired = [key for key, watch in self.watches.items() if watch.expires_at <= now]
        for key in expired:
            del self.watches[key]
        return sorted(self.watches.values(), key=self._priority, reverse=True)

    def skip_reason(self, opportunity: PredictionHuntOpportunity) -> str | None:
        if len(opportunity.legs) < 2:
            return "opportunity does not have enough legs"
        platforms = {leg.platform for leg in opportunity.legs}
        if not {Exchange.KALSHI, Exchange.POLYMARKET}.issubset(platforms):
            return "not a Kalshi/Polymarket opportunity"
        event_date = parse_datetime(opportunity.event_date)
        if event_date is None:
            return "missing or unparseable event_date"
        now = self.clock()
        if event_date < now:
            return "event_date is in the past"
        if event_date > now + timedelta(days=self.max_days_to_resolution):
            return f"event_date is more than {self.max_days_to_resolution} days away"
        return None

    def _evict_if_needed(self) -> None:
        while len(self.watches) > self.max_active_watches:
            lowest = min(self.watches.values(), key=self._priority)
            del self.watches[lowest.key]

    def _priority(self, watch: HotWatch) -> tuple[int, float, Decimal]:
        now = self.clock()
        same_day = int(self.prefer_same_day and watch.event_date.date() == now.date())
        seconds_until_event = (watch.event_date - now).total_seconds()
        return same_day, -seconds_until_event, watch.opportunity.roi_pct


class HotTriggerEngine:
    def __init__(
        self,
        settings: Settings,
        trigger_cost_cents: int,
        near_miss_cost_cents: int,
        stale_ms: int,
        executor: TradeExecutor | None = None,
    ) -> None:
        self.settings = settings
        self.trigger_cost_cents = trigger_cost_cents
        self.near_miss_cost_cents = near_miss_cost_cents
        self.stale_ms = stale_ms
        self.executor = executor
        self.last_execution_opportunity: ArbOpportunity | None = None

    def evaluate(self, watch: HotWatch, now: datetime) -> ArbOpportunity:
        blockers: list[str] = []
        if not watch.books:
            blockers.append("missing live book snapshots")
            return self._blocked(watch, blockers)
        legs: list[ArbLeg] = []
        for ph_leg in watch.opportunity.legs:
            book = watch.books.get((ph_leg.platform, ph_leg.market_id, ph_leg.side))
            if book is None:
                blockers.append(f"missing {ph_leg.platform.value} {ph_leg.side.value} book")
                continue
            if not book.is_fresh(now, self.stale_ms):
                blockers.append(f"stale {ph_leg.platform.value} {ph_leg.side.value} book")
                continue
            if book.best_ask is None:
                blockers.append(f"missing {ph_leg.platform.value} {ph_leg.side.value} ask")
                continue
            legs.append(
                ArbLeg(
                    exchange=ph_leg.platform,
                    market_id=ph_leg.market_id,
                    side=ph_leg.side,
                    price_cents=book.best_ask.price_cents,
                    size=(
                        _contracts_for_budget(self.settings.max_leg_usd, book.best_ask.price_cents)
                        if self.settings.live_trading
                        else min(
                            book.best_ask.size,
                            _contracts_for_budget(self.settings.max_leg_usd, book.best_ask.price_cents),
                        )
                    ),
                    source_price_cents=_predictionhunt_price_cents(ph_leg.price),
                )
            )
        if len(legs) < 2:
            return self._blocked(watch, blockers)
        evaluations = [
            self._evaluate_pair(
                watch.opportunity.group_title,
                first,
                second,
                watch.opportunity.event_type,
            )
            for first in legs
            for second in legs
            if first.exchange is Exchange.POLYMARKET
            and second.exchange is Exchange.KALSHI
            and _legs_are_allowed_pair(watch, first, second)
            and _legs_are_true_opposites(
                watch,
                first,
                second,
                require_known=self.settings.live_trading,
            )
        ]
        if not evaluations:
            blockers.append("missing live Kalshi/Polymarket true-opposite basket")
            return self._blocked(watch, blockers)
        best = max(evaluations, key=lambda item: item.net_profit_cents)
        if blockers and not best.executable:
            best = replace(best, blockers=tuple(dict.fromkeys((*best.blockers, *blockers))))
        return best

    def _evaluate_pair(
        self,
        pair_name: str,
        first: ArbLeg,
        second: ArbLeg,
        event_type: str | None = None,
    ) -> ArbOpportunity:
        blockers: list[str] = []
        first_leg, second_leg = _display_ordered_arb_legs(first, second)
        if min(first_leg.size, second_leg.size) < Decimal("1"):
            blockers.append("displayed depth is below 1 contract")
        matched_size = min(first_leg.size, second_leg.size)
        first_leg = replace(first_leg, size=matched_size)
        second_leg = replace(second_leg, size=matched_size)
        if self.settings.live_trading and self.settings.hot_require_cross_50:
            cross_50_blocker = _cross_50_block_reason(first_leg, second_leg)
            if cross_50_blocker:
                blockers.append(cross_50_blocker)
        if (
            self.settings.live_trading
            and self.settings.hot_require_source_price_alignment
        ):
            for leg in (first_leg, second_leg):
                source_blocker = _source_price_alignment_block_reason(
                    leg,
                    self.settings.hot_source_price_max_deviation_cents,
                )
                if source_blocker:
                    blockers.append(source_blocker)
        gross_cost = first_leg.price_cents + second_leg.price_cents
        buffers = total_cost_adjustment_cents((first_leg, second_leg), self.settings)
        net_profit = Decimal(100 - gross_cost) - buffers
        if net_profit <= 0:
            blockers.append(f"net profit {net_profit}c is not positive after buffers")
        if not self.settings.live_trading:
            blockers.append("live trading disabled")
        return ArbOpportunity(
            pair_name=pair_name,
            buy_yes=first_leg,
            buy_no=second_leg,
            gross_cost_cents=gross_cost,
            buffers_cents=buffers,
            net_profit_cents=net_profit,
            executable=len(blockers) == 0,
            blockers=tuple(blockers),
            event_type=event_type,
        )

    def execute_if_allowed(
        self,
        opportunity: ArbOpportunity,
        execute: bool,
        watch: HotWatch | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        if not execute:
            return False, "paper trigger"
        if not self.executor:
            return False, "executor unavailable"
        if watch is not None and now is not None:
            fast_opportunity, fast_reason = self._fast_path_opportunity(watch, opportunity, now)
            if fast_opportunity is not None:
                self.last_execution_opportunity = fast_opportunity
                return self.executor.execute_fast(fast_opportunity, workflow="run-hot-arb")
            if fast_reason:
                self.last_execution_opportunity = opportunity
                return self.executor.execute(opportunity, workflow="run-hot-arb")
        self.last_execution_opportunity = opportunity
        return self.executor.execute(opportunity, workflow="run-hot-arb")

    def _fast_path_opportunity(
        self,
        watch: HotWatch,
        opportunity: ArbOpportunity,
        now: datetime,
    ) -> tuple[ArbOpportunity | None, str | None]:
        if not self.settings.hot_fast_path:
            return None, "fast path disabled"
        if opportunity.net_profit_cents < self.settings.hot_fast_min_net_edge_cents:
            return None, "fast path edge below threshold"
        yes_book = self._matching_live_book(watch, opportunity.buy_yes)
        no_book = self._matching_live_book(watch, opportunity.buy_no)
        if yes_book is None or no_book is None:
            return None, "fast path missing live book"
        for book in (yes_book, no_book):
            if not book.is_fresh(now, self.settings.hot_fast_max_book_age_ms):
                return None, f"fast path stale {book.exchange.value} {book.side.value} book"
            if book.best_ask is None:
                return None, f"fast path missing {book.exchange.value} {book.side.value} ask"
        assert yes_book.best_ask is not None
        assert no_book.best_ask is not None
        yes_levels = _book_ask_levels(yes_book)
        no_levels = _book_ask_levels(no_book)
        gross_cost = yes_levels[0].price_cents + no_levels[0].price_cents
        if gross_cost <= 0:
            return None, "fast path invalid gross cost"
        contracts_cap = min(
            _whole_contracts(sum((level.size for level in yes_levels), Decimal("0"))),
            _whole_contracts(sum((level.size for level in no_levels), Decimal("0"))),
        )
        if contracts_cap < Decimal("1"):
            return None, "fast path displayed depth below one contract"
        max_leg_usd = min(
            Decimal(self.settings.max_leg_usd),
            Decimal(self.settings.hot_fast_max_total_usd) / Decimal("2"),
        )
        fill = _largest_profitable_blended_fill(
            opportunity.buy_yes,
            opportunity.buy_no,
            yes_levels,
            no_levels,
            contracts_cap,
            max_leg_usd,
            self.settings,
            opportunity.buffers_cents,
        )
        if fill.contracts < Decimal("1"):
            return None, "fast path no profitable blended fill"
        if fill.net_profit_cents < self.settings.hot_fast_min_net_edge_cents:
            return None, "fast path recalculated edge below threshold"
        buy_yes = replace(
            opportunity.buy_yes,
            price_cents=fill.yes_limit_cents,
            size=fill.contracts,
            avg_price_cents=fill.yes_avg_cents,
        )
        buy_no = replace(
            opportunity.buy_no,
            price_cents=fill.no_limit_cents,
            size=fill.contracts,
            avg_price_cents=fill.no_avg_cents,
        )
        if self.settings.hot_require_cross_50:
            cross_50_blocker = _cross_50_block_reason(buy_yes, buy_no)
            if cross_50_blocker:
                return None, f"fast path {cross_50_blocker}"
        if self.settings.hot_require_source_price_alignment:
            for leg in (buy_yes, buy_no):
                source_blocker = _source_price_alignment_block_reason(
                    leg,
                    self.settings.hot_source_price_max_deviation_cents,
                )
                if source_blocker:
                    return None, f"fast path {source_blocker}"
        return (
            replace(
                opportunity,
                buy_yes=buy_yes,
                buy_no=buy_no,
                gross_cost_cents=fill.gross_avg_cents,
                buffers_cents=fill.buffers_cents,
                net_profit_cents=fill.net_profit_cents,
            ),
            None,
        )

    def _matching_live_book(self, watch: HotWatch, leg: ArbLeg) -> LiveLegBook | None:
        if not watch.books:
            return None
        return watch.books.get((leg.exchange, leg.market_id, leg.side))

    def _blocked(self, watch: HotWatch, blockers: list[str]) -> ArbOpportunity:
        empty = ArbLeg(Exchange.KALSHI, "", Side.YES, 0, Decimal("0"))
        return ArbOpportunity(
            pair_name=watch.opportunity.group_title,
            buy_yes=empty,
            buy_no=ArbLeg(Exchange.POLYMARKET, "", Side.NO, 0, Decimal("0")),
            gross_cost_cents=0,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("-100"),
            executable=False,
            blockers=tuple(blockers),
            event_type=watch.opportunity.event_type,
        )


class HotArbRunner:
    def __init__(
        self,
        predictionhunt: PredictionHuntClient,
        kalshi: KalshiClient,
        polymarket: PolymarketClient,
        settings: Settings,
        log_dir: str | Path = "logs",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.predictionhunt = predictionhunt
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.log_dir = _log_dir_path(log_dir)
        self._log_filename_overrides: dict[str, str] = {}
        self._kalshi_market_cache: dict[str, dict] = {}
        self.used_contract_ids: set[str] = set()
        self._live_halt_reason: str | None = None
        self._printed_expiring_candidate_keys: set[str] = set()
        self._printed_expiring_blocked_keys: set[str] = set()
        self._printed_expiring_watch_keys: set[str] = set()
        self.matcher = MarketMatchingEngine(
            kalshi,
            polymarket,
            clock=self.clock,
            log_dir=self.log_dir,
        )

    async def run(
        self,
        category: str | None,
        limit: int,
        predictionhunt_poll_seconds: int,
        hot_window_seconds: int,
        max_days_to_resolution: int,
        prefer_same_day: bool,
        trigger_cost_cents: int,
        near_miss_cost_cents: int,
        stale_ms: int,
        max_active_watches: int,
        execute: bool,
        once: bool = False,
    ) -> None:
        manager = HotWatchManager(
            hot_window_seconds=hot_window_seconds,
            max_days_to_resolution=max_days_to_resolution,
            max_active_watches=max_active_watches,
            prefer_same_day=prefer_same_day,
            clock=self.clock,
        )
        engine = HotTriggerEngine(
            settings=self.settings,
            trigger_cost_cents=trigger_cost_cents,
            near_miss_cost_cents=near_miss_cost_cents,
            stale_ms=stale_ms,
            executor=TradeExecutor(
                self.kalshi,
                self.polymarket,
                max_leg_usd=self.settings.max_leg_usd,
                settings=self.settings,
            ),
        )
        active_tasks: dict[str, asyncio.Task] = {}
        consecutive_poll_errors = 0
        last_heartbeat_at = self.clock()
        while True:
            if execute and self._live_halt_reason:
                print(f"live trading halted: {_short_reason(self._live_halt_reason)}")
                for task in active_tasks.values():
                    task.cancel()
                return
            try:
                opportunities = await asyncio.to_thread(
                    self.predictionhunt.get_arbitrage_opportunities,
                    category or "",
                    limit,
                    0,
                    "polymarket,kalshi",
                )
                consecutive_poll_errors = 0
            except Exception as exc:
                consecutive_poll_errors += 1
                message = str(exc)
                self._log_poll_error(message, consecutive_poll_errors)
                done_keys = [key for key, task in active_tasks.items() if task.done()]
                for key in done_keys:
                    del active_tasks[key]
                retry_seconds = _poll_retry_seconds(predictionhunt_poll_seconds, consecutive_poll_errors)
                last_heartbeat_at = self._print_heartbeat_if_due(
                    last_heartbeat_at,
                    manager,
                    active_tasks,
                    fetched=0,
                    suffix=(
                        f"poll_errors={consecutive_poll_errors} retrying_in={retry_seconds}s "
                        f"last_error={_poll_error_summary(message)}"
                    ),
                )
                if once:
                    return
                await asyncio.sleep(retry_seconds)
                continue
            for opportunity in opportunities:
                event_type_blocker = self._event_type_block_reason(opportunity)
                if event_type_blocker:
                    self._log_candidate(opportunity, "skipped", event_type_blocker)
                    continue
                window_blocker = self._source_event_window_reason(opportunity, max_days_to_resolution)
                if window_blocker:
                    self._log_candidate(opportunity, "skipped", window_blocker)
                    continue
                self._print_fetched_expiring_candidate(
                    opportunity,
                    max_days_to_resolution,
                )
                try:
                    exact_pair_blocker = self._exact_predictionhunt_pair_reason(opportunity)
                    if exact_pair_blocker:
                        self._log_candidate(opportunity, "skipped", exact_pair_blocker)
                        continue
                    opportunity = self._resolve_hot_arb_legs(opportunity)
                    allowed_pairs = self._hot_allowed_pair_keys(opportunity)
                    if not allowed_pairs:
                        self._log_candidate(
                            opportunity,
                            "skipped",
                            "live arb blocked: exact PredictionHunt legs are not one cross-venue BUY YES/BUY NO pair",
                        )
                        continue
                    if execute:
                        try:
                            self.matcher.verify_predictionhunt_opportunity(opportunity)
                        except Exception:
                            pass
                    outcome_keys = _predictionhunt_trusted_outcome_keys({}, allowed_pairs)
                    safety_blocker = self._unsafe_hot_arb_reason(opportunity)
                    if safety_blocker:
                        self._log_candidate(opportunity, "skipped", safety_blocker)
                        continue
                    if execute:
                        venue_blocker = self._venue_resolution_safety_reason(opportunity)
                        if venue_blocker:
                            self._log_candidate(opportunity, "skipped", venue_blocker)
                            continue
                except Exception as exc:
                    self._log_candidate(opportunity, "skipped", f"candidate safety check failed: {exc}")
                    continue
                if self._uses_previously_triggered_contract(opportunity):
                    self._log_candidate(opportunity, "skipped", "contract already used by prior trigger")
                    continue
                candidate_key = watch_key(opportunity)
                if execute and candidate_key not in manager.watches:
                    preflight_blocker = preflight_hot_candidate(self.kalshi, self.polymarket, opportunity)
                    if preflight_blocker:
                        self._log_candidate(opportunity, "preflight_failed", preflight_blocker)
                        continue
                watch, status = manager.add_or_refresh(opportunity, outcome_keys, allowed_pairs)
                if watch is None:
                    self._log_candidate(opportunity, "skipped", status)
                    continue
                self._log_candidate(opportunity, status, "hot watch active")
                if watch.key not in active_tasks:
                    active_tasks[watch.key] = asyncio.create_task(
                        self._watch_market(watch, engine, execute)
                    )
                self._print_watching_expiring_market(
                    watch.opportunity,
                    max_days_to_resolution,
                    watch.key,
                )
            done_keys = [key for key, task in active_tasks.items() if task.done()]
            for key in done_keys:
                del active_tasks[key]
            if execute and self._live_halt_reason:
                print(f"live trading halted: {_short_reason(self._live_halt_reason)}")
                for task in active_tasks.values():
                    task.cancel()
                return
            last_heartbeat_at = self._print_heartbeat_if_due(
                last_heartbeat_at,
                manager,
                active_tasks,
                fetched=len(opportunities),
            )
            if once:
                for task in active_tasks.values():
                    task.cancel()
                return
            await asyncio.sleep(predictionhunt_poll_seconds)

    def _print_heartbeat_if_due(
        self,
        last_heartbeat_at: datetime,
        manager: HotWatchManager,
        active_tasks: dict[str, asyncio.Task],
        fetched: int,
        suffix: str = "",
    ) -> datetime:
        now = self.clock()
        if (now - last_heartbeat_at).total_seconds() < 600:
            return last_heartbeat_at
        details = (
            f"still running: fetched={fetched} "
            f"active_watches={len(manager.active())} tasks={len(active_tasks)}"
        )
        if suffix:
            details = f"{details} {suffix}"
        print(details)
        return now

    def _print_fetched_expiring_candidate(
        self,
        opportunity: PredictionHuntOpportunity,
        max_days_to_resolution: int,
    ) -> None:
        key = watch_key(opportunity)
        if key in self._printed_expiring_candidate_keys:
            return
        event_date = parse_datetime(opportunity.event_date)
        if event_date is None:
            return
        now = self.clock()
        if event_date < now or event_date > now + timedelta(days=max_days_to_resolution):
            return
        self._printed_expiring_candidate_keys.add(key)
        print(
            "fetched expiring candidate: "
            f"expires_in={_duration_until(event_date, now)} "
            f"event_date={opportunity.event_date} "
            f"roi={opportunity.roi_pct}% "
            f"max_wager=${opportunity.max_wager_usd} "
            f"group_id={opportunity.group_id} "
            f"title={_short_reason(opportunity.group_title, limit=120)}"
        )

    def _print_expiring_candidate_blocked(
        self,
        opportunity: PredictionHuntOpportunity,
        reason: str,
    ) -> None:
        key = watch_key(opportunity)
        if key in self._printed_expiring_blocked_keys:
            return
        self._printed_expiring_blocked_keys.add(key)
        print(
            "expiring candidate blocked: "
            f"group_id={opportunity.group_id} "
            f"title={_short_reason(opportunity.group_title, limit=80)} "
            f"reason={_short_reason(reason, limit=220)}"
        )

    def _print_watching_expiring_market(
        self,
        opportunity: PredictionHuntOpportunity,
        max_days_to_resolution: int,
        watch_key_value: str | None = None,
    ) -> None:
        event_date = parse_datetime(opportunity.event_date)
        if event_date is None:
            return
        now = self.clock()
        if event_date < now or event_date > now + timedelta(days=max_days_to_resolution):
            return
        key = watch_key_value or watch_key(opportunity)
        if key in self._printed_expiring_watch_keys:
            return
        self._printed_expiring_watch_keys.add(key)
        print(
            "watching expiring market: "
            f"expires_in={_duration_until(event_date, now)} "
            f"event_date={opportunity.event_date} "
            f"roi={opportunity.roi_pct}% "
            f"max_wager=${opportunity.max_wager_usd} "
            f"group_id={opportunity.group_id} "
            f"title={_short_reason(opportunity.group_title, limit=120)}"
        )

    async def _watch_market(
        self,
        watch: HotWatch,
        engine: HotTriggerEngine,
        execute: bool,
    ) -> None:
        from .websockets import KalshiOrderbookStream, PolymarketOrderbookStream

        watch.books = {}
        now = self.clock()
        for leg in watch.opportunity.legs:
            watch.books[(leg.platform, leg.market_id, leg.side)] = LiveLegBook(
                exchange=leg.platform,
                market_id=leg.market_id,
                side=leg.side,
                best_ask=None,
                updated_at=None,
                connected=False,
                snapshot_ready=False,
            )
        streams = [
            KalshiOrderbookStream(self.kalshi, watch.opportunity.legs),
            PolymarketOrderbookStream(self.polymarket, watch.opportunity.legs),
        ]
        try:
            async for book in merge_streams(streams, watch.expires_at, self.clock):
                if execute and self._live_halt_reason:
                    return
                now = self.clock()
                watch.books[(book.exchange, book.market_id, book.side)] = _book_with_timestamp(book, now)
                _refresh_live_book_timestamps(watch.books, now)
                evaluation = engine.evaluate(watch, now)
                if evaluation.gross_cost_cents and evaluation.net_profit_cents > 0:
                    if self._uses_previously_triggered_contract(watch.opportunity):
                        self._log_trigger(watch, evaluation, "paper" if not execute else "live", "blocked", "contract already used by prior trigger")
                        return
                    mode = "live" if execute else "paper"
                    submitted, message = engine.execute_if_allowed(evaluation, execute, watch=watch, now=now)
                    self._log_trigger(watch, evaluation, mode, "executed" if submitted else "blocked", message)
                    print(
                        f"hot trigger: {evaluation.pair_name} "
                        f"gross={evaluation.gross_cost_cents}c net={evaluation.net_profit_cents}c "
                        f"action={'executed' if submitted else 'blocked'}"
                        f"{'' if submitted else ' reason=' + _short_reason(message)}"
                    )
                    if execute and not submitted and _requires_live_halt(message):
                        self._live_halt_reason = message
                    watch.triggered = True
                    if not execute or submitted:
                        self._mark_contracts_used(watch.opportunity)
                    return
                if (
                    evaluation.gross_cost_cents
                    and evaluation.net_profit_cents <= 0
                    and evaluation.gross_cost_cents <= engine.near_miss_cost_cents
                    and evaluation.gross_cost_cents not in watch.near_misses_logged
                ):
                    watch.near_misses_logged.add(evaluation.gross_cost_cents)
                    self._log_near_miss(watch, evaluation)
                    print(
                        f"near miss: {evaluation.pair_name} "
                        f"gross={evaluation.gross_cost_cents}c net={evaluation.net_profit_cents}c"
                    )
        except RuntimeError as exc:
            self._log_candidate(watch.opportunity, "stream_error", str(exc))
            print(f"hot stream error: {watch.opportunity.group_title} reason={_short_reason(str(exc))}")
            await asyncio.sleep(1)

    def _event_type_block_reason(
        self,
        opportunity: PredictionHuntOpportunity,
    ) -> str | None:
        allowed = {
            _normalized_event_type(value)
            for value in self.settings.hot_allowed_event_types
            if _normalized_event_type(value)
        }
        event_type = _normalized_event_type(opportunity.event_type)
        if event_type in allowed:
            return None
        allowed_text = ",".join(sorted(allowed)) or "sports,esports"
        return (
            "candidate ignored before market checks: "
            f"event_type={opportunity.event_type or 'missing'} is not allowed; "
            f"allowed={allowed_text}"
        )

    def _exact_predictionhunt_pair_reason(
        self,
        opportunity: PredictionHuntOpportunity,
    ) -> str | None:
        if len(opportunity.legs) != 2:
            return "live arb blocked: PredictionHunt must provide exactly two legs"
        if any(not str(leg.market_id or "").strip() for leg in opportunity.legs):
            return "live arb blocked: PredictionHunt exact leg is missing a market id"
        if {leg.platform for leg in opportunity.legs} != {
            Exchange.KALSHI,
            Exchange.POLYMARKET,
        }:
            return "live arb blocked: exact PredictionHunt legs must span Kalshi and Polymarket"
        if {leg.side for leg in opportunity.legs} != {Side.YES, Side.NO}:
            return "live arb blocked: exact PredictionHunt legs must be one BUY YES and one BUY NO"
        return None

    def _resolve_hot_arb_legs(self, opportunity: PredictionHuntOpportunity) -> PredictionHuntOpportunity:
        legs: list[PredictionHuntLeg] = []
        for leg in opportunity.legs:
            if leg.platform is Exchange.POLYMARKET:
                token_id = (
                    leg.market_id
                    if _looks_like_clob_token_id(leg.market_id)
                    else self.polymarket.resolve_clob_token_id(leg.market_id, leg.side)
                )
                legs.append(
                    leg if token_id == leg.market_id else replace(leg, market_id=token_id)
                )
            else:
                legs.append(leg)
        if tuple(legs) == opportunity.legs:
            return opportunity
        return replace(opportunity, legs=tuple(legs))

    def _hot_allowed_pair_keys(
        self,
        opportunity: PredictionHuntOpportunity,
    ) -> set[tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]]:
        if self._exact_predictionhunt_pair_reason(opportunity):
            return set()
        return {
            _pair_key(
                _ph_leg_key(opportunity.legs[0]),
                _ph_leg_key(opportunity.legs[1]),
            )
        }

    def _source_event_window_reason(
        self,
        opportunity: PredictionHuntOpportunity,
        max_days_to_resolution: int,
    ) -> str | None:
        event_date = parse_datetime(opportunity.event_date)
        if event_date is None:
            return "candidate ignored before market checks: missing or unparseable event_date"
        now = self.clock()
        if event_date < now:
            return "candidate ignored before market checks: event_date is in the past"
        if event_date > now + timedelta(days=max_days_to_resolution):
            return (
                "candidate ignored before market checks: "
                f"event_date is more than {max_days_to_resolution} days away"
            )
        return None

    def _venue_resolution_safety_reason(self, opportunity: PredictionHuntOpportunity) -> str | None:
        kalshi_leg = next((leg for leg in opportunity.legs if leg.platform is Exchange.KALSHI), None)
        if kalshi_leg is None:
            return "live trade blocked: missing Kalshi leg for independent resolution-date check"
        try:
            market = self._kalshi_market(kalshi_leg.market_id)
        except Exception as exc:
            ticker_reason = _market_resolution_safety_reason(
                {"ticker": kalshi_leg.market_id},
                max_days_to_resolution=self.settings.max_days_to_resolution,
                now=self.clock(),
                prefix="live trade blocked",
            )
            if ticker_reason is None:
                return None
            return f"live trade blocked: could not verify Kalshi resolution date: {exc}"
        return _market_resolution_safety_reason(
            market,
            max_days_to_resolution=self.settings.max_days_to_resolution,
            now=self.clock(),
            prefix="live trade blocked",
        )

    def _unsafe_hot_arb_reason(self, opportunity: PredictionHuntOpportunity) -> str | None:
        return self._exact_predictionhunt_pair_reason(opportunity)

    def _kalshi_market(self, ticker: str) -> dict:
        if ticker not in self._kalshi_market_cache:
            self._kalshi_market_cache[ticker] = self.kalshi.get_market(ticker)
        return self._kalshi_market_cache[ticker]

    def _uses_previously_triggered_contract(self, opportunity: PredictionHuntOpportunity) -> bool:
        return any(leg.market_id in self.used_contract_ids for leg in opportunity.legs)

    def _mark_contracts_used(self, opportunity: PredictionHuntOpportunity) -> None:
        for leg in opportunity.legs:
            self.used_contract_ids.add(leg.market_id)

    def _log_candidate(self, opportunity: PredictionHuntOpportunity, action: str, message: str) -> None:
        self._write_jsonl(
            "hot_candidates.jsonl",
            {
                "timestamp": self.clock().isoformat(),
                "action": action,
                "message": message,
                "group_id": opportunity.group_id,
                "group_title": opportunity.group_title,
                "event_date": opportunity.event_date,
                "event_type": opportunity.event_type,
                "roi_pct": str(opportunity.roi_pct),
                "legs": [_ph_leg_record(leg) for leg in opportunity.legs],
            },
        )

    def _log_poll_error(self, message: str, consecutive_errors: int) -> None:
        self._write_jsonl(
            "hot_poll_errors.jsonl",
            {
                "timestamp": self.clock().isoformat(),
                "action": "poll_error",
                "message": message,
                "consecutive_errors": consecutive_errors,
            },
        )

    def _log_trigger(
        self,
        watch: HotWatch,
        opportunity: ArbOpportunity,
        mode: str,
        action: str,
        message: str,
    ) -> None:
        self._write_jsonl(
            "hot_live_trades.jsonl" if mode == "live" else "hot_paper_trades.jsonl",
            {
                "timestamp": self.clock().isoformat(),
                "mode": mode,
                "action": action,
                "message": message,
                "predictionhunt": {
                    "group_id": watch.opportunity.group_id,
                    "group_title": watch.opportunity.group_title,
                    "event_date": watch.opportunity.event_date,
                    "event_type": watch.opportunity.event_type,
                    "roi_pct": str(watch.opportunity.roi_pct),
                    "legs": [_ph_leg_record(leg) for leg in watch.opportunity.legs],
                },
                "books": [_book_record(book) for book in (watch.books or {}).values()],
                "verified": _arb_record(opportunity),
            },
        )
        if opportunity.gross_cost_cents > 0 and (mode == "paper" or action == "executed"):
            self._log_profit_spreadsheet_row(watch, opportunity, mode=mode, action=action)

    def _log_profit_spreadsheet_row(
        self,
        watch: HotWatch,
        opportunity: ArbOpportunity,
        mode: str,
        action: str,
    ) -> None:
        self._write_csv_row(
            "trade_profit.csv",
            _profit_spreadsheet_headers(),
            _arb_profit_spreadsheet_row(self.clock().isoformat(), watch, opportunity, mode, action),
        )

    def _log_near_miss(self, watch: HotWatch, opportunity: ArbOpportunity) -> None:
        self._write_jsonl(
            "hot_near_misses.jsonl",
            {
                "timestamp": self.clock().isoformat(),
                "mode": "paper",
                "action": "near_miss",
                "source": "live_books",
                "message": "live basket close to trigger but not executable",
                "predictionhunt": {
                    "group_id": watch.opportunity.group_id,
                    "group_title": watch.opportunity.group_title,
                    "event_date": watch.opportunity.event_date,
                    "event_type": watch.opportunity.event_type,
                    "roi_pct": str(watch.opportunity.roi_pct),
                    "legs": [_ph_leg_record(leg) for leg in watch.opportunity.legs],
                },
                "books": [_book_record(book) for book in (watch.books or {}).values()],
                "verified": _arb_record(opportunity),
            },
        )

    def _write_jsonl(self, filename: str, record: dict) -> None:
        line = json.dumps(record, sort_keys=True, default=_json_default) + "\n"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_path(filename)
        try:
            _append_text(path, line)
        except OSError as exc:
            repaired = self._repair_log_path(filename, path, exc)
            if repaired is not None:
                try:
                    _append_text(repaired, line)
                    return
                except OSError as repaired_exc:
                    exc = repaired_exc
            fallback = self._fallback_log_path(filename, exc)
            try:
                _append_text(fallback, line)
            except OSError as fallback_exc:
                raise RuntimeError(
                    "could not write hot-arb log file "
                    f"to {path!s} or fallback {fallback!s}: {fallback_exc}"
                ) from fallback_exc

    def _write_csv_row(self, filename: str, headers: list[str], row: dict) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self._log_path(filename)
        try:
            _append_csv_row(path, headers, row)
        except OSError as exc:
            repaired = self._repair_log_path(filename, path, exc)
            if repaired is not None:
                try:
                    _append_csv_row(repaired, headers, row)
                    return
                except OSError as repaired_exc:
                    exc = repaired_exc
            fallback = self._fallback_log_path(filename, exc)
            try:
                _append_csv_row(fallback, headers, row)
            except OSError as fallback_exc:
                raise RuntimeError(
                    "could not write hot-arb CSV log file "
                    f"to {path!s} or fallback {fallback!s}: {fallback_exc}"
                ) from fallback_exc

    def _log_path(self, filename: str) -> Path:
        return self.log_dir / self._log_filename_overrides.get(filename, filename)

    def _repair_log_path(self, filename: str, path: Path, exc: OSError) -> Path | None:
        if filename in self._log_filename_overrides:
            return None
        if not path.exists() or not path.is_file():
            return None
        try:
            path.unlink()
        except OSError:
            return None
        print(f"warning: deleted unwritable hot-arb log {path!s}: {exc}; starting a new log")
        return path

    def _fallback_log_path(self, filename: str, exc: OSError) -> Path:
        fallback_filename = self._log_filename_overrides.get(filename)
        original_path = self.log_dir / filename
        if fallback_filename is None:
            fallback_filename = _fallback_log_filename(filename, self.clock())
            self._log_filename_overrides[filename] = fallback_filename
            print(
                f"warning: could not append {original_path!s}: {exc}; "
                f"writing {self.log_dir / fallback_filename!s} instead"
            )
        return self.log_dir / fallback_filename


def _log_dir_path(log_dir: str | Path) -> Path:
    if isinstance(log_dir, Path):
        return log_dir
    cleaned = log_dir.strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    if not cleaned:
        raise RuntimeError("--log-dir must not be empty")
    return Path(cleaned)


def _fallback_log_filename(filename: str, now: datetime) -> str:
    path = Path(filename)
    timestamp = now.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if path.suffix:
        return f"{path.stem}.recovered-{timestamp}{path.suffix}"
    return f"{path.name}.recovered-{timestamp}"


def _append_text(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def _append_csv_row(path: Path, headers: list[str], row: dict) -> None:
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


async def merge_streams(
    streams: Iterable,
    expires_at: datetime,
    clock: Callable[[], datetime],
):
    queue: asyncio.Queue[LiveLegBook | Exception] = asyncio.Queue()

    async def pump(stream) -> None:
        try:
            async for update in stream.listen_until(expires_at):
                await queue.put(update)
        except Exception as exc:
            await queue.put(exc)

    tasks = [asyncio.create_task(pump(stream)) for stream in streams]
    try:
        while clock() < expires_at and any(not task.done() for task in tasks):
            timeout = max(0.1, (expires_at - clock()).total_seconds())
            try:
                update = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            if isinstance(update, Exception):
                raise RuntimeError(str(update)) from update
            yield update
    finally:
        for task in tasks:
            task.cancel()


def watch_key(opportunity: PredictionHuntOpportunity) -> str:
    return "|".join(
        f"{leg.platform.value}:{leg.side.value}:{leg.market_id}"
        for leg in sorted(opportunity.legs, key=lambda item: (item.platform.value, item.side.value, item.market_id))
    )


def _has_hot_arb_pair(legs: Iterable[PredictionHuntLeg]) -> bool:
    materialized = tuple(legs)
    return any(
        first.platform is Exchange.POLYMARKET
        and second.platform is Exchange.KALSHI
        and first.side is not second.side
        for first in materialized
        for second in materialized
    )


def _legs_are_allowed_pair(watch: HotWatch, first: ArbLeg, second: ArbLeg) -> bool:
    if not watch.allowed_pairs:
        return first.side is not second.side
    return _pair_key(_arb_leg_key(first), _arb_leg_key(second)) in watch.allowed_pairs


def _display_ordered_arb_legs(first: ArbLeg, second: ArbLeg) -> tuple[ArbLeg, ArbLeg]:
    if first.side is not second.side:
        return (first, second) if first.side is Side.YES else (second, first)
    return first, second


def _legs_are_true_opposites(
    watch: HotWatch,
    first: ArbLeg,
    second: ArbLeg,
    require_known: bool = False,
) -> bool:
    first_key = watch.outcome_keys.get((first.exchange, first.market_id, first.side))
    second_key = watch.outcome_keys.get((second.exchange, second.market_id, second.side))
    if first_key and second_key:
        return first_key != second_key
    if require_known:
        return False
    return True


def _predictionhunt_trusted_outcome_keys(
    outcome_keys: dict[tuple[Exchange, str, Side], str],
    trusted_pairs: set[tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]],
) -> dict[tuple[Exchange, str, Side], str]:
    trusted = dict(outcome_keys)
    for index, pair in enumerate(sorted(trusted_pairs, key=str)):
        first, second = pair
        trusted[first] = f"predictionhunt_pair_{index}_a"
        trusted[second] = f"predictionhunt_pair_{index}_b"
    return trusted


def _verified_structure_block_reason(verified_structure) -> str:
    reasons: list[str] = []
    for reason in getattr(verified_structure, "reason_codes", ()) or ():
        reasons.append(str(reason))
    for _, _, decision in getattr(verified_structure, "decisions", ()) or ():
        for reason in (*decision.hard_conflicts, *decision.reason_codes):
            reasons.append(str(reason))
    return "; ".join(list(dict.fromkeys(reasons))[:5])


def _live_verified_pair_block_reason(
    verified_structure,
    required_pairs: set[
        tuple[tuple[Exchange, str, Side], tuple[Exchange, str, Side]]
    ],
) -> str | None:
    approved_pairs = set(getattr(verified_structure, "approved_pairs", ()) or ())
    if required_pairs and required_pairs.issubset(approved_pairs):
        return None

    positions_by_leg = {
        position.leg_key: position
        for position in getattr(verified_structure, "positions", ()) or ()
    }
    for pair in required_pairs:
        first = positions_by_leg.get(pair[0])
        second = positions_by_leg.get(pair[1])
        if first is None or second is None:
            continue
        first_outcome = str(first.instrument_outcome or "").strip()
        second_outcome = str(second.instrument_outcome or "").strip()
        if first_outcome and first_outcome == second_outcome:
            return (
                "live settlement verification failed: exact legs buy the same "
                f"settlement outcome ({first_outcome})"
            )

    detail = _verified_structure_block_reason(verified_structure)
    message = (
        "live settlement verification failed: exact PredictionHunt legs were "
        "not independently proven to be settlement complements"
    )
    return f"{message}: {detail}" if detail else message


def _is_what_will_say_mentions_market(opportunity: PredictionHuntOpportunity) -> bool:
    parts = [
        str(opportunity.group_title or ""),
        str(opportunity.event_type or ""),
    ]
    for leg in opportunity.legs:
        parts.extend((str(leg.market_id or ""), str(leg.source_url or "")))
    raw_text = " ".join(parts)
    lowered = raw_text.lower()
    normalized = f" {_norm(raw_text)} "
    event_type = _norm(str(opportunity.event_type or ""))
    mentions_family = (
        event_type == "mentions"
        or "earningsmention" in lowered
        or "earnings call" in normalized
    )
    what_will_say = (
        (" what will " in normalized and " say " in normalized)
        or (" will " in normalized and " say " in normalized and " during " in normalized)
    )
    return mentions_family and what_will_say


def _ph_leg_key(leg: PredictionHuntLeg) -> tuple[Exchange, str, Side]:
    return (leg.platform, leg.market_id, leg.side)


def _arb_leg_key(leg: ArbLeg) -> tuple[Exchange, str, Side]:
    return (leg.exchange, leg.market_id, leg.side)


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


def _unique_predictionhunt_legs(legs: Iterable[PredictionHuntLeg]) -> tuple[PredictionHuntLeg, ...]:
    unique: list[PredictionHuntLeg] = []
    seen: set[tuple[Exchange, str, Side]] = set()
    for leg in legs:
        key = (leg.platform, leg.market_id, leg.side)
        if key in seen:
            continue
        seen.add(key)
        unique.append(leg)
    return tuple(unique)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _normalized_event_type(value: object) -> str:
    normalized = _norm(str(value or "")).replace(" ", "")
    aliases = {
        "sport": "sports",
        "esport": "esports",
        "esportsmarkets": "esports",
    }
    return aliases.get(normalized, normalized)


def _poll_retry_seconds(base_seconds: int, consecutive_errors: int) -> int:
    base = max(base_seconds, 1)
    return min(base * min(max(consecutive_errors, 1), 100), 300)


def _duration_until(target: datetime, now: datetime) -> str:
    total_seconds = max(0, int((target - now).total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _poll_error_summary(message: str) -> str:
    lowered = message.lower()
    if "could not resolve host" in lowered or "could not be resolved" in lowered:
        return "predictionhunt_dns_unresolved"
    if "401" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
        return "predictionhunt_auth_failed"
    if "429" in lowered or "rate limit" in lowered:
        return "predictionhunt_rate_limited"
    if "timed out" in lowered or "timeout" in lowered:
        return "predictionhunt_timeout"
    return _short_reason(message, limit=80)


def _market_resolution_safety_reason(
    market: dict,
    max_days_to_resolution: int,
    now: datetime,
    prefix: str,
) -> str | None:
    resolution_date, field_name = _select_market_resolution_datetime(
        _market_resolution_datetimes(market),
        now,
    )
    if resolution_date is None:
        return f"{prefix}: missing trusted venue resolution/close date"
    if resolution_date < now:
        return f"{prefix}: venue {field_name} is in the past"
    if resolution_date > now + timedelta(days=max_days_to_resolution):
        return f"{prefix}: venue {field_name} is more than {max_days_to_resolution} days away"
    return None


def _market_resolution_datetime(market: dict) -> tuple[datetime | None, str | None]:
    return _select_market_resolution_datetime(_market_resolution_datetimes(market), None)


def _market_resolution_datetimes(market: dict) -> list[tuple[datetime, str]]:
    candidates: list[tuple[datetime, str]] = []
    if not isinstance(market, dict):
        return candidates
    for key in VENUE_DATE_FIELDS:
        value = market.get(key) if isinstance(market, dict) else None
        parsed = parse_datetime(str(value)) if value else None
        if parsed is not None:
            candidates.append((parsed, key))
    ticker_date = _kalshi_ticker_event_date(market.get("ticker"))
    if ticker_date is not None:
        candidates.append((ticker_date, "ticker_date"))
    return candidates


def _select_market_resolution_datetime(
    candidates: list[tuple[datetime, str]],
    now: datetime | None,
) -> tuple[datetime | None, str | None]:
    if not candidates:
        return None, None
    if now is None:
        return min(candidates, key=lambda item: item[0])
    future = [item for item in candidates if item[0] >= now]
    if future:
        return min(future, key=lambda item: item[0])
    return max(candidates, key=lambda item: item[0])


def _kalshi_ticker_event_date(value: object) -> datetime | None:
    if not value:
        return None
    match = KALSHI_TICKER_DATE_RE.search(str(value))
    if not match:
        return None
    try:
        year = 2000 + int(match.group("year"))
        month = KALSHI_MONTHS[match.group("month").upper()]
        day = int(match.group("day"))
        parsed_date = datetime(year, month, day).date()
    except (KeyError, ValueError):
        return None
    return datetime.combine(parsed_date, EVENT_DATE_END_OF_DAY, EVENT_DATE_ZONE).astimezone(timezone.utc)


def _looks_like_clob_token_id(value: str) -> bool:
    return bool(re.fullmatch(r"\d{30,}", str(value or "")))


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if DATE_ONLY_RE.match(value):
        try:
            parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
        return datetime.combine(parsed_date, EVENT_DATE_END_OF_DAY, EVENT_DATE_ZONE).astimezone(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{value}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _refresh_live_book_timestamps(
    books: dict[tuple[Exchange, str, Side], LiveLegBook],
    now: datetime,
) -> None:
    for key, book in list(books.items()):
        if book.connected and book.snapshot_ready and book.updated_at is not None:
            books[key] = _book_with_timestamp(book, now)


def _book_with_timestamp(book: LiveLegBook, updated_at: datetime) -> LiveLegBook:
    return LiveLegBook(
        exchange=book.exchange,
        market_id=book.market_id,
        side=book.side,
        best_ask=book.best_ask,
        updated_at=updated_at,
        connected=book.connected,
        snapshot_ready=book.snapshot_ready,
        ask_levels=book.ask_levels,
    )


def _ph_leg_record(leg: PredictionHuntLeg) -> dict:
    return {
        "platform": leg.platform.value,
        "side": leg.side.value,
        "market_id": leg.market_id,
        "source_url": leg.source_url,
        "price": str(leg.price),
        "liquidity_usd": str(leg.liquidity_usd),
        "fee_usd": str(leg.fee_usd),
    }


def _book_record(book: LiveLegBook) -> dict:
    return {
        "exchange": book.exchange.value,
        "market_id": book.market_id,
        "side": book.side.value,
        "best_ask": None
        if book.best_ask is None
        else {"price_cents": book.best_ask.price_cents, "size": str(book.best_ask.size)},
        "ask_levels": [
            {"price_cents": level.price_cents, "size": str(level.size)}
            for level in book.ask_levels[:5]
        ],
        "updated_at": None if book.updated_at is None else book.updated_at.isoformat(),
        "connected": book.connected,
        "snapshot_ready": book.snapshot_ready,
    }


def _predictionhunt_price_cents(price: Decimal) -> Decimal:
    value = Decimal(price)
    return value * Decimal("100") if abs(value) <= Decimal("1") else value


def _contracts_for_budget(max_usd: int, price_cents: int) -> Decimal:
    if price_cents <= 0:
        return Decimal("0")
    return (Decimal(max_usd) * Decimal("100") / Decimal(price_cents)).to_integral_value(
        rounding=ROUND_FLOOR
    )


def _whole_contracts(value: Decimal) -> Decimal:
    return Decimal(value).to_integral_value(rounding=ROUND_FLOOR)


def _book_ask_levels(book: LiveLegBook) -> list[BookLevel]:
    levels = list(book.ask_levels)
    if levels:
        return sorted(levels, key=lambda level: level.price_cents)
    return [] if book.best_ask is None else [book.best_ask]


def _requires_live_halt(message: str) -> bool:
    if _is_polymarket_geoblock_error(message):
        return True
    lowered = str(message or "").lower()
    return (
        "polymarket_order_state_uncertain" in lowered
        or "manual_review_required" in lowered
    )


def _is_polymarket_geoblock_error(message: str) -> bool:
    lowered = " ".join(str(message or "").lower().split())
    return (
        "trading restricted in your region" in lowered
        or "polymarket_geoblocked" in lowered
    )


def _recovery_action_clears_pause(action: str) -> bool:
    return action in {"hedged", "exited", "not_filled"}


def _short_reason(message: str, limit: int = 160) -> str:
    compact = " ".join(str(message).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _arb_record(opportunity: ArbOpportunity) -> dict:
    return {
        "pair_name": opportunity.pair_name,
        "gross_cost_cents": opportunity.gross_cost_cents,
        "buffers_cents": _decimal_str(opportunity.buffers_cents),
        "net_profit_cents": _decimal_str(opportunity.net_profit_cents),
        "gross_profit_pct": _profit_pct(100 - opportunity.gross_cost_cents, opportunity.gross_cost_cents),
        "guaranteed_profit_pct": _profit_pct(opportunity.net_profit_cents, opportunity.gross_cost_cents),
        "executable": opportunity.executable,
        "blockers": list(opportunity.blockers),
        "buy_yes": _arb_leg_record(opportunity.buy_yes),
        "buy_no": _arb_leg_record(opportunity.buy_no),
    }


def _profit_spreadsheet_headers() -> list[str]:
    return [
        "timestamp",
        "mode",
        "strategy",
        "action",
        "bet",
        "resolution_time",
        "yes_exchange",
        "yes_side",
        "yes_contracts",
        "yes_price_cents",
        "yes_cost_usd",
        "no_exchange",
        "no_side",
        "no_contracts",
        "no_price_cents",
        "no_cost_usd",
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


def _arb_profit_spreadsheet_row(
    timestamp: str,
    watch: HotWatch,
    opportunity: ArbOpportunity,
    mode: str,
    action: str,
) -> dict:
    return {
        "timestamp": timestamp,
        "mode": mode,
        "strategy": "arbitrage",
        "action": action,
        "bet": opportunity.pair_name,
        "resolution_time": watch.opportunity.event_date,
        "yes_exchange": opportunity.buy_yes.exchange.value,
        "yes_side": opportunity.buy_yes.side.value,
        "yes_contracts": _decimal_str(opportunity.buy_yes.size),
        "yes_price_cents": opportunity.buy_yes.price_cents,
        "yes_cost_usd": _leg_cost_usd(opportunity.buy_yes),
        "no_exchange": opportunity.buy_no.exchange.value,
        "no_side": opportunity.buy_no.side.value,
        "no_contracts": _decimal_str(opportunity.buy_no.size),
        "no_price_cents": opportunity.buy_no.price_cents,
        "no_cost_usd": _leg_cost_usd(opportunity.buy_no),
        "ev_exchange": "",
        "ev_side": "",
        "ev_contracts": "",
        "ev_price_cents": "",
        "ev_cost_usd": "",
        "percent_gain": _profit_pct(opportunity.net_profit_cents, opportunity.gross_cost_cents),
        "total_profit_usd": _net_profit_usd(opportunity),
        "profit_cents_per_contract": _decimal_str(opportunity.net_profit_cents),
        "fees_and_buffers_cents_per_contract": _decimal_str(opportunity.buffers_cents),
    }


def _profit_pct(profit_cents: int | Decimal, cost_cents: int) -> str:
    if cost_cents <= 0:
        return "0.00"
    value = (Decimal(profit_cents) / Decimal(cost_cents)) * Decimal("100")
    return str(value.quantize(Decimal("0.01")))


def _paper_size(opportunity: ArbOpportunity) -> Decimal:
    return min(opportunity.buy_yes.size, opportunity.buy_no.size)


def _gross_profit_usd(opportunity: ArbOpportunity) -> str:
    gross_profit_cents = 100 - opportunity.gross_cost_cents
    value = (Decimal(gross_profit_cents) * _paper_size(opportunity)) / Decimal("100")
    return str(value.quantize(Decimal("0.01")))


def _net_profit_usd(opportunity: ArbOpportunity) -> str:
    value = (opportunity.net_profit_cents * _paper_size(opportunity)) / Decimal("100")
    return str(value.quantize(Decimal("0.01")))


def _leg_cost_usd(leg: ArbLeg) -> str:
    price_cents = leg.avg_price_cents if leg.avg_price_cents is not None else Decimal(leg.price_cents)
    value = (Decimal(price_cents) * leg.size) / Decimal("100")
    return str(value.quantize(Decimal("0.01")))


def _decimal_str(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _arb_leg_record(leg: ArbLeg) -> dict:
    return {
        "exchange": leg.exchange.value,
        "market_id": leg.market_id,
        "side": leg.side.value,
        "price_cents": leg.price_cents,
        "avg_price_cents": None if leg.avg_price_cents is None else _decimal_str(leg.avg_price_cents),
        "size": str(leg.size),
    }
