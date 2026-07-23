from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from firstbot.config import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or derive Polymarket CLOB API credentials from POLYMARKET_PRIVATE_KEY."
    )
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="write POLYMARKET_API_KEY, POLYMARKET_API_SECRET, and POLYMARKET_API_PASSPHRASE into .env",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    host = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")
    if not private_key:
        print("POLYMARKET_PRIVATE_KEY is blank or missing in .env", file=sys.stderr)
        return 2

    signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "3"))
    if signature_type == 3:
        try:
            from py_clob_client_v2 import ClobClient
        except ImportError:
            print(
                "Missing py-clob-client-v2. Install dependencies first:\n"
                "  python -m pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 2
        client = ClobClient(host=host, key=private_key, chain_id=137)
        creds = client.create_or_derive_api_key()
    else:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.constants import POLYGON
        except ImportError:
            print(
                "Missing py-clob-client. Install dependencies first:\n"
                "  python -m pip install -r requirements.txt",
                file=sys.stderr,
            )
            return 2
        client = ClobClient(host, key=private_key, chain_id=POLYGON)
        creds = client.create_or_derive_api_creds()
    values = _creds_to_dict(creds)

    print("Derived Polymarket CLOB credentials:")
    print(f"POLYMARKET_API_KEY={values['POLYMARKET_API_KEY']}")
    print(f"POLYMARKET_API_SECRET={values['POLYMARKET_API_SECRET']}")
    print(f"POLYMARKET_API_PASSPHRASE={values['POLYMARKET_API_PASSPHRASE']}")
    print()
    print("Do not paste these into chat. Store them only in your local .env file.")

    if args.write_env:
        _update_env(ROOT / ".env", values)
        print(f"Updated {ROOT / '.env'}")

    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    if not funder:
        print()
        print(
            "POLYMARKET_FUNDER_ADDRESS is still blank. For new API users, Polymarket "
            "calls this the deposit wallet address. Existing users may use their "
            "current proxy/safe wallet address."
        )
    return 0


def _creds_to_dict(creds: Any) -> dict[str, str]:
    return {
        "POLYMARKET_API_KEY": _get(creds, "api_key", "apiKey", "key"),
        "POLYMARKET_API_SECRET": _get(creds, "api_secret", "apiSecret", "secret"),
        "POLYMARKET_API_PASSPHRASE": _get(creds, "api_passphrase", "apiPassphrase", "passphrase"),
    }


def _get(creds: Any, *names: str) -> str:
    value = None
    if isinstance(creds, dict):
        for name in names:
            value = creds.get(name)
            if value is not None:
                break
    else:
        for name in names:
            value = getattr(creds, name, None)
            if value is not None:
                break
    if value is None:
        raise RuntimeError(f"Polymarket SDK response did not include any of {', '.join(names)}")
    return str(value)


def _update_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    updated: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            updated.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in values:
            updated.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            updated.append(line)
    for key, value in values.items():
        if key not in seen:
            updated.append(f"{key}={value}")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
