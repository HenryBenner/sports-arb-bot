from __future__ import annotations

import json
import os
import subprocess
import time
from socket import timeout as SocketTimeout
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class HttpClient:
    def __init__(self, timeout: int = 30, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if params:
            url = f"{url}?{urlencode(params)}"
        request_headers = _default_headers()
        request_headers.update(headers or {})
        return self._json(Request(url, headers=request_headers, method="GET"), request_headers)

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json", **_default_headers()}
        request_headers.update(headers or {})
        return self._json(
            Request(url, data=body, headers=request_headers, method="POST"),
            request_headers,
        )

    def _json(self, request: Request, headers: dict[str, str]) -> Any:
        if os.name == "nt":
            return self._json_with_curl(request, headers)

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {exc.code} from {_redact_url(request.full_url)}: {detail}") from exc
            except (TimeoutError, SocketTimeout, URLError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
        raise RuntimeError(
            f"Could not reach {_redact_url(request.full_url)} after {self.retries + 1} attempts. "
            f"Last error: {last_error}"
        ) from last_error

    def _json_with_curl(self, request: Request, headers: dict[str, str]) -> Any:
        command = [
            "curl.exe",
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--location",
            "--ssl-no-revoke",
            "--tlsv1.2",
            "--max-time",
            str(self.timeout),
            "--retry",
            str(self.retries),
            "--retry-delay",
            "1",
            "--request",
            request.get_method(),
        ]
        for key, value in headers.items():
            command.extend(["--header", f"{key}: {value}"])
        if request.data is not None:
            command.extend(["--data-binary", "@-"])
        command.append(request.full_url)

        result = subprocess.run(
            command,
            input=request.data,
            capture_output=True,
            timeout=(self.timeout * (self.retries + 1)) + 5,
        )
        if result.returncode != 0:
            return self._json_with_powershell(request, headers, result)
        return json.loads(result.stdout.decode("utf-8"))

    def _json_with_powershell(
        self,
        request: Request,
        headers: dict[str, str],
        curl_result: subprocess.CompletedProcess,
    ) -> Any:
        if request.data is not None:
            detail = curl_result.stderr.decode("utf-8", errors="replace").strip()
            body = curl_result.stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Could not reach {_redact_url(request.full_url)} with curl.exe. "
                f"{detail or f'exit code {curl_result.returncode}'}"
                f"{'; body: ' + body if body else ''}"
            )
        header_expr = _powershell_header_expr(headers)
        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            (
                "[Net.ServicePointManager]::SecurityProtocol = "
                "[Net.SecurityProtocolType]::Tls12; "
                "$ProgressPreference='SilentlyContinue'; "
                "Invoke-RestMethod "
                f"-Uri {json.dumps(request.full_url)} "
                f"-TimeoutSec {self.timeout} "
                f"-Headers {header_expr} "
                "| ConvertTo-Json -Depth 100 -Compress"
            ),
        ]
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=self.timeout + 10,
        )
        if result.returncode != 0:
            curl_detail = curl_result.stderr.decode("utf-8", errors="replace").strip()
            ps_detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Could not reach {_redact_url(request.full_url)}. "
                f"curl.exe: {curl_detail or f'exit code {curl_result.returncode}'}; "
                f"PowerShell: {ps_detail or f'exit code {result.returncode}'}"
            )
        return json.loads(result.stdout.decode("utf-8-sig"))


def _default_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "User-Agent": "FirstBot/0.1 (+local arbitrage scanner)",
    }


def _powershell_header_expr(headers: dict[str, str]) -> str:
    pairs = []
    for key, value in headers.items():
        safe_key = key.replace("'", "''")
        safe_value = value.replace("'", "''")
        pairs.append(f"'{safe_key}'='{safe_value}'")
    return "@{" + "; ".join(pairs) + "}"


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    redacted = []
    sensitive = {"api_key", "key", "token", "secret", "passphrase"}
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in sensitive:
            redacted.append((key, "[redacted]"))
        else:
            redacted.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(redacted), parts.fragment))
