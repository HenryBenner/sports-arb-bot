import unittest
from decimal import Decimal
from types import SimpleNamespace

from firstbot.cli import run_predictionhunt, scan_predictionhunt
from firstbot.cli import _run
from firstbot.executor import TradeExecutor
from firstbot.models import ArbLeg, ArbOpportunity, BookLevel, Exchange, OrderBook, Side


class ReadyKalshi:
    def __init__(self, cash=Decimal("100")):
        self.cash = cash
        self.level = BookLevel(45, Decimal("3"))
        self.orders = []

    def supports_immediate_orders(self):
        return True

    def available_cash_usd(self):
        return self.cash

    def get_best_ask(self, ticker, side):
        return self.level

    def create_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"ok": True, "venue": "kalshi", "payload": kwargs}


class ReadyPolymarket:
    def __init__(self, cash=Decimal("100")):
        self.cash = cash
        self.orders = []

    def supports_immediate_orders(self):
        return True

    def available_cash_usd(self):
        return self.cash

    def buy(self, **kwargs):
        self.orders.append(kwargs)
        return {"ok": True, "venue": "polymarket", "payload": kwargs}


class RefreshingPolymarket(ReadyPolymarket):
    def __init__(self, cash=Decimal("100"), level=None):
        super().__init__(cash)
        self.level = level or BookLevel(45, Decimal("1"))

    def get_token_best_ask(self, token_id):
        return self.level


class LadderKalshi(ReadyKalshi):
    def __init__(self, levels, cash=Decimal("100")):
        super().__init__(cash)
        self.levels = levels

    def get_orderbook(self, ticker):
        return OrderBook(
            Exchange.KALSHI,
            ticker,
            yes_asks=self.levels if ticker == "K" else [],
            no_asks=self.levels if ticker != "K" else [],
        )


class LadderPolymarket(ReadyPolymarket):
    def __init__(self, levels, cash=Decimal("100")):
        super().__init__(cash)
        self.levels = levels

    def get_token_ask_levels(self, token_id):
        return self.levels


class FailingPolymarket(ReadyPolymarket):
    def buy(self, **kwargs):
        raise RuntimeError("second venue rejected order")


class DelayedPolymarket(ReadyPolymarket):
    def buy(self, **kwargs):
        self.orders.append(kwargs)
        raise RuntimeError(
            "polymarket_order_state_uncertain: delayed Polymarket FOK order "
            "0xpending was not confirmed filled within 12s"
        )


def executable_opportunity():
    return ArbOpportunity(
        pair_name="Deploy Test",
        buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 45, Decimal("3")),
        buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 45, Decimal("3")),
        gross_cost_cents=90,
        buffers_cents=4,
        net_profit_cents=6,
        executable=True,
        blockers=(),
    )


class DeployGuardTests(unittest.TestCase):
    def test_run_handles_keyboard_interrupt_cleanly(self):
        code = _run(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        self.assertEqual(code, 130)

    def test_old_predictionhunt_scan_execute_is_blocked_before_network(self):
        with self.assertRaisesRegex(RuntimeError, "only allowed from run-hot-arb"):
            scan_predictionhunt(category="sports", limit=1, execute=True)

    def test_old_predictionhunt_runner_execute_is_blocked_before_network(self):
        with self.assertRaisesRegex(RuntimeError, "only allowed from run-hot-arb"):
            run_predictionhunt(
                category="sports",
                limit=1,
                poll_seconds=10,
                max_days_to_resolution=3,
                min_profit_cents=5,
                log_dir="logs",
                execute=True,
                once=True,
            )

    def test_executor_blocks_unknown_workflow(self):
        executor = TradeExecutor(ReadyKalshi(), ReadyPolymarket())

        submitted, message = executor.execute(executable_opportunity(), workflow="scan-predictionhunt")

        self.assertFalse(submitted)
        self.assertIn("only allowed from run-hot-arb", message)

    def test_executor_allows_run_hot_arb_when_venues_ready(self):
        executor = TradeExecutor(ReadyKalshi(), ReadyPolymarket())

        submitted, message = executor.execute(executable_opportunity(), workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("orders submitted", message)

    def test_executor_places_polymarket_first_to_avoid_kalshi_only_fill(self):
        executor = TradeExecutor(ReadyKalshi(), FailingPolymarket())

        submitted, message = executor.execute(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("first leg failed before paired order submission", message)
        self.assertNotIn("manual review required", message)

    def test_executor_blocks_uncertain_polymarket_order_before_kalshi(self):
        kalshi = ReadyKalshi()
        polymarket = DelayedPolymarket()
        executor = TradeExecutor(kalshi, polymarket)

        submitted, message = executor.execute(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("first leg failed before paired order submission", message)
        self.assertIn("polymarket_order_state_uncertain", message)
        self.assertIn("0xpending", message)
        self.assertEqual(kalshi.orders, [])

    def test_executor_marks_manual_review_when_kalshi_fails_after_polymarket_fill(self):
        class FailingKalshi(ReadyKalshi):
            def create_order(self, **kwargs):
                self.orders.append(kwargs)
                raise RuntimeError("kalshi rejected FOK")

        polymarket = ReadyPolymarket()
        executor = TradeExecutor(FailingKalshi(), polymarket)

        submitted, message = executor.execute(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("second leg failed after first leg polymarket", message)
        self.assertIn("manual_review_required", message)
        self.assertEqual(len(polymarket.orders), 1)

    def test_executor_retries_exact_missing_kalshi_leg_after_polymarket_fill(self):
        class RetryKalshi(ReadyKalshi):
            def __init__(self):
                super().__init__()
                self.book_calls = 0

            def get_market(self, ticker):
                return {"ticker": ticker, "status": "open"}

            def get_orderbook(self, ticker):
                self.book_calls += 1
                if self.book_calls == 1:
                    return OrderBook(
                        Exchange.KALSHI,
                        ticker,
                        yes_asks=[BookLevel(45, Decimal("3"))],
                        no_asks=[],
                    )
                return OrderBook(
                    Exchange.KALSHI,
                    ticker,
                    yes_asks=[BookLevel(47, Decimal("3"))],
                    no_asks=[],
                )

            def create_order(self, **kwargs):
                self.orders.append(kwargs)
                if len(self.orders) == 1:
                    raise RuntimeError("kalshi rejected first FOK")
                return {"ok": True, "venue": "kalshi", "payload": kwargs}

        kalshi = RetryKalshi()
        polymarket = ReadyPolymarket()
        executor = TradeExecutor(
            kalshi,
            polymarket,
            settings=SimpleNamespace(
                hot_require_cross_50=False,
                hot_hedge_retry_attempts=2,
                hot_hedge_max_chase_cents=5,
                hot_hedge_retry_delay_seconds=Decimal("0"),
            ),
        )

        submitted, message = executor.execute_fast(executable_opportunity(), workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("missing leg completed after first-leg fill retry", message)
        self.assertEqual(len(polymarket.orders), 1)
        self.assertEqual(len(kalshi.orders), 2)
        self.assertEqual(kalshi.orders[1]["price_cents"], 47)
        self.assertIsNotNone(executor.last_submitted_opportunity)
        self.assertEqual(
            executor.last_submitted_opportunity.buy_yes.avg_price_cents,
            Decimal("47"),
        )

    def test_executor_does_not_retry_missing_leg_past_chase_cap(self):
        class ChaseTooFarKalshi(ReadyKalshi):
            def __init__(self):
                super().__init__()
                self.book_calls = 0

            def get_market(self, ticker):
                return {"ticker": ticker, "status": "open"}

            def get_orderbook(self, ticker):
                self.book_calls += 1
                if self.book_calls == 1:
                    return OrderBook(
                        Exchange.KALSHI,
                        ticker,
                        yes_asks=[BookLevel(45, Decimal("3"))],
                        no_asks=[],
                    )
                return OrderBook(
                    Exchange.KALSHI,
                    ticker,
                    yes_asks=[BookLevel(60, Decimal("3"))],
                    no_asks=[],
                )

            def create_order(self, **kwargs):
                self.orders.append(kwargs)
                raise RuntimeError("kalshi rejected first FOK")

        kalshi = ChaseTooFarKalshi()
        polymarket = ReadyPolymarket()
        executor = TradeExecutor(
            kalshi,
            polymarket,
            settings=SimpleNamespace(
                hot_require_cross_50=False,
                hot_hedge_retry_attempts=2,
                hot_hedge_max_chase_cents=5,
                hot_hedge_retry_delay_seconds=Decimal("0"),
            ),
        )

        submitted, message = executor.execute_fast(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("manual_review_required", message)
        self.assertEqual(len(polymarket.orders), 1)
        self.assertEqual(len(kalshi.orders), 1)

    def test_executor_blocks_before_first_order_when_polymarket_balance_is_low(self):
        kalshi = ReadyKalshi(cash=Decimal("100"))
        polymarket = ReadyPolymarket(cash=Decimal("0.10"))
        executor = TradeExecutor(kalshi, polymarket)

        submitted, message = executor.execute(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("polymarket_balance_insufficient before order submission", message)
        self.assertEqual(polymarket.orders, [])

    def test_executor_blocks_before_first_order_when_polymarket_balance_is_unavailable(self):
        class UnknownBalanceKalshi(ReadyKalshi):
            def available_cash_usd(self):
                raise RuntimeError("balance endpoint failed")

        class UnknownBalancePolymarket(ReadyPolymarket):
            def available_cash_usd(self):
                raise RuntimeError("balance endpoint failed")

        executor = TradeExecutor(UnknownBalanceKalshi(), UnknownBalancePolymarket())

        submitted, message = executor.execute(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("polymarket_balance_unavailable before order submission", message)

    def test_executor_blocks_polymarket_when_kalshi_market_is_inactive(self):
        class InactiveKalshi(ReadyKalshi):
            def get_market(self, ticker):
                return {"ticker": ticker, "status": "closed"}

        polymarket = ReadyPolymarket()
        executor = TradeExecutor(InactiveKalshi(), polymarket)

        submitted, message = executor.execute_fast(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("kalshi_market_not_active before polymarket order", message)
        self.assertEqual(polymarket.orders, [])

    def test_executor_blocks_polymarket_when_kalshi_fok_depth_is_insufficient(self):
        class ThinKalshi(ReadyKalshi):
            def get_market(self, ticker):
                return {"ticker": ticker, "status": "open"}

            def get_orderbook(self, ticker):
                return OrderBook(
                    Exchange.KALSHI,
                    ticker,
                    yes_asks=[BookLevel(45, Decimal("1"))],
                    no_asks=[],
                )

        polymarket = ReadyPolymarket()
        executor = TradeExecutor(ThinKalshi(), polymarket)

        submitted, message = executor.execute_fast(executable_opportunity(), workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("kalshi_fok_depth_insufficient before polymarket order", message)
        self.assertIn("needs 3 contracts <= 45c, available=1", message)
        self.assertEqual(polymarket.orders, [])

    def test_executor_fast_path_checks_balance_but_skips_rest_refresh(self):
        class NoSlowPathKalshi(ReadyKalshi):
            def get_best_ask(self, ticker, side):
                raise RuntimeError("REST refresh should not be called")

        class NoSlowPathPolymarket(ReadyPolymarket):
            def get_token_ask_levels(self, token_id):
                raise RuntimeError("REST refresh should not be called")

        executor = TradeExecutor(NoSlowPathKalshi(), NoSlowPathPolymarket())

        submitted, message = executor.execute_fast(executable_opportunity(), workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("fast path skipped balance checks and REST book refresh", message)
        self.assertIn("orders submitted", message)

    def test_executor_final_guard_blocks_same_side_of_fifty_before_any_order(self):
        class TrackingKalshi(ReadyKalshi):
            def __init__(self):
                super().__init__()
                self.calls = []

            def create_order(self, **kwargs):
                self.calls.append(kwargs)
                return super().create_order(**kwargs)

        class TrackingPolymarket(ReadyPolymarket):
            def __init__(self):
                super().__init__()
                self.calls = []

            def buy(self, **kwargs):
                self.calls.append(kwargs)
                return super().buy(**kwargs)

        opportunity = ArbOpportunity(
            pair_name="Same Side",
            buy_yes=ArbLeg(Exchange.POLYMARKET, "P", Side.YES, 19, Decimal("10")),
            buy_no=ArbLeg(Exchange.KALSHI, "K", Side.NO, 18, Decimal("10")),
            gross_cost_cents=37,
            buffers_cents=0,
            net_profit_cents=63,
            executable=True,
            blockers=(),
        )
        kalshi = TrackingKalshi()
        polymarket = TrackingPolymarket()
        executor = TradeExecutor(
            kalshi,
            polymarket,
            settings=SimpleNamespace(hot_require_cross_50=True),
        )

        submitted, message = executor.execute_fast(opportunity, workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("opposite sides of 50c", message)
        self.assertEqual(kalshi.calls, [])
        self.assertEqual(polymarket.calls, [])

    def test_executor_final_guard_allows_cross_fifty_pair(self):
        opportunity = ArbOpportunity(
            pair_name="Cross Fifty",
            buy_yes=ArbLeg(Exchange.POLYMARKET, "P", Side.YES, 40, Decimal("3")),
            buy_no=ArbLeg(Exchange.KALSHI, "K", Side.NO, 55, Decimal("3")),
            gross_cost_cents=95,
            buffers_cents=0,
            net_profit_cents=5,
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(
            ReadyKalshi(),
            ReadyPolymarket(),
            settings=SimpleNamespace(hot_require_cross_50=True),
        )

        submitted, message = executor.execute_fast(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("orders submitted", message)

    def test_executor_ignores_stale_source_prices_for_exact_legs(self):
        class TrackingKalshi(ReadyKalshi):
            def __init__(self):
                super().__init__()
                self.calls = []

            def create_order(self, **kwargs):
                self.calls.append(kwargs)
                return super().create_order(**kwargs)

        class TrackingPolymarket(ReadyPolymarket):
            def __init__(self):
                super().__init__()
                self.calls = []

            def buy(self, **kwargs):
                self.calls.append(kwargs)
                return super().buy(**kwargs)

        opportunity = ArbOpportunity(
            pair_name="Exact Legs With Stale Source Prices",
            buy_yes=ArbLeg(
                Exchange.POLYMARKET,
                "P",
                Side.YES,
                40,
                Decimal("3"),
                source_price_cents=Decimal("60"),
            ),
            buy_no=ArbLeg(
                Exchange.KALSHI,
                "K",
                Side.NO,
                60,
                Decimal("3"),
                source_price_cents=Decimal("30"),
            ),
            gross_cost_cents=100,
            buffers_cents=0,
            net_profit_cents=1,
            executable=True,
            blockers=(),
        )
        kalshi = TrackingKalshi()
        polymarket = TrackingPolymarket()
        executor = TradeExecutor(
            kalshi,
            polymarket,
            settings=SimpleNamespace(
                hot_require_cross_50=True,
                hot_require_source_price_alignment=True,
                hot_source_price_max_deviation_cents=Decimal("10"),
            ),
        )

        submitted, message = executor.execute_fast(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("orders submitted", message)
        self.assertEqual(len(kalshi.calls), 1)
        self.assertEqual(len(polymarket.calls), 1)

    def test_executor_source_price_guard_allows_matching_live_quotes(self):
        opportunity = ArbOpportunity(
            pair_name="Aligned Mapping",
            buy_yes=ArbLeg(
                Exchange.POLYMARKET,
                "P",
                Side.YES,
                61,
                Decimal("3"),
                source_price_cents=Decimal("60"),
            ),
            buy_no=ArbLeg(
                Exchange.KALSHI,
                "K",
                Side.NO,
                31,
                Decimal("3"),
                source_price_cents=Decimal("30"),
            ),
            gross_cost_cents=92,
            buffers_cents=0,
            net_profit_cents=8,
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(
            ReadyKalshi(),
            ReadyPolymarket(),
            settings=SimpleNamespace(
                hot_require_cross_50=True,
                hot_require_source_price_alignment=True,
                hot_source_price_max_deviation_cents=Decimal("10"),
            ),
        )

        submitted, message = executor.execute_fast(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("orders submitted", message)

    def test_executor_blocks_when_live_depth_cannot_fit_minimum_equal_batch(self):
        opportunity = ArbOpportunity(
            pair_name="Low Notional",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 20, Decimal("5")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 19, Decimal("5")),
            gross_cost_cents=39,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("61"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(
            ReadyKalshi(),
            RefreshingPolymarket(level=BookLevel(19, Decimal("5"))),
        )
        executor.kalshi.level = BookLevel(20, Decimal("5"))

        submitted, message = executor.execute(opportunity, workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("polymarket_min_order_size", message)
        self.assertIn("minimum of 6 shares", message)
        self.assertIn("before order submission", message)

    def test_executor_uses_smallest_equal_size_that_meets_polymarket_minimum(self):
        opportunity = ArbOpportunity(
            pair_name="Resize Minimum",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 20, Decimal("5")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 19, Decimal("5")),
            gross_cost_cents=39,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("61"),
            executable=True,
            blockers=(),
        )
        kalshi = ReadyKalshi()
        kalshi.level = BookLevel(20, Decimal("10"))
        polymarket = RefreshingPolymarket(level=BookLevel(19, Decimal("10")))
        executor = TradeExecutor(kalshi, polymarket, max_leg_usd=Decimal("2"))

        submitted, message = executor.execute(
            opportunity,
            workflow="run-hot-arb",
        )

        self.assertTrue(submitted)
        self.assertIn("refreshed smallest equal FOK size=6", message)
        self.assertIn("orders submitted", message)
        self.assertEqual(polymarket.orders[0]["size"], Decimal("6"))
        self.assertEqual(kalshi.orders[0]["count"], 6)

    def test_executor_uses_polymarket_book_min_order_size_for_both_legs(self):
        class ConstrainedPolymarket(LadderPolymarket):
            def get_token_min_order_size(self, token_id):
                return Decimal("5")

        kalshi = LadderKalshi([BookLevel(60, Decimal("20"))])
        polymarket = ConstrainedPolymarket([BookLevel(30, Decimal("20"))])
        opportunity = ArbOpportunity(
            pair_name="Published Minimum",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 60, Decimal("20")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 30, Decimal("20")),
            gross_cost_cents=90,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("10"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(kalshi, polymarket, max_leg_usd=Decimal("10"))

        submitted, message = executor.execute(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("smallest equal FOK size=5", message)
        self.assertEqual(polymarket.orders[0]["size"], Decimal("5"))
        self.assertEqual(kalshi.orders[0]["count"], 5)

    def test_executor_allows_worse_refresh_when_basket_still_profitable(self):
        kalshi = ReadyKalshi()
        kalshi.level = BookLevel(36, Decimal("20"))
        polymarket = RefreshingPolymarket(level=BookLevel(35, Decimal("20")))
        opportunity = ArbOpportunity(
            pair_name="Still Arb",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 32, Decimal("20")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 35, Decimal("20")),
            gross_cost_cents=67,
            buffers_cents=Decimal("2"),
            net_profit_cents=Decimal("31"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(kalshi, polymarket)

        submitted, message = executor.execute(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("refreshed smallest equal FOK", message)
        self.assertIn("net=27c", message)

    def test_executor_blocks_worse_refresh_when_profit_disappears(self):
        kalshi = ReadyKalshi()
        kalshi.level = BookLevel(70, Decimal("20"))
        polymarket = RefreshingPolymarket(level=BookLevel(35, Decimal("20")))
        opportunity = ArbOpportunity(
            pair_name="Gone",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 32, Decimal("20")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 35, Decimal("20")),
            gross_cost_cents=67,
            buffers_cents=Decimal("2"),
            net_profit_cents=Decimal("31"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(kalshi, polymarket)

        submitted, message = executor.execute(opportunity, workflow="run-hot-arb")

        self.assertFalse(submitted)
        self.assertIn("refreshed basket is no longer profitable", message)

    def test_executor_stops_at_first_exchange_valid_profitable_equal_size(self):
        kalshi = LadderKalshi(
            [
                BookLevel(40, Decimal("1")),
                BookLevel(45, Decimal("2")),
                BookLevel(55, Decimal("10")),
            ]
        )
        polymarket = LadderPolymarket(
            [
                BookLevel(40, Decimal("1")),
                BookLevel(45, Decimal("2")),
                BookLevel(55, Decimal("10")),
            ]
        )
        opportunity = ArbOpportunity(
            pair_name="Blended",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 40, Decimal("10")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 40, Decimal("10")),
            gross_cost_cents=80,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("20"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(kalshi, polymarket, max_leg_usd=Decimal("100"))

        submitted, message = executor.execute(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("refreshed smallest equal FOK size=3", message)
        self.assertIn("limit=45c avg=43.3333c", message)

    def test_executor_stops_before_next_blended_contract_turns_unprofitable(self):
        kalshi = LadderKalshi(
            [
                BookLevel(40, Decimal("3")),
                BookLevel(90, Decimal("10")),
            ]
        )
        polymarket = LadderPolymarket(
            [
                BookLevel(40, Decimal("3")),
                BookLevel(90, Decimal("10")),
            ]
        )
        opportunity = ArbOpportunity(
            pair_name="Stop",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 40, Decimal("10")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 40, Decimal("10")),
            gross_cost_cents=80,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("20"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(kalshi, polymarket, max_leg_usd=Decimal("100"))

        submitted, message = executor.execute(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("refreshed smallest equal FOK size=3", message)
        self.assertIn("limit=40c avg=40c", message)

    def test_executor_blended_fill_respects_per_leg_dollar_cap(self):
        kalshi = LadderKalshi([BookLevel(49, Decimal("10"))])
        polymarket = LadderPolymarket([BookLevel(50, Decimal("10"))])
        opportunity = ArbOpportunity(
            pair_name="Budget",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 49, Decimal("10")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 50, Decimal("10")),
            gross_cost_cents=99,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("1"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(kalshi, polymarket, max_leg_usd=Decimal("1"))

        submitted, message = executor.execute(opportunity, workflow="run-hot-arb")

        self.assertTrue(submitted)
        self.assertIn("refreshed smallest equal FOK size=2", message)

    def test_executor_blocks_when_remaining_budget_cannot_fit_next_minimum_batch(self):
        kalshi = LadderKalshi([BookLevel(40, Decimal("10"))])
        polymarket = LadderPolymarket([BookLevel(52, Decimal("10"))])
        opportunity = ArbOpportunity(
            pair_name="Remaining Budget",
            buy_yes=ArbLeg(Exchange.KALSHI, "K", Side.YES, 40, Decimal("10")),
            buy_no=ArbLeg(Exchange.POLYMARKET, "P", Side.NO, 52, Decimal("10")),
            gross_cost_cents=92,
            buffers_cents=Decimal("0"),
            net_profit_cents=Decimal("8"),
            executable=True,
            blockers=(),
        )
        executor = TradeExecutor(kalshi, polymarket, max_leg_usd=Decimal("5"))

        submitted, message = executor.execute(
            opportunity,
            workflow="run-hot-arb",
            remaining_leg_usd={
                Exchange.KALSHI: Decimal("5"),
                Exchange.POLYMARKET: Decimal("0.96"),
            },
        )

        self.assertFalse(submitted)
        self.assertIn("incremental_budget_exhausted", message)
        self.assertEqual(kalshi.orders, [])
        self.assertEqual(polymarket.orders, [])


if __name__ == "__main__":
    unittest.main()
