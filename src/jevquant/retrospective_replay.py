"""Explicitly assumption-bound P4 replay over the user's Moutai history.

This module does not promote raw timestamp labels or retrospective status files
to verified live-time evidence. Every output is marked diagnostic only.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from .actions import load_cash_dividends
from .baseline_replay import (
    BaselineReplayConfig, SessionExecutionRules, iter_baseline_suite,
    replay_to_jsonable, summarize_baseline_result,
)
from .data import read_daily_vendor_csv, read_vendor_minute_day
from .models import Bar

D = Decimal
SYMBOL = "600519.SH"
TZ = ZoneInfo("Asia/Shanghai")
EXPECTED_END_LABELS = tuple(
    [time(9, minute) for minute in range(35, 60, 5)]
    + [time(10, minute) for minute in range(0, 60, 5)]
    + [time(11, minute) for minute in range(0, 35, 5)]
    + [time(13, minute) for minute in range(5, 60, 5)]
    + [time(14, minute) for minute in range(0, 60, 5)]
    + [time(15, 0)]
)


def normalize_end_labeled_diagnostic(day: date, source_bars: Iterable[Bar]) -> list[Bar]:
    """Map the 48 standard labels to [label-5m, label] under an explicit hypothesis.

    The separate 09:30 opening record is intentionally excluded. This is a
    research scenario, not a provider-verified conversion.
    """
    rows = sorted((bar for bar in source_bars if bar.trade_date == day),
                  key=lambda bar: bar.source_time)
    standard = [bar for bar in rows if bar.source_time.time() != time(9, 30)]
    labels = tuple(bar.source_time.time() for bar in standard)
    if labels != EXPECTED_END_LABELS:
        raise ValueError(f"unexpected end-label grid for {day}: expected 48 standard labels")
    normalized: list[Bar] = []
    for bar in standard:
        interval_end = bar.source_time
        interval_start = interval_end - timedelta(minutes=5)
        if interval_start.date() != day or interval_end.date() != day:
            raise ValueError(f"bar interval crosses a trade date for {day}")
        normalized.append(Bar(
            symbol=bar.symbol, source_time=bar.source_time,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume_shares=bar.volume_shares, amount_cny=bar.amount_cny,
            trade_date=day, available_at=interval_end,
            source_id=bar.source_id, source_row_id=bar.source_row_id,
            quality_flags=(bar.quality_flags - {"time_label_semantics_unverified"})
            | {"assumed_end_labeled_retrospective_only", "availability_assumed_at_interval_end"},
            interval_start=interval_start, interval_end=interval_end,
        ))
    return normalized


def eligible_execution_starts(day: date, all_bars: Iterable[Bar]) -> tuple[datetime, ...]:
    """Return only the plan's allowed start times; other bars remain for marks/signals."""
    starts = []
    for bar in all_bars:
        start = bar.interval_start
        if start is None:
            raise ValueError("normalized diagnostic bar lacks interval start")
        clock = start.time()
        morning = time(9, 35) <= clock <= time(11, 25)
        afternoon = time(13, 5) <= clock <= time(14, 50)
        if (morning or afternoon) and start.date() == day and start.minute % 5 == 0:
            starts.append(start)
    return tuple(sorted(starts))


def _read_status(paths: tuple[Path, ...], expected_dates: set[date]) -> dict[date, tuple[bool, bool]]:
    status: dict[date, tuple[bool, bool]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        fields = payload.get("status_fields")
        rows = payload.get("status_rows")
        if not isinstance(fields, list) or not isinstance(rows, list):
            raise ValueError(f"invalid status file: {path}")
        date_i, code_i = fields.index("date"), fields.index("code")
        trade_i, st_i = fields.index("tradestatus"), fields.index("isST")
        for row in rows:
            if row[code_i].lower() not in {"sh.600519", "600519.sh"}:
                continue
            day = date.fromisoformat(str(row[date_i]))
            if day not in expected_dates:
                continue
            if day in status:
                raise ValueError(f"duplicate retrospective status row: {day}")
            tradestatus, is_st = str(row[trade_i]), str(row[st_i])
            if tradestatus not in {"0", "1"} or is_st not in {"0", "1"}:
                raise ValueError(f"unknown status value on {day}")
            status[day] = (tradestatus == "1", is_st == "1")
    if set(status) != expected_dates:
        missing = sorted(day.isoformat() for day in expected_dates - set(status))
        raise ValueError(f"retrospective status rows do not cover run dates; missing={missing[:10]}")
    return status


def _parquet_day(root: Path, day: date) -> list[Bar]:
    path = root / str(day.year) / f"{day:%Y%m%d}.parquet"
    if not path.is_file():
        raise FileNotFoundError(path)
    return read_vendor_minute_day(path, SYMBOL)


@contextmanager
def _open_jsonl_gz(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8", newline="\n") as text:
                yield text


def run_retrospective_baselines(
    *, minute_root: Path, daily_csv: Path, status_paths: tuple[Path, ...],
    dividend_config: Path, output: Path, start: date, end: date,
    random_seeds: range = range(100),
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Run one baseline suite with clearly recorded retrospective assumptions."""
    all_daily = read_daily_vendor_csv(daily_csv, SYMBOL)
    daily_by_date = {row["trade_date"]: row for row in all_daily}
    if len(daily_by_date) != len(all_daily):
        raise ValueError("daily input has duplicate trade dates")
    all_sessions = tuple(sorted(daily_by_date))
    sessions = tuple(day for day in all_sessions if start <= day <= end)
    if not sessions or sessions[0] != start or sessions[-1] != end:
        raise ValueError("requested endpoints must both be daily trading sessions")
    session_set = set(sessions)
    status = _read_status(status_paths, session_set)
    session_index = {day: index for index, day in enumerate(all_sessions)}
    if any(session_index[day] + 1 >= len(all_sessions) for day in sessions):
        raise ValueError("daily file lacks next-session dates for the requested window")
    next_trade = {day: all_sessions[session_index[day] + 1] for day in sessions}

    # The initial 20-slot liquidity warm-up is prior-session data only; it does
    # not seed cash, positions, marks, or model state in this account.
    first_index = session_index[start]
    warmup_sessions = all_sessions[max(0, first_index - 20):first_index]
    if len(warmup_sessions) != 20:
        raise ValueError("need 20 prior daily sessions for the same-slot liquidity reference")
    prior_volume: list[tuple[date, str, int]] = []
    for index, day in enumerate(warmup_sessions, 1):
        normalized = normalize_end_labeled_diagnostic(day, _parquet_day(minute_root, day))
        prior_volume.extend((day, bar.interval_start.strftime("%H:%M"), bar.volume_shares)
                            for bar in normalized if bar.interval_start is not None)
        if progress_callback and (index % 5 == 0 or index == len(warmup_sessions)):
            progress_callback(f"loaded prior-volume warmup {index}/{len(warmup_sessions)} sessions")

    bars: list[Bar] = []
    all_bar_schedule: dict[date, tuple[datetime, ...]] = {}
    execution_schedule: dict[date, tuple[datetime, ...]] = {}
    for index, day in enumerate(sessions, 1):
        normalized = normalize_end_labeled_diagnostic(day, _parquet_day(minute_root, day))
        bars.extend(normalized)
        all_bar_schedule[day] = tuple(bar.interval_start for bar in normalized if bar.interval_start is not None)
        execution_schedule[day] = eligible_execution_starts(day, normalized)
        if progress_callback and (index % 5 == 0 or index == len(sessions)):
            progress_callback(f"loaded replay data {index}/{len(sessions)} sessions")

    closes = tuple((day, row["close_raw"]) for day, row in daily_by_date.items()
                   if row.get("close_raw") is not None)
    limits: dict[date, SessionExecutionRules] = {}
    eligible: dict[date, bool] = {}
    close_at: dict[date, datetime] = {}
    for day in sessions:
        row = daily_by_date[day]
        up, down = row.get("limit_up_raw"), row.get("limit_down_raw")
        if up is None or down is None:
            raise ValueError(f"daily limit prices missing on {day}")
        trading, is_st = status[day]
        limits[day] = SessionExecutionRules(not trading, up, down)
        eligible[day] = trading and not is_st
        close_at[day] = datetime.combine(day, time(15, 0), TZ)

    dividend_events = load_cash_dividends(dividend_config, SYMBOL)
    output.mkdir(parents=True, exist_ok=False)
    results = iter_baseline_suite(
        bars=bars, trade_dates=sessions, daily_closes=closes,
        buy_eligible_by_date=eligible, execution_schedule=execution_schedule,
        bar_schedule_by_date=all_bar_schedule, prior_volume_history=prior_volume,
        session_close_by_date=close_at, next_trade_date_by_date=next_trade,
        execution_rules_by_date=limits, dividends=dividend_events,
        random_seeds=random_seeds, initial_cash=D("1000000.00"),
    )

    summary_rows = []
    total_accounts = 5 + len(random_seeds)
    with ExitStack() as stack:
        streams = {
            name: stack.enter_context(_open_jsonl_gz(output / f"{name}.jsonl.gz"))
            for name in ("decisions", "orders", "fills", "valuations")
        }
        for account_index, (name, result) in enumerate(results, 1):
            summary_rows.append(summarize_baseline_result(result, D("1000000.00")))
            serial = replay_to_jsonable(result)
            grouped = {
                "decisions": result.decisions,
                "orders": result.orders,
                "fills": serial["fills"],
                "valuations": result.nav_curve,
            }
            for stream_name, records in grouped.items():
                for item in records:
                    record = {"baseline": name, **item}
                    streams[stream_name].write(json.dumps(
                        record, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                    streams[stream_name].write("\n")
            if progress_callback and (account_index % 5 == 0 or account_index == total_accounts):
                progress_callback(f"completed baseline accounts {account_index}/{total_accounts}")
    payload = {
        "schema": "jevquant-p4-retrospective-baselines/v1",
        "run_status": "DIAGNOSTIC_ONLY_NOT_FORMAL_HISTORICAL_EXECUTION_ACCEPTANCE",
        "symbol": SYMBOL,
        "initial_cash_cny": "1000000.00",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "sessions": len(sessions), "warmup_volume_sessions": [d.isoformat() for d in warmup_sessions],
        "data_root_source_paths": {"minute_root": str(minute_root), "daily_csv": str(daily_csv)},
        "timestamp_hypothesis": {
            "mode": "standard_labels_are_interval_end",
            "mapping": "trade_time label L maps to [L-5 minutes, L]",
            "special_0930_record": "excluded from normalized decision/execution bars",
            "availability": "assumed immediately at interval_end; actual provider release delay is unknown",
            "status_data": "retrospective captured values; not point-in-time",
            "fills": "bar-open proxy only; no queue or real execution claim",
        },
        "eligible_execution_start_windows": ["09:35-11:25", "13:05-14:50"],
        "fee_label": "modeled fees per configured schedule; not tax-certified",
        "account_reconciled": all(row["reconciliation_passed"] for row in summary_rows),
        "all_valuations_complete": all(not row["valuation_incomplete"] for row in summary_rows),
        "account_count": len(summary_rows), "summaries": summary_rows,
        "source_files_sha256": {
            "daily_csv": hashlib.sha256(daily_csv.read_bytes()).hexdigest(),
            "dividend_config": hashlib.sha256(dividend_config.read_bytes()).hexdigest(),
            "status_files": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in status_paths},
        },
    }
    (output / "summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                                          encoding="utf-8", newline="\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="P4 retrospective-only Moutai baseline diagnostics")
    parser.add_argument("--minute-root", type=Path, required=True)
    parser.add_argument("--daily-csv", type=Path, required=True)
    parser.add_argument("--status-2022-2023", type=Path, required=True)
    parser.add_argument("--status-2024-plus", type=Path, required=True)
    parser.add_argument("--dividends", type=Path, default=Path("configs/moutai_2023_2024_cash_dividends.json"))
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-diagnostic-end-label-hypothesis", action="store_true",
                        help="explicitly accept an unverified end-label scenario for diagnostic output only")
    args = parser.parse_args()
    if not args.confirm_diagnostic_end_label_hypothesis:
        parser.error("must pass --confirm-diagnostic-end-label-hypothesis; result is never formal acceptance")
    payload = run_retrospective_baselines(
        minute_root=args.minute_root, daily_csv=args.daily_csv,
        status_paths=(args.status_2022_2023, args.status_2024_plus),
        dividend_config=args.dividends, output=args.output, start=args.start, end=args.end,
        progress_callback=lambda message: print(message, flush=True),
    )
    print(json.dumps({
        "output": str(args.output.resolve()), "run_status": payload["run_status"],
        "sessions": payload["sessions"], "account_count": payload["account_count"],
        "account_reconciled": payload["account_reconciled"],
        "all_valuations_complete": payload["all_valuations_complete"],
        "summaries": payload["summaries"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
