from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from decimal import Decimal
from typing import Any

from ..http import HttpClient
from ..models import BookLevel, Exchange, OrderBook, Side
from ..ssl_compat import windows_truststore_context


POLYMARKET_CONFIRMATION_TIMEOUT_SECONDS = 3.5
POLYMARKET_CONFIRMATION_POLL_SECONDS = 0.25
POLYMARKET_CONFIRMED_STATUSES = {"filled", "matched"}
POLYMARKET_TERMINAL_EMPTY_STATUSES = {"canceled", "cancelled", "expired", "failed", "rejected"}


class PolymarketClient:
    def __init__(
        self,
        gamma_url: str,
        clob_url: str,
        private_key: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        funder_address: str | None = None,
        signature_type: int = 3,
        http: HttpClient | None = None,
    ) -> None:
        self.gamma_url = gamma_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.private_key = private_key
        self.api_key = api_key
        self.api_secret = api_secret
        self.api_passphrase = api_passphrase
        self.funder_address = funder_address
        self.signature_type = signature_type
        self.http = http or HttpClient()
        self._trading_client: Any | None = None

    def get_events(self, **params: Any) -> Any:
        return self.http.get_json(f"{self.gamma_url}/events", params=params)

    def get_market_by_token(self, token_id: str) -> dict[str, Any]:
        data = self.http.get_json(f"{self.clob_url}/markets-by-token/{token_id}")
        if not isinstance(data, dict):
            raise RuntimeError(f"Polymarket token lookup did not return a market: {token_id}")
        return data

    def get_orderbook(
        self,
        yes_token_id: str,
        no_token_id: str,
        market_id: str | None = None,
    ) -> OrderBook:
        yes_book = self._token_book(yes_token_id)
        no_book = self._token_book(no_token_id)
        return OrderBook(
            exchange=Exchange.POLYMARKET,
            market_id=market_id or yes_book.get("market") or yes_token_id,
            yes_asks=self._poly_asks(yes_book.get("asks", [])),
            no_asks=self._poly_asks(no_book.get("asks", [])),
            timestamp=yes_book.get("timestamp"),
        )

    def get_token_best_ask(self, token_id: str) -> BookLevel | None:
        asks = self.get_token_ask_levels(token_id)
        return min(asks, key=lambda level: level.price_cents, default=None)

    def get_token_ask_levels(self, token_id: str) -> list[BookLevel]:
        book = self._token_book(token_id)
        return sorted(self._poly_asks(book.get("asks", [])), key=lambda level: level.price_cents)

    def get_token_bid_levels(self, token_id: str) -> list[BookLevel]:
        book = self._token_book(token_id)
        return sorted(self._poly_bids(book.get("bids", [])), key=lambda level: level.price_cents, reverse=True)

    def available_cash_usd(self) -> Decimal:
        client, types = self._client_and_types()
        params = types["BalanceAllowanceParams"](
            asset_type=types["AssetType"].COLLATERAL,
            **({"signature_type": types["SignatureTypeV2"].POLY_1271} if self.signature_type == 3 else {}),
        )
        if self.signature_type == 3 and hasattr(client, "update_balance_allowance"):
            raw = client.update_balance_allowance(params)
            if not _has_balance_or_allowance(raw) and hasattr(client, "get_balance_allowance"):
                raw = client.get_balance_allowance(params)
        else:
            raw = client.get_balance_allowance(params)
        balance = _usdc_amount_from_balance_allowance(raw, "balance")
        allowance = _usdc_amount_from_balance_allowance(raw, "allowance")
        if balance is None:
            raise RuntimeError(f"Polymarket balance response did not include balance: {raw}")
        if allowance is not None:
            return min(balance, allowance)
        return balance

    def resolve_clob_token_id(self, market_id: str, side: Side) -> str:
        try:
            self._token_book(market_id)
            return market_id
        except RuntimeError:
            pass
        market = self._gamma_market(market_id)
        token_ids = _json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))
        if not token_ids:
            raise RuntimeError(f"Polymarket market {market_id} has no clobTokenIds")
        index = 0 if side is Side.YES else 1
        try:
            return str(token_ids[index])
        except IndexError as exc:
            raise RuntimeError(
                f"Polymarket market {market_id} does not include token index {index}"
            ) from exc

    def resolve_clob_token_id_for_outcome(
        self,
        market_id: str,
        outcome: str | None,
        fallback_side: Side,
    ) -> str:
        if not outcome:
            return self.resolve_clob_token_id(market_id, fallback_side)
        market = self._gamma_market(market_id)
        token_ids = _json_list(market.get("clobTokenIds"))
        outcomes = [str(item).strip().lower() for item in _json_list(market.get("outcomes"))]
        desired = outcome.strip().lower()
        if outcomes and desired in outcomes:
            index = outcomes.index(desired)
            try:
                return str(token_ids[index])
            except IndexError as exc:
                raise RuntimeError(
                    f"Polymarket market {market_id} outcome {outcome} has no CLOB token"
                ) from exc
        return self.resolve_clob_token_id(market_id, fallback_side)

    def buy(
        self,
        token_id: str,
        price_cents: int,
        size: Decimal,
        fill_or_kill: bool = True,
        confirmation_timeout_seconds: float | None = None,
        confirmation_poll_seconds: float = POLYMARKET_CONFIRMATION_POLL_SECONDS,
    ) -> dict[str, Any]:
        if not fill_or_kill:
            raise RuntimeError("Polymarket live trading requires immediate order behavior")
        client, types = self._client_and_types()
        if self.signature_type == 3:
            return self._order_v2(
                client,
                types,
                token_id,
                price_cents,
                size,
                types["Side"].BUY,
                confirmation_timeout_seconds=confirmation_timeout_seconds,
                confirmation_poll_seconds=confirmation_poll_seconds,
            )
        order_args = types["OrderArgs"](
            price=float(Decimal(price_cents) / Decimal("100")),
            size=float(size),
            side=types["BUY"],
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        order_type = getattr(types["OrderType"], "FOK", None)
        if order_type is None:
            raise RuntimeError("Installed Polymarket SDK does not expose FOK orders")
        try:
            result = client.post_order(signed_order, order_type)
        except Exception as exc:
            if _looks_like_uncertain_post_error(exc):
                raise RuntimeError(
                    "polymarket_order_state_uncertain: Polymarket order submission "
                    f"returned no reliable status: {exc}"
                ) from exc
            raise
        return self._confirm_fok_result(
            result,
            expected_size=size,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
            confirmation_poll_seconds=confirmation_poll_seconds,
        )

    def sell(
        self,
        token_id: str,
        price_cents: int,
        size: Decimal,
        fill_or_kill: bool = True,
        confirmation_timeout_seconds: float | None = None,
        confirmation_poll_seconds: float = POLYMARKET_CONFIRMATION_POLL_SECONDS,
    ) -> dict[str, Any]:
        if not fill_or_kill:
            raise RuntimeError("Polymarket live trading requires immediate order behavior")
        client, types = self._client_and_types()
        if self.signature_type == 3:
            return self._order_v2(
                client,
                types,
                token_id,
                price_cents,
                size,
                types["Side"].SELL,
                confirmation_timeout_seconds=confirmation_timeout_seconds,
                confirmation_poll_seconds=confirmation_poll_seconds,
            )
        order_args = types["OrderArgs"](
            price=float(Decimal(price_cents) / Decimal("100")),
            size=float(size),
            side=types["SELL"],
            token_id=token_id,
        )
        signed_order = client.create_order(order_args)
        order_type = getattr(types["OrderType"], "FOK", None)
        if order_type is None:
            raise RuntimeError("Installed Polymarket SDK does not expose FOK orders")
        try:
            result = client.post_order(signed_order, order_type)
        except Exception as exc:
            if _looks_like_uncertain_post_error(exc):
                raise RuntimeError(
                    "polymarket_order_state_uncertain: Polymarket order submission "
                    f"returned no reliable status: {exc}"
                ) from exc
            raise
        return self._confirm_fok_result(
            result,
            expected_size=size,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
            confirmation_poll_seconds=confirmation_poll_seconds,
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        order_id = str(order_id or "").strip()
        if not order_id:
            raise RuntimeError("Polymarket get_order requires an order id")
        client, _types = self._client_and_types()
        if not hasattr(client, "get_order"):
            raise RuntimeError("Installed Polymarket SDK does not expose get_order")
        result = client.get_order(order_id)
        if not isinstance(result, dict):
            raise RuntimeError(f"Polymarket get_order did not return an object: {result}")
        return result

    def supports_immediate_orders(self) -> bool:
        if not self._has_trading_credentials():
            return False
        package = "py_clob_client_v2" if self.signature_type == 3 else "py_clob_client"
        return importlib.util.find_spec(package) is not None

    def _has_trading_credentials(self) -> bool:
        if not all(
            [
                self.private_key,
                self.api_key,
                self.api_secret,
                self.api_passphrase,
                self.funder_address,
            ]
        ):
            return False
        return True

    def _client_and_types(self):
        types = self._sdk_types_v2() if self.signature_type == 3 else self._sdk_types()
        if not self._has_trading_credentials():
            raise RuntimeError(
                "Polymarket CLOB credentials and SDK are required for immediate live orders"
            )
        if self._trading_client is None:
            api_creds = types["ApiCreds"](
                api_key=self.api_key,
                api_secret=self.api_secret,
                api_passphrase=self.api_passphrase,
            )
            if self.signature_type == 3:
                self._trading_client = types["ClobClient"](
                    host=self.clob_url,
                    chain_id=types["POLYGON"],
                    key=self.private_key,
                    creds=api_creds,
                    signature_type=types["SignatureTypeV2"].POLY_1271,
                    funder=self.funder_address,
                )
            else:
                self._trading_client = types["ClobClient"](
                    self.clob_url,
                    key=self.private_key,
                    chain_id=types["POLYGON"],
                    creds=api_creds,
                    signature_type=self.signature_type,
                    funder=self.funder_address,
                )
        return self._trading_client, types

    @staticmethod
    def _sdk_types() -> dict[str, Any]:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs, OrderType
            from py_clob_client.constants import BUY, POLYGON
            from py_clob_client.order_builder.constants import SELL
        except ImportError as exc:
            raise RuntimeError("Install py-clob-client to place Polymarket orders") from exc
        return {
            "ClobClient": ClobClient,
            "ApiCreds": ApiCreds,
            "AssetType": AssetType,
            "BalanceAllowanceParams": BalanceAllowanceParams,
            "OrderArgs": OrderArgs,
            "OrderType": OrderType,
            "BUY": BUY,
            "SELL": SELL,
            "POLYGON": POLYGON,
        }

    @staticmethod
    def _sdk_types_v2() -> dict[str, Any]:
        if _windows_python312_v2_blocked():
            raise RuntimeError(
                "Polymarket py-clob-client-v2 crashes in this Windows Python 3.12 runtime; "
                "use the Python 3.11 venv at .venv311 for POLYMARKET_SIGNATURE_TYPE=3"
            )
        try:
            _configure_py_clob_v2_http_client()
            from py_clob_client_v2 import (
                ApiCreds,
                AssetType,
                BalanceAllowanceParams,
                ClobClient,
                OrderArgs,
                OrderPayload,
                OrderType,
                PartialCreateOrderOptions,
                Side as ClobSide,
                SignatureTypeV2,
            )
        except ImportError as exc:
            raise RuntimeError(
                f"Install py-clob-client-v2 to place Polymarket deposit-wallet orders: {exc}"
            ) from exc
        return {
            "ClobClient": ClobClient,
            "ApiCreds": ApiCreds,
            "AssetType": AssetType,
            "BalanceAllowanceParams": BalanceAllowanceParams,
            "OrderArgs": OrderArgs,
            "OrderPayload": OrderPayload,
            "OrderType": OrderType,
            "PartialCreateOrderOptions": PartialCreateOrderOptions,
            "Side": ClobSide,
            "SignatureTypeV2": SignatureTypeV2,
            "POLYGON": 137,
        }

    def _order_v2(
        self,
        client: Any,
        types: dict[str, Any],
        token_id: str,
        price_cents: int,
        size: Decimal,
        side: Any,
        confirmation_timeout_seconds: float | None = None,
        confirmation_poll_seconds: float = POLYMARKET_CONFIRMATION_POLL_SECONDS,
    ) -> dict[str, Any]:
        order_type = getattr(types["OrderType"], "FOK", None)
        if order_type is None:
            raise RuntimeError("Installed Polymarket v2 SDK does not expose FOK orders")
        order_args = types["OrderArgs"](
            token_id=token_id,
            price=float(Decimal(price_cents) / Decimal("100")),
            size=float(size),
            side=side,
        )
        options = types["PartialCreateOrderOptions"](tick_size="0.01", neg_risk=False)
        try:
            try:
                result = client.create_and_post_order(
                    order_args=order_args,
                    options=options,
                    order_type=order_type,
                )
            except TypeError:
                result = client.create_and_post_order(order_args, options, order_type)
        except Exception as exc:
            if _looks_like_uncertain_post_error(exc):
                raise RuntimeError(
                    "polymarket_order_state_uncertain: Polymarket order submission "
                    f"returned no reliable status: {exc}"
                ) from exc
            raise
        return self._confirm_fok_result(
            result,
            expected_size=size,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
            confirmation_poll_seconds=confirmation_poll_seconds,
        )

    def _confirm_fok_result(
        self,
        result: Any,
        expected_size: Decimal,
        confirmation_timeout_seconds: float | None = None,
        confirmation_poll_seconds: float = POLYMARKET_CONFIRMATION_POLL_SECONDS,
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise RuntimeError(
                "polymarket_order_state_uncertain: Polymarket order did not "
                f"return a status object: {result}"
            )
        if result.get("success") is False:
            raise RuntimeError(f"Polymarket order was not successful: {result}")
        if _polymarket_fill_confirmed(result, expected_size):
            return result

        status = _polymarket_order_status(result)
        order_id = _polymarket_order_id(result)
        if status == "delayed":
            if not order_id:
                raise RuntimeError(
                    "polymarket_order_state_uncertain: Polymarket delayed order "
                    f"did not include an order id: {result}"
                )
            return self._await_confirmed_fok_order(
                order_id,
                initial_result=result,
                expected_size=expected_size,
                confirmation_timeout_seconds=confirmation_timeout_seconds,
                confirmation_poll_seconds=confirmation_poll_seconds,
            )
        if status in POLYMARKET_TERMINAL_EMPTY_STATUSES:
            raise RuntimeError(
                f"Polymarket FOK order was not filled status={status}: {result}"
            )
        raise RuntimeError(
            "polymarket_order_state_uncertain: Polymarket FOK order was not "
            f"confirmed filled status={status or 'missing'} order_id={order_id or 'missing'}: {result}"
        )

    def _await_confirmed_fok_order(
        self,
        order_id: str,
        initial_result: dict[str, Any],
        expected_size: Decimal,
        confirmation_timeout_seconds: float | None = None,
        confirmation_poll_seconds: float = POLYMARKET_CONFIRMATION_POLL_SECONDS,
    ) -> dict[str, Any]:
        timeout = (
            POLYMARKET_CONFIRMATION_TIMEOUT_SECONDS
            if confirmation_timeout_seconds is None
            else max(float(confirmation_timeout_seconds), 0)
        )
        poll_seconds = max(float(confirmation_poll_seconds), 0.05)
        deadline = time.monotonic() + timeout
        last_result = initial_result
        while time.monotonic() <= deadline:
            try:
                current = self.get_order(order_id)
            except Exception as exc:
                raise RuntimeError(
                    "polymarket_order_state_uncertain: could not confirm delayed "
                    f"Polymarket order {order_id}: {exc}"
                ) from exc
            if _polymarket_fill_confirmed(current, expected_size):
                return current
            status = _polymarket_order_status(current)
            if status in POLYMARKET_TERMINAL_EMPTY_STATUSES:
                raise RuntimeError(
                    f"Polymarket delayed FOK order {order_id} was not filled "
                    f"status={status}: {current}"
                )
            last_result = current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_seconds, remaining))
        raise RuntimeError(
            "polymarket_order_state_uncertain: delayed Polymarket FOK order "
            f"{order_id} was not confirmed filled within {timeout:g}s: {last_result}"
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        order_id = str(order_id or "").strip()
        if not order_id:
            raise RuntimeError("Polymarket cancel requires an order id")
        client, types = self._client_and_types()
        if "OrderPayload" in types and hasattr(client, "cancel_order"):
            return client.cancel_order(types["OrderPayload"](orderID=order_id))
        if hasattr(client, "cancel"):
            return client.cancel(order_id)
        if hasattr(client, "cancel_orders"):
            return client.cancel_orders([order_id])
        raise RuntimeError("Installed Polymarket SDK does not expose order cancellation")

    def _token_book(self, token_id: str) -> dict[str, Any]:
        try:
            return self.http.get_json(f"{self.clob_url}/book", params={"token_id": token_id})
        except RuntimeError:
            return self.http.get_json(f"{self.clob_url}/orderbook", params={"token_id": token_id})

    def _gamma_market(self, market_id: str) -> dict[str, Any]:
        condition_queries = (
            (
                (f"{self.gamma_url}/markets", {"condition_id": market_id}),
                (f"{self.gamma_url}/markets", {"conditionId": market_id}),
            )
            if _looks_like_condition_id(market_id)
            else ()
        )
        for url, params in (
            (f"{self.gamma_url}/markets", {"id": market_id}),
            *condition_queries,
            (f"{self.gamma_url}/markets", {"slug": market_id}),
            (f"{self.gamma_url}/markets", {"market_slug": market_id}),
            (f"{self.gamma_url}/events", {"slug": market_id}),
            (f"{self.gamma_url}/events", {"market_slug": market_id}),
            (f"{self.gamma_url}/events/slug/{market_id}", None),
        ):
            try:
                data = self.http.get_json(url, params=params)
            except RuntimeError:
                continue
            market = _first_gamma_market(data, market_id)
            if market is not None:
                return market
        raise RuntimeError(f"Polymarket Gamma market not found for id or slug {market_id}")

    @staticmethod
    def _poly_asks(levels: list[dict[str, Any]]) -> list[BookLevel]:
        asks: list[BookLevel] = []
        for level in levels:
            price = int((Decimal(str(level["price"])) * Decimal("100")).to_integral_value())
            size = Decimal(str(level["size"]))
            asks.append(BookLevel(price_cents=price, size=size))
        return asks

    @staticmethod
    def _poly_bids(levels: list[dict[str, Any]]) -> list[BookLevel]:
        bids: list[BookLevel] = []
        for level in levels:
            price = int((Decimal(str(level["price"])) * Decimal("100")).to_integral_value())
            size = Decimal(str(level["size"]))
            bids.append(BookLevel(price_cents=price, size=size))
        return bids


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _configure_py_clob_v2_http_client() -> None:
    if os.name != "nt":
        return
    try:
        import httpx
        from py_clob_client_v2.http_helpers import helpers
    except ImportError:
        return
    if getattr(helpers, "_firstbot_windows_truststore", False):
        return
    try:
        helpers._http_client.close()
    except Exception:
        pass
    helpers._http_client = httpx.Client(
        http2=True,
        timeout=30,
        verify=windows_truststore_context() or True,
    )
    helpers._firstbot_windows_truststore = True


def _windows_python312_v2_blocked() -> bool:
    return os.name == "nt" and sys.version_info >= (3, 12)


def _first_gamma_market(data: Any, market_id: str) -> dict[str, Any] | None:
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if _looks_like_market(data, market_id) and _market_token_ids(data):
            return data
        container_matches = _container_matches_market_id(data, market_id)
        items = []
        for key in ("value", "data", "results", "markets"):
            value = data.get(key)
            if isinstance(value, list):
                items.extend(value)
            elif isinstance(value, dict):
                if _looks_like_market(value, market_id) and _market_token_ids(value):
                    return value
                items.extend(_nested_markets(value))
        event_markets = data.get("markets")
        if isinstance(event_markets, list):
            items.extend(event_markets)
    else:
        return None
    for item in items:
        if isinstance(item, dict) and _looks_like_market(item, market_id) and _market_token_ids(item):
            return item
    if isinstance(data, dict) and container_matches:
        child_markets = [item for item in items if isinstance(item, dict) and _market_token_ids(item)]
        if len(child_markets) == 1:
            return child_markets[0]
    return None


def _looks_like_market(item: dict[str, Any], market_id: str) -> bool:
    needle = str(market_id or "").strip()
    if not needle:
        return False
    if needle in _market_token_ids(item):
        return True
    identifiers = {
        str(item.get("id") or ""),
        str(item.get("slug") or ""),
        str(item.get("market_slug") or ""),
        str(item.get("marketSlug") or ""),
        str(item.get("condition_id") or ""),
        str(item.get("conditionId") or ""),
        str(item.get("question_id") or ""),
        str(item.get("questionId") or ""),
    }
    return needle in {identifier.strip() for identifier in identifiers if identifier}


def _looks_like_condition_id(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("0x") and len(text) >= 10


def _container_matches_market_id(item: dict[str, Any], market_id: str) -> bool:
    needle = str(market_id or "").strip()
    if not needle:
        return False
    identifiers = {
        str(item.get("id") or ""),
        str(item.get("slug") or ""),
        str(item.get("event_slug") or ""),
        str(item.get("eventSlug") or ""),
        str(item.get("market_slug") or ""),
        str(item.get("marketSlug") or ""),
    }
    return needle in {identifier.strip() for identifier in identifiers if identifier}


def _market_token_ids(item: dict[str, Any]) -> set[str]:
    tokens = {str(token).strip() for token in _json_list(item.get("clobTokenIds")) if str(token).strip()}
    tokens.update(
        str(token).strip()
        for token in _json_list(item.get("clob_token_ids"))
        if str(token).strip()
    )
    for token in _json_list(item.get("tokens")):
        if isinstance(token, dict):
            for key in ("token_id", "tokenId", "clobTokenId", "id"):
                value = str(token.get(key) or "").strip()
                if value:
                    tokens.add(value)
        else:
            value = str(token).strip()
            if value:
                tokens.add(value)
    return tokens


def _nested_markets(data: dict[str, Any]) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    for key in ("markets", "value", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            markets.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            markets.extend(_nested_markets(value))
    return markets


def _usdc_amount_from_balance_allowance(raw: dict[str, Any], key: str) -> Decimal | None:
    if not isinstance(raw, dict) or raw.get(key) is None:
        return None
    amount = Decimal(str(raw[key]))
    if amount > Decimal("10000"):
        return amount / Decimal("1000000")
    return amount


def _has_balance_or_allowance(raw: Any) -> bool:
    return isinstance(raw, dict) and (raw.get("balance") is not None or raw.get("allowance") is not None)


def _polymarket_order_status(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("status", "state", "order_status", "orderStatus"):
        value = raw.get(key)
        if value is not None:
            return str(value).strip().lower()
    return ""


def _polymarket_order_id(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("orderID", "orderId", "order_id", "id"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return None


def _polymarket_fill_confirmed(raw: Any, expected_size: Decimal) -> bool:
    status = _polymarket_order_status(raw)
    if status not in POLYMARKET_CONFIRMED_STATUSES:
        return False
    filled_size = _polymarket_filled_size(raw)
    if filled_size is None:
        return True
    return filled_size >= Decimal(expected_size)


def _polymarket_filled_size(raw: Any) -> Decimal | None:
    if not isinstance(raw, dict):
        return None
    for key in (
        "filled_size",
        "filledSize",
        "size_matched",
        "sizeMatched",
        "matched_size",
        "matchedSize",
        "filled",
    ):
        value = _decimal_field(raw, key)
        if value is not None:
            return value
    return None


def _decimal_field(raw: dict[str, Any], key: str) -> Decimal | None:
    if not isinstance(raw, dict) or raw.get(key) in (None, ""):
        return None
    try:
        return Decimal(str(raw[key]))
    except Exception:
        return None


def _looks_like_uncertain_post_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "cloudflare",
            "timeout",
            "timed out",
            "connection aborted",
            "connection reset",
            "remote end closed",
            "502",
            "503",
            "504",
        )
    )
