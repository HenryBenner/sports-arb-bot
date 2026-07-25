from __future__ import annotations

import time
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

    def execute(self, opportunity: ArbOpportunity, workflow: str = "unknown") -> tuple[bool, str]:
        if workflow != self.allowed_workflow:
            return False, f"live execution is only allowed from {self.allowed_workflow}"
        if not opportunity.executable:
            return False, "opportunity is blocked"
        readiness = self.ready_for_immediate_execution()
        if readiness:
            return False, readiness
        try:
            opportunity, refresh_message = self._refresh_arb_for_immediate_fill(opportunity)
        except Exception as exc:
            return False, f"live book refresh failed before order submission: {exc}"
        balance_blocker = self._polymarket_balance_block_reason(opportunity)
        if balance_blocker:
            return False, balance_blocker
        return self._submit_arb_orders(opportunity, prefix=refresh_message)

    def execute_fast(self, opportunity: ArbOpportunity, workflow: str = "unknown") -> tuple[bool, str]:
        if workflow != self.allowed_workflow:
            return False, f"live execution is only allowed from {self.allowed_workflow}"
        if not opportunity.executable:
            return False, "opportunity is blocked"
        readiness = self.ready_for_immediate_execution()
        if readiness:
            return False, readiness
        balance_blocker = self._polymarket_balance_block_reason(opportunity)
        if balance_blocker:
            return False, balance_blocker
        try:
            self._validate_polymarket_minimum_notional(
                opportunity.buy_yes,
                BookLevel(opportunity.buy_yes.price_cents, opportunity.buy_yes.size),
                opportunity.buy_yes.size,
            )
            self._validate_polymarket_minimum_notional(
                opportunity.buy_no,
                BookLevel(opportunity.buy_no.price_cents, opportunity.buy_no.size),
                opportunity.buy_no.size,
            )
        except Exception as exc:
            return False, f"fast path blocked before order submission: {exc}"
        prefix = (
            "fast path skipped balance checks and REST book refresh; "
            f"size={opportunity.buy_yes.size} "
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
        kalshi_blocker = self._kalshi_preflight_block_reason(opportunity)
        if kalshi_blocker:
            return False, kalshi_blocker
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
                    hedge_response, hedge_detail = hedge_result
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
            self._validate_polymarket_minimum_notional(
                refreshed,
                BookLevel(refreshed.price_cents, refreshed.size),
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

    def _refresh_arb_for_immediate_fill(self, opportunity: ArbOpportunity) -> tuple[ArbOpportunity, str]:
        yes_levels = self._fresh_ask_levels(opportunity.buy_yes)
        no_levels = self._fresh_ask_levels(opportunity.buy_no)
        contracts_cap = min(
            _whole_contracts(sum((level.size for level in yes_levels), Decimal("0"))),
            _whole_contracts(sum((level.size for level in no_levels), Decimal("0"))),
        )
        fill = _largest_profitable_blended_fill(
            opportunity.buy_yes,
            opportunity.buy_no,
            yes_levels,
            no_levels,
            contracts_cap,
            self.max_leg_usd,
            self.settings,
            opportunity.buffers_cents,
        )
        if fill.contracts < Decimal("1"):
            best_yes = yes_levels[0] if yes_levels else None
            best_no = no_levels[0] if no_levels else None
            raise RuntimeError(
                "refreshed basket is no longer profitable: no profitable blended fill "
                f"(best YES={_level_text(best_yes)}, best NO={_level_text(best_no)})"
            )
        self._validate_polymarket_minimum_notional(
            opportunity.buy_yes,
            BookLevel(fill.yes_limit_cents, fill.contracts),
            fill.contracts,
        )
        self._validate_polymarket_minimum_notional(
            opportunity.buy_no,
            BookLevel(fill.no_limit_cents, fill.contracts),
            fill.contracts,
        )
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
        refreshed = replace(
            opportunity,
            buy_yes=buy_yes,
            buy_no=buy_no,
            gross_cost_cents=fill.gross_avg_cents,
            buffers_cents=fill.buffers_cents,
            net_profit_cents=fill.net_profit_cents,
        )
        message = (
            f"refreshed blended FOK size={fill.contracts} "
            f"YES={buy_yes.exchange.value}:limit={buy_yes.price_cents}c avg={_decimal_text(fill.yes_avg_cents)}c "
            f"NO={buy_no.exchange.value}:limit={buy_no.price_cents}c avg={_decimal_text(fill.no_avg_cents)}c "
            f"gross_avg={_decimal_text(fill.gross_avg_cents)}c net={_decimal_text(fill.net_profit_cents)}c"
        )
        return refreshed, message

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

    def _validate_polymarket_minimum_notional(self, leg, refreshed_level, contracts: Decimal) -> None:
        if leg.exchange is not Exchange.POLYMARKET:
            return
        notional = (Decimal(refreshed_level.price_cents) * contracts / Decimal("100")).quantize(Decimal("0.01"))
        if notional >= Decimal("1"):
            return
        min_contracts = (Decimal("100") / Decimal(refreshed_level.price_cents)).to_integral_value(
            rounding=ROUND_CEILING
        )
        raise RuntimeError(
            "polymarket_min_notional: Polymarket marketable BUY notional is below $1 minimum "
            f"({contracts} contracts at {refreshed_level.price_cents}c = ${notional}); "
            f"needs at least {min_contracts} contracts at this price, but no larger "
            "profitable basket fits the configured bet limits"
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


def _largest_profitable_blended_fill(
    yes_leg: ArbLeg,
    no_leg: ArbLeg,
    yes_levels: list[BookLevel],
    no_levels: list[BookLevel],
    contracts_cap: Decimal,
    max_leg_usd: Decimal,
    settings: Settings | None,
    fallback_buffers_cents: Decimal,
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
    accepted = _empty_profitable_fill()
    max_leg_cents = max_leg_usd * Decimal("100")

    while contracts < contracts_cap and yes_index < len(yes_ladder) and no_index < len(no_ladder):
        yes_price = yes_ladder[yes_index][0]
        no_price = no_ladder[no_index][0]
        candidate_contracts = contracts + Decimal("1")
        candidate_yes_total = yes_total_cents + Decimal(yes_price)
        candidate_no_total = no_total_cents + Decimal(no_price)
        if candidate_yes_total > max_leg_cents or candidate_no_total > max_leg_cents:
            break

        yes_avg = candidate_yes_total / candidate_contracts
        no_avg = candidate_no_total / candidate_contracts
        candidate_yes_leg = replace(
            yes_leg,
            price_cents=yes_price,
            size=candidate_contracts,
            avg_price_cents=yes_avg,
        )
        candidate_no_leg = replace(
            no_leg,
            price_cents=no_price,
            size=candidate_contracts,
            avg_price_cents=no_avg,
        )
        buffers = (
            total_cost_adjustment_cents((candidate_yes_leg, candidate_no_leg), settings)
            if settings is not None
            else Decimal(fallback_buffers_cents)
        )
        gross_avg = yes_avg + no_avg
        net = Decimal("100") - gross_avg - buffers
        if net <= 0:
            break

        contracts = candidate_contracts
        yes_total_cents = candidate_yes_total
        no_total_cents = candidate_no_total
        accepted = ProfitableFill(
            contracts=contracts,
            yes_limit_cents=yes_price,
            no_limit_cents=no_price,
            yes_avg_cents=yes_avg,
            no_avg_cents=no_avg,
            gross_avg_cents=gross_avg,
            buffers_cents=buffers,
            net_profit_cents=net,
        )

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

    return accepted


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
