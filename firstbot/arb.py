from __future__ import annotations

from decimal import Decimal

from .config import Settings
from .fees import total_cost_adjustment_cents
from .models import ArbLeg, ArbOpportunity, Exchange, MarketPair, OrderBook, Side
from .predictionhunt import PredictionHuntOpportunity


def verify_pair(
    pair: MarketPair,
    kalshi_book: OrderBook,
    polymarket_book: OrderBook,
    settings: Settings,
) -> list[ArbOpportunity]:
    return [
        _build_opportunity(
            pair=pair,
            yes_book=kalshi_book,
            no_book=polymarket_book,
            yes_exchange=Exchange.KALSHI,
            no_exchange=Exchange.POLYMARKET,
            settings=settings,
        ),
        _build_opportunity(
            pair=pair,
            yes_book=polymarket_book,
            no_book=kalshi_book,
            yes_exchange=Exchange.POLYMARKET,
            no_exchange=Exchange.KALSHI,
            settings=settings,
        ),
    ]


def verify_predictionhunt_opportunity(
    opportunity: PredictionHuntOpportunity,
    live_legs: tuple[ArbLeg, ArbLeg],
    settings: Settings,
) -> ArbOpportunity:
    blockers: list[str] = []
    if len(live_legs) != 2:
        raise RuntimeError("exactly two live legs are required")
    sides = {leg.side for leg in live_legs}
    exchanges = {leg.exchange for leg in live_legs}
    if sides != {Side.YES, Side.NO}:
        blockers.append("PredictionHunt legs are not one YES and one NO")
    if exchanges != {Exchange.KALSHI, Exchange.POLYMARKET}:
        blockers.append("PredictionHunt legs are not Kalshi and Polymarket")

    yes_leg = next((leg for leg in live_legs if leg.side is Side.YES), live_legs[0])
    no_leg = next((leg for leg in live_legs if leg.side is Side.NO), live_legs[1])
    max_size = min(yes_leg.size, no_leg.size)
    if max_size < Decimal("1"):
        blockers.append("displayed depth is below 1 contract")

    gross_cost = yes_leg.price_cents + no_leg.price_cents
    buffers = total_cost_adjustment_cents((yes_leg, no_leg), settings)
    net_profit = Decimal(100 - gross_cost) - buffers
    if net_profit < Decimal(settings.min_profit_cents):
        blockers.append(
            f"net profit {net_profit}c is below minimum {settings.min_profit_cents}c"
        )
    if not settings.live_trading:
        blockers.append("live trading disabled")

    return ArbOpportunity(
        pair_name=opportunity.group_title,
        buy_yes=yes_leg,
        buy_no=no_leg,
        gross_cost_cents=gross_cost,
        buffers_cents=buffers,
        net_profit_cents=net_profit,
        executable=len(blockers) == 0,
        blockers=tuple(blockers),
    )


def _build_opportunity(
    pair: MarketPair,
    yes_book: OrderBook,
    no_book: OrderBook,
    yes_exchange: Exchange,
    no_exchange: Exchange,
    settings: Settings,
) -> ArbOpportunity:
    blockers: list[str] = []
    yes_ask = yes_book.best_ask(Side.YES)
    no_ask = no_book.best_ask(Side.NO)

    if not pair.rules_compatible:
        blockers.append("market rules have not been marked compatible")
    if yes_ask is None:
        blockers.append(f"missing YES ask on {yes_exchange.value}")
    if no_ask is None:
        blockers.append(f"missing NO ask on {no_exchange.value}")

    if yes_ask is None or no_ask is None:
        return ArbOpportunity(
            pair_name=pair.name,
            buy_yes=ArbLeg(yes_exchange, yes_book.market_id, Side.YES, 0, Decimal("0")),
            buy_no=ArbLeg(no_exchange, no_book.market_id, Side.NO, 0, Decimal("0")),
            gross_cost_cents=0,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("-100"),
            executable=False,
            blockers=tuple(blockers),
        )

    max_size = min(yes_ask.size, no_ask.size, Decimal(settings.max_leg_usd))
    if max_size < Decimal("1"):
        blockers.append("displayed depth is below 1 contract")

    gross_cost = yes_ask.price_cents + no_ask.price_cents
    yes_leg = ArbLeg(
        exchange=yes_exchange,
        market_id=yes_book.market_id,
        side=Side.YES,
        price_cents=yes_ask.price_cents,
        size=max_size,
    )
    no_leg = ArbLeg(
        exchange=no_exchange,
        market_id=no_book.market_id,
        side=Side.NO,
        price_cents=no_ask.price_cents,
        size=max_size,
    )
    buffers = total_cost_adjustment_cents((yes_leg, no_leg), settings)
    net_profit = Decimal(100 - gross_cost) - buffers
    if net_profit < Decimal(settings.min_profit_cents):
        blockers.append(
            f"net profit {net_profit}c is below minimum {settings.min_profit_cents}c"
        )
    if not settings.live_trading:
        blockers.append("live trading disabled")

    return ArbOpportunity(
        pair_name=pair.name,
        buy_yes=yes_leg,
        buy_no=no_leg,
        gross_cost_cents=gross_cost,
        buffers_cents=buffers,
        net_profit_cents=net_profit,
        executable=len(blockers) == 0,
        blockers=tuple(blockers),
    )
