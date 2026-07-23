from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .arb import verify_predictionhunt_opportunity
from .config import Settings
from .executor import TradeExecutor
from .exchanges import KalshiClient, PolymarketClient
from .models import ArbLeg, Exchange, Side
from .predictionhunt import PredictionHuntClient, PredictionHuntLeg, PredictionHuntOpportunity


DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EVENT_DATE_ZONE = ZoneInfo("America/New_York")
EVENT_DATE_END_OF_DAY = datetime_time(23, 59, 59)


class PredictionHuntRunner:
    def __init__(
        self,
        predictionhunt: PredictionHuntClient,
        kalshi: KalshiClient,
        polymarket: PolymarketClient,
        settings: Settings,
        log_dir: str | Path = "logs",
        min_profit_cents: int = 5,
        max_days_to_resolution: int = 3,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.predictionhunt = predictionhunt
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.settings = replace(settings, min_profit_cents=min_profit_cents)
        self.max_days_to_resolution = max_days_to_resolution
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.executor = TradeExecutor(kalshi, polymarket)
        self.seen_signatures: set[str] = set()
        self.log_dir = Path(log_dir)

    def run(
        self,
        category: str,
        limit: int,
        poll_seconds: int,
        execute: bool,
        once: bool = False,
    ) -> None:
        while True:
            summary = self.poll_once(category=category, limit=limit, execute=execute)
            print(
                f"poll complete: fetched={summary['fetched']} "
                f"eligible={summary['eligible']} verified={summary['verified']} "
                f"executed={summary['executed']}"
            )
            if once:
                return
            time.sleep(poll_seconds)

    def poll_once(self, category: str, limit: int, execute: bool) -> dict[str, int]:
        opportunities = self.predictionhunt.get_arbitrage_opportunities(
            category=category,
            limit=limit,
            min_roi=0,
            platforms="polymarket,kalshi",
        )
        summary = {"fetched": len(opportunities), "eligible": 0, "verified": 0, "executed": 0}
        for ph_opportunity in opportunities:
            skip_reason = self._skip_reason(ph_opportunity)
            if skip_reason:
                self._log(ph_opportunity, None, mode="paper", action="skipped", message=skip_reason)
                continue
            signature = self._signature(ph_opportunity)
            if signature in self.seen_signatures:
                continue
            self.seen_signatures.add(signature)
            summary["eligible"] += 1

            try:
                live_legs = tuple(self._live_leg(leg) for leg in ph_opportunity.legs)
                verified = verify_predictionhunt_opportunity(
                    ph_opportunity,
                    live_legs=(live_legs[0], live_legs[1]),
                    settings=self.settings,
                )
            except RuntimeError as exc:
                self._log(ph_opportunity, None, mode="paper", action="skipped", message=str(exc))
                continue

            if verified.net_profit_cents >= self.settings.min_profit_cents:
                summary["verified"] += 1

            if execute:
                submitted, message = self.executor.execute(verified)
                if submitted:
                    summary["executed"] += 1
                self._log(
                    ph_opportunity,
                    verified,
                    mode="live",
                    action="executed" if submitted else "blocked",
                    message=message,
                )
            else:
                self._log(ph_opportunity, verified, mode="paper", action="paper", message="paper trade")
                print(
                    f"paper: {verified.pair_name} net={verified.net_profit_cents}c "
                    f"blockers={len(verified.blockers)}"
                )
        return summary

    def _skip_reason(self, opportunity: PredictionHuntOpportunity) -> str | None:
        platforms = {leg.platform for leg in opportunity.legs}
        sides = {leg.side for leg in opportunity.legs}
        if platforms != {Exchange.KALSHI, Exchange.POLYMARKET}:
            return "not a Kalshi/Polymarket opportunity"
        if sides != {Side.YES, Side.NO}:
            return "legs are not one YES and one NO"
        event_date = _parse_datetime(opportunity.event_date)
        if event_date is None:
            return "missing or unparseable event_date"
        now = self.clock()
        if event_date < now:
            return "event_date is in the past"
        if event_date > now + timedelta(days=self.max_days_to_resolution):
            return f"event_date is more than {self.max_days_to_resolution} days away"
        return None

    def _live_leg(self, leg: PredictionHuntLeg) -> ArbLeg:
        if leg.platform is Exchange.KALSHI:
            best = self.kalshi.get_best_ask(leg.market_id, leg.side)
        elif leg.platform is Exchange.POLYMARKET:
            best = self.polymarket.get_token_best_ask(leg.market_id)
        else:
            raise RuntimeError(f"unsupported platform: {leg.platform}")
        if best is None:
            raise RuntimeError(
                f"no live ask for {leg.platform.value} {leg.side.value} market {leg.market_id}"
            )
        return ArbLeg(
            exchange=leg.platform,
            market_id=leg.market_id,
            side=leg.side,
            price_cents=best.price_cents,
            size=min(best.size, Decimal(self.settings.max_leg_usd)),
        )

    def _signature(self, opportunity: PredictionHuntOpportunity) -> str:
        legs = "|".join(
            f"{leg.platform.value}:{leg.side.value}:{leg.market_id}:{leg.price}"
            for leg in sorted(opportunity.legs, key=lambda item: (item.platform.value, item.market_id))
        )
        return (
            f"{opportunity.group_id}:{opportunity.group_title}:"
            f"{opportunity.roi_pct}:{opportunity.total_cost}:{legs}"
        )

    def _log(
        self,
        ph_opportunity: PredictionHuntOpportunity,
        verified,
        mode: str,
        action: str,
        message: str,
    ) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / ("live_trades.jsonl" if mode == "live" else "paper_trades.jsonl")
        record = {
            "timestamp": self.clock().isoformat(),
            "mode": mode,
            "action": action,
            "message": message,
            "predictionhunt": {
                "group_id": ph_opportunity.group_id,
                "group_title": ph_opportunity.group_title,
                "event_date": ph_opportunity.event_date,
                "event_type": ph_opportunity.event_type,
                "roi_pct": str(ph_opportunity.roi_pct),
                "total_cost": str(ph_opportunity.total_cost),
                "max_wager_usd": str(ph_opportunity.max_wager_usd),
                "detected_at": ph_opportunity.detected_at,
                "legs": [
                    {
                        "platform": leg.platform.value,
                        "side": leg.side.value,
                        "market_id": leg.market_id,
                        "source_url": leg.source_url,
                        "price": str(leg.price),
                        "liquidity_usd": str(leg.liquidity_usd),
                        "fee_usd": str(leg.fee_usd),
                    }
                    for leg in ph_opportunity.legs
                ],
            },
            "verified": _verified_record(verified),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _verified_record(verified) -> dict | None:
    if verified is None:
        return None
    return {
        "pair_name": verified.pair_name,
        "gross_cost_cents": verified.gross_cost_cents,
        "buffers_cents": _decimal_str(verified.buffers_cents),
        "net_profit_cents": _decimal_str(verified.net_profit_cents),
        "executable": verified.executable,
        "blockers": list(verified.blockers),
        "buy_yes": _leg_record(verified.buy_yes),
        "buy_no": _leg_record(verified.buy_no),
    }


def _leg_record(leg: ArbLeg) -> dict:
    return {
        "exchange": leg.exchange.value,
        "market_id": leg.market_id,
        "side": leg.side.value,
        "price_cents": leg.price_cents,
        "size": str(leg.size),
    }


def _decimal_str(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    if DATE_ONLY_RE.match(value):
        try:
            parsed_date = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
        return datetime.combine(parsed_date, EVENT_DATE_END_OF_DAY, EVENT_DATE_ZONE).astimezone(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{value}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
