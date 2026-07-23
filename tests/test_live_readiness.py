from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from firstbot.config import Settings
from firstbot.models import BookLevel, Exchange, OrderBook, Side
from firstbot.predictionhunt import PredictionHuntLeg, PredictionHuntOpportunity
from firstbot.readiness import (
    LiveReadinessChecker,
    check_kalshi_signing_dependencies,
    preflight_hot_candidate,
)


def settings(
    live: bool = True,
    key_path: str | None = "key.pem",
    polymarket_token: str | None = "PREADY",
) -> Settings:
    return Settings(
        live_trading=live,
        min_profit_cents=1,
        max_leg_usd=5,
        slippage_cents=0,
        fee_buffer_cents=0,
        http_timeout_seconds=30,
        kalshi_base_url="https://kalshi.example.test",
        kalshi_api_key_id="kalshi-key",
        kalshi_private_key_path=key_path,
        kalshi_fee_rate=Decimal("0.07"),
        polymarket_gamma_url="https://gamma.example.test",
        polymarket_clob_url="https://clob.example.test",
        polymarket_private_key="poly-private",
        polymarket_api_key="poly-key",
        polymarket_api_secret="poly-secret",
        polymarket_api_passphrase="poly-pass",
        polymarket_funder_address="0xabc",
        polymarket_signature_type=3,
        polymarket_fee_rate=Decimal("0"),
        predictionhunt_base_url="https://predictionhunt.example.test",
        predictionhunt_api_key="ph-key",
        predictionhunt_arbs_path="/api/v2/arb",
        predictionhunt_ev_path="/api/v2/ev",
        ev_trade_usd=Decimal("5"),
        ev_max_trade_usd=Decimal("25"),
        ev_min_edge_pct=Decimal("0"),
        trigger_cost_cents=99,
        near_miss_cost_cents=100,
        hot_window_seconds=600,
        predictionhunt_poll_seconds=30,
        max_days_to_resolution=3,
        prefer_same_day=True,
        book_stale_ms=1000,
        max_active_watches=20,
        startup_readiness=True,
        readiness_seconds=1,
        readiness_kalshi_ticker="KREADY",
        readiness_polymarket_token=polymarket_token,
    )


class ReadyKalshi:
    base_url = "https://kalshi.example.test"

    def __init__(self) -> None:
        self.api_key_id = "kalshi-key"
        self.private_key_path = "key.pem"
        self.order_calls = 0

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        return {"KALSHI-ACCESS-KEY": "ok"}

    def available_cash_usd(self) -> Decimal:
        return Decimal("12.34")

    def get_markets(self, **params):
        return {"markets": [{"ticker": "KREADY"}]}

    def get_orderbook(self, ticker: str) -> OrderBook:
        return OrderBook(
            Exchange.KALSHI,
            ticker,
            yes_asks=[BookLevel(45, Decimal("10"))],
            no_asks=[BookLevel(55, Decimal("10"))],
        )

    def create_order(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("readiness must not create Kalshi orders")


class ReadyHttp:
    def get_json(self, url: str, params=None):
        if url == "https://polymarket.com/api/geoblock":
            return {"blocked": False}
        return {"markets": [{"clobTokenIds": '["PREADY","POTHER"]'}]}


class ReadyPolymarket:
    gamma_url = "https://gamma.example.test"
    clob_url = "https://clob.example.test"

    def __init__(self) -> None:
        self.http = ReadyHttp()
        self.order_calls = 0

    def _client_and_types(self):
        return object(), {}

    def available_cash_usd(self) -> Decimal:
        return Decimal("47.79")

    def get_events(self, **params):
        return {"events": [{"markets": [{"clobTokenIds": '["PREADY","POTHER"]'}]}]}

    def get_token_ask_levels(self, token_id: str) -> list[BookLevel]:
        return [BookLevel(44, Decimal("9"))]

    def buy(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("readiness must not create Polymarket orders")


async def _fake_kalshi_ws(self, ticker: str) -> str:
    return ticker


async def _fake_polymarket_ws(self, token: str) -> str:
    return token


def opportunity() -> PredictionHuntOpportunity:
    return PredictionHuntOpportunity(
        group_id=1,
        group_title="Readiness Game",
        event_date="2026-07-09T20:00:00Z",
        event_type="sports",
        roi_pct=Decimal("1"),
        total_cost=Decimal("95"),
        max_wager_usd=Decimal("10"),
        detected_at=None,
        legs=(
            PredictionHuntLeg(
                side=Side.YES,
                platform=Exchange.KALSHI,
                market_id="KREADY",
                source_url=None,
                price=Decimal("45"),
                liquidity_usd=Decimal("10"),
                fee_usd=Decimal("0"),
            ),
            PredictionHuntLeg(
                side=Side.NO,
                platform=Exchange.POLYMARKET,
                market_id="PREADY",
                source_url=None,
                price=Decimal("50"),
                liquidity_usd=Decimal("10"),
                fee_usd=Decimal("0"),
            ),
        ),
        raw={},
    )


class LiveReadinessTests(unittest.TestCase):
    def test_hard_block_when_cffi_backend_is_missing(self):
        def missing_cffi(name: str):
            if name == "_cffi_backend":
                exc = ImportError("No module named _cffi_backend")
                exc.name = "_cffi_backend"
                raise exc
            return object()

        with self.assertRaisesRegex(RuntimeError, "kalshi_signing_dependency_missing: _cffi_backend"):
            check_kalshi_signing_dependencies(missing_cffi)

    def test_bad_kalshi_private_key_path_is_specific(self):
        checker = LiveReadinessChecker(settings(key_path="missing.pem"), ReadyKalshi(), ReadyPolymarket())

        with self.assertRaisesRegex(RuntimeError, "kalshi_private_key_file_missing: missing.pem"):
            checker._check_kalshi_signing()

    def test_failed_kalshi_signing_is_specific(self):
        class BadSigningKalshi(ReadyKalshi):
            def _auth_headers(self, method: str, path: str):
                raise RuntimeError("signing failed")

        with tempfile.TemporaryDirectory() as tmp:
            key_path = str(Path(tmp) / "key.pem")
            Path(key_path).write_text("not used", encoding="utf-8")
            checker = LiveReadinessChecker(settings(key_path=key_path), BadSigningKalshi(), ReadyPolymarket())

            with self.assertRaisesRegex(RuntimeError, "signing failed"):
                checker._check_kalshi_signing()

    def test_failed_kalshi_balance_is_specific(self):
        class BadBalanceKalshi(ReadyKalshi):
            def available_cash_usd(self):
                raise RuntimeError("balance endpoint failed")

        checker = LiveReadinessChecker(settings(), BadBalanceKalshi(), ReadyPolymarket())

        with self.assertRaisesRegex(RuntimeError, "balance endpoint failed"):
            checker._check_kalshi_balance()

    def test_missing_polymarket_sdk_is_specific(self):
        checker = LiveReadinessChecker(settings(), ReadyKalshi(), ReadyPolymarket())

        with patch("firstbot.readiness.importlib.util.find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "polymarket_sdk_missing: py_clob_client_v2"):
                checker._check_polymarket_sdk()

    def test_failed_polymarket_balance_is_specific(self):
        class BadBalancePolymarket(ReadyPolymarket):
            def available_cash_usd(self):
                raise RuntimeError("Unauthorized/Invalid api key")

        checker = LiveReadinessChecker(settings(), ReadyKalshi(), BadBalancePolymarket())

        with self.assertRaisesRegex(RuntimeError, "Unauthorized/Invalid api key"):
            checker._check_polymarket_balance()

    def test_failed_polymarket_book_is_specific(self):
        class BadBookPolymarket(ReadyPolymarket):
            def get_token_ask_levels(self, token_id: str):
                raise RuntimeError("CLOB rejected token")

        checker = LiveReadinessChecker(settings(), ReadyKalshi(), BadBookPolymarket())

        with self.assertRaisesRegex(RuntimeError, "CLOB rejected token"):
            checker._check_polymarket_rest_book()

    def test_auto_polymarket_book_skips_stale_404_tokens(self):
        class MixedBookPolymarket(ReadyPolymarket):
            def get_events(self, **params):
                return {
                    "events": [
                        {
                            "markets": [
                                {"clobTokenIds": '["STALE404","LIVEBOOK"]'},
                            ]
                        }
                    ]
                }

            def _client_and_types(self):
                class Client:
                    def get_sampling_simplified_markets(self):
                        raise RuntimeError("sampling unavailable")

                return Client(), {}

            def get_token_ask_levels(self, token_id: str):
                if token_id == "STALE404":
                    raise RuntimeError("HTTP 404 from CLOB")
                if token_id == "LIVEBOOK":
                    return [BookLevel(44, Decimal("9"))]
                return []

        checker = LiveReadinessChecker(settings(polymarket_token=None), ReadyKalshi(), MixedBookPolymarket())

        self.assertEqual(checker._check_polymarket_rest_book(), "LIVEBOOK")

    def test_failed_kalshi_websocket_auth_is_specific(self):
        class BadAuthKalshi(ReadyKalshi):
            def _auth_headers(self, method: str, path: str):
                raise RuntimeError("websocket auth failed")

        checker = LiveReadinessChecker(settings(), BadAuthKalshi(), ReadyPolymarket())

        with patch("firstbot.readiness.check_websockets_dependency", return_value="OK"):
            with patch.dict("sys.modules", {"websockets": Mock()}):
                with self.assertRaisesRegex(RuntimeError, "websocket auth failed"):
                    import asyncio

                    asyncio.run(checker._check_kalshi_websocket("KREADY"))

    def test_geoblock_check_hard_blocks_restricted_region(self):
        class BlockedHttp:
            def get_json(self, url: str, params=None):
                return {"blocked": True, "country": "US", "region": "NY"}

        polymarket = ReadyPolymarket()
        polymarket.http = BlockedHttp()
        checker = LiveReadinessChecker(settings(), ReadyKalshi(), polymarket)

        with self.assertRaisesRegex(
            RuntimeError,
            "polymarket_geoblocked: trading restricted in your region",
        ):
            checker._check_polymarket_geoblock()

    def test_polymarket_websocket_readiness_retries_transient_handshake_timeout(self):
        checker = LiveReadinessChecker(settings(), ReadyKalshi(), ReadyPolymarket())
        attempts = {"count": 0}

        class FakeWebSocket:
            async def __aenter__(self):
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise TimeoutError("timed out during opening handshake")
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def send(self, message):
                return None

            async def recv(self):
                raise TimeoutError()

        class FakeWebsockets:
            @staticmethod
            def connect(*args, **kwargs):
                return FakeWebSocket()

        import asyncio

        with patch("firstbot.readiness.check_websockets_dependency", return_value="OK"):
            with patch.dict("sys.modules", {"websockets": FakeWebsockets}):
                result = asyncio.run(checker._check_polymarket_websocket("PREADY"))

        self.assertEqual(result, "PREADY")
        self.assertEqual(attempts["count"], 2)

    def test_readiness_never_calls_order_methods(self):
        kalshi = ReadyKalshi()
        polymarket = ReadyPolymarket()
        with tempfile.TemporaryDirectory() as tmp:
            key_path = str(Path(tmp) / "key.pem")
            Path(key_path).write_text("not used", encoding="utf-8")
            checker = LiveReadinessChecker(
                settings(key_path=key_path),
                kalshi,
                polymarket,
                log_dir=tmp,
            )
            with patch("firstbot.readiness.check_kalshi_signing_dependencies", return_value="OK"):
                with patch("firstbot.readiness.check_websockets_dependency", return_value="OK"):
                    with patch("firstbot.readiness.importlib.util.find_spec", return_value=object()):
                        with patch.object(LiveReadinessChecker, "_check_kalshi_websocket", _fake_kalshi_ws):
                            with patch.object(LiveReadinessChecker, "_check_polymarket_websocket", _fake_polymarket_ws):
                                records = checker.run(print_status=False)

        self.assertEqual(kalshi.order_calls, 0)
        self.assertEqual(polymarket.order_calls, 0)
        self.assertTrue(all(record.status == "ok" for record in records))

    def test_candidate_preflight_allows_healthy_legs(self):
        reason = preflight_hot_candidate(ReadyKalshi(), ReadyPolymarket(), opportunity())

        self.assertIsNone(reason)

    def test_candidate_preflight_blocks_bad_actual_market_leg(self):
        class BadKalshi(ReadyKalshi):
            def get_orderbook(self, ticker: str):
                raise RuntimeError("404 market not found")

        reason = preflight_hot_candidate(BadKalshi(), ReadyPolymarket(), opportunity())

        self.assertIn("kalshi_orderbook_unavailable KREADY", reason)
        self.assertIn("404 market not found", reason)

    def test_run_hot_arb_exits_before_polling_when_startup_readiness_fails(self):
        import firstbot.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            key_path = str(Path(tmp) / "key.pem")
            Path(key_path).write_text("not used", encoding="utf-8")
            ready_settings = settings(key_path=key_path)
            with patch.object(cli.Settings, "from_env", return_value=ready_settings):
                with patch.object(cli, "_deploy_guard", return_value=None):
                    with patch.object(cli, "PredictionHuntClient") as predictionhunt_cls:
                        with patch.object(cli.LiveReadinessChecker, "run", side_effect=RuntimeError("readiness bad")):
                            with self.assertRaisesRegex(RuntimeError, "readiness bad"):
                                cli.run_hot_arb(
                                    category=None,
                                    limit=1,
                                    predictionhunt_poll_seconds=30,
                                    hot_window_seconds=10,
                                    max_days_to_resolution=3,
                                    prefer_same_day=True,
                                    trigger_cost_cents=None,
                                    near_miss_cost_cents=None,
                                    book_stale_ms=None,
                                    max_active_watches=None,
                                    log_dir=tmp,
                                    execute=True,
                                    once=True,
                                )

        predictionhunt_cls.assert_not_called()

    def test_skip_startup_readiness_allows_polling_construction(self):
        import firstbot.cli as cli

        class EmptyPredictionHunt:
            def __init__(self, *args, **kwargs):
                pass

            def get_arbitrage_opportunities(self, *args, **kwargs):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            key_path = str(Path(tmp) / "key.pem")
            Path(key_path).write_text("not used", encoding="utf-8")
            ready_settings = settings(key_path=key_path)
            with patch.object(cli.Settings, "from_env", return_value=ready_settings):
                with patch.object(cli, "_deploy_guard", return_value=None):
                    with patch.object(cli, "PredictionHuntClient", EmptyPredictionHunt):
                        with patch.object(cli.LiveReadinessChecker, "run") as readiness_run:
                            with patch.object(
                                cli.LiveReadinessChecker,
                                "_check_polymarket_geoblock",
                                return_value="not blocked",
                            ):
                                code = cli.run_hot_arb(
                                    category=None,
                                    limit=1,
                                    predictionhunt_poll_seconds=30,
                                    hot_window_seconds=10,
                                    max_days_to_resolution=3,
                                    prefer_same_day=True,
                                    trigger_cost_cents=None,
                                    near_miss_cost_cents=None,
                                    book_stale_ms=None,
                                    max_active_watches=None,
                                    log_dir=tmp,
                                    execute=True,
                                    once=True,
                                    skip_startup_readiness=True,
                                )

        self.assertEqual(code, 0)
        readiness_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
