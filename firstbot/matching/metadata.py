from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..predictionhunt import PredictionHuntLeg
from .utils import first_value, json_list, polymarket_slug_from_url


@dataclass(frozen=True)
class KalshiEventBundle:
    event_ticker: str | None
    raw_event: dict[str, Any]
    markets: tuple[dict[str, Any], ...]
    source_market: dict[str, Any]


@dataclass(frozen=True)
class PolymarketEventBundle:
    event_id: str | None
    event_slug: str | None
    raw_event: dict[str, Any]
    markets: tuple[dict[str, Any], ...]
    source_market: dict[str, Any]


class KalshiMetadataResolver:
    def __init__(self, kalshi: Any) -> None:
        self.kalshi = kalshi
        self._by_ticker: dict[str, KalshiEventBundle] = {}
        self._event_cache: dict[str, dict[str, Any]] = {}

    def resolve(self, ticker: str) -> KalshiEventBundle:
        if ticker in self._by_ticker:
            return self._by_ticker[ticker]
        source_market = dict(self.kalshi.get_market(ticker))
        event_ticker = str(
            first_value(source_market, "event_ticker", "eventTicker", "event_id", "eventId") or ""
        ).strip() or _event_ticker_from_market_ticker(ticker)
        raw_event: dict[str, Any] = {}
        markets: list[dict[str, Any]] = [source_market]
        if event_ticker and hasattr(self.kalshi, "get_event"):
            raw_event = self._event_cache.get(event_ticker) or self.kalshi.get_event(
                event_ticker,
                with_nested_markets=True,
            )
            self._event_cache[event_ticker] = raw_event
            markets = _kalshi_event_markets(raw_event) or markets
        bundle = KalshiEventBundle(
            event_ticker=event_ticker,
            raw_event=raw_event,
            markets=tuple(markets),
            source_market=source_market,
        )
        self._by_ticker[ticker] = bundle
        return bundle


class PolymarketMetadataResolver:
    def __init__(self, polymarket: Any) -> None:
        self.polymarket = polymarket
        self._by_key: dict[str, PolymarketEventBundle] = {}

    def resolve(self, leg: PredictionHuntLeg) -> PolymarketEventBundle:
        slug = polymarket_slug_from_url(leg.source_url)
        key = slug or leg.market_id
        if key in self._by_key:
            return self._by_key[key]
        token_market = self._market_by_clob_token(leg.market_id)
        raw_event: dict[str, Any] = {}
        markets: list[dict[str, Any]] = []
        if slug:
            raw_event, markets = self._fetch_event_markets(slug)
        if not markets and hasattr(self.polymarket, "_gamma_market"):
            market = self.polymarket._gamma_market(slug or leg.market_id)
            markets = [market]
            raw_event = market
        source_market = _market_containing_token(markets, leg.market_id) or _market_matching_id(markets, leg.market_id)
        if source_market is None and token_market is not None:
            markets = _unique_markets([*markets, token_market])
            source_market = token_market
        if source_market is None:
            raise RuntimeError(f"polymarket_token_not_in_source_event: {leg.market_id}")
        event_id = str(first_value(raw_event, "id", "event_id", "eventId") or "").strip() or None
        event_slug = str(first_value(raw_event, "slug", "event_slug", "eventSlug") or slug or "").strip() or None
        bundle = PolymarketEventBundle(
            event_id=event_id,
            event_slug=event_slug,
            raw_event=raw_event,
            markets=tuple(markets),
            source_market=source_market,
        )
        self._by_key[key] = bundle
        return bundle

    def _fetch_event_markets(self, slug: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not hasattr(self.polymarket, "gamma_url") or not hasattr(self.polymarket, "http"):
            return {}, []
        for url, params in (
            (f"{self.polymarket.gamma_url}/events", {"slug": slug}),
            (f"{self.polymarket.gamma_url}/events", {"market_slug": slug}),
            (f"{self.polymarket.gamma_url}/events/slug/{slug}", None),
            (f"{self.polymarket.gamma_url}/markets", {"slug": slug}),
            (f"{self.polymarket.gamma_url}/markets", {"market_slug": slug}),
        ):
            try:
                data = self.polymarket.http.get_json(url, params=params)
            except Exception:
                continue
            event = _first_event(data) or (data if isinstance(data, dict) else {})
            markets = _gamma_markets(data)
            if markets:
                return event, markets
        return {}, []

    def _market_by_clob_token(self, token_id: str) -> dict[str, Any] | None:
        if not _looks_like_clob_token_id(token_id):
            return None
        token_record = self._clob_market_by_token(token_id)
        if not token_record:
            return None
        condition_id = _condition_id(token_record)
        if condition_id:
            market = self._gamma_market_by_condition_id(condition_id)
            if market is not None:
                return market
        return _market_from_token_record(token_id, token_record)

    def _clob_market_by_token(self, token_id: str) -> dict[str, Any] | None:
        if not hasattr(self.polymarket, "clob_url") or not hasattr(self.polymarket, "http"):
            return None
        try:
            data = self.polymarket.http.get_json(f"{self.polymarket.clob_url}/markets-by-token/{token_id}")
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _gamma_market_by_condition_id(self, condition_id: str) -> dict[str, Any] | None:
        if not hasattr(self.polymarket, "gamma_url") or not hasattr(self.polymarket, "http"):
            return None
        for params in (
            {"condition_id": condition_id},
            {"conditionId": condition_id},
            {"condition_ids": condition_id},
            {"conditionIds": condition_id},
        ):
            try:
                data = self.polymarket.http.get_json(f"{self.polymarket.gamma_url}/markets", params=params)
            except Exception:
                continue
            market = _market_matching_id(_gamma_markets(data), condition_id)
            if market is not None:
                return market
        return None


def _event_ticker_from_market_ticker(ticker: str) -> str | None:
    parts = str(ticker or "").split("-")
    if len(parts) < 2:
        return None
    return "-".join(parts[:-1])


def _kalshi_event_markets(raw_event: dict[str, Any]) -> list[dict[str, Any]]:
    for container in (raw_event, raw_event.get("event") if isinstance(raw_event, dict) else None):
        if not isinstance(container, dict):
            continue
        markets = container.get("markets")
        if isinstance(markets, list):
            return [item for item in markets if isinstance(item, dict)]
    return []


def _first_event(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        if isinstance(data.get("event"), dict):
            return data["event"]
        for key in ("events", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return next((item for item in value if isinstance(item, dict)), None)
    if isinstance(data, list):
        return next((item for item in data if isinstance(item, dict)), None)
    return None


def _gamma_markets(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    markets: list[dict[str, Any]] = []
    if _token_ids(data):
        markets.append(data)
    for key in ("markets", "value", "data", "results"):
        value = data.get(key)
        if isinstance(value, list):
            markets.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            markets.extend(_gamma_markets(value))
    return _unique_markets(markets)


def _unique_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for market in markets:
        key = str(first_value(market, "id", "slug", "market_slug", "marketSlug") or id(market))
        if key in seen:
            continue
        seen.add(key)
        unique.append(market)
    return unique


def _market_containing_token(markets: list[dict[str, Any]], token_id: str) -> dict[str, Any] | None:
    return next((market for market in markets if token_id in _token_ids(market)), None)


def _market_matching_id(markets: list[dict[str, Any]], market_id: str) -> dict[str, Any] | None:
    needle = str(market_id or "").strip()
    identifiers = (
        "id",
        "slug",
        "market_slug",
        "marketSlug",
        "condition_id",
        "conditionId",
        "question_id",
        "questionId",
    )
    return next(
        (
            market
            for market in markets
            if needle and needle in {str(market.get(key) or "").strip() for key in identifiers}
        ),
        None,
    )


def _token_ids(market: dict[str, Any]) -> tuple[str, ...]:
    tokens = [str(token).strip() for token in json_list(market.get("clobTokenIds")) if str(token).strip()]
    tokens.extend(
        str(token).strip()
        for token in json_list(market.get("clob_token_ids"))
        if str(token).strip()
    )
    for token in json_list(market.get("tokens")):
        if isinstance(token, dict):
            for key in ("token_id", "tokenId", "clobTokenId", "id"):
                value = str(token.get(key) or "").strip()
                if value:
                    tokens.append(value)
        else:
            value = str(token).strip()
            if value:
                tokens.append(value)
    return tuple(dict.fromkeys(tokens))


def _looks_like_clob_token_id(value: str) -> bool:
    text = str(value or "").strip()
    return text.isdigit() and len(text) >= 20


def _condition_id(data: dict[str, Any]) -> str | None:
    value = first_value(data, "condition_id", "conditionId", "market", "market_id", "marketId")
    text = str(value or "").strip()
    return text or None


def _market_from_token_record(token_id: str, data: dict[str, Any]) -> dict[str, Any]:
    primary = str(first_value(data, "primary_token_id", "primaryTokenId") or "").strip()
    secondary = str(first_value(data, "secondary_token_id", "secondaryTokenId") or "").strip()
    token_ids = [item for item in (primary, secondary) if item]
    if not token_ids:
        token_ids = [token_id]
    condition_id = _condition_id(data)
    return {
        "id": condition_id or token_id,
        "condition_id": condition_id,
        "question": first_value(data, "question", "title", "description") or "",
        "outcomes": first_value(data, "outcomes", "shortOutcomes"),
        "clobTokenIds": token_ids,
        "tokens": first_value(data, "tokens"),
    }
