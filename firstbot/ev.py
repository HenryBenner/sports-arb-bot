from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from .config import Settings
from .fees import leg_fee_cents_per_contract
from .models import ArbLeg, BookLevel, EVOpportunity, Exchange
from .predictionhunt import PredictionHuntEVBet


ONE_HUNDRED = Decimal("100")


def verify_ev_bet(
    bet: PredictionHuntEVBet,
    best_ask: BookLevel | None,
    settings: Settings,
) -> EVOpportunity:
    blockers: list[str] = []
    if best_ask is None:
        blockers.append("missing live ask")
        return _blocked(bet, blockers)

    contracts, stake_usd = _contracts_for_stake(
        price_cents=best_ask.price_cents,
        displayed_size=best_ask.size,
        default_trade_usd=settings.ev_trade_usd,
        max_trade_usd=settings.ev_max_trade_usd,
        api_max_wager_usd=bet.max_wager_usd,
    )
    leg = ArbLeg(
        exchange=bet.platform,
        market_id=bet.market_id,
        side=bet.side,
        price_cents=best_ask.price_cents,
        size=contracts,
    )
    if contracts < Decimal("1"):
        blockers.append("displayed depth or stake is below 1 contract")

    if bet.ev_pct <= settings.ev_min_edge_pct:
        blockers.append(f"EV {bet.ev_pct}% is not above minimum {settings.ev_min_edge_pct}%")

    if bet.price > 0 and Decimal(best_ask.price_cents) > (bet.price * ONE_HUNDRED):
        blockers.append("live ask is worse than PredictionHunt EV price")

    fee_cents = leg_fee_cents_per_contract(leg, settings)
    extra_buffers = Decimal(settings.slippage_cents + settings.fee_buffer_cents)
    fair_value_cents = None
    edge_cents = None
    if bet.fair_probability is not None:
        fair_value_cents = _normalize_probability(bet.fair_probability) * ONE_HUNDRED
        edge_cents = fair_value_cents - Decimal(best_ask.price_cents)
        expected_profit_cents = edge_cents - fee_cents - extra_buffers
    else:
        expected_profit_cents = (
            Decimal(best_ask.price_cents) * (bet.ev_pct / ONE_HUNDRED)
        ) - fee_cents - extra_buffers

    if expected_profit_cents <= 0:
        blockers.append(f"expected profit {expected_profit_cents}c is not positive after fees")

    expected_profit_usd = (expected_profit_cents * contracts) / ONE_HUNDRED
    if not settings.live_trading:
        blockers.append("live trading disabled")

    return EVOpportunity(
        name=bet.group_title,
        leg=leg,
        live_price_cents=best_ask.price_cents,
        fair_value_cents=fair_value_cents,
        edge_cents=edge_cents,
        ev_pct=bet.ev_pct,
        expected_profit_cents=expected_profit_cents,
        expected_profit_usd=expected_profit_usd,
        stake_usd=stake_usd,
        executable=len(blockers) == 0,
        blockers=tuple(blockers),
    )


def _contracts_for_stake(
    price_cents: int,
    displayed_size: Decimal,
    default_trade_usd: Decimal,
    max_trade_usd: Decimal,
    api_max_wager_usd: Decimal,
) -> tuple[Decimal, Decimal]:
    if price_cents <= 0:
        return Decimal("0"), Decimal("0")
    target_usd = min(default_trade_usd, max_trade_usd)
    if api_max_wager_usd > 0:
        target_usd = min(target_usd, api_max_wager_usd, max_trade_usd)
    cost_per_contract = Decimal(price_cents) / ONE_HUNDRED
    contracts = (target_usd / cost_per_contract).to_integral_value(rounding=ROUND_FLOOR)
    contracts = min(contracts, displayed_size.to_integral_value(rounding=ROUND_FLOOR))
    stake_usd = (contracts * cost_per_contract).quantize(Decimal("0.01"))
    return contracts, stake_usd


def _normalize_probability(value: Decimal) -> Decimal:
    if value > 1:
        return value / ONE_HUNDRED
    return value


def _blocked(bet: PredictionHuntEVBet, blockers: list[str]) -> EVOpportunity:
    leg = ArbLeg(bet.platform, bet.market_id, bet.side, 0, Decimal("0"))
    return EVOpportunity(
        name=bet.group_title,
        leg=leg,
        live_price_cents=0,
        fair_value_cents=None,
        edge_cents=None,
        ev_pct=bet.ev_pct,
        expected_profit_cents=Decimal("-100"),
        expected_profit_usd=Decimal("0"),
        stake_usd=Decimal("0"),
        executable=False,
        blockers=tuple(blockers),
    )
