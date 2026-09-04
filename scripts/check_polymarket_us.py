from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from firstbot.config import Settings
from firstbot.exchanges import PolymarketUSClient, create_polymarket_client


async def check_websocket(client: PolymarketUSClient, slug: str) -> None:
    websocket = client.market_websocket()
    await websocket.connect()
    try:
        await websocket.subscribe_market_data("firstbot-readiness", [slug])
        await asyncio.sleep(2)
    finally:
        await websocket.close()


def main() -> int:
    settings = Settings.from_env()
    client = create_polymarket_client(settings)
    if not isinstance(client, PolymarketUSClient):
        raise RuntimeError("POLYMARKET_VENUE is not set to us")
    client.validate_credentials_locally()
    data = client.get_markets(limit=20, active=True)
    markets = data.get("markets", []) if isinstance(data, dict) else []
    slug = next(
        (str(market.get("slug")) for market in markets if market.get("slug")),
        None,
    )
    if not slug:
        raise RuntimeError("Polymarket US returned no active market slug")
    client.get_market_by_slug(slug)
    client._client().markets.book(slug)
    client.available_cash_usd()
    asyncio.run(check_websocket(client, slug))
    print("Polymarket US credentials, REST market data, balance, and WebSocket OK")
    print("No order endpoint was called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
