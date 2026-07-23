from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .exchanges import KalshiClient, PolymarketClient
from .models import MarketPair


@dataclass(frozen=True)
class MarketInput:
    query: str
    candidate: str | None = None
    source_url: str | None = None
    buy_platform: str | None = None
    sell_platform: str | None = None


@dataclass(frozen=True)
class ResolvedMarket:
    pair: MarketPair | None
    kalshi_match: dict[str, Any] | None
    polymarket_match: dict[str, Any] | None
    warnings: tuple[str, ...]


def parse_market_input(value: str, candidate: str | None = None) -> MarketInput:
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        params = parse_qs(parsed.query)
        query = _title_from_path(parsed.path)
        return MarketInput(
            query=query,
            candidate=_one(params, "candidate") or candidate,
            source_url=value,
            buy_platform=_one(params, "buy"),
            sell_platform=_one(params, "sell"),
        )
    return MarketInput(query=value.strip(), candidate=candidate)


def resolve_market(
    market_input: MarketInput,
    kalshi: KalshiClient,
    polymarket: PolymarketClient,
    rules_compatible: bool = False,
) -> ResolvedMarket:
    warnings: list[str] = []
    unsupported = _unsupported_platforms(market_input)
    if unsupported:
        warnings.append(
            "PredictionHunt URL mentions unsupported platform(s): "
            + ", ".join(sorted(unsupported))
            + ". This bot currently trades Kalshi and Polymarket only."
        )
    requested_platforms = _requested_platforms(market_input)
    if requested_platforms and requested_platforms != {"kalshi", "polymarket"}:
        warnings.append(
            "Skipping exchange lookups because this URL is not a Kalshi/Polymarket pair."
        )
        return ResolvedMarket(None, None, None, tuple(warnings))

    kalshi_match = _best_kalshi_match(kalshi, market_input.query, market_input.candidate)
    polymarket_match = _best_polymarket_match(
        polymarket,
        market_input.query,
        market_input.candidate,
    )

    if kalshi_match is None:
        warnings.append("could not confidently match a Kalshi market")
    if polymarket_match is None:
        warnings.append("could not confidently match a Polymarket binary market")
    if kalshi_match is None or polymarket_match is None:
        return ResolvedMarket(None, kalshi_match, polymarket_match, tuple(warnings))

    tokens = _polymarket_binary_tokens(polymarket_match)
    if tokens is None:
        warnings.append("matched Polymarket market is not a supported YES/NO binary market")
        return ResolvedMarket(None, kalshi_match, polymarket_match, tuple(warnings))

    pair = MarketPair(
        name=market_input.query,
        kalshi_ticker=str(kalshi_match["ticker"]),
        polymarket_yes_token_id=tokens[0],
        polymarket_no_token_id=tokens[1],
        rules_compatible=rules_compatible,
        notes="Resolved from URL/name. Manually verify settlement rules before enabling.",
    )
    return ResolvedMarket(pair, kalshi_match, polymarket_match, tuple(warnings))


def _title_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "odds":
        slug = parts[1]
    elif parts:
        slug = parts[-1]
    else:
        slug = ""
    return unquote(slug).replace("-", " ").strip()


def _one(params: dict[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values else None


def _unsupported_platforms(market_input: MarketInput) -> set[str]:
    supported = {"kalshi", "polymarket"}
    found = _requested_platforms(market_input)
    return found - supported


def _requested_platforms(market_input: MarketInput) -> set[str]:
    return {
        value.strip().lower()
        for value in (market_input.buy_platform, market_input.sell_platform)
        if value
    }


def _best_kalshi_match(
    kalshi: KalshiClient,
    query: str,
    candidate: str | None,
) -> dict[str, Any] | None:
    response = kalshi.get_markets(status="open", limit=1000)
    markets = response.get("markets", []) if isinstance(response, dict) else []
    ranked = sorted(
        [market for market in markets if _has_required_query_tokens(query, _kalshi_text(market))],
        key=lambda item: _score(
            query,
            candidate,
            _kalshi_text(item),
        ),
        reverse=True,
    )
    return ranked[0] if ranked and _score_market(query, candidate, ranked[0]) >= 0.45 else None


def _best_polymarket_match(
    polymarket: PolymarketClient,
    query: str,
    candidate: str | None,
) -> dict[str, Any] | None:
    events = polymarket.get_events(active="true", closed="false", limit=100)
    if isinstance(events, dict):
        events = events.get("events", events.get("data", []))
    markets: list[dict[str, Any]] = []
    for event in events if isinstance(events, list) else []:
        for market in event.get("markets", []):
            market["_event_title"] = event.get("title") or event.get("slug") or ""
            markets.append(market)
    ranked = sorted(
        [market for market in markets if _has_required_query_tokens(query, _polymarket_text(market))],
        key=lambda item: _score(
            query,
            candidate,
            _polymarket_text(item),
        ),
        reverse=True,
    )
    for market in ranked[:10]:
        if _polymarket_binary_tokens(market) and _score_polymarket(query, candidate, market) >= 0.45:
            return market
    return None


def _score_market(query: str, candidate: str | None, market: dict[str, Any]) -> float:
    return _score(query, candidate, _kalshi_text(market))


def _score_polymarket(query: str, candidate: str | None, market: dict[str, Any]) -> float:
    return _score(query, candidate, _polymarket_text(market))


def _kalshi_text(market: dict[str, Any]) -> str:
    return " ".join(
        str(market.get(field, ""))
        for field in ("title", "subtitle", "yes_sub_title", "no_sub_title", "ticker")
    )


def _polymarket_text(market: dict[str, Any]) -> str:
    return " ".join(
        str(market.get(field, ""))
        for field in ("question", "title", "slug", "_event_title")
    )


def _has_required_query_tokens(query: str, text: str) -> bool:
    required = {
        token
        for token in _norm(query).split()
        if len(token) > 2 and token not in {"the", "and", "for", "win", "will"}
    }
    text_tokens = set(_norm(text).split())
    return required.issubset(text_tokens)


def _score(query: str, candidate: str | None, text: str) -> float:
    query_norm = _norm(query)
    text_norm = _norm(text)
    ratio = SequenceMatcher(None, query_norm, text_norm).ratio()
    query_tokens = set(query_norm.split())
    text_tokens = set(text_norm.split())
    overlap = len(query_tokens & text_tokens) / max(len(query_tokens), 1)
    candidate_bonus = 0.0
    if candidate and _norm(candidate) in text_norm:
        candidate_bonus = 0.15
    return min(1.0, max(ratio, overlap) + candidate_bonus)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _polymarket_binary_tokens(market: dict[str, Any]) -> tuple[str, str] | None:
    outcomes = _maybe_json(market.get("outcomes") or [])
    token_ids = _maybe_json(market.get("clobTokenIds") or market.get("clob_token_ids") or [])
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return None
    if len(outcomes) != 2 or len(token_ids) != 2:
        return None
    labels = [str(outcome).lower() for outcome in outcomes]
    if "yes" not in labels or "no" not in labels:
        return None
    yes_index = labels.index("yes")
    no_index = labels.index("no")
    return str(token_ids[yes_index]), str(token_ids[no_index])


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
