"""Small, traceable JEV-to-account runs on the supplied Moutai history.

All historical outputs are explicitly retrospective diagnostics: the source
minute labels are mapped under an unverified interval-end hypothesis, and the
status files are not point-in-time records. This module never connects to a
broker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .actions import load_cash_dividends
from .data import read_daily_vendor_csv
from .execution import make_protection_price, match_open_proxy
from .ledger import Account, FeeSchedule, plan_entry_quantity
from .liquidity import build_liquidity_reference
from .models import Bar, Fill, OrderIntent, Side
from .provider import (JevConfigurationError, JevResponseError, decide_cached,
                       request_hash)
from .reconciliation import reconcile_account_events
from .retrospective_replay import SYMBOL, _parquet_day, _read_status, normalize_end_labeled_diagnostic
from .usage import UsageLedger

D = Decimal
TZ = ZoneInfo("Asia/Shanghai")
try:
    from typesafe_sdk._core.errors import TypeSafeError
    PROVIDER_ERRORS = (JevConfigurationError, JevResponseError, TypeSafeError)
except ImportError:
    PROVIDER_ERRORS = (JevConfigurationError, JevResponseError)
DECISION_INTERVAL = timedelta(minutes=5)
INSTRUCTIONS = (
    "你是研究模拟中的受限动作判断器。只依据输入快照决定现在请求BUY还是WAIT；"
    "已有可卖持仓时决定HOLD还是SELL。不要计算股数或改写规则。"
    "目标仓位固定为下次开仓时净资产的80%，以未来约5个交易日的持有价值为判断背景，"
    "这不是第5日强制卖出。不加仓、不杠杆、不当日卖出。"
    "动作概率不是盈利概率，证据不足时选择WAIT或HOLD。"
)
INSTANT_SNAPSHOT_INSTRUCTIONS = (
    "你是研究模拟中的受限动作判断器。只依据输入快照判断现在是否立即建立多头仓位：选择BUY立即按本快照当前收盘价记账，"
    "或选择WAIT保持现金；已有可卖持仓时选择HOLD或SELL，SELL按本快照当前收盘价记账。"
    "不要计算股数或改写规则。目标仓位固定为下次开仓时净资产的80%，以未来约5个交易日的持有价值为判断背景，"
    "这不是第5日强制卖出。不加仓、不杠杆、不当日卖出。动作概率不是盈利概率，证据不足时选择WAIT或HOLD。"
)


def _decimal(value: Any) -> Decimal | None:
    return value if isinstance(value, Decimal) else (D(str(value)) if value is not None else None)


def _state(day: date, session_index: int, at: datetime, bars: list[Bar],
           daily_rows: list[dict[str, Any]], warm_volume_history: list[tuple[date, str, int]],
           session_rank: dict[date, int], account: Account, allowed: list[str],
           limit_row: dict[str, Any], *, execution_mode: str = "delayed_bar_open") -> dict[str, Any]:
    visible = [bar for bar in bars if bar.interval_end is not None and bar.interval_end <= at]
    if not visible or visible[-1].interval_end != at:
        raise ValueError("decision snapshot must end on a completed visible bar")
    current_session_bars = [bar for bar in visible if bar.trade_date == day]
    history = sorted((row for row in daily_rows
                      if row["trade_date"] < day and row.get("close_raw") is not None),
                     key=lambda row: row["trade_date"])
    last24 = visible[-24:]
    if len(last24) != 24:
        raise ValueError("JEV calls require 24 completed five-minute bars across the visible session history")
    current_price = visible[-1].close
    anchor = last24[0].open
    daily_closes = [D(row["close_raw"]) for row in history]

    def daily_return(window: int) -> Decimal | None:
        if len(daily_closes) <= window:
            return None
        return daily_closes[-1] / daily_closes[-1 - window] - D("1")

    def mean_tail(values: list[Decimal], n: int) -> Decimal | None:
        return sum(values[-n:], D("0")) / D(n) if len(values) >= n else None

    slot = visible[-1].interval_start.strftime("%H:%M") if visible[-1].interval_start else ""
    session_high = max(item.high for item in current_session_bars)
    session_low = min(item.low for item in current_session_bars)
    session_range_position = ((current_price - session_low) / (session_high - session_low)
                              if session_high > session_low else None)
    daily_returns = [daily_closes[index] / daily_closes[index - 1] - D("1")
                     for index in range(max(1, len(daily_closes) - 20), len(daily_closes))]
    holding_sessions = max((session_rank.get(day, 0) - session_rank.get(lot.acquired_on, session_rank.get(day, 0)) + 1
        for lot in account.lots if lot.symbol == SYMBOL), default=0)
    position_value = D(account.shares_total) * current_price
    nav = account.nav({SYMBOL: current_price})
    cost_basis = sum((D(lot.quantity) * lot.entry_price for lot in account.lots if lot.symbol == SYMBOL), D("0"))
    unrealized = current_price * D(account.shares_total) / cost_basis - D("1") if cost_basis else None
    bar_returns = {}
    for n in (1, 3, 6, 12):
        prior_bar = visible[-1 - n] if len(visible) > n else None
        bar_returns[str(n)] = (visible[-1].close / prior_bar.close - D("1")
            if prior_bar is not None and prior_bar.trade_date == day else None)

    def same_slot_relative_volume(bar: Bar) -> str | None:
        if bar.interval_start is None or bar.trade_date is None:
            return None
        try:
            ref = build_liquidity_reference(warm_volume_history, signal_date=bar.trade_date,
                                            slot=bar.interval_start.strftime("%H:%M"))
        except ValueError:
            return None
        return str(D(bar.volume_shares) / ref.median_volume_shares) if ref.median_volume_shares else None

    distinct_sessions = sorted({bar.trade_date for bar in last24 if bar.trade_date is not None})
    session_offsets = {session: index - (len(distinct_sessions) - 1)
                       for index, session in enumerate(distinct_sessions)}
    previous_close = D(history[-1]["close_raw"]) if history else None

    def normalized(value: Decimal) -> str:
        return str(value / anchor * D("100"))

    return {
        "schema_version": "state_v1",
        "instrument": {"asset_id": "ASSET_001", "exchange": "CN_A_SHARE", "price_basis": "window_normalized",
                       "time_zone": "Asia/Shanghai"},
        "snapshot": {"session_index": session_index, "bar_slot_index": len(current_session_bars),
                     "decision_clock": at.strftime("%H:%M"), "visible_bar_count": len(visible),
                     "crossed_session_in_recent_window": len(distinct_sessions) > 1,
                     "snapshot_mode": ("retrospective_instant_snapshot_fill_diagnostic"
                         if execution_mode == "instant_snapshot_close" else "retrospective_end_label_diagnostic")},
        "market": {
            "current_close_index_100": normalized(current_price),
            "current_bar": {"slot": slot, "open_index_100": normalized(visible[-1].open),
                            "high_index_100": normalized(visible[-1].high),
                            "low_index_100": normalized(visible[-1].low),
                            "close_index_100": normalized(visible[-1].close),
                            "relative_volume_same_slot": same_slot_relative_volume(visible[-1])},
            "completed_5m_bars": [{"session_offset": session_offsets.get(bar.trade_date, 0),
                "clock_slot": bar.interval_start.strftime("%H:%M") if bar.interval_start else None,
                "slot_index": sum(1 for prior in visible if prior.trade_date == bar.trade_date
                                  and prior.interval_end <= bar.interval_end),
                "open_index_100": normalized(bar.open), "high_index_100": normalized(bar.high),
                "low_index_100": normalized(bar.low), "close_index_100": normalized(bar.close),
                "relative_volume_same_slot": same_slot_relative_volume(bar)}
                for bar in last24],
            "features": {
                **{f"return_{n}_bars": str(value) if value is not None else None
                   for n, value in bar_returns.items()},
                **{f"return_{n}_sessions_raw_unadjusted": str(daily_return(n)) if daily_return(n) is not None else None
                   for n in (5, 20, 60)},
                "price_vs_ma20_raw_unadjusted": (str(current_price / mean_tail(daily_closes, 20) - D("1"))
                    if mean_tail(daily_closes, 20) else None),
                "price_vs_ma60_raw_unadjusted": (str(current_price / mean_tail(daily_closes, 60) - D("1"))
                    if mean_tail(daily_closes, 60) else None),
                "daily_volatility_20_raw_unadjusted": str(statistics.pstdev(daily_returns)) if len(daily_returns) >= 2 else None,
                "intraday_range_position": str(session_range_position) if session_range_position is not None else None,
                "gap_from_previous_session_raw_unadjusted": (str(current_session_bars[0].open / previous_close - D("1"))
                    if current_session_bars and previous_close else None),
            },
            "prior_daily_close_indices_100": [str(close / anchor * D("100")) for close in daily_closes[-65:]],
            "price_band_room_fraction": {
                "to_upper": str(D(limit_row["limit_up_raw"]) / current_price - D("1")),
                "to_lower": str(D(limit_row["limit_down_raw"]) / current_price - D("1")),
            },
        },
        "account": {"position_state": "LONG" if account.shares_total else "FLAT",
                    "nav_ratio_to_initial": str(nav / account.initial_cash),
                    "cash_weight": str(account.cash_total / nav),
                    "position_weight": str(position_value / nav),
                    "sellable_fraction": str(D(account.shares_sellable(day, SYMBOL)) / D(account.shares_total))
                        if account.shares_total else "0",
                    "holding_sessions": holding_sessions,
                    "unrealized_return_before_costs": str(unrealized) if unrealized is not None else None,
                    "receivables_ratio_to_initial": str(account.receivables / account.initial_cash)},
        "policy_context": {"long_only": True, "entry_target_weight": "0.80",
                            "review_horizon_sessions": 5, "new_purchases_sellable_next_session": True,
                            "execution_delay_minutes": 0 if execution_mode == "instant_snapshot_close" else 5,
                            "slippage_bps_per_side_assumption": 0 if execution_mode == "instant_snapshot_close" else 5,
                            "can_open_min_lot": current_price * 100 / nav <= D("0.80"),
                            "allowed_actions": allowed, "leverage_allowed": False,
                            "broker_orders_enabled": False},
        "data_quality": {"raw_price_returns_unadjusted_for_unmapped_actions": True,
            "five_minute_interval_end_is_assumed_not_provider_verified": True,
            "availability_at_interval_end_is_assumed": True,
            "historical_status_is_retrospective_not_point_in_time": True},
    }


def _json_line(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _eligible_execution_bars(bars: list[Bar], arrival: datetime, cutoff: datetime) -> list[Bar]:
    return [bar for bar in bars if bar.interval_start is not None and bar.interval_end is not None
            and bar.interval_start >= arrival and bar.interval_end <= cutoff
            and (time(9, 35) <= bar.interval_start.time() <= time(11, 25)
                 or time(13, 5) <= bar.interval_start.time() <= time(14, 50))]


def _execution_bar_at_arrival(bars_by_start: dict[datetime, Bar], decision_at: datetime) -> Bar | None:
    arrival = decision_at + DECISION_INTERVAL
    bar = bars_by_start.get(arrival)
    if bar is None or bar.interval_end is None:
        return None
    start_clock, end_clock = arrival.time(), bar.interval_end.time()
    morning = time(9, 30) <= start_clock < time(11, 30) and end_clock <= time(11, 30)
    afternoon = time(13, 0) <= start_clock < time(15, 0) and end_clock <= time(15, 0)
    return bar if morning or afternoon else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _unresolved_provider_hashes(errors: list[dict[str, Any]], successful_hashes: set[str]) -> set[str]:
    failed = {row["request_hash"] for row in errors
              if row.get("api_request_attempted") and row.get("request_hash")}
    return failed - successful_hashes


def _instant_snapshot_fill(account: Account, *, order_id: str, side: Side, day: date,
                           next_trade_day: date, at: datetime, price: Decimal,
                           fee_schedule: FeeSchedule, target_weight: Decimal = D("0.80"),
                           lot_size: int = 100) -> tuple[Fill | None, int]:
    """Idealized attribution fill at the exact close supplied to JEV.

    This deliberately skips delay, slippage, OHLC range matching and the
    historical liquidity cap. Cash sufficiency, dated fees, board-lot sizing,
    the target weight and T+1 sellability remain enforced by the account.
    """
    if side is Side.BUY:
        target = plan_entry_quantity(account.nav({SYMBOL: price}), target_weight,
            price, account.cash_available, reserve=D("0.00"), lot_size=lot_size,
            fee_schedule=fee_schedule)
        quantity = target
    else:
        target = account.shares_sellable(day, SYMBOL)
        quantity = target
    if quantity <= 0:
        return None, target
    gross = D(quantity) * price
    fill = Fill(f"{order_id}:snapshot-close", order_id, SYMBOL, side, quantity,
        price, fee_schedule.incremental_fee(side, D("0.00"), gross), day, at)
    if side is Side.BUY:
        account.buy(fill, fee_schedule, next_trade_day)
    else:
        account.sell(fill, fee_schedule)
    return fill, target


def _truncate_run_logs(output: Path, checkpoint_day: date | None) -> None:
    # Errors are attempt evidence and remain append-only across event replay.
    for name in ("decisions.jsonl", "orders.jsonl", "fills.jsonl", "nav.jsonl"):
        path = output / name
        if not path.exists():
            continue
        retained = []
        for row in _read_jsonl(path):
            day_text = row.get("trade_date")
            if checkpoint_day is not None and day_text and date.fromisoformat(day_text) <= checkpoint_day:
                retained.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        path.write_text("".join(row + "\n" for row in retained), encoding="utf-8", newline="\n")


def _rebuild_account_from_fills(*, initial_cash: Decimal, trade_days: tuple[date, ...],
                                all_calendar_days: tuple[date, ...],
                                fills_path: Path, dividends: list[Any],
                                checkpoint_day: date) -> tuple[Account, list[Any]]:
    """Restore the ledger by replaying persisted fill/action events, independently of a pickle."""
    from .models import Fill
    account = Account(initial_cash)
    parsed = []
    for row in _read_jsonl(fills_path):
        fill = Fill(row["fill_id"], row["order_id"], SYMBOL, Side(row["side"]),
                    int(row["quantity"]), D(row["price_cny"]), D(row["fee_cny"]),
                    date.fromisoformat(row["trade_date"]), datetime.fromisoformat(row["filled_at"]))
        if fill.trade_date <= checkpoint_day:
            parsed.append(fill)
    parsed.sort(key=lambda item: (item.trade_date, item.filled_at, item.fill_id))
    for day in trade_days:
        if day > checkpoint_day:
            break
        day_fills = [item for item in parsed if item.trade_date == day]
        fees = FeeSchedule.for_trade_date(day)
        for fill in day_fills:
            if fill.side is Side.BUY:
                next_day = next((candidate for candidate in all_calendar_days if candidate > day), None)
                if next_day is None:
                    raise ValueError("daily calendar lacks next session while restoring a buy fill")
                account.buy(fill, fees, next_day)
            else:
                account.sell(fill, fees)
        for event in dividends:
            account.apply_cash_dividend_event(event, day)
    return account, parsed


def _save_checkpoint(output: Path, *, fingerprint: str, day: date, account: Account,
                     nav: Decimal, fill_count: int) -> None:
    payload = {"schema": "jevquant-p6-checkpoint/v1", "run_fingerprint": fingerprint,
        "last_completed_session": day.isoformat(), "cash_available_cny": str(account.cash_available),
        "cash_reserved_cny": str(account.cash_reserved), "receivables_cny": str(account.receivables),
        "shares": account.shares_total, "nav_cny": str(nav), "fill_count": fill_count,
        "seen_event_ids": sorted(account.seen_event_ids)}
    path = output / "checkpoint.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                          encoding="utf-8", newline="\n")
    temporary.replace(path)


def run_p6_sample(*, minute_root: Path, daily_csv: Path, status_paths: tuple[Path, Path],
                  dividend_config: Path, output: Path, start: date, sessions: int,
                  resume: bool = False,
                  execution_mode: str = "delayed_bar_open",
                  client: Any | None = None,
                  progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if sessions not in (1, 20):
        raise ValueError("P6 phases are defined as exactly 1 or 20 trading sessions")
    if execution_mode not in {"delayed_bar_open", "instant_snapshot_close"}:
        raise ValueError("unsupported P6 execution mode")
    instructions = (INSTANT_SNAPSHOT_INSTRUCTIONS if execution_mode == "instant_snapshot_close"
                    else INSTRUCTIONS)
    daily_rows = read_daily_vendor_csv(daily_csv, SYMBOL)
    by_day = {row["trade_date"]: row for row in daily_rows}
    trade_days = tuple(sorted(day for day in by_day if day >= start))[:sessions]
    if len(trade_days) != sessions:
        raise ValueError("daily file does not contain the requested number of sessions")
    all_days = tuple(sorted(by_day))
    session_rank = {day: index for index, day in enumerate(all_days, 1)}
    first_idx = all_days.index(trade_days[0])
    warm_days = all_days[max(0, first_idx - 21):first_idx]
    if len(warm_days) != 21:
        raise ValueError("P6 needs 21 preceding trading sessions for 20-slot references on the prior-session bars")
    status = _read_status(status_paths, set(trade_days))
    all_dividends = load_cash_dividends(dividend_config, SYMBOL)
    warm_volumes: list[tuple[date, str, int]] = []
    normalized_by_day: dict[date, list[Bar]] = {}
    minute_paths = {day: minute_root / str(day.year) / f"{day:%Y%m%d}.parquet"
                    for day in (*warm_days, *trade_days)}
    input_hashes = {"daily_csv": _sha256_file(daily_csv),
        "dividend_config": _sha256_file(dividend_config),
        "status_files": {str(path): _sha256_file(path) for path in status_paths},
        "minute_partitions": {day.isoformat(): _sha256_file(path) for day, path in minute_paths.items()}}
    run_identity = {"start": start.isoformat(), "sessions": sessions, "symbol": SYMBOL,
                    "target_weight": "0.80", "decision_interval_minutes": 5,
                    "execution_mode": execution_mode,
                    "input_hashes": input_hashes, "run_version": "p6-sample-v3"}
    fingerprint = hashlib.sha256(json.dumps(run_identity, sort_keys=True, separators=(",", ":"))
                                 .encode("utf-8")).hexdigest()
    for day in warm_days:
        normalized = normalize_end_labeled_diagnostic(day, _parquet_day(minute_root, day))
        warm_volumes.extend((day, bar.interval_start.strftime("%H:%M"), bar.volume_shares)
                            for bar in normalized if bar.interval_start is not None)
    for day in trade_days:
        normalized_by_day[day] = normalize_end_labeled_diagnostic(day, _parquet_day(minute_root, day))
    historical_bars = normalize_end_labeled_diagnostic(warm_days[-1], _parquet_day(minute_root, warm_days[-1]))
    checkpoint_path = output / "checkpoint.json"
    checkpoint_day: date | None = None
    if resume:
        if not output.is_dir():
            raise ValueError("resume requires an existing output directory")
        if checkpoint_path.is_file():
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if checkpoint.get("schema") != "jevquant-p6-checkpoint/v1" or checkpoint.get("run_fingerprint") != fingerprint:
                raise ValueError("checkpoint schema or source/config fingerprint does not match this run")
            checkpoint_day = date.fromisoformat(checkpoint["last_completed_session"])
            if checkpoint_day not in trade_days:
                raise ValueError("checkpoint session is outside the requested P6 window")
            _truncate_run_logs(output, checkpoint_day)
        else:
            # A cache-only retry directory starts from the initial account while
            # reusing validated responses. This supports recovery when the first
            # session was interrupted before its first durable checkpoint.
            _truncate_run_logs(output, None)
    else:
        output.mkdir(parents=True, exist_ok=False)
    cache = output / "jev-response-cache.json"
    usage = UsageLedger(output / "jev-usage.jsonl")
    decision_path, order_path, fill_path, nav_path = (output / name for name in
        ("decisions.jsonl", "orders.jsonl", "fills.jsonl", "nav.jsonl"))
    error_path = output / "errors.jsonl"
    if checkpoint_day is None:
        account = Account(D("1000000.00"))
        fills = []
        decisions_count = provider_calls = 0
        fee_total = D("0.00")
        cash_reconciliation = None
        equity_marks: dict[str, Decimal] = {}
        prior_usage: set[str] = set()
    else:
        account, fills = _rebuild_account_from_fills(initial_cash=D("1000000.00"),
            trade_days=trade_days, all_calendar_days=all_days, fills_path=fill_path, dividends=all_dividends,
            checkpoint_day=checkpoint_day)
        checkpoint_row = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint_nav = account.nav({SYMBOL: D(by_day[checkpoint_day]["close_raw"])})
        if (account.cash_total != D(checkpoint_row["cash_available_cny"])
                or account.cash_reserved != D(checkpoint_row["cash_reserved_cny"])
                or account.receivables != D(checkpoint_row["receivables_cny"])
                or account.shares_total != int(checkpoint_row["shares"])
                or checkpoint_nav != D(checkpoint_row["nav_cny"])
                or len(fills) != int(checkpoint_row["fill_count"])
                or sorted(account.seen_event_ids) != checkpoint_row["seen_event_ids"]):
            raise ValueError("event replay does not match the last durable account checkpoint")
        cash_reconciliation = reconcile_account_events(D("1000000.00"), fills, all_dividends,
            checkpoint_day, account.cash_total, account.receivables, {SYMBOL: account.shares_total})
        if not cash_reconciliation.passed:
            raise ValueError("restored event ledger failed independent cash/share reconciliation")
        equity_marks = {SYMBOL: D(by_day[checkpoint_day]["close_raw"])}
        decisions_count = len(_read_jsonl(decision_path)) + len(_read_jsonl(error_path))
        fee_total = sum((fill.fee for fill in fills), D("0.00"))
        for prior_day in trade_days:
            if prior_day > checkpoint_day:
                break
            previous = normalized_by_day[prior_day]
            historical_bars = (historical_bars + previous)[-48:]
            warm_volumes.extend((prior_day, bar.interval_start.strftime("%H:%M"), bar.volume_shares)
                                for bar in previous if bar.interval_start is not None)
        prior_usage = {row["request_hash"] for row in _read_jsonl(output / "jev-usage.jsonl")
                       if row.get("request_hash")}
        provider_calls = len(prior_usage)
    attempted_hashes = set(prior_usage)
    # An error row is append-only evidence of a failed attempt. A later
    # successful request with the same hash resolves it, even if resume starts
    # after that decision's session.
    failed_hashes = _unresolved_provider_hashes(_read_jsonl(error_path), prior_usage)
    attempted_hashes.update(failed_hashes)
    for day_index, day in enumerate(trade_days, 1):
        if checkpoint_day is not None and day <= checkpoint_day:
            continue
        bars = normalized_by_day[day]
        day_row = by_day[day]
        if not status[day][0] or status[day][1]:
            raise ValueError(f"historical status excludes {day}; P6 sample requires an eligible session")
        if day_row.get("limit_up_raw") is None or day_row.get("limit_down_raw") is None:
            raise ValueError(f"missing daily limit bands on {day}")
        pending: tuple[OrderIntent, FeeSchedule, int, int, Any] | None = None
        bars_by_start = {bar.interval_start: bar for bar in bars if bar.interval_start is not None}
        if len(bars_by_start) != len(bars):
            raise ValueError(f"missing or duplicate interval start on {day}")
        for bar_index, decision_bar in enumerate(bars):
            at = decision_bar.interval_end
            if at is None or decision_bar.available_at is None or decision_bar.available_at != at:
                raise ValueError(f"decision requires a closed and available bar on {day}")
            # An order arriving at this bar's open is matched before this bar's
            # close becomes a new decision snapshot. No future bar is processed
            # ahead of its own event time.
            if pending is not None:
                order, fee_schedule, requested_quantity, target_quantity, liquidity = pending
                if decision_bar.interval_start == order.arrival_at:
                    matched = match_open_proxy(order, decision_bar, fee_schedule=fee_schedule,
                        limit_up=day_row["limit_up_raw"], limit_down=day_row["limit_down_raw"])
                    fill = matched.fill
                    if fill is not None:
                        if order.side is Side.BUY:
                            following = next((candidate for candidate in all_days if candidate > day), None)
                            if following is None:
                                raise ValueError("daily data lacks next trade date for T+1 sellability")
                            account.buy(fill, fee_schedule, following)
                            account.cancel_buy(order.order_id)
                        else:
                            account.sell(fill, fee_schedule)
                            account.cancel_sell(order.order_id)
                        fills.append(fill)
                        fee_total += fill.fee
                        _json_line(fill_path, {"fill_id": fill.fill_id, "order_id": order.order_id,
                            "side": fill.side.value, "quantity": fill.quantity, "price_cny": str(fill.price),
                            "fee_cny": str(fill.fee), "trade_date": fill.trade_date.isoformat(),
                            "filled_at": fill.filled_at.isoformat(),
                            "source_bar": {"interval_start": decision_bar.interval_start.isoformat(),
                                "interval_end": decision_bar.interval_end.isoformat(),
                                "quality_flags": sorted(decision_bar.quality_flags)}})
                    else:
                        if order.side is Side.BUY:
                            account.cancel_buy(order.order_id)
                        else:
                            account.cancel_sell(order.order_id)
                    _json_line(order_path, {"order_id": order.order_id, "side": order.side.value,
                        "trade_date": day.isoformat(), "quantity": requested_quantity,
                        "target_quantity_before_liquidity_cap": target_quantity,
                        "status": matched.reason, "decision_at": order.decision_at.isoformat(),
                        "arrival_at": order.arrival_at.isoformat(), "expires_at": at.isoformat(),
                        "liquidity_cap_shares": liquidity.cap_shares})
                    pending = None
                elif decision_bar.interval_start is not None and decision_bar.interval_start > order.arrival_at:
                    if order.side is Side.BUY:
                        account.cancel_buy(order.order_id)
                    else:
                        account.cancel_sell(order.order_id)
                    _json_line(order_path, {"order_id": order.order_id, "side": order.side.value,
                        "trade_date": day.isoformat(), "quantity": requested_quantity,
                        "status": "ARRIVAL_BAR_MISSING", "decision_at": order.decision_at.isoformat(),
                        "arrival_at": order.arrival_at.isoformat(), "expires_at": at.isoformat()})
                    pending = None

            decisions_count += 1
            if pending is not None:
                _json_line(decision_path, {"trade_date": day.isoformat(), "decision_at": at.isoformat(),
                    "decision": {"action": "SKIP_PENDING_ORDER", "source": "execution_rule",
                                 "request_hash": None}})
                continue
            sellable = account.shares_sellable(day, SYMBOL)
            if account.shares_total and not sellable:
                allowed = ["HOLD"]
            elif account.shares_total:
                allowed = ["HOLD", "SELL"]
            else:
                allowed = ["BUY", "WAIT"]
            visible_bars = historical_bars + bars[:bar_index + 1]
            state = _state(day, day_index, at, visible_bars, daily_rows, warm_volumes, session_rank,
                           account, allowed, day_row, execution_mode=execution_mode)
            digest = request_hash(state, allowed, instructions)
            api_attempted = allowed in (["BUY", "WAIT"], ["HOLD", "SELL"])
            if api_attempted and digest not in attempted_hashes:
                provider_calls += 1
                attempted_hashes.add(digest)
            try:
                decision = decide_cached(state, instructions, cache, client=client)
            except PROVIDER_ERRORS as exc:
                if api_attempted:
                    failed_hashes.add(digest)
                _json_line(error_path, {"trade_date": day.isoformat(), "decision_at": at.isoformat(),
                    "request_hash": digest, "api_request_attempted": api_attempted,
                    "error_type": type(exc).__name__, "error": str(exc),
                    "action": "SKIP_NO_ORDER", "account_mutated": False})
                raise RuntimeError(f"JEV decision failed at {day} {at:%H:%M}; resume from the last verified session") from exc
            if decision.source == "jev":
                if decision.request_hash not in prior_usage:
                    usage.record(decision)
                    prior_usage.add(decision.request_hash)
                failed_hashes.discard(decision.request_hash)
            _json_line(decision_path, {"trade_date": day.isoformat(), "decision_at": at.isoformat(),
                "state": state, "decision": {"action": decision.action_requested,
                "probabilities": decision.option_probabilities, "confidence": decision.distribution_confidence,
                "provider_choice": decision.provider_choice, "model_requested": decision.model_requested,
                "model_resolved": decision.model_resolved, "request_hash": decision.request_hash,
                "source": decision.source, "input_tokens": decision.input_tokens,
                "output_tokens": decision.output_tokens, "estimated_cost_usd": decision.estimated_cost_usd}})
            if decision.action_requested not in {"BUY", "SELL"}:
                continue
            side = Side(decision.action_requested)
            if execution_mode == "instant_snapshot_close":
                fee_schedule = FeeSchedule.for_trade_date(day)
                order_id = f"p6-instant-{day:%Y%m%d}-{at:%H%M}-{side.value.lower()}"
                next_trade_day = next((candidate for candidate in all_days if candidate > day), None)
                if next_trade_day is None:
                    raise ValueError("daily data lacks next trade date for T+1 accounting")
                fill, target = _instant_snapshot_fill(account, order_id=order_id, side=side,
                    day=day, next_trade_day=next_trade_day, at=at, price=decision_bar.close,
                    fee_schedule=fee_schedule)
                if fill is None:
                    _json_line(order_path, {"trade_date": day.isoformat(), "decision_at": at.isoformat(),
                        "side": side.value, "status": "REJECTED_ZERO_QUANTITY",
                        "execution_mode": execution_mode})
                    continue
                quantity = fill.quantity
                fills.append(fill)
                fee_total += fill.fee
                _json_line(fill_path, {"fill_id": fill.fill_id, "order_id": order_id,
                    "side": side.value, "quantity": quantity, "price_cny": str(fill.price),
                    "fee_cny": str(fill.fee), "trade_date": fill.trade_date.isoformat(),
                    "filled_at": fill.filled_at.isoformat(), "execution_mode": execution_mode,
                    "source_bar": {"interval_start": decision_bar.interval_start.isoformat(),
                        "interval_end": decision_bar.interval_end.isoformat(),
                        "quality_flags": sorted(decision_bar.quality_flags)}})
                _json_line(order_path, {"order_id": order_id, "side": side.value,
                    "trade_date": day.isoformat(), "quantity": quantity,
                    "target_quantity_before_liquidity_cap": target,
                    "status": "FILLED_INSTANT_SNAPSHOT_CLOSE", "execution_mode": execution_mode,
                    "decision_at": at.isoformat(), "arrival_at": at.isoformat(),
                    "price_cny": str(decision_bar.close), "liquidity_cap_applied": False,
                    "slippage_bps": 0})
                continue
            arrival = at + DECISION_INTERVAL
            execution_bar = _execution_bar_at_arrival(bars_by_start, at)
            if execution_bar is None:
                _json_line(order_path, {"trade_date": day.isoformat(), "decision_at": at.isoformat(),
                    "side": side.value, "status": "NO_EXECUTION_BAR_AT_ARRIVAL",
                    "arrival_at": arrival.isoformat()})
                continue
            slot = arrival.strftime("%H:%M")
            liquidity = build_liquidity_reference(warm_volumes, signal_date=day, slot=slot)
            if liquidity.cap_shares <= 0:
                _json_line(order_path, {"trade_date": day.isoformat(), "decision_at": at.isoformat(),
                                        "side": side.value, "status": "REJECTED_ZERO_LIQUIDITY_CAP"})
                continue
            fee_schedule = FeeSchedule.for_trade_date(day)
            if side is Side.BUY:
                target = plan_entry_quantity(account.nav({SYMBOL: decision_bar.close}), D("0.80"),
                    make_protection_price(side, decision_bar.close), account.cash_available,
                    reserve=D("0.00"), lot_size=100, fee_schedule=fee_schedule)
                quantity = min(target, liquidity.cap_shares)
            else:
                quantity = min(sellable, liquidity.cap_shares)
            if quantity <= 0:
                _json_line(order_path, {"trade_date": day.isoformat(), "decision_at": at.isoformat(),
                                        "side": side.value, "status": "REJECTED_ZERO_QUANTITY"})
                continue
            order_id = f"p6-{day:%Y%m%d}-{at:%H%M}-{side.value.lower()}"
            expire_at = execution_bar.interval_end
            assert expire_at is not None
            order = OrderIntent(order_id, SYMBOL, side, quantity,
                make_protection_price(side, decision_bar.close), at, expire_at,
                liquidity_reference=liquidity, decision_at=at, arrival_at=arrival)
            if side is Side.BUY:
                reserve = D(quantity) * order.limit_price + fee_schedule.estimate(Side.BUY, D(quantity) * order.limit_price)
                reserve = min(account.cash_available, reserve)
                account.reserve_buy(order_id, reserve)
            else:
                account.reserve_sell(order_id, SYMBOL, quantity, day)
            pending = (order, fee_schedule, quantity,
                       target if side is Side.BUY else sellable, liquidity)

        if pending is not None:
            order, _, _, _, _ = pending
            if order.side is Side.BUY:
                account.cancel_buy(order.order_id)
            else:
                account.cancel_sell(order.order_id)
            _json_line(order_path, {"order_id": order.order_id, "side": order.side.value,
                "trade_date": day.isoformat(), "status": "CANCELLED_AT_SESSION_END",
                "decision_at": order.decision_at.isoformat(), "arrival_at": order.arrival_at.isoformat()})

        # Apply effective-date actions before the close valuation and checkpoint.
        for event in all_dividends:
            account.apply_cash_dividend_event(event, day)
        # Carry marks across dates. Daily close is a mark only and does not place orders.
        close = _decimal(day_row.get("close_raw"))
        if close is not None:
            equity_marks[SYMBOL] = close
        nav = account.nav(equity_marks)
        _json_line(nav_path, {"trade_date": day.isoformat(), "valuation_at": datetime.combine(day, time(15, 0), TZ).isoformat(),
                              "cash_cny": str(account.cash_total), "shares": account.shares_total,
                              "mark_price_cny": str(equity_marks.get(SYMBOL)) if SYMBOL in equity_marks else None,
                              "receivables_cny": str(account.receivables), "nav_cny": str(nav),
                              "drawdown_from_initial": str((D("1000000") - nav) / D("1000000"))})
        rec = reconcile_account_events(D("1000000.00"), fills, all_dividends, day,
            account.cash_total, account.receivables, {SYMBOL: account.shares_total})
        if not rec.passed:
            failure = {"status": "P6_REPLAY_FAILED_INDEPENDENT_ACCOUNT_RECONCILIATION",
                "trade_date": day.isoformat(), "recorded_cash_available_cny": str(account.cash_available),
                "recorded_cash_reserved_cny": str(account.cash_reserved),
                "expected_cash_cny": str(rec.expected_cash), "recorded_cash_total_cny": str(rec.recorded_cash),
                "cash_difference_cny": str(rec.cash_difference),
                "expected_shares": dict(rec.expected_shares),
                "recorded_shares": dict(rec.recorded_shares), "share_differences": dict(rec.share_differences)}
            (output / "failure.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
            raise RuntimeError(f"independent reconciliation failed on {day}: {rec}")
        cash_reconciliation = rec
        _save_checkpoint(output, fingerprint=fingerprint, day=day, account=account,
                         nav=nav, fill_count=len(fills))
        if progress:
            progress(f"P6 {day_index}/{len(trade_days)} sessions; decisions={decisions_count}; unique_api_requests={len(attempted_hashes)}; NAV={nav}")
        historical_bars = (historical_bars + bars)[-48:]
        warm_volumes.extend((day, bar.interval_start.strftime("%H:%M"), bar.volume_shares)
                            for bar in bars if bar.interval_start is not None)

    final_nav = account.nav(equity_marks)
    source_paths = [daily_csv, dividend_config, *status_paths]
    errors = _read_jsonl(error_path)
    summary = {
        "schema": "jevquant-p6-live-sample/v1",
        "run_status": ("RETROSPECTIVE_JEV_DIAGNOSTIC_WITH_API_GAPS" if failed_hashes
                       else "RETROSPECTIVE_JEV_DIAGNOSTIC_AFTER_RECOVERED_API_RETRY" if errors
                       else "RETROSPECTIVE_JEV_DIAGNOSTIC_NOT_FORWARD_SIMULATION"),
        "symbol": SYMBOL, "initial_cash_cny": "1000000.00",
        "execution_mode": execution_mode,
        "fill_policy": ("instant snapshot-bar close, zero delay/slippage/liquidity cap; dated fees and T+1 retained"
                        if execution_mode == "instant_snapshot_close" else
                        "five-minute delayed next-bar open proxy with adverse slippage and historical liquidity cap"),
        "start_date": trade_days[0].isoformat(), "end_date": trade_days[-1].isoformat(),
        "sessions": len(trade_days), "decision_count": decisions_count,
        "provider_call_count": len(prior_usage) + len(failed_hashes),
        "provider_call_attempt_count": len(prior_usage) + sum(
            1 for row in errors if row.get("api_request_attempted")),
        "failed_provider_request_count": len(failed_hashes),
        "resolved_provider_error_count": len({row.get("request_hash") for row in errors
            if row.get("api_request_attempted") and row.get("request_hash") not in failed_hashes}),
        "decision_error_count": len(errors), "resolved_model": "jev-1.13.0",
        "target_entry_fraction": "0.80", "ending_cash_cny": str(account.cash_total),
        "ending_shares": account.shares_total, "ending_nav_cny": str(final_nav),
        "total_return": str(final_nav / D("1000000") - D("1")), "fees_cny": str(fee_total),
        "fill_count": len(fills), "account_reconciled": bool(cash_reconciliation and cash_reconciliation.passed),
        "reconciliation": {"expected_cash_cny": str(cash_reconciliation.expected_cash) if cash_reconciliation else None,
                           "recorded_cash_cny": str(cash_reconciliation.recorded_cash) if cash_reconciliation else None,
                           "expected_receivables_cny": str(cash_reconciliation.expected_receivables) if cash_reconciliation else None,
                           "share_differences": dict(cash_reconciliation.share_differences) if cash_reconciliation else None},
        "assumptions": ["vendor label L assumed to represent [L-5m,L]",
            "availability is assumed at interval end; actual provider delay is unknown",
            "retrospective status records are not point-in-time evidence",
            ("instant fill at the same decision snapshot close is an idealized attribution diagnostic, not executable timing"
             if execution_mode == "instant_snapshot_close" else
             "one bar-open fill proxy with 5bp adverse slippage; no queue reconstruction"),
            "cash-dividend configuration is partial and scoped to 2023-2024",
            "this is historical replay using live JEV calls, not forward simulation or strategy acceptance"],
        "source_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths},
        "minute_partition_sha256": {day.isoformat(): input_hashes["minute_partitions"][day.isoformat()]
                                     for day in (*warm_days, *trade_days)},
        "checkpoint": {"last_completed_session": trade_days[-1].isoformat(),
                       "run_fingerprint": fingerprint, "event_replay_resume_supported": True,
                       "resume_source_session": checkpoint_day.isoformat() if checkpoint_day else None,
                       "event_replay_matched_checkpoint": True},
        "artifacts": {"decisions": "decisions.jsonl", "orders": "orders.jsonl",
                      "fills": "fills.jsonl", "nav": "nav.jsonl", "usage": "jev-usage.jsonl",
                      "cache": "jev-response-cache.json", "errors": "errors.jsonl"},
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
                                          encoding="utf-8", newline="\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="P6 live-JEV decision chain on retrospective Moutai data")
    parser.add_argument("--minute-root", type=Path, required=True)
    parser.add_argument("--daily-csv", type=Path, required=True)
    parser.add_argument("--status-2022-2023", type=Path, required=True)
    parser.add_argument("--status-2024-plus", type=Path, required=True)
    parser.add_argument("--dividends", type=Path, default=Path("configs/moutai_2023_2024_cash_dividends.json"))
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--sessions", type=int, choices=(1, 20), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execution-mode", choices=("delayed_bar_open", "instant_snapshot_close"),
                        default="delayed_bar_open",
                        help="instant_snapshot_close is an idealized zero-delay attribution diagnostic")
    parser.add_argument("--resume", action="store_true",
                        help="restore from the last verified session, or replay from initial cash in a cache-only retry directory")
    parser.add_argument("--acknowledge-historical-data-to-live-jev", action="store_true",
                        help="acknowledge sending historical price/account snapshots to the configured JEV provider")
    args = parser.parse_args()
    if not args.acknowledge_historical_data_to_live_jev:
        parser.error("must acknowledge historical price/account snapshots are sent to the configured JEV provider")
    summary = run_p6_sample(minute_root=args.minute_root, daily_csv=args.daily_csv,
        status_paths=(args.status_2022_2023, args.status_2024_plus), dividend_config=args.dividends,
        output=args.output, start=args.start, sessions=args.sessions, resume=args.resume,
        execution_mode=args.execution_mode,
        progress=lambda message: print(message, flush=True))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
