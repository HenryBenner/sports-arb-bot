from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def _decimal_env(name: str, default: str) -> Decimal:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return Decimal(default)
    return Decimal(value)


@dataclass(frozen=True)
class Settings:
    live_trading: bool
    min_profit_cents: int
    max_leg_usd: int
    slippage_cents: int
    fee_buffer_cents: int
    http_timeout_seconds: int
    kalshi_base_url: str
    kalshi_api_key_id: str | None
    kalshi_private_key_path: str | None
    kalshi_fee_rate: Decimal
    polymarket_gamma_url: str
    polymarket_clob_url: str
    polymarket_private_key: str | None
    polymarket_api_key: str | None
    polymarket_api_secret: str | None
    polymarket_api_passphrase: str | None
    polymarket_funder_address: str | None
    polymarket_signature_type: int
    polymarket_fee_rate: Decimal
    predictionhunt_base_url: str
    predictionhunt_api_key: str | None
    predictionhunt_arbs_path: str
    predictionhunt_ev_path: str
    ev_trade_usd: Decimal
    ev_max_trade_usd: Decimal
    ev_min_edge_pct: Decimal
    trigger_cost_cents: int
    near_miss_cost_cents: int
    hot_window_seconds: int
    predictionhunt_poll_seconds: int
    max_days_to_resolution: int
    prefer_same_day: bool
    book_stale_ms: int
    max_active_watches: int
    hot_fast_path: bool = True
    hot_fast_max_total_usd: Decimal = Decimal("20")
    hot_fast_max_book_age_ms: int = 1000
    hot_fast_min_net_edge_cents: Decimal = Decimal("0")
    hot_require_cross_50: bool = True
    hot_require_source_price_alignment: bool = True
    hot_source_price_max_deviation_cents: Decimal = Decimal("10")
    hot_allowed_event_types: tuple[str, ...] = ("sports", "esports")
    hot_geoblock_check: bool = True
    startup_readiness: bool = True
    readiness_seconds: int = 10
    readiness_kalshi_ticker: str | None = None
    readiness_polymarket_token: str | None = None
    predictionhunt_ws_url: str | None = None
    predictionhunt_signal_channels: tuple[str, ...] = ("smart_money", "fade_finder")
    signal_no_min_cents: int = 55
    signal_no_max_cents: int = 75
    signal_max_chase_cents: int = 3
    signal_min_depth_usd: Decimal = Decimal("5")
    signal_max_spread_cents: int = 5
    signal_min_score: int = 70
    signal_min_ev_cents: Decimal = Decimal("1")
    signal_cooldown_seconds: int = 900
    signal_daily_loss_limit_usd: Decimal = Decimal("25")
    signal_paper_trade_usd: Decimal = Decimal("100")
    signal_paper_min_trade_usd: Decimal = Decimal("1")
    signal_paper_max_trade_usd: Decimal = Decimal("100")
    signal_live_trade_usd: Decimal = Decimal("5")
    signal_min_depth_multiple: Decimal = Decimal("3")
    signal_paper_min_score: int = 60
    signal_paper_max_spread_cents: int = 5
    signal_paper_max_chase_cents: int = 5
    signal_paper_min_ev_cents: Decimal = Decimal("-2.5")
    signal_paper_enforce_cooldown: bool = False
    manual_arb_min_net_edge_cents: Decimal = Decimal("1")
    manual_arb_max_usd: Decimal = Decimal("5")
    manual_arb_max_contracts: int = 5
    manual_arb_max_spread_cents: int = 5
    manual_arb_max_unhedged_usd: Decimal = Decimal("0")
    manual_arb_max_failed_orders: int = 1
    manual_arb_max_partial_fills: int = 0
    manual_arb_daily_stop_loss_usd: Decimal = Decimal("25")
    polymarket_relayer_url: str | None = None
    polymarket_builder_api_key: str | None = None
    polymarket_builder_secret: str | None = None
    polymarket_builder_passphrase: str | None = None
    polymarket_deposit_wallet_factory: str = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            live_trading=_bool_env("BOT_LIVE_TRADING", False),
            min_profit_cents=_int_env("BOT_MIN_PROFIT_CENTS", 1),
            max_leg_usd=_int_env("BOT_MAX_LEG_USD", 5),
            slippage_cents=_int_env("BOT_SLIPPAGE_CENTS", 0),
            fee_buffer_cents=_int_env("BOT_FEE_BUFFER_CENTS", 0),
            http_timeout_seconds=_int_env("BOT_HTTP_TIMEOUT_SECONDS", 30),
            kalshi_base_url=os.getenv(
                "KALSHI_BASE_URL",
                "https://external-api.demo.kalshi.co/trade-api/v2",
            ).rstrip("/"),
            kalshi_api_key_id=os.getenv("KALSHI_API_KEY_ID") or None,
            kalshi_private_key_path=os.getenv("KALSHI_PRIVATE_KEY_PATH") or None,
            kalshi_fee_rate=_decimal_env("KALSHI_FEE_RATE", "0.07"),
            polymarket_gamma_url=os.getenv(
                "POLYMARKET_GAMMA_URL",
                "https://gamma-api.polymarket.com",
            ).rstrip("/"),
            polymarket_clob_url=os.getenv(
                "POLYMARKET_CLOB_URL",
                "https://clob.polymarket.com",
            ).rstrip("/"),
            polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY") or None,
            polymarket_api_key=os.getenv("POLYMARKET_API_KEY") or None,
            polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET") or None,
            polymarket_api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE") or None,
            polymarket_funder_address=os.getenv("POLYMARKET_FUNDER_ADDRESS") or None,
            polymarket_signature_type=_int_env("POLYMARKET_SIGNATURE_TYPE", 3),
            polymarket_relayer_url=os.getenv("POLYMARKET_RELAYER_URL") or os.getenv("RELAYER_URL") or None,
            polymarket_builder_api_key=os.getenv("POLYMARKET_BUILDER_API_KEY") or os.getenv("BUILDER_API_KEY") or None,
            polymarket_builder_secret=os.getenv("POLYMARKET_BUILDER_SECRET") or os.getenv("BUILDER_SECRET") or None,
            polymarket_builder_passphrase=os.getenv("POLYMARKET_BUILDER_PASSPHRASE") or os.getenv("BUILDER_PASS_PHRASE") or None,
            polymarket_deposit_wallet_factory=os.getenv(
                "POLYMARKET_DEPOSIT_WALLET_FACTORY",
                "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07",
            ),
            polymarket_fee_rate=_decimal_env("POLYMARKET_FEE_RATE", "0.05"),
            predictionhunt_base_url=os.getenv(
                "PREDICTIONHUNT_BASE_URL",
                "https://www.predictionhunt.com",
            ).rstrip("/"),
            predictionhunt_api_key=os.getenv("PREDICTIONHUNT_API_KEY") or None,
            predictionhunt_arbs_path=os.getenv(
                "PREDICTIONHUNT_ARBS_PATH",
                "/api/v2/arb",
            ),
            predictionhunt_ev_path=os.getenv(
                "PREDICTIONHUNT_EV_PATH",
                "/api/v2/ev",
            ),
            ev_trade_usd=_decimal_env("BOT_EV_TRADE_USD", "5"),
            ev_max_trade_usd=_decimal_env("BOT_EV_MAX_TRADE_USD", "25"),
            ev_min_edge_pct=_decimal_env("BOT_EV_MIN_EDGE_PCT", "0"),
            trigger_cost_cents=_int_env("BOT_TRIGGER_COST_CENTS", 99),
            near_miss_cost_cents=_int_env("BOT_NEAR_MISS_COST_CENTS", 100),
            hot_window_seconds=_int_env("BOT_HOT_WINDOW_SECONDS", 600),
            predictionhunt_poll_seconds=_int_env("BOT_PREDICTIONHUNT_POLL_SECONDS", 30),
            max_days_to_resolution=_int_env("BOT_MAX_DAYS_TO_RESOLUTION", 3),
            prefer_same_day=_bool_env("BOT_PREFER_SAME_DAY", True),
            book_stale_ms=_int_env("BOT_BOOK_STALE_MS", 1000),
            max_active_watches=_int_env("BOT_MAX_ACTIVE_WATCHES", 250),
            hot_fast_path=_bool_env("BOT_HOT_FAST_PATH", True),
            hot_fast_max_total_usd=_decimal_env("BOT_HOT_FAST_MAX_TOTAL_USD", "20"),
            hot_fast_max_book_age_ms=_int_env("BOT_HOT_FAST_MAX_BOOK_AGE_MS", 1000),
            hot_fast_min_net_edge_cents=_decimal_env("BOT_HOT_FAST_MIN_NET_EDGE_CENTS", "0"),
            hot_require_cross_50=_bool_env("BOT_HOT_REQUIRE_CROSS_50", True),
            hot_require_source_price_alignment=_bool_env(
                "BOT_HOT_REQUIRE_SOURCE_PRICE_ALIGNMENT",
                True,
            ),
            hot_source_price_max_deviation_cents=_decimal_env(
                "BOT_HOT_SOURCE_PRICE_MAX_DEVIATION_CENTS",
                "10",
            ),
            hot_allowed_event_types=_channels_env(
                "BOT_HOT_ALLOWED_EVENT_TYPES",
                ("sports", "esports"),
            ),
            hot_geoblock_check=_bool_env("BOT_HOT_GEOBLOCK_CHECK", True),
            startup_readiness=_bool_env("BOT_STARTUP_READINESS", True),
            readiness_seconds=_int_env("BOT_READINESS_SECONDS", 10),
            readiness_kalshi_ticker=os.getenv("BOT_READINESS_KALSHI_TICKER") or None,
            readiness_polymarket_token=os.getenv("BOT_READINESS_POLYMARKET_TOKEN") or None,
            predictionhunt_ws_url=os.getenv("PREDICTIONHUNT_WS_URL") or None,
            predictionhunt_signal_channels=_channels_env(
                "PREDICTIONHUNT_SIGNAL_CHANNELS",
                ("smart_money", "fade_finder"),
            ),
            signal_no_min_cents=_int_env(
                "BOT_SIGNAL_PRICE_MIN_CENTS",
                _int_env("BOT_SIGNAL_NO_MIN_CENTS", 55),
            ),
            signal_no_max_cents=_int_env(
                "BOT_SIGNAL_PRICE_MAX_CENTS",
                _int_env("BOT_SIGNAL_NO_MAX_CENTS", 75),
            ),
            signal_max_chase_cents=_int_env("BOT_SIGNAL_MAX_CHASE_CENTS", 3),
            signal_min_depth_usd=_decimal_env("BOT_SIGNAL_MIN_DEPTH_USD", "5"),
            signal_max_spread_cents=_int_env("BOT_SIGNAL_MAX_SPREAD_CENTS", 5),
            signal_min_score=_int_env("BOT_SIGNAL_MIN_SCORE", 70),
            signal_min_ev_cents=_decimal_env("BOT_SIGNAL_MIN_EV_CENTS", "1"),
            signal_cooldown_seconds=_int_env("BOT_SIGNAL_COOLDOWN_SECONDS", 900),
            signal_daily_loss_limit_usd=_decimal_env("BOT_SIGNAL_DAILY_LOSS_LIMIT_USD", "25"),
            signal_paper_trade_usd=_decimal_env("BOT_SIGNAL_PAPER_TRADE_USD", "100"),
            signal_paper_min_trade_usd=_decimal_env("BOT_SIGNAL_PAPER_MIN_TRADE_USD", "1"),
            signal_paper_max_trade_usd=_decimal_env("BOT_SIGNAL_PAPER_MAX_TRADE_USD", "100"),
            signal_live_trade_usd=_decimal_env("BOT_SIGNAL_LIVE_TRADE_USD", "5"),
            signal_min_depth_multiple=_decimal_env("BOT_SIGNAL_MIN_DEPTH_MULTIPLE", "3"),
            signal_paper_min_score=_int_env("BOT_SIGNAL_PAPER_MIN_SCORE", 60),
            signal_paper_max_spread_cents=_int_env("BOT_SIGNAL_PAPER_MAX_SPREAD_CENTS", 5),
            signal_paper_max_chase_cents=_int_env("BOT_SIGNAL_PAPER_MAX_CHASE_CENTS", 5),
            signal_paper_min_ev_cents=_decimal_env("BOT_SIGNAL_PAPER_MIN_EV_CENTS", "-2.5"),
            signal_paper_enforce_cooldown=_bool_env("BOT_SIGNAL_PAPER_ENFORCE_COOLDOWN", False),
            manual_arb_min_net_edge_cents=_decimal_env("BOT_MANUAL_ARB_MIN_NET_EDGE_CENTS", "1"),
            manual_arb_max_usd=_decimal_env("BOT_MANUAL_ARB_MAX_USD", "5"),
            manual_arb_max_contracts=_int_env("BOT_MANUAL_ARB_MAX_CONTRACTS", 5),
            manual_arb_max_spread_cents=_int_env("BOT_MANUAL_ARB_MAX_SPREAD_CENTS", 5),
            manual_arb_max_unhedged_usd=_decimal_env("BOT_MANUAL_ARB_MAX_UNHEDGED_USD", "0"),
            manual_arb_max_failed_orders=_int_env("BOT_MANUAL_ARB_MAX_FAILED_ORDERS", 1),
            manual_arb_max_partial_fills=_int_env("BOT_MANUAL_ARB_MAX_PARTIAL_FILLS", 0),
            manual_arb_daily_stop_loss_usd=_decimal_env("BOT_MANUAL_ARB_DAILY_STOP_LOSS_USD", "25"),
        )


def _channels_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    channels = tuple(item.strip() for item in value.split(",") if item.strip())
    return channels or default
