from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .arb import verify_pair, verify_predictionhunt_opportunity
from .config import Settings
from .executor import TradeExecutor
from .exchanges import KalshiClient, PolymarketClient
from .hot import HotArbRunner, LiveLegBook, merge_streams
from .http import HttpClient
from .manual_sports_arb import ManualPairInput, ManualSportsArbRunner, load_mapping
from .models import ArbLeg, Exchange, MarketPair, Side
from .polymarket_deposit import deploy_deposit_wallet
from .predictionhunt import PredictionHuntClient, PredictionHuntLeg
from .readiness import LiveReadinessChecker
from .resolver import parse_market_input, resolve_market
from .runner import PredictionHuntRunner
from .signal_results import SignalResultUpdater
from .signals import SignalBotRunner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="firstbot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="scan configured market pairs")
    scan_parser.add_argument("--config", required=True, help="market-pair JSON file")
    scan_parser.add_argument(
        "--offline-fixture",
        help="optional fixture JSON with captured orderbooks for testing",
    )
    resolve_parser = subparsers.add_parser(
        "scan-input",
        help="scan one market from a URL or market-name input",
    )
    resolve_parser.add_argument(
        "--input",
        required=True,
        help="PredictionHunt URL or plain market name, for example 'Tunisia vs Japan'",
    )
    resolve_parser.add_argument(
        "--candidate",
        help="optional candidate/team/outcome hint, for example 'Japan'",
    )
    resolve_parser.add_argument(
        "--rules-compatible",
        action="store_true",
        help="mark matched contracts as rules-compatible after manual verification",
    )
    subparsers.add_parser("doctor", help="test exchange API connectivity")
    hunt_parser = subparsers.add_parser(
        "scan-predictionhunt",
        help="scan PredictionHunt sports arbs and verify against live books",
    )
    hunt_parser.add_argument("--category", default="sports", help="PredictionHunt category")
    hunt_parser.add_argument("--limit", type=int, default=25, help="maximum postings to inspect")
    run_hunt_parser = subparsers.add_parser(
        "run-predictionhunt",
        help="diagnostic PredictionHunt paper verifier; cannot place trades",
    )
    run_hunt_parser.add_argument("--category", default="sports", help="PredictionHunt category")
    run_hunt_parser.add_argument("--limit", type=int, default=25, help="maximum opportunities per poll")
    run_hunt_parser.add_argument("--poll-seconds", type=int, default=10, help="seconds between polls")
    run_hunt_parser.add_argument(
        "--max-days-to-resolution",
        type=int,
        default=3,
        help="skip markets resolving farther out than this",
    )
    run_hunt_parser.add_argument(
        "--min-profit-cents",
        type=int,
        default=5,
        help="minimum verified net profit per share after buffers",
    )
    run_hunt_parser.add_argument("--log-dir", default="logs", help="directory for JSONL paper/live logs")
    run_hunt_parser.add_argument(
        "--paper",
        action="store_true",
        help="paper-trade only; this command cannot place trades",
    )
    run_hunt_parser.add_argument("--once", action="store_true", help="run one poll and exit")
    hot_parser = subparsers.add_parser(
        "run-hot-arb",
        help="poll PredictionHunt broadly, open hot WebSocket watches, and trigger from live books",
    )
    hot_parser.add_argument(
        "--category",
        default=None,
        help="optional sports or esports filter; hot mode rejects all other event types",
    )
    hot_parser.add_argument("--limit", type=int, default=250, help="maximum opportunities per poll")
    hot_parser.add_argument(
        "--predictionhunt-poll-seconds",
        type=int,
        default=None,
        help="seconds between PredictionHunt macro polls",
    )
    hot_parser.add_argument(
        "--hot-window-seconds",
        type=int,
        default=None,
        help="seconds to keep exchange WebSocket streams open for each candidate",
    )
    hot_parser.add_argument(
        "--max-days-to-resolution",
        type=int,
        default=None,
        help="skip markets resolving farther out than this",
    )
    hot_parser.add_argument(
        "--prefer-same-day",
        action="store_true",
        help="prioritize same-day events when active watch capacity is full",
    )
    hot_parser.add_argument(
        "--trigger-cost-cents",
        type=int,
        default=None,
        help="legacy display/default value; hot trades now require positive net profit after buffers",
    )
    hot_parser.add_argument(
        "--near-miss-cost-cents",
        type=int,
        default=None,
        help="log near misses when basket cost is at or below this gross cost",
    )
    hot_parser.add_argument("--book-stale-ms", type=int, default=None, help="max age for live book updates")
    hot_parser.add_argument("--max-active-watches", type=int, default=None, help="max concurrent hot watches")
    hot_parser.add_argument(
        "--skip-startup-readiness",
        action="store_true",
        help="emergency/debug only: skip live exchange readiness checks before polling",
    )
    hot_parser.add_argument("--readiness-seconds", type=int, default=None, help="seconds for startup WebSocket probes")
    hot_parser.add_argument("--readiness-kalshi-ticker", help="Kalshi ticker for startup readiness")
    hot_parser.add_argument("--readiness-polymarket-token", help="Polymarket token for startup readiness")
    hot_parser.add_argument("--log-dir", default="logs", help="directory for JSONL hot logs")
    hot_mode = hot_parser.add_mutually_exclusive_group()
    hot_mode.add_argument("--paper", action="store_true", help="paper-trigger only")
    hot_mode.add_argument("--execute", action="store_true", help="attempt live execution on triggers")
    hot_parser.add_argument("--once", action="store_true", help="run one macro poll and exit")
    signal_parser = subparsers.add_parser(
        "run-signal-bot",
        help="consume PredictionHunt smart-money/fade-finder signals for selective paper/live trades",
    )
    signal_parser.add_argument("--log-dir", default="logs", help="directory for signal JSONL logs")
    signal_mode = signal_parser.add_mutually_exclusive_group()
    signal_mode.add_argument("--paper", action="store_true", help="paper-trade only")
    signal_mode.add_argument("--execute", action="store_true", help="attempt live signal execution")
    signal_parser.add_argument("--once", action="store_true", help="process one signal and exit")
    signal_parser.add_argument("--price-min-cents", dest="no_min_cents", metavar="PRICE_MIN_CENTS", type=int, help="minimum accepted signal-side ask")
    signal_parser.add_argument("--price-max-cents", dest="no_max_cents", metavar="PRICE_MAX_CENTS", type=int, help="maximum accepted signal-side ask")
    signal_parser.add_argument("--max-chase-cents", type=int, help="maximum worse current price versus signal")
    signal_parser.add_argument("--min-score", type=int, help="minimum signal score")
    signal_parser.add_argument("--min-ev-cents", type=Decimal, help="minimum estimated EV in cents")
    update_signal_parser = subparsers.add_parser(
        "update-signal-results",
        help="update signal paper CSV rows after markets resolve",
    )
    update_signal_parser.add_argument("--log-dir", default="logs", help="directory containing signal logs")
    update_signal_parser.add_argument("--csv", default=None, help="path to signal_paper_trades.csv")
    update_signal_parser.add_argument("--no-backup", action="store_true", help="rewrite CSV without creating a backup")
    readiness_parser = subparsers.add_parser(
        "run-live-readiness",
        help="dry-run live readiness checks for Kalshi and Polymarket; never places orders",
    )
    readiness_parser.add_argument("--seconds", type=int, default=None, help="seconds for WebSocket probes")
    readiness_parser.add_argument("--kalshi-ticker", help="Kalshi ticker for orderbook/WebSocket checks")
    readiness_parser.add_argument("--polymarket-token", help="Polymarket token for CLOB book/WebSocket checks")
    readiness_parser.add_argument("--log-dir", default="logs", help="directory for readiness JSONL logs")
    subparsers.add_parser(
        "deploy-polymarket-deposit-wallet",
        help="derive/deploy the POLY_1271 Polymarket deposit wallet used as the sigtype=3 funder",
    )
    manual_parser = subparsers.add_parser(
        "run-manual-sports-arb",
        help="track one manually supplied Polymarket/Kalshi sports arbitrage pair",
    )
    manual_parser.add_argument("--polymarket-url", required=True, help="Polymarket market URL")
    manual_parser.add_argument("--kalshi-url", required=True, help="Kalshi market URL")
    manual_mode = manual_parser.add_mutually_exclusive_group()
    manual_mode.add_argument("--scan", action="store_true", help="scan/log only")
    manual_mode.add_argument("--paper", action="store_true", help="paper-trade positive net-edge arbs")
    manual_mode.add_argument("--execute", action="store_true", help="run live readiness checks; v1 blocks real orders")
    manual_parser.add_argument("--sport", help="optional sport label")
    manual_parser.add_argument("--event-label", help="optional event/team label")
    manual_parser.add_argument("--mapping-file", help="optional JSON mapping confirming settlement compatibility")
    manual_parser.add_argument("--safe-to-trade", action="store_true", help="manual confirmation that the pair resolves as true opposites")
    manual_parser.add_argument("--seconds", type=int, default=30, help="seconds to run")
    manual_parser.add_argument("--log-dir", default="logs", help="directory for manual arb logs")
    manual_parser.add_argument("--min-net-edge-cents", type=Decimal, help="minimum net edge after fees")
    manual_parser.add_argument("--max-arb-usd", type=Decimal, help="maximum dollars per arbitrage")
    manual_parser.add_argument("--max-contracts", type=int, help="maximum contracts per arbitrage")
    manual_parser.add_argument("--book-stale-ms", type=int, help="max orderbook age")
    manual_parser.add_argument("--max-spread-cents", type=int, help="max same-venue YES+NO spread above 100c")
    ws_parser = subparsers.add_parser(
        "ws-probe",
        help="diagnostic only: write live Kalshi/Polymarket WebSocket book updates to JSONL",
    )
    ws_parser.add_argument("--kalshi-ticker", help="Kalshi market ticker to stream")
    ws_parser.add_argument(
        "--kalshi-side",
        choices=("yes", "no"),
        default="yes",
        help="Kalshi side to label/evaluate for the probe",
    )
    ws_parser.add_argument("--polymarket-token", help="Polymarket CLOB token/asset id to stream")
    ws_parser.add_argument(
        "--polymarket-side",
        choices=("yes", "no"),
        default="yes",
        help="PredictionHunt side for the Polymarket token",
    )
    ws_parser.add_argument("--seconds", type=int, default=30, help="seconds to stream before exiting")
    ws_parser.add_argument("--output", default="logs/ws_probe.jsonl", help="JSONL output path")
    ws_parser.add_argument(
        "--raw",
        action="store_true",
        help="write raw exchange WebSocket messages instead of parsed book updates",
    )
    args = parser.parse_args(argv)

    if args.command == "scan":
        return _run(lambda: scan(args.config, args.offline_fixture))
    if args.command == "scan-input":
        return _run(lambda: scan_input(args.input, args.candidate, args.rules_compatible))
    if args.command == "doctor":
        return doctor()
    if args.command == "scan-predictionhunt":
        return _run(lambda: scan_predictionhunt(args.category, args.limit, False))
    if args.command == "run-predictionhunt":
        return _run(
            lambda: run_predictionhunt(
                category=args.category,
                limit=args.limit,
                poll_seconds=args.poll_seconds,
                max_days_to_resolution=args.max_days_to_resolution,
                min_profit_cents=args.min_profit_cents,
                log_dir=args.log_dir,
                execute=False,
                once=args.once,
            )
        )
    if args.command == "run-hot-arb":
        return _run(
            lambda: run_hot_arb(
                category=args.category,
                limit=args.limit,
                predictionhunt_poll_seconds=args.predictionhunt_poll_seconds,
                hot_window_seconds=args.hot_window_seconds,
                max_days_to_resolution=args.max_days_to_resolution,
                prefer_same_day=args.prefer_same_day,
                trigger_cost_cents=args.trigger_cost_cents,
                near_miss_cost_cents=args.near_miss_cost_cents,
                book_stale_ms=args.book_stale_ms,
                max_active_watches=args.max_active_watches,
                log_dir=args.log_dir,
                execute=args.execute,
                once=args.once,
                skip_startup_readiness=args.skip_startup_readiness,
                readiness_seconds=args.readiness_seconds,
                readiness_kalshi_ticker=args.readiness_kalshi_ticker,
                readiness_polymarket_token=args.readiness_polymarket_token,
            )
        )
    if args.command == "run-signal-bot":
        return _run(
            lambda: run_signal_bot(
                log_dir=args.log_dir,
                execute=args.execute,
                once=args.once,
                no_min_cents=args.no_min_cents,
                no_max_cents=args.no_max_cents,
                max_chase_cents=args.max_chase_cents,
                min_score=args.min_score,
                min_ev_cents=args.min_ev_cents,
            )
        )
    if args.command == "update-signal-results":
        return _run(
            lambda: update_signal_results(
                log_dir=args.log_dir,
                csv_path=args.csv,
                backup=not args.no_backup,
            )
        )
    if args.command == "run-live-readiness":
        return _run(
            lambda: run_live_readiness(
                seconds=args.seconds,
                kalshi_ticker=args.kalshi_ticker,
                polymarket_token=args.polymarket_token,
                log_dir=args.log_dir,
            )
        )
    if args.command == "deploy-polymarket-deposit-wallet":
        return _run(deploy_polymarket_deposit_wallet)
    if args.command == "run-manual-sports-arb":
        return _run(
            lambda: run_manual_sports_arb(
                polymarket_url=args.polymarket_url,
                kalshi_url=args.kalshi_url,
                mode="live" if args.execute else ("paper" if args.paper else "scan"),
                sport=args.sport,
                event_label=args.event_label,
                mapping_file=args.mapping_file,
                safe_to_trade=args.safe_to_trade,
                seconds=args.seconds,
                log_dir=args.log_dir,
                min_net_edge_cents=args.min_net_edge_cents,
                max_arb_usd=args.max_arb_usd,
                max_contracts=args.max_contracts,
                book_stale_ms=args.book_stale_ms,
                max_spread_cents=args.max_spread_cents,
            )
        )
    if args.command == "ws-probe":
        return _run(
            lambda: ws_probe(
                kalshi_ticker=args.kalshi_ticker,
                kalshi_side=args.kalshi_side,
                polymarket_token=args.polymarket_token,
                polymarket_side=args.polymarket_side,
                seconds=args.seconds,
                output=args.output,
                raw=args.raw,
            )
        )
    return 2


def _run(action) -> int:
    try:
        return action()
    except KeyboardInterrupt:
        print("Stopped by user. No further trades will be placed.")
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}")
        print("No trade was placed.")
        return 1


def scan(config_path: str, offline_fixture: str | None = None) -> int:
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    pairs = _load_pairs(Path(config_path))
    fixture = _load_fixture(Path(offline_fixture)) if offline_fixture else None

    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        http=http,
    )

    for pair in pairs:
        if fixture:
            kalshi_book = fixture["kalshi"][pair.kalshi_ticker]
            polymarket_book = fixture["polymarket"][pair.polymarket_yes_token_id]
        else:
            kalshi_book = kalshi.get_orderbook(pair.kalshi_ticker)
            polymarket_book = polymarket.get_orderbook(
                pair.polymarket_yes_token_id,
                pair.polymarket_no_token_id,
                market_id=pair.polymarket_yes_token_id,
            )

        for opportunity in verify_pair(pair, kalshi_book, polymarket_book, settings):
            _print_opportunity(opportunity)
    return 0


def scan_input(input_value: str, candidate: str | None, rules_compatible: bool) -> int:
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        http=http,
    )
    market_input = parse_market_input(input_value, candidate=candidate)
    resolved = resolve_market(
        market_input,
        kalshi=kalshi,
        polymarket=polymarket,
        rules_compatible=rules_compatible,
    )

    print(f"Input market: {market_input.query}")
    if market_input.candidate:
        print(f"Candidate hint: {market_input.candidate}")
    for warning in resolved.warnings:
        print(f"warning: {warning}")
    if resolved.kalshi_match:
        print(
            "Kalshi match: "
            f"{resolved.kalshi_match.get('ticker')} - {resolved.kalshi_match.get('title')}"
        )
    if resolved.polymarket_match:
        print(
            "Polymarket match: "
            f"{resolved.polymarket_match.get('question') or resolved.polymarket_match.get('title')}"
        )
    if resolved.pair is None:
        print("No executable pair was built. Try a more specific market name/candidate.")
        return 1

    kalshi_book = kalshi.get_orderbook(resolved.pair.kalshi_ticker)
    polymarket_book = polymarket.get_orderbook(
        resolved.pair.polymarket_yes_token_id,
        resolved.pair.polymarket_no_token_id,
        market_id=resolved.pair.polymarket_yes_token_id,
    )
    for opportunity in verify_pair(resolved.pair, kalshi_book, polymarket_book, settings):
        _print_opportunity(opportunity)
    return 0


def doctor() -> int:
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    checks = [
        ("Kalshi markets", f"{settings.kalshi_base_url}/markets", {"status": "open", "limit": 1}),
        ("Polymarket events", f"{settings.polymarket_gamma_url}/events", {"active": "true", "closed": "false", "limit": 1}),
    ]
    ok = True
    for label, url, params in checks:
        try:
            http.get_json(url, params=params)
            print(f"ok: {label}")
        except RuntimeError as exc:
            ok = False
            print(f"failed: {label}")
            print(f"  {exc}")
    print(f"BOT_LIVE_TRADING={settings.live_trading}")
    return 0 if ok else 1


def scan_predictionhunt(category: str, limit: int, execute: bool) -> int:
    if execute:
        raise RuntimeError("live execution is only allowed from run-hot-arb")
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        http=http,
    )
    predictionhunt = PredictionHuntClient(
        base_url=settings.predictionhunt_base_url,
        api_key=settings.predictionhunt_api_key,
        arbs_path=settings.predictionhunt_arbs_path,
        ev_path=settings.predictionhunt_ev_path,
        http=http,
    )
    executor = TradeExecutor(kalshi, polymarket)
    opportunities = predictionhunt.get_arbitrage_opportunities(category=category, limit=limit)
    if not opportunities:
        print("No PredictionHunt opportunities returned.")
        return 0

    seen = 0
    verified = 0
    for ph_opportunity in opportunities:
        seen += 1
        print(f"\nPredictionHunt: {ph_opportunity.group_title}")
        print(
            f"PH roi={ph_opportunity.roi_pct}% "
            f"cost={ph_opportunity.total_cost} "
            f"max_wager=${ph_opportunity.max_wager_usd}"
        )
        try:
            live_legs = tuple(
                _live_leg_from_predictionhunt_leg(leg, kalshi, polymarket, settings)
                for leg in ph_opportunity.legs
            )
        except RuntimeError as exc:
            print(f"skipped: {exc}")
            continue
        verified_opportunity = verify_predictionhunt_opportunity(
            ph_opportunity,
            live_legs=(live_legs[0], live_legs[1]),
            settings=settings,
        )
        _print_opportunity(verified_opportunity)
        if verified_opportunity.net_profit_cents >= settings.min_profit_cents:
            verified += 1
        if execute:
            submitted, message = executor.execute(verified_opportunity)
            print(("executed: " if submitted else "not executed: ") + message)

    print(f"\nInspected {seen} Kalshi/Polymarket postings; {verified} cleared profit math before blockers.")
    if execute and not settings.live_trading:
        print("Execution requested, but BOT_LIVE_TRADING=false keeps orders blocked.")
    return 0


def deploy_polymarket_deposit_wallet() -> int:
    settings = Settings.from_env()
    deployment = deploy_deposit_wallet(settings)
    print(f"expected/deployed deposit wallet: {deployment.expected_wallet}")
    print("Set these in .env before live Polymarket trading:")
    print("POLYMARKET_SIGNATURE_TYPE=3")
    print(f"POLYMARKET_FUNDER_ADDRESS={deployment.expected_wallet}")
    print("Then fund the deposit wallet and approve/sync collateral before running --execute.")
    return 0


def run_predictionhunt(
    category: str,
    limit: int,
    poll_seconds: int,
    max_days_to_resolution: int,
    min_profit_cents: int,
    log_dir: str,
    execute: bool,
    once: bool,
) -> int:
    if execute:
        raise RuntimeError("live execution is only allowed from run-hot-arb")
    if poll_seconds < 1:
        raise RuntimeError("--poll-seconds must be at least 1")
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        http=http,
    )
    predictionhunt = PredictionHuntClient(
        base_url=settings.predictionhunt_base_url,
        api_key=settings.predictionhunt_api_key,
        arbs_path=settings.predictionhunt_arbs_path,
        ev_path=settings.predictionhunt_ev_path,
        http=http,
    )
    runner = PredictionHuntRunner(
        predictionhunt=predictionhunt,
        kalshi=kalshi,
        polymarket=polymarket,
        settings=settings,
        log_dir=log_dir,
        min_profit_cents=min_profit_cents,
        max_days_to_resolution=max_days_to_resolution,
    )
    print(
        f"running PredictionHunt poller category={category} "
        f"poll_seconds={poll_seconds} execute={execute} live={settings.live_trading}"
    )
    runner.run(
        category=category,
        limit=limit,
        poll_seconds=poll_seconds,
        execute=execute,
        once=once,
    )
    return 0


def run_hot_arb(
    category: str | None,
    limit: int,
    predictionhunt_poll_seconds: int | None,
    hot_window_seconds: int | None,
    max_days_to_resolution: int | None,
    prefer_same_day: bool,
    trigger_cost_cents: int | None,
    near_miss_cost_cents: int | None,
    book_stale_ms: int | None,
    max_active_watches: int | None,
    log_dir: str,
    execute: bool,
    once: bool,
    skip_startup_readiness: bool = False,
    readiness_seconds: int | None = None,
    readiness_kalshi_ticker: str | None = None,
    readiness_polymarket_token: str | None = None,
) -> int:
    settings = Settings.from_env()
    poll_seconds = predictionhunt_poll_seconds or settings.predictionhunt_poll_seconds
    hot_seconds = hot_window_seconds or settings.hot_window_seconds
    max_days = max_days_to_resolution or settings.max_days_to_resolution
    trigger_cents = trigger_cost_cents or settings.trigger_cost_cents
    near_miss_cents = near_miss_cost_cents or settings.near_miss_cost_cents
    stale_ms = book_stale_ms or settings.book_stale_ms
    max_watches = max_active_watches or settings.max_active_watches
    prefer_today = prefer_same_day or settings.prefer_same_day
    allowed_event_types = {
        str(value).strip().lower().replace("-", "").replace(" ", "")
        for value in settings.hot_allowed_event_types
        if str(value).strip()
    }
    requested_category = (
        str(category).strip().lower().replace("-", "").replace(" ", "")
        if category
        else None
    )
    if requested_category and requested_category not in allowed_event_types:
        raise RuntimeError(
            "run-hot-arb only allows sports/esports categories; "
            f"received --category {category}"
        )
    if poll_seconds < 1:
        raise RuntimeError("--predictionhunt-poll-seconds must be at least 1")
    if hot_seconds < 1:
        raise RuntimeError("--hot-window-seconds must be at least 1")
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        private_key=settings.polymarket_private_key,
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
        funder_address=settings.polymarket_funder_address,
        signature_type=settings.polymarket_signature_type,
        http=http,
    )
    if execute:
        _deploy_guard(settings, kalshi, polymarket)
        if settings.startup_readiness and not skip_startup_readiness:
            LiveReadinessChecker(
                settings=settings,
                kalshi=kalshi,
                polymarket=polymarket,
                log_dir=log_dir,
                seconds=readiness_seconds,
                kalshi_ticker=readiness_kalshi_ticker,
                polymarket_token=readiness_polymarket_token,
            ).run(print_status=True)
        elif skip_startup_readiness:
            print("warning: startup live readiness skipped")
            if settings.hot_geoblock_check:
                checker = LiveReadinessChecker(
                    settings=settings,
                    kalshi=kalshi,
                    polymarket=polymarket,
                    log_dir=log_dir,
                    seconds=readiness_seconds,
                )
                checker._check_polymarket_geoblock()
                print("Polymarket geographic eligibility OK")
    predictionhunt = PredictionHuntClient(
        base_url=settings.predictionhunt_base_url,
        api_key=settings.predictionhunt_api_key,
        arbs_path=settings.predictionhunt_arbs_path,
        ev_path=settings.predictionhunt_ev_path,
        http=http,
    )
    runner = HotArbRunner(
        predictionhunt=predictionhunt,
        kalshi=kalshi,
        polymarket=polymarket,
        settings=settings,
        log_dir=log_dir,
    )
    print(
        "running hot arb "
        f"category={category or ','.join(settings.hot_allowed_event_types)} "
        f"poll_seconds={poll_seconds} "
        f"hot_window={hot_seconds}s trigger=net_profit>0c "
        f"fee_buffer={settings.fee_buffer_cents}c slippage={settings.slippage_cents}c "
        f"near_miss={near_miss_cents}c "
        f"execute={execute} live={settings.live_trading}"
    )
    asyncio.run(
        runner.run(
            category=category,
            limit=limit,
            predictionhunt_poll_seconds=poll_seconds,
            hot_window_seconds=hot_seconds,
            max_days_to_resolution=max_days,
            prefer_same_day=prefer_today,
            trigger_cost_cents=trigger_cents,
            near_miss_cost_cents=near_miss_cents,
            stale_ms=stale_ms,
            max_active_watches=max_watches,
            execute=execute,
            once=once,
        )
    )
    return 0


def run_live_readiness(
    seconds: int | None,
    kalshi_ticker: str | None,
    polymarket_token: str | None,
    log_dir: str,
) -> int:
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        private_key=settings.polymarket_private_key,
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
        funder_address=settings.polymarket_funder_address,
        signature_type=settings.polymarket_signature_type,
        http=http,
    )
    LiveReadinessChecker(
        settings=settings,
        kalshi=kalshi,
        polymarket=polymarket,
        log_dir=log_dir,
        seconds=seconds,
        kalshi_ticker=kalshi_ticker,
        polymarket_token=polymarket_token,
    ).run(print_status=True)
    print("live readiness OK")
    return 0


def run_signal_bot(
    log_dir: str,
    execute: bool,
    once: bool,
    no_min_cents: int | None,
    no_max_cents: int | None,
    max_chase_cents: int | None,
    min_score: int | None,
    min_ev_cents: Decimal | None,
) -> int:
    settings = Settings.from_env()
    updates = {}
    if no_min_cents is not None:
        updates["signal_no_min_cents"] = no_min_cents
    if no_max_cents is not None:
        updates["signal_no_max_cents"] = no_max_cents
    if max_chase_cents is not None:
        updates["signal_max_chase_cents"] = max_chase_cents
    if min_score is not None:
        updates["signal_min_score"] = min_score
    if min_ev_cents is not None:
        updates["signal_min_ev_cents"] = min_ev_cents
    if updates:
        settings = replace(settings, **updates)
    if execute:
        _signal_deploy_guard(settings)
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        private_key=settings.polymarket_private_key,
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
        funder_address=settings.polymarket_funder_address,
        signature_type=settings.polymarket_signature_type,
        http=http,
    )
    predictionhunt = PredictionHuntClient(
        base_url=settings.predictionhunt_base_url,
        api_key=settings.predictionhunt_api_key,
        arbs_path=settings.predictionhunt_arbs_path,
        ev_path=settings.predictionhunt_ev_path,
        http=http,
    )
    runner = SignalBotRunner(
        predictionhunt=predictionhunt,
        kalshi=kalshi,
        polymarket=polymarket,
        settings=settings,
        log_dir=log_dir,
    )
    print(
        "running signal bot "
        f"channels={','.join(settings.predictionhunt_signal_channels)} "
        f"paper={not execute} execute={execute} live={settings.live_trading}"
    )
    asyncio.run(runner.run(execute=execute, once=once))
    return 0


def update_signal_results(log_dir: str, csv_path: str | None, backup: bool) -> int:
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        http=http,
    )
    updater = SignalResultUpdater(kalshi, polymarket, log_dir=log_dir)
    summary = updater.update_csv(csv_path=csv_path, backup=backup)
    print(
        "signal results updated "
        f"rows={summary.total_rows} updated={summary.updated_rows} "
        f"pending={summary.pending_rows} errors={summary.error_rows}"
    )
    if summary.backup_path:
        print(f"backup: {summary.backup_path}")
    return 0


def run_manual_sports_arb(
    polymarket_url: str,
    kalshi_url: str,
    mode: str,
    sport: str | None,
    event_label: str | None,
    mapping_file: str | None,
    safe_to_trade: bool,
    seconds: int,
    log_dir: str,
    min_net_edge_cents: Decimal | None,
    max_arb_usd: Decimal | None,
    max_contracts: int | None,
    book_stale_ms: int | None,
    max_spread_cents: int | None,
) -> int:
    if seconds < 1:
        raise RuntimeError("--seconds must be at least 1")
    settings = Settings.from_env()
    updates = {}
    if min_net_edge_cents is not None:
        updates["manual_arb_min_net_edge_cents"] = min_net_edge_cents
    if max_arb_usd is not None:
        updates["manual_arb_max_usd"] = max_arb_usd
    if max_contracts is not None:
        updates["manual_arb_max_contracts"] = max_contracts
    if book_stale_ms is not None:
        updates["book_stale_ms"] = book_stale_ms
    if max_spread_cents is not None:
        updates["manual_arb_max_spread_cents"] = max_spread_cents
    if updates:
        settings = replace(settings, **updates)
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        private_key=settings.polymarket_private_key,
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
        funder_address=settings.polymarket_funder_address,
        signature_type=settings.polymarket_signature_type,
        http=http,
    )
    runner = ManualSportsArbRunner(kalshi, polymarket, settings, log_dir=log_dir)
    pair_input = ManualPairInput(
        polymarket_url=polymarket_url,
        kalshi_url=kalshi_url,
        sport=sport,
        event_label=event_label,
        safe_to_trade=safe_to_trade,
        mapping=load_mapping(mapping_file),
    )
    pair, safety = runner.resolve_pair(pair_input)
    print(f"manual sports arb mode={mode} seconds={seconds} live={settings.live_trading}")
    print(f"Polymarket: {pair.polymarket_title} [{pair.polymarket_slug}]")
    print(f"Kalshi: {pair.kalshi_title} [{pair.kalshi_ticker}]")
    if safety.warnings:
        for warning in safety.warnings:
            print(f"warning: {warning}")
    if not safety.safe:
        print(f"safety: blocked ({safety.reason})")
    deadline = datetime.now(timezone.utc).timestamp() + seconds
    tick_count = 0
    while datetime.now(timezone.utc).timestamp() < deadline:
        tick_count += 1
        decision = runner.tick(pair, safety, mode)
        best = decision.best_direction
        print(
            f"manual arb {decision.decision}: best={best.name} "
            f"gross={_fmt_decimal(best.gross_edge_cents)}c "
            f"fees={_fmt_decimal(best.fees_cents)}c "
            f"net={_fmt_decimal(best.net_edge_cents)}c "
            f"size={_fmt_decimal(best.contracts)} "
            f"reason={decision.rejection_reason or 'ok'}"
        )
        if mode == "live" and decision.decision == "blocked":
            submitted, message = runner.live_attempt(decision)
            print(f"live attempt submitted={submitted} message={message}")
        if seconds == 1:
            break
        import time

        time.sleep(1)
    print(f"manual sports arb complete: ticks={tick_count}")
    return 0


def ws_probe(
    kalshi_ticker: str | None,
    kalshi_side: str,
    polymarket_token: str | None,
    polymarket_side: str,
    seconds: int,
    output: str,
    raw: bool,
) -> int:
    if not kalshi_ticker and not polymarket_token:
        raise RuntimeError("provide --kalshi-ticker, --polymarket-token, or both")
    if seconds < 1:
        raise RuntimeError("--seconds must be at least 1")
    settings = Settings.from_env()
    http = HttpClient(timeout=settings.http_timeout_seconds)
    kalshi = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        private_key_path=settings.kalshi_private_key_path,
        http=http,
    )
    polymarket = PolymarketClient(
        gamma_url=settings.polymarket_gamma_url,
        clob_url=settings.polymarket_clob_url,
        http=http,
    )
    asyncio.run(
        _run_ws_probe(
            kalshi=kalshi,
            polymarket=polymarket,
            kalshi_ticker=kalshi_ticker,
            kalshi_side=Side(kalshi_side),
            polymarket_token=polymarket_token,
            polymarket_side=Side(polymarket_side),
            seconds=seconds,
            output=Path(output),
            raw=raw,
        )
    )
    return 0


async def _run_ws_probe(
    kalshi: KalshiClient,
    polymarket: PolymarketClient,
    kalshi_ticker: str | None,
    kalshi_side: Side,
    polymarket_token: str | None,
    polymarket_side: Side,
    seconds: int,
    output: Path,
    raw: bool,
) -> None:
    from datetime import datetime, timedelta, timezone

    from .websockets import KalshiOrderbookStream, PolymarketOrderbookStream

    legs: list[PredictionHuntLeg] = []
    if kalshi_ticker:
        legs.append(_probe_leg(Exchange.KALSHI, kalshi_ticker, kalshi_side))
    if polymarket_token:
        polymarket_token = polymarket.resolve_clob_token_id(polymarket_token, Side(polymarket_side))
        legs.append(_probe_leg(Exchange.POLYMARKET, polymarket_token, polymarket_side))
    streams = [
        KalshiOrderbookStream(kalshi, tuple(legs)),
        PolymarketOrderbookStream(polymarket, tuple(legs)),
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    if raw:
        await _run_raw_ws_probe(streams, expires_at, output)
        return
    count = 0
    print(f"probing WebSockets for {seconds}s -> {output}")
    try:
        async for update in merge_streams(streams, expires_at, lambda: datetime.now(timezone.utc)):
            count += 1
            record = _live_book_record(update)
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            best = record["best_ask"]
            price = "none" if best is None else f"{best['price_cents']}c x {best['size']}"
            print(f"ws update {count}: {record['exchange']} {record['side']} {record['market_id']} ask={price}")
    except RuntimeError as exc:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "stream_error",
            "message": str(exc),
        }
        with output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        raise
    print(f"ws probe complete: updates={count}")


async def _run_raw_ws_probe(streams: list, expires_at, output: Path) -> None:
    queue: asyncio.Queue[dict] = asyncio.Queue()

    async def pump(exchange: str, stream) -> None:
        try:
            async for message in stream.raw_listen_until(expires_at):
                await queue.put(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "exchange": exchange,
                        "message": message,
                    }
                )
        except Exception as exc:
            await queue.put(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "exchange": exchange,
                    "action": "stream_error",
                    "message": str(exc),
                }
            )

    labels = ["kalshi", "polymarket"]
    tasks = [asyncio.create_task(pump(label, stream)) for label, stream in zip(labels, streams)]
    count = 0
    print(f"probing raw WebSockets -> {output}")
    try:
        while any(not task.done() for task in tasks):
            timeout = max(0.1, (expires_at - datetime.now(timezone.utc)).total_seconds())
            try:
                record = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            count += 1
            with output.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
            print(f"raw ws {count}: {record.get('exchange')} {record.get('action', 'message')}")
    finally:
        for task in tasks:
            task.cancel()
    print(f"raw ws probe complete: messages={count}")


def _probe_leg(exchange: Exchange, market_id: str, side: Side) -> PredictionHuntLeg:
    return PredictionHuntLeg(
        side=side,
        platform=exchange,
        market_id=market_id,
        source_url=None,
        price=Decimal("0"),
        liquidity_usd=Decimal("0"),
        fee_usd=Decimal("0"),
    )


def _live_book_record(book: LiveLegBook) -> dict:
    return {
        "timestamp": None if book.updated_at is None else book.updated_at.isoformat(),
        "exchange": book.exchange.value,
        "market_id": book.market_id,
        "side": book.side.value,
        "best_ask": None
        if book.best_ask is None
        else {"price_cents": book.best_ask.price_cents, "size": str(book.best_ask.size)},
        "connected": book.connected,
        "snapshot_ready": book.snapshot_ready,
    }


def _deploy_guard(
    settings: Settings,
    kalshi: KalshiClient,
    polymarket: PolymarketClient,
) -> None:
    missing: list[str] = []
    if not settings.live_trading:
        missing.append("BOT_LIVE_TRADING=true")
    if not settings.predictionhunt_api_key:
        missing.append("PREDICTIONHUNT_API_KEY")
    if not settings.kalshi_api_key_id:
        missing.append("KALSHI_API_KEY_ID")
    if not settings.kalshi_private_key_path:
        missing.append("KALSHI_PRIVATE_KEY_PATH")
    if not polymarket.supports_immediate_orders():
        missing.append("Polymarket CLOB SDK/credentials with FOK support")
    if not kalshi.supports_immediate_orders():
        missing.append("Kalshi immediate order support")
    if missing:
        raise RuntimeError("live deploy guard failed: missing " + ", ".join(missing))


def _signal_deploy_guard(settings: Settings) -> None:
    missing: list[str] = []
    if not settings.live_trading:
        missing.append("BOT_LIVE_TRADING=true")
    if not settings.predictionhunt_api_key:
        missing.append("PREDICTIONHUNT_API_KEY")
    if not settings.predictionhunt_ws_url:
        missing.append("PREDICTIONHUNT_WS_URL")
    if not settings.kalshi_api_key_id:
        missing.append("KALSHI_API_KEY_ID")
    if not settings.kalshi_private_key_path:
        missing.append("KALSHI_PRIVATE_KEY_PATH")
    if missing:
        raise RuntimeError("signal live deploy guard failed: missing " + ", ".join(missing))


def _live_leg_from_predictionhunt_leg(leg, kalshi, polymarket, settings: Settings) -> ArbLeg:
    if leg.platform is Exchange.KALSHI:
        best = kalshi.get_best_ask(leg.market_id, leg.side)
    elif leg.platform is Exchange.POLYMARKET:
        best = polymarket.get_token_best_ask(leg.market_id)
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
        size=min(best.size, Decimal(settings.max_leg_usd)),
    )


def _load_pairs(path: Path) -> list[MarketPair]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        MarketPair(
            name=item["name"],
            kalshi_ticker=item["kalshi_ticker"],
            polymarket_yes_token_id=item["polymarket_yes_token_id"],
            polymarket_no_token_id=item["polymarket_no_token_id"],
            rules_compatible=bool(item.get("rules_compatible", False)),
            notes=item.get("notes", ""),
        )
        for item in data.get("pairs", [])
    ]


def _print_opportunity(opportunity) -> None:
    status = "EXECUTABLE" if opportunity.executable else "blocked"
    print(
        f"{status}: {opportunity.pair_name} "
        f"gross={opportunity.gross_cost_cents}c "
        f"buffers={opportunity.buffers_cents}c "
        f"net={opportunity.net_profit_cents}c "
        f"YES={opportunity.buy_yes.exchange.value}@{opportunity.buy_yes.price_cents}c "
        f"NO={opportunity.buy_no.exchange.value}@{opportunity.buy_no.price_cents}c"
    )
    for blocker in opportunity.blockers:
        print(f"  - {blocker}")


def _fmt_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _load_fixture(path: Path) -> dict:
    from .models import BookLevel, Exchange, OrderBook
    from decimal import Decimal

    data = json.loads(path.read_text(encoding="utf-8"))
    parsed = {"kalshi": {}, "polymarket": {}}
    for exchange_name, exchange in (("kalshi", Exchange.KALSHI), ("polymarket", Exchange.POLYMARKET)):
        for market_id, book in data.get(exchange_name, {}).items():
            parsed[exchange_name][market_id] = OrderBook(
                exchange=exchange,
                market_id=market_id,
                yes_asks=[
                    BookLevel(int(level["price_cents"]), Decimal(str(level["size"])))
                    for level in book.get("yes_asks", [])
                ],
                no_asks=[
                    BookLevel(int(level["price_cents"]), Decimal(str(level["size"])))
                    for level in book.get("no_asks", [])
                ],
            )
    return parsed
