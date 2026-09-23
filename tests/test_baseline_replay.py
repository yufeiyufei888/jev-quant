from datetime import date, datetime, time, timedelta
from decimal import Decimal as D
import json

import pytest

from jevquant.baseline_replay import (
    BaselineReplayConfig, SessionExecutionRules, iter_baseline_suite,
    replay_to_jsonable, run_baseline_replay, summarize_baseline_result,
)
from jevquant.models import Bar


def _fixture(baseline="BH80", seed=None, *, omit_execution_date=None,
             ma_rising=False, ma_reversal=False, omit_all_data_date=None):
    first = date(2024, 1, 2)
    dates = tuple(first + timedelta(days=index) for index in range(22 if ma_reversal else 21))
    bars = []
    schedule = {}
    closes = []
    eligible = {day: False for day in dates}
    eligible[dates[-1]] = not ma_reversal
    if ma_reversal:
        eligible[dates[-2]] = True
    for index, day in enumerate(dates):
        price = (D("1") if ma_reversal and index in {20, 21}
                 else D("100") + D(index) / D("10"))
        starts = [datetime.combine(day, time(hour, minute))
                  for hour, minute in ((9, 30), (9, 35), (9, 40))]
        schedule[day] = tuple(starts)
        for slot_index, start in enumerate(starts):
            if day == omit_all_data_date:
                continue
            if day == omit_execution_date and start.time() == time(9, 40):
                continue
            end = start + timedelta(minutes=5)
            opening = (price - D("0.50") if slot_index == 2 and index != 21 else price)
            bars.append(Bar(
                "600519.SH", end, opening, max(opening, price) + D("0.10"),
                min(opening, price) - D("0.10"),
                price, 1_000_000 + index * 10_000, trade_date=day, available_at=end,
                source_id="synthetic-baseline", source_row_id=f"{day}:{slot_index}",
                interval_start=start, interval_end=end,
            ))
        close = D("1") if ma_reversal and index == 20 else D(index + 80 if ma_rising or ma_reversal else "100")
        closes.append((day, close))
    sessions_close = {day: datetime.combine(day, time(9, 45)) for day in dates}
    next_trade = {day: day + timedelta(days=1) for day in dates}
    rules = {day: SessionExecutionRules(False, D("200"), D("0.50")) for day in dates}
    config = BaselineReplayConfig(baseline=baseline, random_seed=seed)
    return config, bars, dates, closes, eligible, schedule, sessions_close, next_trade, rules


def _run(baseline="BH80", seed=None, **kw):
    config, bars, dates, closes, eligible, schedule, session_close, next_trade, rules = _fixture(
        baseline, seed, **kw)
    return run_baseline_replay(
        config, bars=bars, trade_dates=dates, daily_closes=closes,
        buy_eligible_by_date=eligible, execution_schedule=schedule,
        session_close_by_date=session_close, next_trade_date_by_date=next_trade,
        execution_rules_by_date=rules)


def test_buy_hold_uses_common_account_fees_t1_liquidity_and_reconciliation():
    result = _run("BH80")
    assert result.status == "ACCOUNT_REPLAY_PASS"
    assert result.reconciliation.passed
    assert len(result.fills) == 1
    assert result.fills[0].side.value == "BUY"
    assert result.ending_shares > 0
    assert result.ending_cash < D("210000")
    assert result.ending_nav > D("1000000")
    assert result.orders[0]["liquidity_reference"]["session_dates"] == [
        (date(2024, 1, 2) + timedelta(days=index)).isoformat() for index in range(20)]


def test_cash_baseline_is_exactly_static_and_serializes_to_json():
    result = _run("CASH")
    assert result.ending_cash == result.ending_nav == D("1000000.00")
    assert result.ending_shares == 0 and result.fills == ()
    document = replay_to_jsonable(result)
    assert document["reconciliation"]["passed"] is True
    json.dumps(document, ensure_ascii=False)


def test_ma5_20_baseline_uses_only_prior_daily_closes_and_shared_fill_path():
    result = _run("MA5_20", ma_rising=True)
    assert result.reconciliation.passed
    assert [fill.side.value for fill in result.fills] == ["BUY"]
    buy = next(item for item in result.decisions if item["action"] == "BUY")
    assert buy["trade_date"] == "2024-01-22"
    assert buy["prior_daily_close_count"] == 20


def test_missing_scheduled_execution_bar_releases_cash_and_retries_later():
    result = _run("BH80", omit_execution_date=date(2024, 1, 22))
    assert result.reconciliation.passed
    assert len(result.fills) == 0
    assert result.ending_cash == D("1000000.00")
    assert any(order["status"] == "SCHEDULED_EXECUTION_BAR_MISSING" for order in result.orders)
    assert any(item["trade_date"] == "2024-01-22" and item["action"] == "BUY"
               for item in result.decisions)


def test_random_baseline_requires_fixed_seed_and_is_repeatable():
    with pytest.raises(ValueError, match="fixed seed"):
        BaselineReplayConfig("RANDOM")
    first = _run("RANDOM", 31)
    second = _run("RANDOM", 31)
    assert first.fills == second.fills
    assert first.nav_curve == second.nav_curve
    assert first.reconciliation.passed and second.reconciliation.passed


def test_real_vendor_bars_without_interval_or_availability_are_rejected():
    config, bars, dates, closes, eligible, schedule, session_close, next_trade, rules = _fixture()
    broken = list(bars)
    first = broken[0]
    broken[0] = Bar(first.symbol, first.source_time, first.open, first.high, first.low,
                    first.close, first.volume_shares, trade_date=first.trade_date)
    with pytest.raises(ValueError, match="explicit, complete bar intervals"):
        run_baseline_replay(
            config, bars=broken, trade_dates=dates, daily_closes=closes,
            buy_eligible_by_date=eligible, execution_schedule=schedule,
            session_close_by_date=session_close, next_trade_date_by_date=next_trade,
            execution_rules_by_date=rules)


def test_ma_exit_waits_for_t1_and_only_sells_on_the_next_trade_date():
    result = _run("MA5_20", ma_reversal=True)
    assert result.reconciliation.passed
    assert [fill.side.value for fill in result.fills] == ["BUY", "SELL"]
    assert result.fills[0].trade_date == date(2024, 1, 22)
    assert result.fills[1].trade_date == date(2024, 1, 23)
    assert result.fills[1].trade_date > result.fills[0].trade_date
    assert result.ending_shares == 0


def test_limit_up_queue_is_rejected_without_account_mutation():
    config, bars, dates, closes, eligible, schedule, session_close, next_trade, rules = _fixture()
    rules[dates[-1]] = SessionExecutionRules(False, D("102"), D("0.50"))
    result = run_baseline_replay(
        config, bars=bars, trade_dates=dates, daily_closes=closes,
        buy_eligible_by_date=eligible, execution_schedule=schedule,
        session_close_by_date=session_close, next_trade_date_by_date=next_trade,
        execution_rules_by_date=rules)
    assert result.reconciliation.passed
    assert result.fills == ()
    assert result.ending_cash == D("1000000.00")
    assert any(order["status"] == "LIMIT_UP_QUEUE_UNMODELED" for order in result.orders)


def test_missing_session_keeps_position_and_marks_valuation_incomplete():
    result = _run("MA5_20", ma_reversal=True, omit_all_data_date=date(2024, 1, 23))
    assert result.reconciliation.passed
    assert result.ending_shares > 0
    assert result.valuation_incomplete
    assert result.status == "ACCOUNT_REPLAY_INCOMPLETE_VALUATION"
    assert result.nav_curve[-1]["stale_mark"] is True


def test_baseline_suite_runs_independent_accounts_and_seeded_random_replicates():
    config, bars, dates, closes, eligible, schedule, session_close, next_trade, rules = _fixture()
    results = list(iter_baseline_suite(
        bars=bars, trade_dates=dates, daily_closes=closes,
        buy_eligible_by_date=eligible, execution_schedule=schedule,
        session_close_by_date=session_close, next_trade_date_by_date=next_trade,
        execution_rules_by_date=rules, random_seeds=range(3)))
    assert [name for name, _ in results] == [
        "CASH", "BH80", "BH50", "BH_MAX", "MA5_20",
        "RANDOM-seed-0", "RANDOM-seed-1", "RANDOM-seed-2"]
    assert all(result.reconciliation.passed for _, result in results)
    cash = results[0][1]
    bh80 = results[1][1]
    bh50 = results[2][1]
    assert cash.ending_shares == 0
    assert bh80.ending_shares > bh50.ending_shares > 0
    summary = summarize_baseline_result(bh80, D("1000000.00"))
    assert summary["filled_trades"] == 1
    assert summary["reconciliation_passed"] is True


def test_nonexecution_bars_are_accepted_and_prestart_liquidity_is_not_account_history():
    config, bars, dates, closes, eligible, all_starts, session_close, next_trade, rules = _fixture()
    eligible = {day: True for day in dates}
    execution_starts = {day: tuple(start for start in starts if start.time() != time(9, 30))
                        for day, starts in all_starts.items()}
    warmup = tuple((date(2023, 12, 1) + timedelta(days=index), "09:40", 1_000_000)
                   for index in range(20))
    result = run_baseline_replay(
        config, bars=bars, trade_dates=dates, daily_closes=closes,
        buy_eligible_by_date=eligible, execution_schedule=execution_starts,
        bar_schedule_by_date=all_starts, prior_volume_history=warmup,
        session_close_by_date=session_close, next_trade_date_by_date=next_trade,
        execution_rules_by_date=rules)
    assert result.status == "ACCOUNT_REPLAY_PASS"
    assert result.reconciliation.passed
    assert len(result.fills) == 1
    assert result.fills[0].filled_at.time() == time(9, 40)
    assert result.orders[0]["liquidity_reference"]["session_dates"] == [
        (date(2023, 12, 1) + timedelta(days=index)).isoformat() for index in range(20)]
    assert {row["trade_date"] for row in result.nav_curve} <= {day.isoformat() for day in dates}


def test_prior_volume_history_cannot_include_replay_dates_or_duplicate_slots():
    config, bars, dates, closes, eligible, schedule, session_close, next_trade, rules = _fixture()
    bad_history = [(dates[0], "09:40", 1_000_000)]
    with pytest.raises(ValueError, match="must precede"):
        run_baseline_replay(
            config, bars=bars, trade_dates=dates, daily_closes=closes,
            buy_eligible_by_date=eligible, execution_schedule=schedule,
            prior_volume_history=bad_history, session_close_by_date=session_close,
            next_trade_date_by_date=next_trade, execution_rules_by_date=rules)


def test_execution_schedule_must_be_a_subset_of_available_bar_starts():
    config, bars, dates, closes, eligible, schedule, session_close, next_trade, rules = _fixture()
    invalid_execution = dict(schedule)
    invalid_execution[dates[0]] = (datetime.combine(dates[0], time(9, 25)),)
    with pytest.raises(ValueError, match="eligible execution start"):
        run_baseline_replay(
            config, bars=bars, trade_dates=dates, daily_closes=closes,
            buy_eligible_by_date=eligible, execution_schedule=invalid_execution,
            bar_schedule_by_date=schedule,
            session_close_by_date=session_close, next_trade_date_by_date=next_trade,
            execution_rules_by_date=rules)
