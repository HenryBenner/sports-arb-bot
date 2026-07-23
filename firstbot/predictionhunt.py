from __future__ import annotations

from dataclasses import dataclass
import json
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from .http import HttpClient
from .models import Exchange, Side


@dataclass(frozen=True)
class PredictionHuntLeg:
    side: Side
    platform: Exchange
    market_id: str
    source_url: str | None
    price: Decimal
    liquidity_usd: Decimal
    fee_usd: Decimal


@dataclass(frozen=True)
class PredictionHuntOpportunity:
    group_id: int | None
    group_title: str
    event_date: str | None
    event_type: str | None
    roi_pct: Decimal
    total_cost: Decimal
    max_wager_usd: Decimal
    detected_at: str | None
    legs: tuple[PredictionHuntLeg, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class PredictionHuntEVBet:
    group_id: int | None
    group_title: str
    event_date: str | None
    event_type: str | None
    platform: Exchange
    market_id: str
    side: Side
    source_url: str | None
    price: Decimal
    fair_probability: Decimal | None
    ev_pct: Decimal
    edge_pct: Decimal
    max_wager_usd: Decimal
    detected_at: str | None
    raw: dict[str, Any]


class PredictionHuntClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        arbs_path: str,
        ev_path: str = "/api/v2/ev",
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.arbs_path = arbs_path if arbs_path.startswith("/") else f"/{arbs_path}"
        self.ev_path = ev_path if ev_path.startswith("/") else f"/{ev_path}"
        self.http = http or HttpClient()

    def get_arbitrage_opportunities(
        self,
        category: str = "sports",
        limit: int = 25,
        min_roi: float = 0,
        platforms: str = "polymarket,kalshi",
    ) -> list[PredictionHuntOpportunity]:
        if not self.api_key:
            raise RuntimeError("PREDICTIONHUNT_API_KEY is required")
        data = self.http.get_json(
            f"{self.base_url}{self.arbs_path}",
            params={
                "min_roi": min_roi,
                "platforms": platforms,
                "limit": limit,
            },
            headers=self._headers(),
        )
        opportunities = [_opportunity(item) for item in _items(data)]
        if category:
            normalized = category.lower()
            opportunities = [
                opp
                for opp in opportunities
                if (opp.event_type or "").lower() == normalized
            ]
        return opportunities

    def get_expected_value_bets(
        self,
        category: str | None = None,
        limit: int = 25,
        min_ev: float = 0,
        platforms: str = "polymarket,kalshi",
    ) -> list[PredictionHuntEVBet]:
        if not self.api_key:
            raise RuntimeError("PREDICTIONHUNT_API_KEY is required")
        data = self.http.get_json(
            f"{self.base_url}{self.ev_path}",
            params={
                "min_ev": min_ev,
                "platforms": platforms,
                "limit": limit,
            },
            headers=self._headers(),
        )
        bets = [_ev_bet(item) for item in _items(data)]
        if category:
            normalized = category.lower()
            bets = [
                bet
                for bet in bets
                if (bet.event_type or "").lower() == normalized
            ]
        return bets

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
        }

    async def listen_signal_channels(
        self,
        ws_url: str,
        channels: tuple[str, ...],
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.api_key:
            raise RuntimeError("PREDICTIONHUNT_API_KEY is required")
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use PredictionHunt signal streams") from exc

        async with websockets.connect(ws_url, additional_headers=self._headers()) as websocket:
            for channel in channels:
                await websocket.send(json.dumps({"action": "subscribe", "channel": channel}))
            while True:
                raw = await websocket.recv()
                message = json.loads(raw)
                if isinstance(message, dict):
                    yield message


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("opportunities", "arbitrage", "arbs", "odds", "markets", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _opportunity(item: dict[str, Any]) -> PredictionHuntOpportunity:
    legs = tuple(_leg(leg) for leg in item.get("legs", []))
    if len(legs) != 2:
        raise RuntimeError("PredictionHunt opportunity did not include exactly two legs")
    return PredictionHuntOpportunity(
        group_id=_optional_int(item.get("group_id")),
        group_title=str(item.get("group_title") or item.get("title") or ""),
        event_date=item.get("event_date"),
        event_type=item.get("event_type"),
        roi_pct=_decimal(item.get("roi_pct")),
        total_cost=_decimal(item.get("total_cost")),
        max_wager_usd=_decimal(item.get("max_wager_usd")),
        detected_at=item.get("detected_at"),
        legs=(legs[0], legs[1]),
        raw=item,
    )


def _leg(item: dict[str, Any]) -> PredictionHuntLeg:
    platform = _platform(item.get("platform"))
    if platform is None:
        raise RuntimeError(f"unsupported PredictionHunt platform: {item.get('platform')}")
    return PredictionHuntLeg(
        side=Side(str(item.get("side", "")).lower()),
        platform=platform,
        market_id=str(item.get("market_id") or ""),
        source_url=item.get("source_url"),
        price=_decimal(item.get("price")),
        liquidity_usd=_decimal(item.get("liquidity_usd")),
        fee_usd=_decimal(item.get("fee_usd")),
    )


def _ev_bet(item: dict[str, Any]) -> PredictionHuntEVBet:
    platform = _platform(_first(item, "platform", "exchange", "venue"))
    if platform is None:
        raise RuntimeError(f"unsupported PredictionHunt EV platform: {_first(item, 'platform', 'exchange', 'venue')}")
    side_value = str(_first(item, "side", "outcome_side", "direction") or "").lower()
    return PredictionHuntEVBet(
        group_id=_optional_int(_first(item, "group_id", "market_group_id", "event_id")),
        group_title=str(_first(item, "group_title", "title", "market_title", "question") or ""),
        event_date=_first(item, "event_date", "end_date", "close_time", "resolution_date"),
        event_type=_first(item, "event_type", "category"),
        platform=platform,
        market_id=str(_first(item, "market_id", "token_id", "ticker") or ""),
        side=Side(side_value),
        source_url=_first(item, "source_url", "url"),
        price=_decimal(_first(item, "price", "market_price", "ask", "best_ask")),
        fair_probability=_optional_decimal(
            _first(item, "fair_probability", "true_probability", "probability", "estimated_probability")
        ),
        ev_pct=_decimal(_first(item, "ev_pct", "expected_value_pct", "roi_pct", "value_pct")),
        edge_pct=_decimal(_first(item, "edge_pct", "edge", "edge_percent")),
        max_wager_usd=_decimal(
            _first(item, "max_wager_usd", "max_stake_usd", "breakeven_wager_usd", "max_trade_usd")
        ),
        detected_at=_first(item, "detected_at", "as_of", "updated_at"),
        raw=item,
    )


def _platform(value: Any) -> Exchange | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    aliases = {"calshubot": "kalshi", "calshi": "kalshi"}
    normalized = aliases.get(normalized, normalized)
    if normalized == "kalshi":
        return Exchange.KALSHI
    if normalized == "polymarket":
        return Exchange.POLYMARKET
    return None


def _decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None
