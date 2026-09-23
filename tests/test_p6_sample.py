from datetime import date, datetime, time
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from jevquant.models import Bar
from jevquant.ledger import Account, FeeSchedule
from jevquant.models import Fill, Side
from jevquant.p6_sample import (_eligible_execution_bars, _next_order_cutoff, _read_jsonl,
                               _rebuild_account_from_fills, _save_checkpoint,
                               _state, _truncate_run_logs)
from jevquant.reconciliation import reconcile_account_events

TZ = ZoneInfo("Asia/Shanghai")


def _bar(start: time, end: time) -> Bar:
    day = date(2023, 1, 3)
    interval_start = datetime.combine(day, start, TZ)
    interval_end = datetime.combine(day, end, TZ)
    return Bar("600519.SH", interval_end, D("100"), D("101"), D("99"), D("100"), 1000,
               trade_date=day, available_at=interval_end, interval_start=interval_start,
               interval_end=interval_end)


def test_p6_orders_expire_at_next_decision_or_session_close():
    day = date(2023, 1, 3)
    assert _next_order_cutoff(day, 0) == datetime.combine(day, time(10, 5), TZ)
    assert _next_order_cutoff(day, 3) == datetime.combine(day, time(11, 30), TZ)
    assert _next_order_cutoff(day, 4) == datetime.combine(day, time(13, 35), TZ)
    assert _next_order_cutoff(day, 7) == datetime.combine(day, time(15, 0), TZ)


def test_p6_execution_uses_only_full_post_arrival_bars_before_expiry():
    day = date(2023, 1, 3)
    bars = [_bar(time(9, 35), time(9, 40)), _bar(time(9, 40), time(9, 45)),
            _bar(time(10, 0), time(10, 5)), _bar(time(10, 5), time(10, 10))]
    arrival = datetime.combine(day, time(9, 40), TZ)
    cutoff = datetime.combine(day, time(10, 5), TZ)
    selected = _eligible_execution_bars(bars, arrival, cutoff)
    assert [(bar.interval_start.time(), bar.interval_end.time()) for bar in selected] == [
        (time(9, 40), time(9, 45)), (time(10, 0), time(10, 5))]


def test_p6_jev_state_is_anonymized_and_ratio_scaled():
    day = date(2023, 1, 3)
    from datetime import timedelta
    prior_day = day - timedelta(days=1)
    prior_bars = []
    for index in range(23):
        start_minute = 13 * 60 + 10 + index * 5
        start = time(start_minute // 60, start_minute % 60)
        end_minute = start_minute + 5
        end = time(end_minute // 60, end_minute % 60)
        prior_bars.append(Bar("600519.SH", datetime.combine(prior_day, end, TZ),
            D("1730.00"), D("1731.00"), D("1729.00"), D("1730.00"), 20000,
            trade_date=prior_day, available_at=datetime.combine(prior_day, end, TZ),
            interval_start=datetime.combine(prior_day, start, TZ),
            interval_end=datetime.combine(prior_day, end, TZ)))
    bar = Bar("600519.SH", datetime.combine(day, time(9, 35), TZ), D("1730.00"), D("1731.00"),
              D("1729.00"), D("1730.00"), 20000, trade_date=day,
              available_at=datetime.combine(day, time(9, 35), TZ),
              interval_start=datetime.combine(day, time(9, 30), TZ),
              interval_end=datetime.combine(day, time(9, 35), TZ))
    daily = []
    for index in range(65):
        daily.append({"trade_date": day - timedelta(days=65-index), "close_raw": D("1700.00") + index})
    daily.append({"trade_date": day, "close_raw": D("1730.00"),
                  "limit_up_raw": D("1903.00"), "limit_down_raw": D("1557.00")})
    warm = [(day - timedelta(days=20-index), "09:30", 20000) for index in range(20)]
    state = _state(day, 1, bar.interval_end, prior_bars + [bar], list(reversed(daily)), warm,
                   {row["trade_date"]: i+1 for i, row in enumerate(daily)},
                   Account(D("1000000")), ["BUY", "WAIT"], daily[-1])
    encoded = __import__("json").dumps(state, ensure_ascii=False)
    assert state["instrument"]["asset_id"] == "ASSET_001"
    assert state["market"]["current_close_index_100"] == "100"
    assert len(state["market"]["completed_5m_bars"]) == 24
    assert state["snapshot"]["crossed_session_in_recent_window"] is True
    assert state["policy_context"]["review_horizon_sessions"] == 5
    assert abs(float(state["market"]["features"]["price_vs_ma20_raw_unadjusted"])) < 0.02
    assert "600519.SH" not in encoded
    assert "2023-01-03" not in encoded
    assert "1730.00" not in encoded
    assert "1000000" not in encoded


def test_p6_buy_fill_releases_unused_price_protection_reserve():
    day = date(2023, 1, 3)
    fees = FeeSchedule.for_trade_date(day)
    account = Account(D("1000000.00"))
    order_id = "p6-buy"
    reserve = D("100") * D("81.00") + fees.estimate(Side.BUY, D("100") * D("81.00"))
    account.reserve_buy(order_id, reserve)
    fill_gross = D("100") * D("80.00")
    fill = Fill("fill-1", order_id, "600519.SH", Side.BUY, 100, D("80.00"),
                fees.estimate(Side.BUY, fill_gross), day,
                datetime.combine(day, time(10, 0), TZ))
    account.buy(fill, fees, date(2023, 1, 4))
    account.cancel_buy(order_id)
    assert account.cash_reserved == 0
    reconciled = reconcile_account_events(D("1000000.00"), [fill], [], day,
        account.cash_total, account.receivables, {"600519.SH": account.shares_total})
    assert reconciled.passed


def test_p6_checkpoint_restores_from_fill_events_and_truncates_incomplete_day(tmp_path):
    first, second, third = date(2023, 1, 3), date(2023, 1, 4), date(2023, 1, 5)
    fees = FeeSchedule.for_trade_date(first)
    account = Account(D("1000000.00"))
    gross = D("100") * D("80.00")
    fee = fees.estimate(Side.BUY, gross)
    fill = Fill("f1", "o1", "600519.SH", Side.BUY, 100, D("80.00"), fee,
                first, datetime.combine(first, time(10, 0), TZ))
    account.buy(fill, fees, second)
    fills_path = tmp_path / "fills.jsonl"
    fills_path.write_text(__import__("json").dumps({"fill_id": fill.fill_id,
        "order_id": fill.order_id, "side": fill.side.value, "quantity": fill.quantity,
        "price_cny": str(fill.price), "fee_cny": str(fill.fee),
        "trade_date": fill.trade_date.isoformat(), "filled_at": fill.filled_at.isoformat()}) + "\n",
        encoding="utf-8")
    _save_checkpoint(tmp_path, fingerprint="fingerprint", day=first, account=account,
                     nav=account.nav({"600519.SH": D("80.00")}), fill_count=1)
    (tmp_path / "decisions.jsonl").write_text(
        '{"trade_date":"2023-01-03","id":1}\n{"trade_date":"2023-01-04","id":2}\n',
        encoding="utf-8")
    restored, fills = _rebuild_account_from_fills(initial_cash=D("1000000.00"),
        trade_days=(first, second), all_calendar_days=(first, second, third),
        fills_path=fills_path, dividends=[], checkpoint_day=first)
    checkpoint = __import__("json").loads((tmp_path / "checkpoint.json").read_text(encoding="utf-8"))
    assert restored.cash_available == account.cash_available
    assert restored.shares_total == account.shares_total == 100
    assert restored.nav({"600519.SH": D("80.00")}) == D(checkpoint["nav_cny"])
    assert len(fills) == checkpoint["fill_count"] == 1
    _truncate_run_logs(tmp_path, first)
    assert len(_read_jsonl(tmp_path / "decisions.jsonl")) == 1
