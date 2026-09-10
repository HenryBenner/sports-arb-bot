"""Read-only, seeded sampling of real game contracts; no trading date window."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from firstbot.exchanges.polymarket_us import PolymarketUSClient
from firstbot.exchanges.international_mapping import align_participants, participants, timestamp, values
from firstbot.http import HttpClient
from firstbot.models import Side


def collect_samples(http, us, leagues, rng, per_league):
    samples = []
    for league in leagues:
        inventory = http.get_json(f"https://gateway.polymarket.us/v2/leagues/{league}/events")
        games = [e for e in inventory.get("events", []) if any(
            m.get("sportsMarketType") not in (None, "", "futures") for m in e.get("markets", []))]
        if not games:
            archived = us.get_events(closed=True, tagSlug=league, limit=100)
            games = [e for e in archived.get("events", []) if any(
                m.get("sportsMarketType") not in (None, "", "futures") for m in e.get("markets", []))]
        if not games:
            samples.append({"league": league, "international": None, "sampling_reason": "no game inventory returned"})
            continue
        for event in rng.sample(games, min(per_league, len(games))):
            record = {"league": league, "us": event, "international": None}
            matches = http.get_json("https://gamma-api.polymarket.com/events", {"slug": event["slug"]})
            if not matches:
                # Slugs are only a discovery shortcut for the sampler, never an
                # approval signal for the mapper being tested.
                names = participants(event)
                search = http.get_json("https://gamma-api.polymarket.com/public-search",
                                       {"q": " vs. ".join(names), "limit_per_type": 100,
                                        **({"events_status": "closed", "keep_closed_markets": 1} if event.get("closed") else {})})
                matches = []
                for candidate in search.get("events", []):
                    if candidate.get("startTime"):
                        try:
                            if timestamp(candidate["startTime"]).date() != timestamp(event.get("startTime")).date():
                                continue
                        except RuntimeError:
                            continue
                    full = http.get_json("https://gamma-api.polymarket.com/events/slug/" + candidate["slug"])
                    try:
                        if participants(full) == participants(align_participants(full, event)) and timestamp(full.get("startTime")).date() == timestamp(event.get("startTime")).date():
                            matches.append(full)
                    except RuntimeError:
                        continue
            if len(matches) == 1:
                record["international"] = matches[0]
            else:
                record["sampling_reason"] = f"International event discovery found {len(matches)} counterparts"
            samples.append(record)
    return samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--leagues", default="mlb,nfl,epl,ufc,atp,nba,nhl")
    parser.add_argument("--per-league", type=int, default=2)
    parser.add_argument("--source-samples", help="Replay previously collected random event samples")
    parser.add_argument("--output", default="logs/random_sports_mapping_report.json")
    parser.add_argument("--types", default="moneyline,spreads,totals,first_half_moneyline,first_half_spreads,first_half_totals")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    http = HttpClient(timeout=15, retries=0)
    # Public metadata only: no account key, trade client calls, or PredictionHunt.
    us = PolymarketUSClient("https://gateway.polymarket.us", "https://api.polymarket.us", "wss://api.polymarket.us/v1/ws/markets", http=http)
    samples = (json.loads(Path(args.source_samples).read_text(encoding="utf-8")) if args.source_samples else
               collect_samples(http, us, args.leagues.split(","), rng, args.per_league))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix(".samples.json").write_text(json.dumps(samples), encoding="utf-8")
    results = []
    for sample in samples:
        event = sample.get("international")
        if not event:
            results.append({"league": sample["league"], "status": "not_sampled",
                            "reason": sample.get("sampling_reason", "International counterpart not found")})
            continue
        for kind in args.types.split(","):
            markets = [m for m in event.get("markets", []) if m.get("sportsMarketType") == kind]
            if not markets:
                continue
            market = rng.choice(markets)
            tokens, outcomes = values(market.get("clobTokenIds")), values(market.get("outcomes"))
            if len(tokens) != 2 or len(outcomes) != 2:
                continue
            index = rng.randrange(2)
            label = str(outcomes[index])
            side = Side(label.lower()) if label.lower() in ("yes", "no") else Side.YES
            result = {"league": sample["league"], "event": event["slug"], "question": market["question"],
                      "market_type": kind, "token": str(tokens[index]), "intended_outcome": label}
            try:
                evidence = us.international_mapper.inspect(str(tokens[index]), side)
                result.update(evidence=evidence, status="identity_match" if evidence["identity_matches"] == 1 and not evidence["unresolved_candidates"] else "not_exact")
            except Exception as exc:
                result.update(status="error", reason=str(exc))
            results.append(result)
            candidates = result.get("evidence", {}).get("candidates", [])
            print(f"{sample['league']} {kind} {market['question']}: {result['status']} " +
                  (", ".join(c['us_slug'] + ' / ' + c['us_outcome'] for c in candidates) or result.get('reason', '')), flush=True)
    counts = dict(Counter(r["status"] for r in results))
    report = {"observed_at": datetime.now(timezone.utc).isoformat(), "seed": args.seed,
              "diagnostic_only": True, "summary": counts, "results": results}
    output.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Summary: {counts}; evidence: {output}")
    return 0 if all(r["status"] == "identity_match" for r in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
