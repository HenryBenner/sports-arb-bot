from __future__ import annotations

import sys
import logging
import argparse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from firstbot.config import Settings
from firstbot.exchanges import PolymarketUSClient, create_polymarket_client
from firstbot.models import Exchange, Side
from firstbot.predictionhunt import PredictionHuntClient


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Read-only International -> US contract mapping diagnostic")
    parser.add_argument("--token", help="Inspect one International outcome token without querying PredictionHunt")
    parser.add_argument("--side", choices=("yes", "no"), default="yes", help="PredictionHunt pair side")
    args = parser.parse_args()
    settings = Settings.from_env()
    polymarket = create_polymarket_client(settings)
    if not isinstance(polymarket, PolymarketUSClient):
        raise RuntimeError("POLYMARKET_VENUE is not set to us")
    if args.token:
        try:
            print(polymarket.resolve_predictionhunt_market(args.token, Side(args.side)))
            return 0
        except Exception as exc:
            print(str(exc))
            return 2
    predictionhunt = PredictionHuntClient(
        settings.predictionhunt_base_url,
        settings.predictionhunt_api_key,
        settings.predictionhunt_arbs_path,
        settings.predictionhunt_ev_path,
    )
    fetched = predictionhunt.get_arbitrage_opportunities(category="", limit=100)
    print(f"Fetched categories: {dict(Counter(o.event_type for o in fetched))}")
    opportunities = [o for o in fetched if (o.event_type or "").lower() in {"sports", "esports"}]
    print(f"Excluded non-sports opportunities: {len(fetched) - len(opportunities)}")
    for opportunity in opportunities:
        print(f"Sports candidate: {opportunity.group_title}; event_date={opportunity.event_date}")
    legs = [
        leg for opportunity in opportunities for leg in opportunity.legs
        if leg.platform is Exchange.POLYMARKET
    ]
    mapped = 0
    failures: list[str] = []
    for leg in legs:
        try:
            reference = polymarket.resolve_predictionhunt_market(
                leg.market_id, leg.side, leg.source_url
            )
            print(f"international_id={leg.market_id} -> {reference} confidence=exact")
            mapped += 1
        except Exception as exc:
            failures.append(str(exc))
            print(f"international_id={leg.market_id}: {exc}")
    print(f"PredictionHunt opportunities: {len(opportunities)}")
    print(f"Polymarket legs: {len(legs)}")
    print(f"Exact Polymarket US mappings: {mapped}")
    print(f"Unmapped legs: {len(failures)}")
    if failures:
        print(f"First failure: {failures[0]}")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
