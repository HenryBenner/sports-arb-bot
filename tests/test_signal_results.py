from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from firstbot.signal_results import SignalResultUpdater


HEADERS = [
    "paper_trade_id",
    "exchange",
    "market_id",
    "side",
    "contracts",
    "stake_usd",
    "result_status",
    "resolved_outcome",
    "exit_value_usd",
    "realized_pnl_usd",
    "notes",
]


class FakePolymarket:
    def __init__(self, markets):
        self.markets = markets

    def _gamma_market(self, market_id):
        if market_id not in self.markets:
            raise RuntimeError(f"missing market {market_id}")
        return self.markets[market_id]


class FakeKalshi:
    def __init__(self, markets):
        self.markets = markets

    def get_markets(self, **params):
        ticker = params.get("ticker")
        market = self.markets.get(ticker)
        return {"markets": [] if market is None else [market]}


class SignalResultUpdaterTests(unittest.TestCase):
    def test_updates_polymarket_token_win_from_jsonl_source_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal_paper_trades.csv"
            self._write_rows(
                path,
                [
                    {
                        "paper_trade_id": "p1",
                        "exchange": "polymarket",
                        "market_id": "yes-token",
                        "side": "yes",
                        "contracts": "10",
                        "stake_usd": "4.00",
                        "result_status": "pending",
                    }
                ],
            )
            (Path(tmp) / "signal_paper_trades.jsonl").write_text(
                json.dumps(
                    {
                        "evaluation": {"market_id": "yes-token"},
                        "signal": {"market_id": "sluggy", "side": "yes"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            updater = SignalResultUpdater(
                FakeKalshi({}),
                FakePolymarket(
                    {
                        "sluggy": {
                            "closed": True,
                            "outcomes": '["Yes","No"]',
                            "clobTokenIds": '["yes-token","no-token"]',
                            "outcomePrices": '["1","0"]',
                        }
                    }
                ),
                log_dir=tmp,
            )

            summary = updater.update_csv(path, backup=False)
            row = self._read_rows(path)[0]

            self.assertEqual(summary.updated_rows, 1)
            self.assertEqual(row["result_status"], "won")
            self.assertEqual(row["resolved_outcome"], "Yes")
            self.assertEqual(row["exit_value_usd"], "10.00")
            self.assertEqual(row["realized_pnl_usd"], "6.00")

    def test_updates_polymarket_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal_paper_trades.csv"
            self._write_rows(
                path,
                [
                    {
                        "paper_trade_id": "p1",
                        "exchange": "polymarket",
                        "market_id": "yes-token",
                        "side": "yes",
                        "contracts": "5",
                        "stake_usd": "3.00",
                        "result_status": "pending",
                    }
                ],
            )
            updater = SignalResultUpdater(
                FakeKalshi({}),
                FakePolymarket(
                    {
                        "yes-token": {
                            "closed": True,
                            "outcomes": '["Yes","No"]',
                            "clobTokenIds": '["yes-token","no-token"]',
                            "outcomePrices": '["0","1"]',
                        }
                    }
                ),
                log_dir=tmp,
            )

            updater.update_csv(path, backup=False)
            row = self._read_rows(path)[0]

            self.assertEqual(row["result_status"], "lost")
            self.assertEqual(row["resolved_outcome"], "No")
            self.assertEqual(row["exit_value_usd"], "0.00")
            self.assertEqual(row["realized_pnl_usd"], "-3.00")

    def test_updates_kalshi_settlement_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal_paper_trades.csv"
            self._write_rows(
                path,
                [
                    {
                        "paper_trade_id": "k1",
                        "exchange": "kalshi",
                        "market_id": "KXTEST",
                        "side": "no",
                        "contracts": "2",
                        "stake_usd": "1.20",
                        "result_status": "pending",
                    }
                ],
            )
            updater = SignalResultUpdater(
                FakeKalshi({"KXTEST": {"ticker": "KXTEST", "status": "settled", "settlement_value": "0"}}),
                FakePolymarket({}),
                log_dir=tmp,
            )

            updater.update_csv(path, backup=False)
            row = self._read_rows(path)[0]

            self.assertEqual(row["result_status"], "won")
            self.assertEqual(row["resolved_outcome"], "no")
            self.assertEqual(row["realized_pnl_usd"], "0.80")

    def test_leaves_unclosed_market_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal_paper_trades.csv"
            self._write_rows(
                path,
                [
                    {
                        "paper_trade_id": "p1",
                        "exchange": "polymarket",
                        "market_id": "yes-token",
                        "side": "yes",
                        "contracts": "5",
                        "stake_usd": "3.00",
                        "result_status": "pending",
                    }
                ],
            )
            updater = SignalResultUpdater(
                FakeKalshi({}),
                FakePolymarket({"yes-token": {"closed": False}}),
                log_dir=tmp,
            )

            summary = updater.update_csv(path, backup=False)
            row = self._read_rows(path)[0]

            self.assertEqual(summary.pending_rows, 1)
            self.assertEqual(row["result_status"], "pending")
            self.assertIn("market not closed", row["notes"])

    def _write_rows(self, path: Path, rows: list[dict]):
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    def _read_rows(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))


if __name__ == "__main__":
    unittest.main()
