from __future__ import annotations

import argparse
from pathlib import Path


ENV_NAMES = {
    "public": "POLYMARKET_US_KEY_ID",
    "puplic": "POLYMARKET_US_KEY_ID",
    "key": "POLYMARKET_US_KEY_ID",
    "key_id": "POLYMARKET_US_KEY_ID",
    "public_key": "POLYMARKET_US_KEY_ID",
    "puplic_key": "POLYMARKET_US_KEY_ID",
    "private": "POLYMARKET_US_SECRET_KEY",
    "private_key": "POLYMARKET_US_SECRET_KEY",
    "secret": "POLYMARKET_US_SECRET_KEY",
    "secret_key": "POLYMARKET_US_SECRET_KEY",
}


def parse_credentials(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        separators = [(line.find(char), char) for char in ("=", ":") if char in line]
        separator = min(separators)[1] if separators else None
        if separator is None:
            continue
        key, value = (part.strip() for part in line.split(separator, 1))
        env_name = ENV_NAMES.get(key.lower().replace(" ", "_"))
        if env_name and value:
            values[env_name] = value.strip('"\'')
    missing = {
        "POLYMARKET_US_KEY_ID", "POLYMARKET_US_SECRET_KEY"
    } - values.keys()
    if missing:
        raise RuntimeError("credential file is missing: " + ", ".join(sorted(missing)))
    return values


def update_env(path: Path, values: dict[str, str]) -> None:
    desired = {
        "POLYMARKET_VENUE": "us",
        "POLYMARKET_US_PUBLIC_URL": "https://gateway.polymarket.us",
        "POLYMARKET_US_API_URL": "https://api.polymarket.us",
        "POLYMARKET_US_WS_URL": "wss://api.polymarket.us/v1/ws/markets",
        **values,
    }
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    written: set[str] = set()
    result: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in desired:
            result.append(f"{key}={desired[key]}")
            written.add(key)
        else:
            result.append(line)
    if result and result[-1]:
        result.append("")
    for key, value in desired.items():
        if key not in written:
            result.append(f"{key}={value}")
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    args = parser.parse_args()
    update_env(args.env, parse_credentials(args.source))
    print("Polymarket US credentials imported into .env (values hidden).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
