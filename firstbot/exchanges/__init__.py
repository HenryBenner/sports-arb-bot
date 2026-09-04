from .kalshi import KalshiClient
from .polymarket import PolymarketClient
from .polymarket_us import PolymarketUSClient


def create_polymarket_client(settings, http=None):
    if settings.polymarket_venue == "us":
        return PolymarketUSClient(
            public_url=settings.polymarket_us_public_url,
            api_url=settings.polymarket_us_api_url,
            websocket_url=settings.polymarket_us_ws_url,
            key_id=settings.polymarket_us_key_id,
            secret_key=settings.polymarket_us_secret_key,
            timeout=settings.http_timeout_seconds,
        )
    if settings.polymarket_venue != "global":
        raise RuntimeError("POLYMARKET_VENUE must be either 'global' or 'us'")
    return PolymarketClient(
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


__all__ = [
    "KalshiClient", "PolymarketClient", "PolymarketUSClient",
    "create_polymarket_client",
]
