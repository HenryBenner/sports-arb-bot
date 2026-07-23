from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from .config import Settings
from .models import ArbLeg, Exchange


CENT = Decimal("0.01")
MILL = Decimal("0.001")
HUNDRED = Decimal("100")


def leg_fee_cents_per_contract(leg: ArbLeg, settings: Settings) -> Decimal:
    if leg.size <= 0:
        return Decimal("0")
    rate = _fee_rate(leg.exchange, settings)
    if rate <= 0:
        return Decimal("0")
    price_cents = leg.avg_price_cents if leg.avg_price_cents is not None else Decimal(leg.price_cents)
    price = Decimal(price_cents) / HUNDRED
    fee_usd = leg.size * rate * price * (Decimal("1") - price)
    rounded_fee_usd = fee_usd.quantize(_rounding_unit(leg.exchange), rounding=ROUND_CEILING)
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


def _rounding_unit(exchange: Exchange) -> Decimal:
    if exchange is Exchange.POLYMARKET:
        return MILL
    return CENT
