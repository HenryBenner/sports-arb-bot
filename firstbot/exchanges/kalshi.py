from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..http import HttpClient
from ..models import BookLevel, Exchange, FeeSchedule, OrderBook, Side


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        api_key_id: str | None = None,
        private_key_path: str | None = None,
        private_key: str | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.private_key_path = private_key_path
        self.private_key = private_key
        self.http = http or HttpClient()
        self._taker_fee_schedules: dict[str, tuple[float, FeeSchedule]] = {}

    def get_markets(self, **params: Any) -> dict[str, Any]:
        return self.http.get_json(f"{self.base_url}/markets", params=params)

    def get_market(self, ticker: str) -> dict[str, Any]:
        raw = self.http.get_json(f"{self.base_url}/markets/{ticker}")
        if isinstance(raw, dict) and isinstance(raw.get("market"), dict):
            return raw["market"]
        return raw

    def get_event(self, event_ticker: str, with_nested_markets: bool = True) -> dict[str, Any]:
        return self.http.get_json(
            f"{self.base_url}/events/{event_ticker}",
            params={"with_nested_markets": str(bool(with_nested_markets)).lower()},
        )

    def get_series(self, series_ticker: str) -> dict[str, Any]:
        raw = self.http.get_json(f"{self.base_url}/series/{series_ticker}")
        if isinstance(raw, dict) and isinstance(raw.get("series"), dict):
            return raw["series"]
        if not isinstance(raw, dict):
            raise RuntimeError(f"Kalshi series lookup did not return an object: {series_ticker}")
        return raw

    def get_taker_fee_schedule(self, ticker: str) -> FeeSchedule:
        cached = self._taker_fee_schedules.get(ticker)
        now_monotonic = time.monotonic()
        if cached is not None and cached[0] > now_monotonic:
            return cached[1]

        market = self.get_market(ticker)
        waiver_raw = market.get("fee_waiver_expiration_time")
        waiver_expiration = _parse_utc_datetime(waiver_raw)
        if waiver_raw not in (None, "") and waiver_expiration is None:
            raise RuntimeError(
                f"Kalshi market {ticker} returned invalid fee_waiver_expiration_time={waiver_raw}"
            )
        now_utc = datetime.now(timezone.utc)
        if waiver_expiration is not None and waiver_expiration > now_utc:
            schedule = FeeSchedule(
                exchange=Exchange.KALSHI,
                fee_type="quadratic",
                rate=Decimal("0"),
                multiplier=Decimal("0"),
                source=f"kalshi market waiver through {waiver_expiration.isoformat()}",
            )
            ttl = max(1.0, min(300.0, (waiver_expiration - now_utc).total_seconds()))
            self._taker_fee_schedules[ticker] = (now_monotonic + ttl, schedule)
            return schedule

        event_ticker = str(market.get("event_ticker") or "").strip()
        if not event_ticker:
            raise RuntimeError(f"Kalshi market {ticker} did not include event_ticker")
        raw_event = self.get_event(event_ticker, with_nested_markets=False)
        event = raw_event.get("event", raw_event) if isinstance(raw_event, dict) else {}
        if not isinstance(event, dict):
            raise RuntimeError(f"Kalshi event lookup did not return an object: {event_ticker}")
        series_ticker = str(event.get("series_ticker") or "").strip()
        if not series_ticker:
            raise RuntimeError(f"Kalshi event {event_ticker} did not include series_ticker")
        series = self.get_series(series_ticker)

        fee_type_override = event.get("fee_type_override")
        fee_type = str(
            fee_type_override
            if fee_type_override not in (None, "")
            else series.get("fee_type", "")
        ).strip().lower()
        multiplier_override = event.get("fee_multiplier_override")
        multiplier_raw = (
            multiplier_override
            if multiplier_override is not None
            else series.get("fee_multiplier")
        )
        if not fee_type:
            raise RuntimeError(f"Kalshi series {series_ticker} did not include fee_type")
        if multiplier_raw is None or str(multiplier_raw).strip() == "":
            raise RuntimeError(f"Kalshi series {series_ticker} did not include fee_multiplier")
        try:
            multiplier = Decimal(str(multiplier_raw))
        except Exception as exc:
            raise RuntimeError(
                f"Kalshi series {series_ticker} returned invalid fee_multiplier={multiplier_raw}"
            ) from exc
        if multiplier < 0:
            raise RuntimeError(
                f"Kalshi series {series_ticker} returned negative fee_multiplier={multiplier_raw}"
            )

        schedule = FeeSchedule(
            exchange=Exchange.KALSHI,
            fee_type=fee_type,
            rate=Decimal("0.07"),
            multiplier=multiplier,
            source=(
                f"kalshi event override {event_ticker}"
                if fee_type_override not in (None, "") or multiplier_override is not None
                else f"kalshi series {series_ticker}"
            ),
        )
        self._taker_fee_schedules[ticker] = (now_monotonic + 300.0, schedule)
        return schedule

    def get_orderbook(self, ticker: str) -> OrderBook:
        raw = self.http.get_json(f"{self.base_url}/markets/{ticker}/orderbook")
        if isinstance(raw.get("orderbook_fp"), dict):
            book_fp = raw["orderbook_fp"]
            yes_bids = self._kalshi_bid_levels(book_fp.get("yes_dollars", []))
            no_bids = self._kalshi_bid_levels(book_fp.get("no_dollars", []))
            return OrderBook(
                exchange=Exchange.KALSHI,
                market_id=ticker,
                yes_asks=self._asks_from_opposing_bids(no_bids),
                no_asks=self._asks_from_opposing_bids(yes_bids),
            )
        book = raw.get("orderbook", raw)
        yes_levels = book.get("yes", [])
        no_levels = book.get("no", [])
        return OrderBook(
            exchange=Exchange.KALSHI,
            market_id=ticker,
            yes_asks=self._kalshi_asks(yes_levels),
            no_asks=self._kalshi_asks(no_levels),
        )

    def get_best_ask(self, ticker: str, side: Side) -> BookLevel | None:
        return self.get_orderbook(ticker).best_ask(side)

    def available_cash_usd(self) -> Decimal:
        path = "/portfolio/balance"
        raw = self.http.get_json(
            f"{self.base_url}{path}",
            headers=self._auth_headers("GET", path),
        )
        return _cash_from_balance_response(raw)

    def supports_immediate_orders(self) -> bool:
        return True

    def create_order(
        self,
        ticker: str,
        side: Side,
        count: int,
        price_cents: int,
        time_in_force: str = "fill_or_kill",
    ) -> dict[str, Any]:
        book_side, yes_price_cents = _v2_order_side_and_price(side, price_cents)
        payload: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": str(uuid.uuid4()),
            "side": book_side,
            "count": f"{Decimal(count):.2f}",
            "price": _fixed_dollar_price(yes_price_cents),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": False,
            "reduce_only": False,
        }
        path = "/portfolio/events/orders"
        return self.http.post_json(
            f"{self.base_url}{path}",
            payload,
            headers=self._auth_headers("POST", path),
        )

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        if not self.api_key_id or not (self.private_key or self.private_key_path):
            raise RuntimeError("Kalshi API key and private key are required for trading")
        timestamp_ms = str(int(time.time() * 1000))
        signing_path = self._signing_path(path)
        message = f"{timestamp_ms}{method}{signing_path}".encode("utf-8")
        signature = self._sign(message)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    def _signing_path(self, path: str) -> str:
        base_path = urlsplit(self.base_url).path.rstrip("/")
        if path.startswith("/trade-api/") or not base_path or path.startswith(base_path):
            return path
        return f"{base_path}{path}"

    def _sign(self, message: bytes) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise RuntimeError("Install cryptography to sign Kalshi requests") from exc

        if self.private_key:
            private_key_bytes = _private_key_bytes_from_env(self.private_key)
        else:
            private_key_bytes = Path(self.private_key_path or "").read_bytes()
        private_key = serialization.load_pem_private_key(private_key_bytes, password=None)
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    @staticmethod
    def _kalshi_asks(levels: list[list[Any]]) -> list[BookLevel]:
        asks: list[BookLevel] = []
        for level in levels:
            if len(level) < 2:
                continue
            price = int(Decimal(str(level[0])))
            size = Decimal(str(level[1]))
            asks.append(BookLevel(price_cents=price, size=size))
        return asks

    @staticmethod
    def _kalshi_bid_levels(levels: list[list[Any]]) -> list[tuple[Decimal, Decimal]]:
        bids: list[tuple[Decimal, Decimal]] = []
        for level in levels:
            if len(level) < 2:
                continue
            price_cents = Decimal(str(level[0])) * Decimal("100")
            size = Decimal(str(level[1]))
            bids.append((price_cents, size))
        return bids

    @staticmethod
    def _asks_from_opposing_bids(levels: list[tuple[Decimal, Decimal]]) -> list[BookLevel]:
        asks: list[BookLevel] = []
        for bid_cents, size in levels:
            ask_cents = int((Decimal("100") - bid_cents).to_integral_value())
            asks.append(BookLevel(price_cents=ask_cents, size=size))
        return sorted(asks, key=lambda level: level.price_cents)


def _cash_from_balance_response(raw: dict[str, Any]) -> Decimal:
    candidates = [
        raw.get("balance"),
        raw.get("cash_balance"),
        raw.get("available_balance"),
        raw.get("available_cash"),
        raw.get("portfolio", {}).get("balance") if isinstance(raw.get("portfolio"), dict) else None,
    ]
    for value in candidates:
        if value is None:
            continue
        amount = Decimal(str(value))
        if amount > Decimal("10000"):
            return amount / Decimal("100")
        return amount
    raise RuntimeError("Kalshi balance response did not include available cash")


def _parse_utc_datetime(value: Any) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _private_key_bytes_from_env(value: str) -> bytes:
    text = str(value).strip().strip('"').strip("'")
    if "\\n" in text and "\n" not in text:
        text = text.replace("\\n", "\n")
    return text.encode("utf-8")


def _v2_order_side_and_price(side: Side, price_cents: int) -> tuple[str, int]:
    if side is Side.YES:
        return "bid", price_cents
    return "ask", 100 - price_cents


def _fixed_dollar_price(price_cents: int) -> str:
    return f"{Decimal(price_cents) / Decimal('100'):.4f}"
