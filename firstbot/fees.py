from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from .config import Settings
from .models import ArbLeg, Exchange


CENT = Decimal("0.01")
CENTICENT = Decimal("0.0001")
POLYMARKET_FEE_PRECISION = Decimal("0.00001")
HUNDRED = Decimal("100")


def leg_fee_cents_per_contract(leg: ArbLeg, settings: Settings) -> Decimal:
    if leg.size <= 0:
        return Decimal("0")
    if leg.fee_schedule is not None:
        return _scheduled_leg_fee_cents_per_contract(leg)
    rate = _fee_rate(leg.exchange, settings)
    if rate <= 0:
        return Decimal("0")
    price_cents = leg.avg_price_cents if leg.avg_price_cents is not None else Decimal(leg.price_cents)
    price = Decimal(price_cents) / HUNDRED
    fee_usd = leg.size * rate * price * (Decimal("1") - price)
    rounded_fee_usd = fee_usd.quantize(_legacy_rounding_unit(leg.exchange), rounding=ROUND_CEILING)
    return (rounded_fee_usd * HUNDRED) / leg.size


def total_fee_cents_per_contract(legs: tuple[ArbLeg, ArbLeg], settings: Settings) -> Decimal:
    return sum((leg_fee_cents_per_contract(leg, settings) for leg in legs), Decimal("0"))


def total_cost_adjustment_cents(legs: tuple[ArbLeg, ArbLeg], settings: Settings) -> Decimal:
    exchange_fees = total_fee_cents_per_contract(legs, settings)
    extra_buffers = Decimal(settings.slippage_cents + settings.fee_buffer_cents)
    return exchange_fees + extra_buffers


def _fee_rate(exchange: Exchange, settings: Settings) -> Decimal:
    if exchange is Exchange.KALSHI:
        return settings.kalshi_fee_rate
    if exchange is Exchange.POLYMARKET:
        return settings.polymarket_fee_rate
    return Decimal("0")


def _scheduled_leg_fee_cents_per_contract(leg: ArbLeg) -> Decimal:
    schedule = leg.fee_schedule
    assert schedule is not None
    if schedule.exchange is not leg.exchange:
        raise ValueError(
            f"fee schedule exchange {schedule.exchange.value} does not match leg {leg.exchange.value}"
        )
    if schedule.rate <= 0 or schedule.multiplier <= 0:
        return Decimal("0")

    fee_slices = _fee_slices(leg)

    if leg.exchange is Exchange.KALSHI:
        if schedule.fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
            raise ValueError(f"unsupported live Kalshi fee type: {schedule.fee_type}")
        raw_fee_usd = sum(
            (
                size
                * schedule.rate
                * schedule.multiplier
                * price
                * (Decimal("1") - price)
                for price, size in fee_slices
            ),
            Decimal("0"),
        )
        position_cost_usd = sum(
            (size * price for price, size in fee_slices),
            Decimal("0"),
        )
        rounded_total_usd = (position_cost_usd + raw_fee_usd).quantize(
            CENTICENT,
            rounding=ROUND_CEILING,
        )
        rounded_fee_usd = rounded_total_usd - position_cost_usd
    elif leg.exchange is Exchange.POLYMARKET:
        raw_fee_usd = sum(
            (
                size
                * schedule.rate
                * schedule.multiplier
                * ((price * (Decimal("1") - price)) ** schedule.exponent)
                for price, size in fee_slices
            ),
            Decimal("0"),
        )
        rounded_fee_usd = raw_fee_usd.quantize(
            POLYMARKET_FEE_PRECISION,
            rounding=ROUND_HALF_UP,
        )
    else:
        raise ValueError(f"unsupported fee schedule exchange: {leg.exchange.value}")

    return (rounded_fee_usd * HUNDRED) / leg.size


def _fee_slices(leg: ArbLeg) -> tuple[tuple[Decimal, Decimal], ...]:
    remaining = Decimal(leg.size)
    slices: list[tuple[Decimal, Decimal]] = []
    for level in leg.fee_price_levels:
        if remaining <= 0:
            break
        size = min(remaining, Decimal(level.size))
        if size <= 0:
            continue
        slices.append((Decimal(level.price_cents) / HUNDRED, size))
        remaining -= size
    if remaining > 0:
        fallback_price_cents = (
            leg.avg_price_cents
            if leg.avg_price_cents is not None
            else Decimal(leg.price_cents)
        )
        slices.append((Decimal(fallback_price_cents) / HUNDRED, remaining))
    return tuple(slices)


def _legacy_rounding_unit(exchange: Exchange) -> Decimal:
    if exchange is Exchange.POLYMARKET:
        return Decimal("0.001")
    return CENT
