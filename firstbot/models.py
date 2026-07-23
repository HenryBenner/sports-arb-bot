from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Exchange(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class BookLevel:
    price_cents: int
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    exchange: Exchange
    market_id: str
    yes_asks: list[BookLevel]
    no_asks: list[BookLevel]
    timestamp: str | None = None

    def best_ask(self, side: Side) -> BookLevel | None:
        levels = self.yes_asks if side is Side.YES else self.no_asks
        return min(levels, key=lambda level: level.price_cents, default=None)


@dataclass(frozen=True)
class MarketPair:
    name: str
    kalshi_ticker: str
    polymarket_yes_token_id: str
    polymarket_no_token_id: str
    rules_compatible: bool
    notes: str = ""


@dataclass(frozen=True)
class ArbLeg:
    exchange: Exchange
    market_id: str
    side: Side
    price_cents: int
    size: Decimal
    avg_price_cents: Decimal | None = None
    source_price_cents: Decimal | None = None


@dataclass(frozen=True)
class ArbOpportunity:
    pair_name: str
    buy_yes: ArbLeg
    buy_no: ArbLeg
    gross_cost_cents: int | Decimal
    buffers_cents: Decimal
    net_profit_cents: Decimal
    executable: bool
    blockers: tuple[str, ...]
    event_type: str | None = None


@dataclass(frozen=True)
class EVOpportunity:
    name: str
    leg: ArbLeg
    live_price_cents: int
    fair_value_cents: Decimal | None
    edge_cents: Decimal | None
    ev_pct: Decimal
    expected_profit_cents: Decimal
    expected_profit_usd: Decimal
    stake_usd: Decimal
    executable: bool
    blockers: tuple[str, ...]
