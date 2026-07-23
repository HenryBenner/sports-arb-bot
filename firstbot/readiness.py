from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .exchanges import KalshiClient, PolymarketClient
from .models import Exchange, OrderBook, Side
from .predictionhunt import PredictionHuntLeg, PredictionHuntOpportunity
from .ssl_compat import websocket_ssl_context


@dataclass(frozen=True)
class ReadinessRecord:
    timestamp: str
    venue: str
    check: str
    status: str
    duration_ms: int
    message: str


class LiveReadinessChecker:
    def __init__(
        self,
        settings: Settings,
        kalshi: KalshiClient,
        polymarket: PolymarketClient,
        log_dir: str | Path = "logs",
        seconds: int | None = None,
        kalshi_ticker: str | None = None,
        polymarket_token: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.log_dir = Path(log_dir)
        self.seconds = seconds if seconds is not None else settings.readiness_seconds
        self.kalshi_ticker = kalshi_ticker or settings.readiness_kalshi_ticker
        self.polymarket_token = polymarket_token or settings.readiness_polymarket_token
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.records: list[ReadinessRecord] = []

    def run(self, print_status: bool = True) -> list[ReadinessRecord]:
        if self.seconds < 1:
            raise RuntimeError("--readiness-seconds must be at least 1")
        self.records = []
        failures: list[str] = []
        kalshi_ticker: str | None = None
        polymarket_token: str | None = None
        restore_http = self._shorten_http_timeouts()

        try:
            self._recorded("kalshi", "Kalshi signing dependency", check_kalshi_signing_dependencies, failures, print_status)
            self._recorded("both", "WebSocket dependency", check_websockets_dependency, failures, print_status)
            if self.settings.hot_geoblock_check:
                self._recorded(
                    "polymarket",
                    "Polymarket geographic eligibility",
                    self._check_polymarket_geoblock,
                    failures,
                    print_status,
                )
            self._recorded("polymarket", "Polymarket SDK", self._check_polymarket_sdk, failures, print_status)
            self._recorded("kalshi", "Kalshi signing", self._check_kalshi_signing, failures, print_status)
            self._recorded("kalshi", "Kalshi balance", self._check_kalshi_balance, failures, print_status)
            kalshi_ticker = self._recorded(
                "kalshi",
                "Kalshi REST orderbook",
                self._check_kalshi_rest_orderbook,
                failures,
                print_status,
            )
            self._recorded(
                "polymarket",
                "Polymarket balance/allowance",
                self._check_polymarket_balance,
                failures,
                print_status,
            )
            polymarket_token = self._recorded(
                "polymarket",
                "Polymarket REST book",
                self._check_polymarket_rest_book,
                failures,
                print_status,
            )
            if kalshi_ticker:
                self._recorded(
                    "kalshi",
                    "Kalshi WebSocket",
                    lambda: asyncio.run(self._check_kalshi_websocket(str(kalshi_ticker))),
                    failures,
                    print_status,
                )
            if polymarket_token:
                self._recorded(
                    "polymarket",
                    "Polymarket WebSocket",
                    lambda: asyncio.run(self._check_polymarket_websocket(str(polymarket_token))),
                    failures,
                    print_status,
                )
        finally:
            restore_http()

        if failures:
            raise RuntimeError("live readiness failed: " + "; ".join(failures))
        return self.records

    def _shorten_http_timeouts(self) -> Callable[[], None]:
        target_timeout = max(5, min(self.settings.http_timeout_seconds, self.seconds + 2))
        saved: list[tuple[Any, str, Any]] = []
        for client in (self.kalshi, self.polymarket):
            http = getattr(client, "http", None)
            if http is None:
                continue
            for attr, value in (("timeout", target_timeout), ("retries", 0)):
                if not hasattr(http, attr):
                    continue
                saved.append((http, attr, getattr(http, attr)))
                setattr(http, attr, value)

        def restore() -> None:
            for obj, attr, value in saved:
                setattr(obj, attr, value)

        return restore

    def _recorded(
        self,
        venue: str,
        check: str,
        action: Callable[[], Any],
        failures: list[str],
        print_status: bool,
    ) -> Any:
        started = time.monotonic()
        status = "ok"
        message = "OK"
        result: Any = None
        if print_status:
            print(f"{check} checking...", flush=True)
        try:
            result = action()
            if result is not None:
                message = str(result)
        except Exception as exc:
            status = "failed"
            message = str(exc)
            failures.append(f"{check}: {message}")
        duration_ms = int((time.monotonic() - started) * 1000)
        record = ReadinessRecord(
            timestamp=self.clock().isoformat(),
            venue=venue,
            check=check,
            status=status,
            duration_ms=duration_ms,
            message=message,
        )
        self.records.append(record)
        self._write(record)
        if print_status:
            label = f"{check} {'OK' if status == 'ok' else 'FAILED'}"
            print(label if status == "ok" else f"{label}: {message}", flush=True)
        if status != "ok":
            return None
        return result

    def _write(self, record: ReadinessRecord) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": record.timestamp,
            "venue": record.venue,
            "check": record.check,
            "status": record.status,
            "duration_ms": record.duration_ms,
            "message": record.message,
        }
        with (self.log_dir / "live_readiness.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _check_polymarket_sdk(self) -> str:
        package = "py_clob_client_v2" if self.settings.polymarket_signature_type == 3 else "py_clob_client"
        if importlib.util.find_spec(package) is None:
            raise RuntimeError(f"polymarket_sdk_missing: {package}")
        self.polymarket._client_and_types()
        return "OK"

    def _check_polymarket_geoblock(self) -> str:
        http = getattr(self.polymarket, "http", None)
        if http is None or not hasattr(http, "get_json"):
            raise RuntimeError("polymarket_geoblock_check_unavailable")
        data = http.get_json("https://polymarket.com/api/geoblock")
        if not isinstance(data, dict) or "blocked" not in data:
            raise RuntimeError(f"polymarket_geoblock_response_invalid: {data}")
        blocked = data.get("blocked")
        if isinstance(blocked, str):
            blocked = blocked.strip().lower() in {"1", "true", "yes"}
        if bool(blocked):
            location = ", ".join(
                str(data.get(key)).strip()
                for key in ("country", "region", "city")
                if data.get(key)
            )
            suffix = f" ({location})" if location else ""
            raise RuntimeError(f"polymarket_geoblocked: trading restricted in your region{suffix}")
        return "not blocked"

    def _check_kalshi_signing(self) -> str:
        if not self.settings.kalshi_api_key_id:
            raise RuntimeError("kalshi_api_key_missing")
        key_path = self.settings.kalshi_private_key_path
        if not key_path:
            raise RuntimeError("kalshi_private_key_path_missing")
        if not Path(key_path).is_file():
            raise RuntimeError(f"kalshi_private_key_file_missing: {key_path}")
        self.kalshi._auth_headers("GET", "/trade-api/ws/v2")
        return "OK"

    def _check_kalshi_balance(self) -> str:
        cash = self.kalshi.available_cash_usd()
        return f"${cash:.2f}"

    def _check_polymarket_balance(self) -> str:
        cash = self.polymarket.available_cash_usd()
        return f"${cash:.2f}"

    def _check_kalshi_rest_orderbook(self) -> str:
        ticker = self.kalshi_ticker or self._auto_kalshi_ticker()
        book = self.kalshi.get_orderbook(ticker)
        _require_book_depth(book, ticker, "kalshi")
        self.kalshi_ticker = ticker
        return ticker

    def _check_polymarket_rest_book(self) -> str:
        if not self.polymarket_token:
            token = self._auto_polymarket_token()
            self.polymarket_token = token
            return token
        token = self.polymarket_token
        levels = self.polymarket.get_token_ask_levels(token)
        if not levels:
            raise RuntimeError(f"polymarket_orderbook_empty: {token}")
        self.polymarket_token = token
        return token

    async def _check_kalshi_websocket(self, ticker: str) -> str:
        check_websockets_dependency()
        from .websockets import _kalshi_ws_url

        import websockets

        headers = self.kalshi._auth_headers("GET", "/trade-api/ws/v2")
        url = _kalshi_ws_url(self.kalshi.base_url)
        ssl_context = websocket_ssl_context(url)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                async with websockets.connect(
                    url,
                    additional_headers=headers,
                    ssl=ssl_context,
                    open_timeout=max(10, self.seconds),
                ) as websocket:
                    await websocket.send(
                        json.dumps(
                            {
                                "id": 1,
                                "cmd": "subscribe",
                                "params": {
                                    "channels": ["orderbook_delta"],
                                    "market_tickers": [ticker],
                                },
                            }
                        )
                    )
                    await _wait_for_optional_message(websocket, self.seconds)
                return ticker
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(attempt)
        raise RuntimeError(f"{last_error} after 3 attempts")

    async def _check_polymarket_websocket(self, token: str) -> str:
        check_websockets_dependency()
        from .websockets import _polymarket_ws_url

        import websockets

        url = _polymarket_ws_url(self.polymarket.clob_url)
        ssl_context = websocket_ssl_context(url)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                async with websockets.connect(
                    url,
                    ssl=ssl_context,
                    open_timeout=max(10, self.seconds),
                ) as websocket:
                    await websocket.send(json.dumps({"assets_ids": [token], "type": "market"}))
                    await _wait_for_optional_message(websocket, self.seconds)
                return token
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(attempt)
        raise RuntimeError(f"{last_error} after 3 attempts")

    def _auto_kalshi_ticker(self) -> str:
        data = self.kalshi.get_markets(status="open", limit=50)
        last_error: Exception | None = None
        for market in _items(data):
            ticker = _first_string(market, "ticker", "market_ticker", "id")
            if not ticker:
                continue
            try:
                book = self.kalshi.get_orderbook(ticker)
                _require_book_depth(book, ticker, "kalshi")
                return ticker
            except Exception as exc:
                last_error = exc
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"could_not_auto_select_kalshi_readiness_ticker{detail}")

    def _auto_polymarket_token(self) -> str:
        sources = (
            self._polymarket_sampling_markets,
            lambda: self.polymarket.get_events(active="true", closed="false", limit=5),
            lambda: self.polymarket.http.get_json(
                f"{self.polymarket.gamma_url}/markets",
                params={"active": "true", "closed": "false", "limit": 10},
            ),
        )
        last_error: Exception | None = None
        for source in sources:
            try:
                data = source()
            except Exception as exc:
                last_error = exc
                continue
            token = self._first_live_polymarket_token(data)
            if token:
                return token
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"could_not_auto_select_polymarket_readiness_token{detail}")

    def _polymarket_sampling_markets(self) -> Any:
        client, _ = self.polymarket._client_and_types()
        if hasattr(client, "get_sampling_simplified_markets"):
            return client.get_sampling_simplified_markets()
        if hasattr(client, "get_sampling_markets"):
            return client.get_sampling_markets()
        raise RuntimeError("polymarket_sampling_markets_unavailable")

    def _first_live_polymarket_token(self, data: Any) -> str | None:
        checked = 0
        last_error: Exception | None = None
        for item in _items(data):
            for market in _market_candidates(item):
                for token in _clob_tokens(market):
                    checked += 1
                    if checked > 50:
                        return None
                    try:
                        if self.polymarket.get_token_ask_levels(token):
                            return token
                    except Exception as exc:
                        last_error = exc
                        continue
        if last_error is not None:
            raise RuntimeError(f"no_live_polymarket_token_with_book: {last_error}")
        return None


def check_kalshi_signing_dependencies(
    import_module: Callable[[str], Any] = importlib.import_module,
) -> str:
    try:
        import_module("_cffi_backend")
        import_module("cryptography.hazmat.primitives.hashes")
        import_module("cryptography.hazmat.primitives.serialization")
        import_module("cryptography.hazmat.primitives.asymmetric.padding")
    except ImportError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        raise RuntimeError(f"kalshi_signing_dependency_missing: {missing}") from exc
    return "OK"


def check_websockets_dependency(
    import_module: Callable[[str], Any] = importlib.import_module,
) -> str:
    try:
        import_module("websockets")
    except ImportError as exc:
        missing = getattr(exc, "name", None) or str(exc)
        raise RuntimeError(f"websocket_dependency_missing: {missing}") from exc
    return "OK"


def preflight_hot_candidate(
    kalshi: KalshiClient,
    polymarket: PolymarketClient,
    opportunity: PredictionHuntOpportunity,
) -> str | None:
    seen: set[tuple[Exchange, str, Side]] = set()
    for leg in opportunity.legs:
        key = (leg.platform, leg.market_id, leg.side)
        if key in seen:
            continue
        seen.add(key)
        if leg.platform is Exchange.KALSHI:
            try:
                book = kalshi.get_orderbook(leg.market_id)
            except Exception as exc:
                return f"kalshi_orderbook_unavailable {leg.market_id}: {exc}"
            levels = book.yes_asks if leg.side is Side.YES else book.no_asks
            if not levels:
                return f"kalshi_orderbook_empty {leg.market_id} {leg.side.value}"
        elif leg.platform is Exchange.POLYMARKET:
            try:
                levels = polymarket.get_token_ask_levels(leg.market_id)
            except Exception as exc:
                return f"polymarket_orderbook_unavailable {leg.market_id}: {exc}"
            if not levels:
                return f"polymarket_orderbook_empty {leg.market_id}"
    return None


async def _wait_for_optional_message(websocket: Any, seconds: int) -> None:
    try:
        raw = await asyncio.wait_for(websocket.recv(), timeout=max(0.1, seconds))
    except asyncio.TimeoutError:
        return
    try:
        message = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return
    if isinstance(message, dict):
        error = message.get("error") or message.get("msg", {}).get("error")
        if error:
            raise RuntimeError(f"websocket_subscription_error: {error}")


def _require_book_depth(book: OrderBook, market_id: str, venue: str) -> None:
    if not book.yes_asks and not book.no_asks:
        raise RuntimeError(f"{venue}_orderbook_empty: {market_id}")


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("markets", "events", "data", "results", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [data]


def _market_candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    markets = item.get("markets")
    if isinstance(markets, list):
        candidates = [market for market in markets if isinstance(market, dict)]
        if candidates:
            return candidates
    return [item]


def _clob_tokens(market: dict[str, Any]) -> list[str]:
    raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    tokens = _json_list(raw)
    if not tokens and isinstance(market.get("tokens"), list):
        for item in market["tokens"]:
            if isinstance(item, dict):
                value = item.get("token_id") or item.get("tokenId") or item.get("id")
                if value:
                    tokens.append(value)
    return [str(token).strip() for token in tokens if str(token).strip()]


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


def _first_string(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return None
