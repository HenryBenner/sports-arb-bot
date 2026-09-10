"""Regression snapshots from public International and US APIs, September 10, 2026."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from firstbot.exchanges.international_mapping import InternationalMarketMapper
from firstbot.models import Side


CASES = json.loads((Path(__file__).parent / "fixtures/polymarket_sports_samples.json").read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["label"])
def test_real_event_identity_and_outcome(case):
    source, event, target = case["international_market"], case["international_event"], case["us_event"]
    http = Mock()
    http.get_json.side_effect = lambda url, params=None: [source] if url.endswith("/markets") else event
    sdk = SimpleNamespace(search=Mock(), events=Mock())
    sdk.search.query.return_value = {"events": [target]}
    sdk.events.retrieve_by_slug.return_value = target
    us = Mock()
    us._client.return_value = sdk
    us.get_events.return_value = {"events": [target]}
    us.get_market_by_slug.side_effect = lambda slug: next(m for m in target["markets"] if m["slug"] == slug)
    mapper = InternationalMarketMapper(us, http=http)
    side = Side(case["outcome"].lower()) if case["outcome"].lower() in ("yes", "no") else Side.YES
    report = mapper.inspect(case["token"], side)
    project = lambda candidates: [{"slug": c["us_slug"], "side": c["us_outcome"]} for c in candidates]
    assert project(report["candidates"]) == case["expected"]
    assert project(report["near_matches"]) == case["expected_near"]
    assert not report["unresolved_candidates"]
    assert all(c["rules_status"] != "exact" for c in report["candidates"])
