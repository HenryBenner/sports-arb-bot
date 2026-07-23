from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from .exchanges import KalshiClient, PolymarketClient
from .hot import LiveLegBook
from .models import BookLevel, Exchange, Side
from .predictionhunt import PredictionHuntLeg
from .ssl_compat import websocket_ssl_context


class AggregatedBook:
    def __init__(self, exchange: Exchange, market_id: str) -> None:
        self.exchange = exchange
        self.market_id = market_id
        self.levels: dict[Side, dict[int, Decimal]] = {Side.YES: {}, Side.NO: {}}
        self.bid_levels: dict[Side, dict[Decimal, Decimal]] = {Side.YES: {}, Side.NO: {}}
        self.snapshot_ready = False
        self.connected = True

    def replace(self, side: Side, levels: list[BookLevel]) -> None:
        self.levels[side] = {
            level.price_cents: level.size for level in levels if level.size > Decimal("0")
        }
        self.snapshot_ready = True

    def apply_delta(self, side: Side, price_cents: int, delta: Decimal) -> None:
        current = self.levels[side].get(price_cents, Decimal("0"))
        updated = current + delta
        if updated <= Decimal("0"):
            self.levels[side].pop(price_cents, None)
        else:
            self.levels[side][price_cents] = updated
        self.snapshot_ready = True

    def replace_kalshi_bids(self, side: Side, levels: list[tuple[Decimal, Decimal]]) -> None:
        self.bid_levels[side] = {
            price_cents: size for price_cents, size in levels if size > Decimal("0")
        }
        self.snapshot_ready = True

    def apply_kalshi_bid_delta(self, side: Side, price_cents: Decimal, delta: Decimal) -> None:
        current = self.bid_levels[side].get(price_cents, Decimal("0"))
        updated = current + delta
        if updated <= Decimal("0"):
            self.bid_levels[side].pop(price_cents, None)
        else:
            self.bid_levels[side][price_cents] = updated
        self.snapshot_ready = True

    def kalshi_buy_ask(self, side: Side, now: datetime) -> LiveLegBook:
        opposing_side = Side.NO if side is Side.YES else Side.YES
        opposing_bids = self.bid_levels[opposing_side]
        levels: list[BookLevel] = []
        for bid_price, size in sorted(opposing_bids.items(), key=lambda item: item[0], reverse=True):
            ask_cents_decimal = Decimal("100") - bid_price
            ask_cents = int(ask_cents_decimal.to_integral_value(rounding=ROUND_CEILING))
            levels.append(BookLevel(price_cents=ask_cents, size=size))
        best = min(levels, key=lambda level: level.price_cents, default=None)
        return LiveLegBook(
            exchange=self.exchange,
            market_id=self.market_id,
            side=side,
            best_ask=best,
            updated_at=now,
            connected=self.connected,
            snapshot_ready=self.snapshot_ready,
            ask_levels=tuple(levels),
        )

    def best_ask(self, side: Side, now: datetime) -> LiveLegBook:
        levels = self.levels[side]
        price = min(levels, default=None)
        best = None if price is None else BookLevel(price_cents=price, size=levels[price])
        ask_levels = tuple(
            BookLevel(price_cents=level_price, size=size)
            for level_price, size in sorted(levels.items(), key=lambda item: item[0])
        )
        return LiveLegBook(
            exchange=self.exchange,
            market_id=self.market_id,
            side=side,
            best_ask=best,
            updated_at=now,
            connected=self.connected,
            snapshot_ready=self.snapshot_ready,
            ask_levels=ask_levels,
        )


class KalshiOrderbookStream:
    def __init__(self, kalshi: KalshiClient, legs: tuple[PredictionHuntLeg, ...]) -> None:
        self.kalshi = kalshi
        self.legs = tuple(leg for leg in legs if leg.platform is Exchange.KALSHI)
        self.url = _kalshi_ws_url(kalshi.base_url)

    async def listen_until(self, expires_at: datetime) -> AsyncIterator[LiveLegBook]:
        if not self.legs:
            return
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use live Kalshi streams") from exc

        tickers = sorted({leg.market_id for leg in self.legs})
        headers = self.kalshi._auth_headers("GET", "/trade-api/ws/v2")
        ssl_context = websocket_ssl_context(self.url)
        async with websockets.connect(self.url, additional_headers=headers, ssl=ssl_context) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": tickers,
                        },
                    }
                )
            )
            books: dict[str, AggregatedBook] = {}
            while datetime.now(timezone.utc) < expires_at:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=max(0.1, (expires_at - datetime.now(timezone.utc)).total_seconds()),
                )
                for update in parse_kalshi_message(json.loads(raw), books):
                    yield update

    async def raw_listen_until(self, expires_at: datetime) -> AsyncIterator[dict[str, Any]]:
        if not self.legs:
            return
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use live Kalshi streams") from exc

        tickers = sorted({leg.market_id for leg in self.legs})
        headers = self.kalshi._auth_headers("GET", "/trade-api/ws/v2")
        ssl_context = websocket_ssl_context(self.url)
        async with websockets.connect(self.url, additional_headers=headers, ssl=ssl_context) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["orderbook_delta"],
                            "market_tickers": tickers,
                        },
                    }
                )
            )
            while datetime.now(timezone.utc) < expires_at:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=max(0.1, (expires_at - datetime.now(timezone.utc)).total_seconds()),
                )
                yield json.loads(raw)


class PolymarketOrderbookStream:
    def __init__(self, polymarket: PolymarketClient, legs: tuple[PredictionHuntLeg, ...]) -> None:
        self.polymarket = polymarket
        self.legs = tuple(leg for leg in legs if leg.platform is Exchange.POLYMARKET)
        self.url = _polymarket_ws_url(polymarket.clob_url)

    async def listen_until(self, expires_at: datetime) -> AsyncIterator[LiveLegBook]:
        if not self.legs:
            return
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use live Polymarket streams") from exc

        token_ids = sorted({leg.market_id for leg in self.legs})
        side_by_token_id = {leg.market_id: leg.side for leg in self.legs}
        ssl_context = websocket_ssl_context(self.url)
        async with websockets.connect(self.url, ssl=ssl_context) as websocket:
            await websocket.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
            books: dict[str, AggregatedBook] = {}
            while datetime.now(timezone.utc) < expires_at:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=max(0.1, (expires_at - datetime.now(timezone.utc)).total_seconds()),
                )
                for update in parse_polymarket_message(json.loads(raw), books):
                    yield _remap_polymarket_token_side(update, side_by_token_id)

    async def raw_listen_until(self, expires_at: datetime) -> AsyncIterator[dict[str, Any] | list[dict[str, Any]]]:
        if not self.legs:
            return
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("Install websockets to use live Polymarket streams") from exc

        token_ids = sorted({leg.market_id for leg in self.legs})
        ssl_context = websocket_ssl_context(self.url)
        async with websockets.connect(self.url, ssl=ssl_context) as websocket:
            await websocket.send(json.dumps({"assets_ids": token_ids, "type": "market"}))
            while datetime.now(timezone.utc) < expires_at:
                raw = await asyncio.wait_for(
                    websocket.recv(),
                    timeout=max(0.1, (expires_at - datetime.now(timezone.utc)).total_seconds()),
                )
                yield json.loads(raw)


def parse_kalshi_message(
    message: dict[str, Any],
    books: dict[str, AggregatedBook] | None = None,
    now: datetime | None = None,
) -> list[LiveLegBook]:
    books = books if books is not None else {}
    now = now or datetime.now(timezone.utc)
    msg = message.get("msg", {})
    market_id = str(msg.get("market_ticker") or "")
    if not market_id:
        return []
    book = books.setdefault(market_id, AggregatedBook(Exchange.KALSHI, market_id))
    if message.get("type") == "orderbook_snapshot":
        book.replace_kalshi_bids(Side.YES, _kalshi_bid_levels_from_pairs(msg.get("yes_dollars_fp", [])))
        book.replace_kalshi_bids(Side.NO, _kalshi_bid_levels_from_pairs(msg.get("no_dollars_fp", [])))
        return [book.kalshi_buy_ask(Side.YES, now), book.kalshi_buy_ask(Side.NO, now)]
    if message.get("type") == "orderbook_delta":
        side = Side(str(msg.get("side", "")).lower())
        price_cents = _dollars_to_cents_decimal(msg.get("price_dollars"))
        delta = Decimal(str(msg.get("delta_fp", "0")))
        book.apply_kalshi_bid_delta(side, price_cents, delta)
        affected_buy_side = Side.NO if side is Side.YES else Side.YES
        return [book.kalshi_buy_ask(affected_buy_side, now)]
    return []


def parse_polymarket_message(
    message: dict[str, Any] | list[dict[str, Any]],
    books: dict[str, AggregatedBook] | None = None,
    now: datetime | None = None,
) -> list[LiveLegBook]:
    books = books if books is not None else {}
    now = now or datetime.now(timezone.utc)
    messages = message if isinstance(message, list) else [message]
    updates: list[LiveLegBook] = []
    for item in messages:
        event_type = item.get("event_type") or item.get("type")
        market_id = str(item.get("asset_id") or item.get("token_id") or item.get("market") or "")
        if not market_id:
            continue
        book = books.setdefault(market_id, AggregatedBook(Exchange.POLYMARKET, market_id))
        if event_type in {"book", "orderbook", "snapshot"}:
            asks = _poly_levels(item.get("asks", []))
            book.replace(Side.YES, asks)
            updates.append(book.best_ask(Side.YES, now))
        elif event_type in {"price_change", "book_delta", "delta"}:
            changes = item.get("changes") if isinstance(item.get("changes"), list) else [item]
            for change in changes:
                asset_id = str(change.get("asset_id") or change.get("token_id") or market_id)
                change_book = books.setdefault(asset_id, AggregatedBook(Exchange.POLYMARKET, asset_id))
                side_text = str(change.get("side") or change.get("change_type") or "sell").lower()
                if side_text not in {"sell", "ask", "asks"}:
                    continue
                price_cents = _price_to_cents_or_none(change.get("price"))
                size = _decimal_or_none(change.get("size", change.get("new_size")))
                if price_cents is None or size is None:
                    continue
                if size <= Decimal("0"):
                    change_book.levels[Side.YES].pop(price_cents, None)
                else:
                    change_book.levels[Side.YES][price_cents] = size
                change_book.snapshot_ready = True
                updates.append(change_book.best_ask(Side.YES, now))
    return updates


def _remap_polymarket_token_side(
    update: LiveLegBook,
    side_by_token_id: dict[str, Side],
) -> LiveLegBook:
    side = side_by_token_id.get(update.market_id, update.side)
    if side is update.side:
        return update
    return LiveLegBook(
        exchange=update.exchange,
        market_id=update.market_id,
        side=side,
        best_ask=update.best_ask,
        updated_at=update.updated_at,
        connected=update.connected,
        snapshot_ready=update.snapshot_ready,
        ask_levels=update.ask_levels,
    )


def _kalshi_bid_levels_from_pairs(levels: list[list[Any]]) -> list[tuple[Decimal, Decimal]]:
    return [
        (_dollars_to_cents_decimal(price), Decimal(str(size)))
        for price, size in levels
    ]


def _poly_levels(levels: list[dict[str, Any]]) -> list[BookLevel]:
    return [
        BookLevel(price_cents=_price_to_cents(level["price"]), size=Decimal(str(level["size"])))
        for level in levels
    ]


def _price_to_cents(value: Any) -> int:
    return int((Decimal(str(value)) * Decimal("100")).to_integral_value())


def _price_to_cents_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return _price_to_cents(value)
    except (InvalidOperation, ValueError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _dollars_to_cents_decimal(value: Any) -> Decimal:
    return Decimal(str(value)) * Decimal("100")


def _kalshi_ws_url(base_url: str) -> str:
    if "demo" in base_url:
        return "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
    return "wss://external-api-ws.kalshi.com/trade-api/ws/v2"


def _polymarket_ws_url(clob_url: str) -> str:
    if "clob.polymarket.com" in clob_url:
        return "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    return clob_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/ws/market"
