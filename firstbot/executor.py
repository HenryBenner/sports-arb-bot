from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .config import Settings
from .exchanges import KalshiClient, PolymarketClient
from .fees import total_cost_adjustment_cents
from .models import ArbOpportunity, BookLevel, EVOpportunity, Exchange, Side
from .models import ArbLeg
from .models import OrderBook


@dataclass(frozen=True)
class ProfitableFill:
    contracts: Decimal
    yes_limit_cents: int
    no_limit_cents: int
    yes_avg_cents: Decimal
    no_avg_cents: Decimal
    gross_avg_cents: Decimal
    buffers_cents: Decimal
    net_profit_cents: Decimal
    yes_fee_price_levels: tuple[BookLevel, ...] = ()
    no_fee_price_levels: tuple[BookLevel, ...] = ()


class TradeExecutor:
    def __init__(
        self,
        kalshi: KalshiClient,
        polymarket: PolymarketClient,
        allowed_workflow: str = "run-hot-arb",
        max_leg_usd: int | Decimal = 10**9,
        settings: Settings | None = None,
    ) -> None:
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.allowed_workflow = allowed_workflow
        self.max_leg_usd = Decimal(max_leg_usd)
        self.settings = settings
        self.last_submitted_opportunity: ArbOpportunity | None = None

    def execute(
        self,
        opportunity: ArbOpportunity,
        workflow: str = "unknown",
        remaining_leg_usd: dict[Exchange, Decimal] | None = None,
    ) -> tuple[bool, str]:
        self.last_submitted_opportunity = None
        if workflow != self.allowed_workflow:
            return False, f"live execution is only allowed from {self.allowed_workflow}"
        if not opportunity.executable:
            return False, _blocked_arb_message(opportunity)
        readiness = self.ready_for_immediate_execution()
        if readiness:
            return False, readiness
        try:
            opportunity, refresh_message = self._refresh_arb_for_immediate_fill(
                opportunity,
                remaining_leg_usd=remaining_leg_usd,
            )
        except Exception as exc:
            return False, f"live book refresh failed before order submission: {exc}"
        return self._submit_arb_orders(opportunity, prefix=refresh_message)

    def execute_fast(
        self,
        opportunity: ArbOpportunity,
        workflow: str = "unknown",
        remaining_leg_usd: dict[Exchange, Decimal] | None = None,
    ) -> tuple[bool, str]:
        self.last_submitted_opportunity = None
        if workflow != self.allowed_workflow:
            return False, f"live execution is only allowed from {self.allowed_workflow}"
        if not opportunity.executable:
            return False, _blocked_arb_message(opportunity)
        readiness = self.ready_for_immediate_execution()
        if readiness:
            return False, readiness
        try:
            opportunity = self._smallest_approved_equal_batch(
                opportunity,
                remaining_leg_usd=remaining_leg_usd,
            )
        except Exception as exc:
            return False, f"fast path blocked before order submission: {exc}"
        try:
            self._validate_polymarket_minimum_order_size(
                opportunity.buy_yes,
                opportunity.buy_yes.size,
            )
            self._validate_polymarket_minimum_order_size(
                opportunity.buy_no,
                opportunity.buy_no.size,
            )
        except Exception as exc:
            return False, f"fast path blocked before order submission: {exc}"
        prefix = (
            "fast path skipped balance checks and REST book refresh; "
            f"smallest_equal_size={opportunity.buy_yes.size} "
            f"gross={_decimal_text(Decimal(opportunity.gross_cost_cents))}c "
            f"net={_decimal_text(opportunity.net_profit_cents)}c"
        )
        return self._submit_arb_orders(opportunity, prefix=prefix)

    def _submit_arb_orders(self, opportunity: ArbOpportunity, prefix: str = "") -> tuple[bool, str]:
        if self.settings is not None and self.settings.hot_require_cross_50:
            cross_50_blocker = _cross_50_block_reason(
                opportunity.buy_yes,
                opportunity.buy_no,
            )
            if cross_50_blocker:
                return (
                    False,
                    f"live opposite-price guard failed before order submission: {cross_50_blocker}",
                )
        preflight_blocker = self._submission_preflight_block_reason(opportunity)
        if preflight_blocker:
            return False, preflight_blocker
        first_leg, second_leg = _execution_order(opportunity)
        first_result = None
        polymarket_confirmation_timeout = _polymarket_confirmation_timeout_seconds(
            opportunity.event_type
        )
        try:
            first_result = self._buy_leg(
                first_leg,
                polymarket_confirmation_timeout_seconds=polymarket_confirmation_timeout,
            )
            second_result = self._buy_leg(
                second_leg,
                polymarket_confirmation_timeout_seconds=polymarket_confirmation_timeout,
            )
        except Exception as exc:
            if first_result is not None:
                hedge_result = self._complete_missing_leg_after_first_fill(
                    second_leg,
                    polymarket_confirmation_timeout_seconds=polymarket_confirmation_timeout,
                    original_error=exc,
                )
                if hedge_result is not None:
                    hedge_response, hedge_detail, completed_leg = hedge_result
                    completed_opportunity = _opportunity_with_completed_leg(
                        opportunity,
                        second_leg,
                        completed_leg,
                    )
                    self.last_submitted_opportunity = completed_opportunity
                    prefix = f"{prefix}; " if prefix else ""
                    return (
                        True,
                        f"{prefix}orders submitted: {first_leg.exchange.value}={first_result} "
                        f"{second_leg.exchange.value}={hedge_response}; "
                        f"missing leg completed after first-leg fill retry: {hedge_detail}",
                    )
                return (
                    False,
                    f"second leg failed after first leg {first_leg.exchange.value} "
                    f"response={first_result}; manual_review_required: {exc}",
                )
            return False, f"first leg failed before paired order submission: {exc}"
        prefix = f"{prefix}; " if prefix else ""
        self.last_submitted_opportunity = opportunity
        return (
            True,
            f"{prefix}orders submitted: "
            f"{first_leg.exchange.value}={first_result} {second_leg.exchange.value}={second_result}",
        )

    def execute_ev(self, opportunity: EVOpportunity, workflow: str = "unknown") -> tuple[bool, str]:
        if workflow != self.allowed_workflow:
            return False, f"live execution is only allowed from {self.allowed_workflow}"
        if not opportunity.executable:
            return False, "EV opportunity is blocked"
        readiness = self.ready_for_exchange(opportunity.leg.exchange)
        if readiness:
            return False, readiness
        try:
            result = self._buy_leg(opportunity.leg)
        except Exception as exc:
            return False, str(exc)
        return True, f"EV order submitted: {result}"

    def ready_for_immediate_execution(self) -> str | None:
        if not self.kalshi.supports_immediate_orders():
            return "Kalshi client does not support immediate orders"
        if not self.polymarket.supports_immediate_orders():
            return "Polymarket client does not support immediate orders"
        return None

    def ready_for_exchange(self, exchange: Exchange) -> str | None:
        if exchange is Exchange.KALSHI and not self.kalshi.supports_immediate_orders():
            return "Kalshi client does not support immediate orders"
        if exchange is Exchange.POLYMARKET and not self.polymarket.supports_immediate_orders():
            return "Polymarket client does not support immediate orders"
        return None

    def _buy_leg(
        self,
        leg,
        polymarket_confirmation_timeout_seconds: float | None = None,
    ):
        count = int(leg.size.to_integral_value())
        if count <= 0:
            raise RuntimeError("leg size must be at least one contract")
        if leg.exchange is Exchange.KALSHI:
            return self.kalshi.create_order(
                ticker=leg.market_id,
                side=leg.side,
                count=count,
                price_cents=leg.price_cents,
                time_in_force="fill_or_kill",
            )
        if leg.exchange is Exchange.POLYMARKET:
            return self.polymarket.buy(
                token_id=leg.market_id,
                price_cents=leg.price_cents,
                size=Decimal(count),
                fill_or_kill=True,
                confirmation_timeout_seconds=polymarket_confirmation_timeout_seconds,
            )
        raise RuntimeError(f"unsupported exchange: {leg.exchange}")

    def _complete_missing_leg_after_first_fill(
        self,
        leg: ArbLeg,
        polymarket_confirmation_timeout_seconds: float | None,
        original_error: Exception,
    ):
        attempts = _settings_int(self.settings, "hot_hedge_retry_attempts", 2)
        if attempts <= 0:
            return None
        delay_seconds = _settings_decimal(
            self.settings,
            "hot_hedge_retry_delay_seconds",
            Decimal("0"),
        )
        max_chase_cents = _settings_int(self.settings, "hot_hedge_max_chase_cents", 5)
        failures = [str(original_error)]
        for attempt in range(1, attempts + 1):
            try:
                refreshed_leg = self._refreshed_missing_leg_for_hedge(
                    leg,
                    max_chase_cents=max_chase_cents,
                )
                result = self._buy_leg(
                    refreshed_leg,
                    polymarket_confirmation_timeout_seconds=polymarket_confirmation_timeout_seconds,
                )
                return (
                    result,
                    f"attempt={attempt} limit={refreshed_leg.price_cents}c "
                    f"avg={_decimal_text(refreshed_leg.avg_price_cents or Decimal(refreshed_leg.price_cents))}c",
                    refreshed_leg,
                )
            except Exception as exc:
                failures.append(f"retry {attempt}: {exc}")
                if attempt < attempts and delay_seconds > 0:
                    time.sleep(float(delay_seconds))
        return None

    def _refreshed_missing_leg_for_hedge(
        self,
        leg: ArbLeg,
        max_chase_cents: int,
    ) -> ArbLeg:
        levels = self._fresh_ask_levels(leg)
        max_limit = min(99, int(leg.price_cents) + max(0, int(max_chase_cents)))
        fill = _weighted_fill_for_size(levels, leg.size)
        if fill is None:
            raise RuntimeError(
                f"missing_leg_hedge_depth_unavailable: no {leg.exchange.value} "
                f"{leg.side.value} depth for {leg.market_id}"
            )
        limit_cents, avg_cents = fill
        if limit_cents > max_limit:
            raise RuntimeError(
                "missing_leg_hedge_chase_limit_exceeded: "
                f"{leg.exchange.value} {leg.market_id} {leg.side.value} needs "
                f"{limit_cents}c, max={max_limit}c"
            )
        refreshed = replace(
            leg,
            price_cents=limit_cents,
            avg_price_cents=avg_cents,
        )
        if refreshed.exchange is Exchange.POLYMARKET:
            self._validate_polymarket_minimum_order_size(
                refreshed,
                refreshed.size,
            )
            notional = _leg_notional_usd(refreshed)
            try:
                available = Decimal(self.polymarket.available_cash_usd())
            except Exception as exc:
                raise RuntimeError(
                    f"polymarket_balance_unavailable during missing leg hedge: {exc}"
                ) from exc
            if available < notional:
                raise RuntimeError(
                    "polymarket_balance_insufficient during missing leg hedge: "
                    f"available=${_decimal_text(available)} required=${_decimal_text(notional)}"
                )
        return refreshed

    def _refresh_arb_for_immediate_fill(
        self,
        opportunity: ArbOpportunity,
        remaining_leg_usd: dict[Exchange, Decimal] | None = None,
    ) -> tuple[ArbOpportunity, str]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            yes_future = pool.submit(self._fresh_ask_levels, opportunity.buy_yes)
            no_future = pool.submit(self._fresh_ask_levels, opportunity.buy_no)
            yes_levels = yes_future.result()
            no_levels = no_future.result()
        contracts_cap = min(
            _whole_contracts(sum((level.size for level in yes_levels), Decimal("0"))),
            _whole_contracts(sum((level.size for level in no_levels), Decimal("0"))),
        )
        leg_budgets = _leg_budgets(
            self.max_leg_usd,
            remaining_leg_usd,
        )
        polymarket_min_order_size = self._polymarket_minimum_contracts(opportunity)
        fill = _smallest_profitable_equal_fill(
            opportunity.buy_yes,
            opportunity.buy_no,
            yes_levels,
            no_levels,
            contracts_cap,
            leg_budgets,
            self.settings,
            opportunity.buffers_cents,
            polymarket_min_order_size=polymarket_min_order_size,
        )
        if fill.contracts < Decimal("1"):
            unrestricted_fill = _smallest_profitable_equal_fill(
                opportunity.buy_yes,
                opportunity.buy_no,
                yes_levels,
                no_levels,
                contracts_cap,
                {
                    Exchange.KALSHI: Decimal("Infinity"),
                    Exchange.POLYMARKET: Decimal("Infinity"),
                },
                self.settings,
                opportunity.buffers_cents,
                polymarket_min_order_size=polymarket_min_order_size,
            )
            best_yes = yes_levels[0] if yes_levels else None
            best_no = no_levels[0] if no_levels else None
            if unrestricted_fill.contracts >= Decimal("1"):
                raise RuntimeError(
                    "incremental_budget_exhausted: remaining per-leg budget cannot fit "
                    f"the smallest exchange-valid equal batch of {unrestricted_fill.contracts} "
                    f"contracts (remaining Kalshi=${_decimal_text(leg_budgets[Exchange.KALSHI])}, "
                    f"Polymarket=${_decimal_text(leg_budgets[Exchange.POLYMARKET])})"
                )
            if contracts_cap < polymarket_min_order_size:
                raise RuntimeError(
                    "polymarket_min_order_size: live equal depth cannot fit "
                    f"Polymarket minimum of {polymarket_min_order_size} shares"
                )
            raise RuntimeError(
                "refreshed basket is no longer profitable: no exchange-valid profitable "
                "equal batch "
                f"(best YES={_level_text(best_yes)}, best NO={_level_text(best_no)})"
            )
        self._validate_polymarket_minimum_order_size(
            opportunity.buy_yes,
            fill.contracts,
        )
        self._validate_polymarket_minimum_order_size(
            opportunity.buy_no,
            fill.contracts,
        )
        buy_yes = replace(
            opportunity.buy_yes,
            price_cents=fill.yes_limit_cents,
            size=fill.contracts,
            avg_price_cents=fill.yes_avg_cents,
            fee_price_levels=fill.yes_fee_price_levels,
        )
        buy_no = replace(
            opportunity.buy_no,
            price_cents=fill.no_limit_cents,
            size=fill.contracts,
            avg_price_cents=fill.no_avg_cents,
            fee_price_levels=fill.no_fee_price_levels,
        )
        refreshed = replace(
            opportunity,
            buy_yes=buy_yes,
            buy_no=buy_no,
            gross_cost_cents=fill.gross_avg_cents,
            buffers_cents=fill.buffers_cents,
            net_profit_cents=fill.net_profit_cents,
        )
        message = (
            f"refreshed smallest equal FOK size={fill.contracts} "
            f"YES={buy_yes.exchange.value}:limit={buy_yes.price_cents}c avg={_decimal_text(fill.yes_avg_cents)}c "
            f"NO={buy_no.exchange.value}:limit={buy_no.price_cents}c avg={_decimal_text(fill.no_avg_cents)}c "
            f"gross_avg={_decimal_text(fill.gross_avg_cents)}c net={_decimal_text(fill.net_profit_cents)}c"
        )
        return refreshed, message

    def _smallest_approved_equal_batch(
        self,
        opportunity: ArbOpportunity,
        remaining_leg_usd: dict[Exchange, Decimal] | None,
    ) -> ArbOpportunity:
        contracts_cap = min(
            _whole_contracts(opportunity.buy_yes.size),
            _whole_contracts(opportunity.buy_no.size),
        )
        polymarket_leg = _polymarket_leg(opportunity)
        if polymarket_leg is None:
            minimum_contracts = Decimal("1")
        else:
            minimum_contracts = self._polymarket_minimum_contracts(opportunity)
        if contracts_cap < minimum_contracts:
            raise RuntimeError(
                "polymarket_min_order_size: approved live depth cannot fit the "
                f"smallest equal batch of {minimum_contracts} contracts"
            )
        budgets = _leg_budgets(self.max_leg_usd, remaining_leg_usd)
        for leg in (opportunity.buy_yes, opportunity.buy_no):
            required_usd = (
                Decimal(leg.price_cents) * minimum_contracts / Decimal("100")
            )
            if required_usd > budgets[leg.exchange]:
                raise RuntimeError(
                    "incremental_budget_exhausted: remaining per-leg budget cannot fit "
                    f"the smallest exchange-valid equal batch of {minimum_contracts} contracts"
                )
        return replace(
            opportunity,
            buy_yes=replace(
                opportunity.buy_yes,
                size=minimum_contracts,
                avg_price_cents=Decimal(opportunity.buy_yes.price_cents),
            ),
            buy_no=replace(
                opportunity.buy_no,
                size=minimum_contracts,
                avg_price_cents=Decimal(opportunity.buy_no.price_cents),
            ),
        )

    def _fresh_ask_levels(self, leg) -> list[BookLevel]:
        if leg.exchange is Exchange.KALSHI:
            if hasattr(self.kalshi, "get_orderbook"):
                book: OrderBook = self.kalshi.get_orderbook(leg.market_id)
                levels = book.yes_asks if leg.side.value == "yes" else book.no_asks
                return sorted(levels, key=lambda item: item.price_cents)
            if hasattr(self.kalshi, "get_best_ask"):
                level = self.kalshi.get_best_ask(leg.market_id, leg.side)
                return [] if level is None else [level]
            return [BookLevel(leg.price_cents, leg.size)]
        elif leg.exchange is Exchange.POLYMARKET:
            if hasattr(self.polymarket, "get_token_ask_levels"):
                return self.polymarket.get_token_ask_levels(leg.market_id)
            if hasattr(self.polymarket, "get_token_best_ask"):
                level = self.polymarket.get_token_best_ask(leg.market_id)
                return [] if level is None else [level]
            return [BookLevel(leg.price_cents, leg.size)]
        else:
            raise RuntimeError(f"unsupported exchange for live refresh: {leg.exchange}")

    def _validate_polymarket_minimum_order_size(
        self,
        leg: ArbLeg,
        contracts: Decimal,
    ) -> None:
        if leg.exchange is not Exchange.POLYMARKET:
            return
        minimum_contracts = self._polymarket_minimum_contracts_for_leg(leg)
        if contracts >= minimum_contracts:
            return
        raise RuntimeError(
            "polymarket_min_order_size: Polymarket order is below the live book minimum "
            f"({contracts} shares requested; minimum={minimum_contracts})"
        )

    def _polymarket_minimum_contracts(self, opportunity: ArbOpportunity) -> Decimal:
        leg = _polymarket_leg(opportunity)
        if leg is None:
            return Decimal("1")
        return self._polymarket_minimum_contracts_for_leg(leg)

    def _polymarket_minimum_contracts_for_leg(self, leg: ArbLeg) -> Decimal:
        if hasattr(self.polymarket, "get_token_min_order_size"):
            minimum = Decimal(self.polymarket.get_token_min_order_size(leg.market_id))
            return max(
                Decimal("1"),
                minimum.to_integral_value(rounding=ROUND_CEILING),
            )
        # Legacy and test clients do not expose book constraints.
        return max(
            Decimal("1"),
            (Decimal("100") / Decimal(leg.price_cents)).to_integral_value(
                rounding=ROUND_CEILING
            ),
        )

    def _polymarket_balance_block_reason(self, opportunity: ArbOpportunity) -> str | None:
        polymarket_leg = _polymarket_leg(opportunity)
        if polymarket_leg is None:
            return None
        notional = _leg_notional_usd(polymarket_leg)
        try:
            available = Decimal(self.polymarket.available_cash_usd())
        except Exception as exc:
            return f"polymarket_balance_unavailable before order submission: {exc}"
        if available >= notional:
            return None
        return (
            "polymarket_balance_insufficient before order submission: "
            f"available=${_decimal_text(available)} required=${_decimal_text(notional)}"
        )

    def _submission_preflight_block_reason(
        self,
        opportunity: ArbOpportunity,
    ) -> str | None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            balance_future = pool.submit(
                self._polymarket_balance_block_reason,
                opportunity,
            )
            kalshi_future = pool.submit(
                self._kalshi_preflight_block_reason,
                opportunity,
            )
            balance_blocker = balance_future.result()
            kalshi_blocker = kalshi_future.result()
        return balance_blocker or kalshi_blocker

    def _kalshi_preflight_block_reason(self, opportunity: ArbOpportunity) -> str | None:
        kalshi_leg = _kalshi_leg(opportunity)
        if kalshi_leg is None:
            return None
        if hasattr(self.kalshi, "get_market"):
            try:
                market = self.kalshi.get_market(kalshi_leg.market_id)
            except Exception as exc:
                return f"kalshi_preflight_unavailable before polymarket order: market check failed: {exc}"
            inactive_reason = _kalshi_market_inactive_reason(market)
            if inactive_reason:
                return (
                    "kalshi_market_not_active before polymarket order: "
                    f"{kalshi_leg.market_id} {inactive_reason}"
                )
        if not hasattr(self.kalshi, "get_orderbook"):
            return None
        try:
            book: OrderBook = self.kalshi.get_orderbook(kalshi_leg.market_id)
        except Exception as exc:
            return f"kalshi_preflight_unavailable before polymarket order: orderbook check failed: {exc}"
        levels = book.yes_asks if kalshi_leg.side is Side.YES else book.no_asks
        executable_size = sum(
            (
                Decimal(level.size)
                for level in levels
                if int(level.price_cents) <= int(kalshi_leg.price_cents)
            ),
            Decimal("0"),
        )
        required_size = Decimal(kalshi_leg.size)
        if executable_size >= required_size:
            return None
        return (
            "kalshi_fok_depth_insufficient before polymarket order: "
            f"{kalshi_leg.market_id} {kalshi_leg.side.value} needs "
            f"{_decimal_text(required_size)} contracts <= {kalshi_leg.price_cents}c, "
            f"available={_decimal_text(executable_size)}"
        )


def _smallest_profitable_equal_fill(
    yes_leg: ArbLeg,
    no_leg: ArbLeg,
    yes_levels: list[BookLevel],
    no_levels: list[BookLevel],
    contracts_cap: Decimal,
    leg_budgets_usd: dict[Exchange, Decimal],
    settings: Settings | None,
    fallback_buffers_cents: Decimal,
    polymarket_min_order_size: Decimal = Decimal("1"),
) -> ProfitableFill:
    yes_ladder = _whole_contract_ladder(yes_levels)
    no_ladder = _whole_contract_ladder(no_levels)
    if not yes_ladder or not no_ladder or contracts_cap < Decimal("1"):
        return _empty_profitable_fill()

    yes_index = 0
    no_index = 0
    yes_remaining = yes_ladder[0][1]
    no_remaining = no_ladder[0][1]
    contracts = Decimal("0")
    yes_total_cents = Decimal("0")
    no_total_cents = Decimal("0")
    yes_fee_price_levels: list[BookLevel] = []
    no_fee_price_levels: list[BookLevel] = []
    yes_budget_cents = leg_budgets_usd.get(yes_leg.exchange, Decimal("0")) * Decimal("100")
    no_budget_cents = leg_budgets_usd.get(no_leg.exchange, Decimal("0")) * Decimal("100")

    while contracts < contracts_cap and yes_index < len(yes_ladder) and no_index < len(no_ladder):
        yes_price = yes_ladder[yes_index][0]
        no_price = no_ladder[no_index][0]
        candidate_contracts = contracts + Decimal("1")
        candidate_yes_total = yes_total_cents + Decimal(yes_price)
        candidate_no_total = no_total_cents + Decimal(no_price)
        if candidate_yes_total > yes_budget_cents or candidate_no_total > no_budget_cents:
            break

        yes_avg = candidate_yes_total / candidate_contracts
        no_avg = candidate_no_total / candidate_contracts
        candidate_yes_fee_levels = _fee_levels_with_contract(
            yes_fee_price_levels,
            yes_price,
        )
        candidate_no_fee_levels = _fee_levels_with_contract(
            no_fee_price_levels,
            no_price,
        )
        candidate_yes_leg = replace(
            yes_leg,
            price_cents=yes_price,
            size=candidate_contracts,
            avg_price_cents=yes_avg,
            fee_price_levels=candidate_yes_fee_levels,
        )
        candidate_no_leg = replace(
            no_leg,
            price_cents=no_price,
            size=candidate_contracts,
            avg_price_cents=no_avg,
            fee_price_levels=candidate_no_fee_levels,
        )
        buffers = (
            total_cost_adjustment_cents((candidate_yes_leg, candidate_no_leg), settings)
            if settings is not None
            else Decimal(fallback_buffers_cents)
        )
        gross_avg = yes_avg + no_avg
        net = Decimal("100") - gross_avg - buffers

        if net > 0 and candidate_contracts >= max(
            Decimal("1"),
            Decimal(polymarket_min_order_size).to_integral_value(rounding=ROUND_CEILING),
        ):
            return ProfitableFill(
                contracts=candidate_contracts,
                yes_limit_cents=yes_price,
                no_limit_cents=no_price,
                yes_avg_cents=yes_avg,
                no_avg_cents=no_avg,
                gross_avg_cents=gross_avg,
                buffers_cents=buffers,
                net_profit_cents=net,
                yes_fee_price_levels=candidate_yes_fee_levels,
                no_fee_price_levels=candidate_no_fee_levels,
            )

        contracts = candidate_contracts
        yes_total_cents = candidate_yes_total
        no_total_cents = candidate_no_total
        yes_fee_price_levels = list(candidate_yes_fee_levels)
        no_fee_price_levels = list(candidate_no_fee_levels)
        yes_remaining -= Decimal("1")
        no_remaining -= Decimal("1")
        if yes_remaining <= 0:
            yes_index += 1
            if yes_index < len(yes_ladder):
                yes_remaining = yes_ladder[yes_index][1]
        if no_remaining <= 0:
            no_index += 1
            if no_index < len(no_ladder):
                no_remaining = no_ladder[no_index][1]

    return _empty_profitable_fill()


def _leg_budgets(
    max_leg_usd: Decimal,
    remaining_leg_usd: dict[Exchange, Decimal] | None,
) -> dict[Exchange, Decimal]:
    budgets: dict[Exchange, Decimal] = {}
    for exchange in (Exchange.KALSHI, Exchange.POLYMARKET):
        remaining = (
            Decimal(max_leg_usd)
            if remaining_leg_usd is None
            else Decimal(remaining_leg_usd.get(exchange, Decimal("0")))
        )
        budgets[exchange] = max(Decimal("0"), min(Decimal(max_leg_usd), remaining))
    return budgets


def _cross_50_block_reason(first_leg: ArbLeg, second_leg: ArbLeg) -> str | None:
    limit_prices = (Decimal(first_leg.price_cents), Decimal(second_leg.price_cents))
    if not _strictly_straddles_fifty(*limit_prices):
        return (
            "approved leg limits must be on opposite sides of 50c "
            f"(got {first_leg.price_cents}c and {second_leg.price_cents}c)"
        )
    average_prices = (
        first_leg.avg_price_cents
        if first_leg.avg_price_cents is not None
        else Decimal(first_leg.price_cents),
        second_leg.avg_price_cents
        if second_leg.avg_price_cents is not None
        else Decimal(second_leg.price_cents),
    )
    if not _strictly_straddles_fifty(*average_prices):
        return (
            "approved average fills must be on opposite sides of 50c "
            f"(got {_decimal_text(average_prices[0])}c and "
            f"{_decimal_text(average_prices[1])}c)"
        )
    return None


def _source_price_alignment_block_reason(
    leg: ArbLeg,
    max_deviation_cents: Decimal,
) -> str | None:
    if leg.source_price_cents is None:
        return None
    source = Decimal(leg.source_price_cents)
    if source <= 0 or source >= 100:
        return (
            f"{leg.exchange.value} {leg.side.value.upper()} has invalid "
            f"PredictionHunt price {_decimal_text(source)}c"
        )
    prices = (
        ("limit", Decimal(leg.price_cents)),
        (
            "average fill",
            leg.avg_price_cents
            if leg.avg_price_cents is not None
            else Decimal(leg.price_cents),
        ),
    )
    complement = Decimal("100") - source
    for label, current in prices:
        source_distance = abs(current - source)
        complement_distance = abs(current - complement)
        if source != Decimal("50") and complement_distance < source_distance:
            return (
                f"{leg.exchange.value} {leg.side.value.upper()} {label} "
                f"{_decimal_text(current)}c is closer to the complementary "
                f"PredictionHunt price {_decimal_text(complement)}c than the "
                f"quoted {_decimal_text(source)}c"
            )
        if source_distance > max_deviation_cents:
            return (
                f"{leg.exchange.value} {leg.side.value.upper()} {label} "
                f"{_decimal_text(current)}c is {_decimal_text(source_distance)}c "
                f"from PredictionHunt's {_decimal_text(source)}c quote; maximum "
                f"is {_decimal_text(max_deviation_cents)}c"
            )
    return None


def _strictly_straddles_fifty(first_price: Decimal, second_price: Decimal) -> bool:
    fifty = Decimal("50")
    return (first_price < fifty < second_price) or (
        second_price < fifty < first_price
    )


def _whole_contract_ladder(levels: list[BookLevel]) -> list[tuple[int, Decimal]]:
    ladder: list[tuple[int, Decimal]] = []
    for level in sorted(levels, key=lambda item: item.price_cents):
        size = _whole_contracts(level.size)
        if size >= Decimal("1"):
            ladder.append((level.price_cents, size))
    return ladder


def _fee_levels_with_contract(
    levels: list[BookLevel],
    price_cents: int,
) -> tuple[BookLevel, ...]:
    if levels and levels[-1].price_cents == price_cents:
        return (
            *levels[:-1],
            BookLevel(price_cents=price_cents, size=levels[-1].size + Decimal("1")),
        )
    return (*levels, BookLevel(price_cents=price_cents, size=Decimal("1")))


def _weighted_fill_for_size(
    levels: list[BookLevel],
    contracts: Decimal,
) -> tuple[int, Decimal] | None:
    needed = _whole_contracts(contracts)
    if needed < Decimal("1"):
        return None
    remaining = needed
    total_cents = Decimal("0")
    limit_cents = 0
    for price_cents, available in _whole_contract_ladder(levels):
        take = min(remaining, available)
        if take <= 0:
            continue
        total_cents += Decimal(price_cents) * take
        limit_cents = price_cents
        remaining -= take
        if remaining <= 0:
            return limit_cents, total_cents / needed
    return None


def _whole_contracts(value: Decimal) -> Decimal:
    return Decimal(value).to_integral_value(rounding=ROUND_FLOOR)


def _empty_profitable_fill() -> ProfitableFill:
    return ProfitableFill(
        contracts=Decimal("0"),
        yes_limit_cents=0,
        no_limit_cents=0,
        yes_avg_cents=Decimal("0"),
        no_avg_cents=Decimal("0"),
        gross_avg_cents=Decimal("0"),
        buffers_cents=Decimal("0"),
        net_profit_cents=Decimal("-100"),
    )


def _level_text(level: BookLevel | None) -> str:
    if level is None:
        return "none"
    return f"{level.price_cents}c x {level.size}"


def _blocked_arb_message(opportunity: ArbOpportunity) -> str:
    details = "; ".join(str(reason) for reason in opportunity.blockers if str(reason).strip())
    return "opportunity is blocked" if not details else f"opportunity is blocked: {details}"


def _decimal_text(value: Decimal) -> str:
    text = format(Decimal(value).quantize(Decimal("0.0001")), "f").rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _execution_order(opportunity: ArbOpportunity):
    legs = (opportunity.buy_yes, opportunity.buy_no)
    polymarket_leg = next((leg for leg in legs if leg.exchange is Exchange.POLYMARKET), None)
    if polymarket_leg is None:
        return legs
    other_leg = opportunity.buy_no if polymarket_leg is opportunity.buy_yes else opportunity.buy_yes
    return polymarket_leg, other_leg


def _opportunity_with_completed_leg(
    opportunity: ArbOpportunity,
    original_leg: ArbLeg,
    completed_leg: ArbLeg,
) -> ArbOpportunity:
    if original_leg is opportunity.buy_yes:
        buy_yes, buy_no = completed_leg, opportunity.buy_no
    else:
        buy_yes, buy_no = opportunity.buy_yes, completed_leg
    yes_avg = buy_yes.avg_price_cents or Decimal(buy_yes.price_cents)
    no_avg = buy_no.avg_price_cents or Decimal(buy_no.price_cents)
    gross = yes_avg + no_avg
    return replace(
        opportunity,
        buy_yes=buy_yes,
        buy_no=buy_no,
        gross_cost_cents=gross,
        net_profit_cents=Decimal("100") - gross - opportunity.buffers_cents,
    )


def _polymarket_leg(opportunity: ArbOpportunity) -> ArbLeg | None:
    return next(
        (
            leg
            for leg in (opportunity.buy_yes, opportunity.buy_no)
            if leg.exchange is Exchange.POLYMARKET
        ),
        None,
    )


def _kalshi_leg(opportunity: ArbOpportunity) -> ArbLeg | None:
    return next(
        (
            leg
            for leg in (opportunity.buy_yes, opportunity.buy_no)
            if leg.exchange is Exchange.KALSHI
        ),
        None,
    )


def _kalshi_market_inactive_reason(market: object) -> str | None:
    if not isinstance(market, dict):
        return None
    if _truthy(market.get("closed")):
        return "closed=true"
    if _truthy(market.get("archived")):
        return "archived=true"
    if _truthy(market.get("paused")):
        return "paused=true"
    for key in ("status", "state", "trading_status"):
        value = market.get(key)
        if value is None:
            continue
        normalized = str(value).strip().lower()
        if normalized in {
            "active",
            "open",
            "opened",
            "trading",
            "initialized",
            "live",
        }:
            return None
        if normalized in {
            "closed",
            "settled",
            "resolved",
            "finalized",
            "inactive",
            "halted",
            "paused",
            "terminated",
            "delisted",
        }:
            return f"{key}={value}"
    return None


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _settings_int(settings: Settings | None, name: str, default: int) -> int:
    value = getattr(settings, name, default) if settings is not None else default
    return int(value)


def _settings_decimal(
    settings: Settings | None,
    name: str,
    default: Decimal,
) -> Decimal:
    value = getattr(settings, name, default) if settings is not None else default
    return Decimal(value)


def _leg_notional_usd(leg: ArbLeg) -> Decimal:
    return (Decimal(leg.price_cents) * Decimal(leg.size) / Decimal("100")).quantize(
        Decimal("0.0001")
    )


def _polymarket_confirmation_timeout_seconds(event_type: str | None) -> float:
    normalized = " ".join(str(event_type or "").lower().replace("_", " ").split())
    if normalized in {"crypto", "cryptocurrency", "finance", "financial", "financials"}:
        return 2.0
    return 12.0
