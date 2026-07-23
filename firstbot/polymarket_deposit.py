from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings


@dataclass(frozen=True)
class DepositWalletDeployment:
    expected_wallet: str
    confirmed: Any


def deploy_deposit_wallet(settings: Settings) -> DepositWalletDeployment:
    missing = _missing_deposit_wallet_settings(settings)
    if missing:
        raise RuntimeError("deposit wallet deploy guard failed: missing " + ", ".join(missing))
    try:
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig
    except ImportError as exc:
        raise RuntimeError(
            "Install py-builder-relayer-client and py-builder-signing-sdk to deploy a Polymarket deposit wallet"
        ) from exc

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=settings.polymarket_builder_api_key,
            secret=settings.polymarket_builder_secret,
            passphrase=settings.polymarket_builder_passphrase,
        )
    )
    relayer = RelayClient(
        settings.polymarket_relayer_url,
        137,
        settings.polymarket_private_key,
        builder_config,
    )
    expected_wallet = relayer.get_expected_deposit_wallet()
    response = relayer.deploy_deposit_wallet()
    confirmed = response.wait()
    return DepositWalletDeployment(str(expected_wallet), confirmed)


def _missing_deposit_wallet_settings(settings: Settings) -> list[str]:
    missing: list[str] = []
    checks = {
        "POLYMARKET_RELAYER_URL": settings.polymarket_relayer_url,
        "POLYMARKET_PRIVATE_KEY": settings.polymarket_private_key,
        "POLYMARKET_BUILDER_API_KEY": settings.polymarket_builder_api_key,
        "POLYMARKET_BUILDER_SECRET": settings.polymarket_builder_secret,
        "POLYMARKET_BUILDER_PASSPHRASE": settings.polymarket_builder_passphrase,
    }
    for name, value in checks.items():
        if not value:
            missing.append(name)
    return missing
