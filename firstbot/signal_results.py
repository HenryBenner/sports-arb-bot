from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import Side


@dataclass(frozen=True)
class Settlement:
    status: str
    resolved_outcome: str
    winning: bool | None
    note: str = ""


@dataclass(frozen=True)
class SignalResultSummary:
    total_rows: int
    updated_rows: int
    pending_rows: int
    error_rows: int
    backup_path: Path | None


RESULT_HEADERS = [
    "result_status",
    "resolved_outcome",
    "exit_value_usd",
    "realized_pnl_usd",
    "notes",
]


class SignalResultUpdater:
    def __init__(self, kalshi, polymarket, log_dir: str | Path = "logs") -> None:
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.log_dir = Path(log_dir)
        self.signal_index = self._load_signal_index()

    def update_csv(
        self,
        csv_path: str | Path | None = None,
        backup: bool = True,
    ) -> SignalResultSummary:
        path = Path(csv_path) if csv_path else self.log_dir / "signal_paper_trades.csv"
        if not path.exists():
            raise RuntimeError(f"{path} does not exist")
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = list(reader.fieldnames or [])
            rows = list(reader)
        for header in RESULT_HEADERS:
            if header not in headers:
                headers.append(header)
        backup_path = None
        if backup:
            backup_path = path.with_suffix(path.suffix + f".bak-results-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            backup_path.write_bytes(path.read_bytes())

        updated = 0
        pending = 0
        errors = 0
        output_rows: list[dict[str, Any]] = []
        for row in rows:
            normalized = {header: row.get(header, "") for header in headers}
            if (normalized.get("result_status") or "").strip().lower() not in {"", "pending", "open"}:
                output_rows.append(normalized)
                continue
            try:
                settlement = self._settlement_for_row(normalized)
            except Exception as exc:
                errors += 1
                normalized["result_status"] = "pending"
                normalized["notes"] = _append_note(normalized.get("notes", ""), f"settlement_error: {exc}")
                output_rows.append(normalized)
                continue
            if settlement.status == "pending":
                pending += 1
                normalized["result_status"] = "pending"
                if settlement.note:
                    normalized["notes"] = _append_note(normalized.get("notes", ""), settlement.note)
                output_rows.append(normalized)
                continue
            self._apply_settlement(normalized, settlement)
            updated += 1
            output_rows.append(normalized)

        try:
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(output_rows)
        except PermissionError as exc:
            raise RuntimeError(f"Could not write {path}; close it in Excel or any CSV viewer and rerun") from exc
        return SignalResultSummary(len(rows), updated, pending, errors, backup_path)

    def _settlement_for_row(self, row: dict[str, Any]) -> Settlement:
        exchange = (row.get("exchange") or "").strip().lower()
        if exchange == "polymarket":
            return self._polymarket_settlement(row)
        if exchange == "kalshi":
            return self._kalshi_settlement(row)
        return Settlement("pending", "", None, f"unsupported exchange for settlement: {exchange}")

    def _polymarket_settlement(self, row: dict[str, Any]) -> Settlement:
        market_id = (row.get("market_id") or "").strip()
        source_id = self.signal_index.get(_row_key(row), {}).get("signal_market_id") or market_id
        try:
            market = self.polymarket._gamma_market(source_id)
        except Exception:
            market = self.polymarket._gamma_market(market_id)
        closed = _truthy(market.get("closed")) or str(market.get("status") or "").lower() in {"resolved", "closed", "settled"}
        outcomes = _json_list(market.get("outcomes"))
        token_ids = [str(item) for item in _json_list(market.get("clobTokenIds") or market.get("clob_token_ids"))]
        prices = [_decimal_or_none(item) for item in _json_list(market.get("outcomePrices") or market.get("outcome_prices"))]

        if token_ids and prices and market_id in token_ids:
            index = token_ids.index(market_id)
            if prices[index] == Decimal("1"):
                return Settlement("won", _outcome_name(outcomes, index, row), True)
            if prices[index] == Decimal("0") and any(price == Decimal("1") for price in prices if price is not None):
                winner_index = next(i for i, price in enumerate(prices) if price == Decimal("1"))
                return Settlement("lost", _outcome_name(outcomes, winner_index, row), False)

        direct = _direct_outcome(market)
        if direct is not None:
            side = (row.get("side") or "").strip().lower()
            won = direct == side
            return Settlement("won" if won else "lost", direct, won)
        if not closed:
            return Settlement("pending", "", None, "market not closed")
        return Settlement("pending", "", None, "closed market but resolved outcome was unclear")

    def _kalshi_settlement(self, row: dict[str, Any]) -> Settlement:
        ticker = (row.get("market_id") or "").strip()
        response = self.kalshi.get_markets(ticker=ticker, limit=1)
        markets = response.get("markets", []) if isinstance(response, dict) else []
        market = next((item for item in markets if str(item.get("ticker") or "").upper() == ticker.upper()), markets[0] if markets else {})
        direct = _direct_outcome(market)
        value = _decimal_or_none(
            market.get("settlement_value")
            or market.get("expiration_value")
            or market.get("result_value")
        )
        if direct is None and value is not None:
            if value > Decimal("1"):
                value = value / Decimal("100")
            direct = "yes" if value >= Decimal("0.5") else "no"
        if direct is not None:
            side = (row.get("side") or "").strip().lower()
            won = direct == side
            return Settlement("won" if won else "lost", direct, won)
        status = str(market.get("status") or "").lower()
        if status not in {"closed", "settled", "resolved", "finalized"}:
            return Settlement("pending", "", None, "market not settled")
        return Settlement("pending", "", None, "settled market but resolved outcome was unclear")

    def _apply_settlement(self, row: dict[str, Any], settlement: Settlement) -> None:
        contracts = _decimal(row.get("contracts"))
        stake = _decimal(row.get("stake_usd"))
        if settlement.status == "won":
            exit_value = contracts
        elif settlement.status == "lost":
            exit_value = Decimal("0")
        elif settlement.status == "void":
            exit_value = stake
        else:
            exit_value = Decimal("0")
        row["result_status"] = settlement.status
        row["resolved_outcome"] = settlement.resolved_outcome
        row["exit_value_usd"] = _money(exit_value)
        row["realized_pnl_usd"] = _money(exit_value - stake)
        if settlement.note:
            row["notes"] = _append_note(row.get("notes", ""), settlement.note)

    def _load_signal_index(self) -> dict[str, dict[str, str]]:
        path = self.log_dir / "signal_paper_trades.jsonl"
        index: dict[str, dict[str, str]] = {}
        if not path.exists():
            return index
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            evaluation = record.get("evaluation")
            signal = record.get("signal")
            if not isinstance(evaluation, dict) or not isinstance(signal, dict):
                continue
            market_id = str(evaluation.get("market_id") or "")
            side = str(signal.get("side") or "")
            if market_id and side:
                index[f"{market_id}|{side}".lower()] = {
                    "signal_market_id": str(signal.get("market_id") or ""),
                    "outcome": str(signal.get("outcome") or ""),
                }
        return index


def _row_key(row: dict[str, Any]) -> str:
    return f"{row.get('market_id', '')}|{row.get('side', '')}".lower()


def _direct_outcome(market: dict[str, Any]) -> str | None:
    for key in ("resolved_outcome", "resolvedOutcome", "winning_outcome", "winningOutcome", "winner", "result", "outcome"):
        value = market.get(key)
        if value is None or value == "":
            continue
        text = str(value).strip().lower()
        if text in {"yes", "y", "true", "1"}:
            return Side.YES.value
        if text in {"no", "n", "false", "0"}:
            return Side.NO.value
    return None


def _outcome_name(outcomes: list[Any], index: int, row: dict[str, Any]) -> str:
    if 0 <= index < len(outcomes):
        return str(outcomes[index])
    return str(row.get("side") or "")


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal(value: Any) -> Decimal:
    return _decimal_or_none(value) or Decimal("0")


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "closed", "resolved"}


def _append_note(existing: str | None, note: str) -> str:
    existing = (existing or "").strip()
    if not existing:
        return note
    if note in existing:
        return existing
    return f"{existing}; {note}"
