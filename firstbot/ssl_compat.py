from __future__ import annotations

import os
import ssl


def windows_truststore_context() -> ssl.SSLContext | None:
    if os.name != "nt":
        return None
    try:
        import truststore
    except ImportError:
        return None
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def websocket_ssl_context(url: str) -> ssl.SSLContext | None:
    if not url.lower().startswith("wss://"):
        return None
    return windows_truststore_context()
