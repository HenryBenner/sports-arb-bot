from __future__ import annotations

import base64
import importlib.util
import os
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any
from urllib.parse import unquote, urlsplit

from ..models import BookLevel, Exchange, FeeSchedule, OrderBook, Side


REF_SEPARATOR = "::"
CONFIRMED_STATES = {"order_state_filled", "filled"}
TERMINAL_EMPTY_STATES = {
    "order_state_canceled", "order_state_cancelled", "order_state_expired",
    "order_state_rejected", "canceled", "cancelled", "expired", "rejected",
}


class PolymarketUSClient:
    """Expose the Polymarket US retail API through FirstBot's exchange interface."""

    is_us = True

    def __init__(
        self,
        public_url: str,
        api_url: str,
        websocket_url: str,
        key_id: str | None = None,
        secret_key: str | None = None,
        timeout: int = 30,
        sdk_client: Any | None = None,
    ) -> None:
        self.public_url = public_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.websocket_url = websocket_url
        self.gamma_url = self.public_url
        self.clob_url = self.api_url
        self.key_id = key_id
        self.secret_key = secret_key
        self.timeout = timeout
        self._sdk_client = sdk_client

    def _client(self) -> Any:
        if self._sdk_client is None:
            if os.name == "nt":
                try:
                    import truststore

                    truststore.inject_into_ssl()
                except ImportError:
                    pass
            try:
                from polymarket_us import PolymarketUS
            except ImportError as exc:
                raise RuntimeError("polymarket_us_sdk_missing: install polymarket-us") from exc
            kwargs: dict[str, Any] = {
                "gateway_base_url": self.public_url,
                "api_base_url": self.api_url,
                "timeout": float(self.timeout),
            }
            if self.key_id and self.secret_key:
                kwargs.update(key_id=self.key_id, secret_key=self.secret_key)
            self._sdk_client = PolymarketUS(**kwargs)
        return self._sdk_client

    def validate_credentials_locally(self) -> str:
        if not self.key_id:
            raise RuntimeError("polymarket_us_key_id_missing")
        if not self.secret_key:
            raise RuntimeError("polymarket_us_secret_key_missing")
        try:
            decoded = base64.b64decode(self.secret_key, validate=True)
        except Exception as exc:
            raise RuntimeError("polymarket_us_secret_key_not_base64") from exc
        if len(decoded) not in {32, 64}:
            raise RuntimeError(
                f"polymarket_us_secret_key_invalid_length: decoded {len(decoded)} bytes"
            )
        self._client()
        return "OK"

    def get_events(self, **params: Any) -> Any:
        return self._client().events.list(params)

    def get_markets(self, **params: Any) -> Any:
        return self._client().markets.list(params)

    def get_market_by_slug(self, slug: str) -> dict[str, Any]:
        result = self._client().markets.retrieve_by_slug(slug)
        if isinstance(result, dict) and isinstance(result.get("market"), dict):
            result = result["market"]
        if not isinstance(result, dict):
            raise RuntimeError(f"Polymarket US market lookup returned invalid data: {slug}")
        return result

    def resolve_predictionhunt_market(
        self,
        market_id: str,
        side: Side,
        source_url: str | None = None,
    ) -> str:
        current_slug, _ = self._split_ref(market_id)
        url_slug = _market_slug_from_url(source_url)
        candidates = [value for value in (url_slug, current_slug) if value]
        errors: list[str] = []
        for slug in dict.fromkeys(candidates):
            if slug.isdigit():
                continue
            try:
                market = self.get_market_by_slug(slug)
            except Exception as exc:
                errors.append(str(exc))
                continue
            resolved = str(market.get("slug") or slug).strip()
            if resolved:
                return self._ref(resolved, side)
        detail = f": {'; '.join(errors)}" if errors else ""
        raise RuntimeError(
            "polymarket_us_market_mapping_required: PredictionHunt must provide a "
            f"Polymarket US market slug or URL; received {market_id!r}{detail}"
        )

    def resolve_clob_token_id(self, market_id: str, side: Side) -> str:
        return self.resolve_predictionhunt_market(market_id, side)

    def resolve_clob_token_id_for_outcome(
        self, market_id: str, outcome: str | None, fallback_side: Side
    ) -> str:
        normalized = str(outcome or "").strip().lower()
        side = Side(normalized) if normalized in {"yes", "no"} else fallback_side
        return self.resolve_predictionhunt_market(market_id, side)

    def get_orderbook(
        self, yes_token_id: str, no_token_id: str, market_id: str | None = None
    ) -> OrderBook:
        yes_slug, _ = self._split_ref(yes_token_id)
        no_slug, _ = self._split_ref(no_token_id)
        if yes_slug != no_slug:
            raise RuntimeError("Polymarket US YES and NO references must use one market slug")
        data = self._book_data(yes_slug)
        return OrderBook(
            exchange=Exchange.POLYMARKET,
            market_id=market_id or yes_slug,
            yes_asks=self._offer_levels(data),
            no_asks=self._no_ask_levels(data),
            timestamp=str(data.get("transactTime") or "") or None,
        )

    def get_token_ask_levels(
        self, token_id: str, side: Side | None = None
    ) -> list[BookLevel]:
        slug, encoded_side = self._split_ref(token_id)
        selected_side = side or encoded_side
        if selected_side is None:
            raise RuntimeError("Polymarket US market reference is missing its YES/NO side")
        data = self._book_data(slug)
        return self._offer_levels(data) if selected_side is Side.YES else self._no_ask_levels(data)

    def get_token_best_ask(
        self, token_id: str, side: Side | None = None
    ) -> BookLevel | None:
        return min(
            self.get_token_ask_levels(token_id, side),
            key=lambda level: level.price_cents,
            default=None,
        )

    def get_token_bid_levels(
        self, token_id: str, side: Side | None = None
    ) -> list[BookLevel]:
        slug, encoded_side = self._split_ref(token_id)
        selected_side = side or encoded_side
        if selected_side is None:
            raise RuntimeError("Polymarket US market reference is missing its YES/NO side")
        data = self._book_data(slug)
        if selected_side is Side.YES:
            return self._bid_levels(data)
        return sorted(
            [BookLevel(100 - level.price_cents, level.size) for level in self._offer_levels(data)],
            key=lambda level: level.price_cents,
            reverse=True,
        )

    def get_token_min_order_size(self, token_id: str) -> Decimal:
        return Decimal("1")

    def get_taker_fee_schedule(self, token_id: str) -> FeeSchedule:
        return FeeSchedule(
            exchange=Exchange.POLYMARKET,
            fee_type="polymarket_us_curve",
            rate=Decimal("0.05"),
            exponent=Decimal("1"),
            source="Polymarket US exchange-wide taker fee",
        )

    def available_cash_usd(self) -> Decimal:
        raw = self._client().account.balances()
        balances = raw.get("balances") if isinstance(raw, dict) else None
        if not isinstance(balances, list):
            balances = [raw] if isinstance(raw, dict) else []
        for balance in balances:
            if not isinstance(balance, dict):
                continue
            if str(balance.get("currency") or "USD").upper() != "USD":
                continue
            value = balance.get("buyingPower", balance.get("currentBalance"))
            if value is not None:
                return Decimal(str(value))
        raise RuntimeError(f"Polymarket US balance response has no USD buying power: {raw}")

    def buy(
        self, token_id: str, price_cents: int, size: Decimal,
        fill_or_kill: bool = True, confirmation_timeout_seconds: float | None = None,
        confirmation_poll_seconds: float = 0.2,
    ) -> dict[str, Any]:
        return self._submit(token_id, price_cents, size, True, fill_or_kill)

    def sell(
        self, token_id: str, price_cents: int, size: Decimal,
        fill_or_kill: bool = True, confirmation_timeout_seconds: float | None = None,
        confirmation_poll_seconds: float = 0.2,
    ) -> dict[str, Any]:
        return self._submit(token_id, price_cents, size, False, fill_or_kill)

    def _submit(
        self, token_id: str, price_cents: int, size: Decimal,
        buy: bool, fill_or_kill: bool,
    ) -> dict[str, Any]:
        if not fill_or_kill:
            raise RuntimeError("Polymarket US live trading requires FOK orders")
        slug, side = self._split_ref(token_id)
        if side is None:
            raise RuntimeError("Polymarket US order reference is missing its YES/NO side")
        intent = {
            (True, Side.YES): "ORDER_INTENT_BUY_LONG",
            (True, Side.NO): "ORDER_INTENT_BUY_SHORT",
            (False, Side.YES): "ORDER_INTENT_SELL_LONG",
            (False, Side.NO): "ORDER_INTENT_SELL_SHORT",
        }[(buy, side)]
        raw = self._client().orders.create({
            "marketSlug": slug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {
                "value": str((Decimal(price_cents) / Decimal("100")).quantize(Decimal("0.001"))),
                "currency": "USD",
            },
            "quantity": float(size),
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": True,
            "maxBlockTime": "3",
        })
        return self._normalize_order_result(raw, size)

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._normalize_order_result(
            self._client().orders.retrieve(order_id), None, require_fill=False
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        order = self.get_order(order_id)
        slug = str(order.get("marketSlug") or "")
        return self._client().orders.cancel(order_id, {"marketSlug": slug})

    def supports_immediate_orders(self) -> bool:
        return bool(
            self.key_id and self.secret_key
            and importlib.util.find_spec("polymarket_us") is not None
        )

    def market_websocket(self) -> Any:
        websocket = self._client().ws.markets()
        parsed = urlsplit(self.websocket_url)
        if parsed.scheme and parsed.netloc:
            websocket.base_url = f"{parsed.scheme}://{parsed.netloc}"
            websocket.path = parsed.path or "/v1/ws/markets"
        return websocket

    def _book_data(self, slug: str) -> dict[str, Any]:
        raw = self._client().markets.book(slug)
        data = raw.get("marketData") if isinstance(raw, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError(f"Polymarket US book returned invalid data for {slug}: {raw}")
        return data

    @staticmethod
    def _offer_levels(data: dict[str, Any]) -> list[BookLevel]:
        return _levels(data.get("offers"), reverse=False)

    @staticmethod
    def _bid_levels(data: dict[str, Any]) -> list[BookLevel]:
        return _levels(data.get("bids"), reverse=True)

    @classmethod
    def _no_ask_levels(cls, data: dict[str, Any]) -> list[BookLevel]:
        return sorted(
            [BookLevel(100 - level.price_cents, level.size) for level in cls._bid_levels(data)],
            key=lambda level: level.price_cents,
        )

    @staticmethod
    def _ref(slug: str, side: Side) -> str:
        return f"{slug}{REF_SEPARATOR}{side.value}"

    @staticmethod
    def _split_ref(value: str) -> tuple[str, Side | None]:
        text = str(value or "").strip()
        if REF_SEPARATOR not in text:
            return text, None
        slug, raw_side = text.rsplit(REF_SEPARATOR, 1)
        try:
            return slug, Side(raw_side.lower())
        except ValueError as exc:
            raise RuntimeError(f"Invalid Polymarket US market side in {value!r}") from exc

    @staticmethod
    def _normalize_order_result(
        raw: Any, expected_size: Decimal | None, require_fill: bool = True
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise RuntimeError(f"Polymarket US order returned invalid data: {raw}")
        order = raw.get("order") if isinstance(raw.get("order"), dict) else raw
        executions = raw.get("executions") if isinstance(raw.get("executions"), list) else []
        execution_orders = [
            item.get("order") for item in executions
            if isinstance(item, dict) and isinstance(item.get("order"), dict)
        ]
        if execution_orders:
            order = execution_orders[-1]
        state = str(order.get("state") or raw.get("state") or "").lower()
        order_id = str(order.get("id") or raw.get("id") or "")
        filled = Decimal(str(order.get("cumQuantity") or "0"))
        if state in CONFIRMED_STATES or (
            expected_size is not None and filled >= expected_size
        ):
            return {
                **order, "success": True, "status": "matched",
                "orderID": order_id, "size_matched": str(filled or expected_size or 0),
            }
        if not require_fill:
            return {**order, "orderID": order_id, "status": state}
        if state in TERMINAL_EMPTY_STATES:
            raise RuntimeError(f"Polymarket US FOK order was not filled state={state}: {raw}")
        raise RuntimeError(
            "polymarket_order_state_uncertain: Polymarket US FOK order was not "
            f"confirmed filled state={state or 'missing'} order_id={order_id or 'missing'}: {raw}"
        )


def _levels(raw_levels: Any, reverse: bool) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for item in raw_levels if isinstance(raw_levels, list) else []:
        if not isinstance(item, dict):
            continue
        px = item.get("px")
        value = px.get("value") if isinstance(px, dict) else px
        quantity = item.get("qty", item.get("quantity"))
        try:
            price = int(
                (Decimal(str(value)) * Decimal("100")).to_integral_value(
                    rounding=ROUND_FLOOR if reverse else ROUND_CEILING
                )
            )
            size = Decimal(str(quantity))
        except Exception:
            continue
        if 0 < price < 100 and size > 0:
            levels.append(BookLevel(price, size))
    return sorted(levels, key=lambda level: level.price_cents, reverse=reverse)


def _market_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = [unquote(part) for part in urlsplit(url).path.split("/") if part]
    for marker in ("market", "event"):
        if marker in parts:
            index = parts.index(marker) + 1
            if index < len(parts):
                return parts[index]
    return parts[-1] if parts else None
