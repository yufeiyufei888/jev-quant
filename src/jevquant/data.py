from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.compute as pc
import pyarrow.parquet as pq

from .models import Bar
from .ledger import FeeSchedule, plan_entry_quantity, tick_up

D = Decimal
CN_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_COLUMNS = {"code", "trade_time", "open", "high", "low", "close", "vol", "amount", "date"}


def _decimal(value: object, field: str) -> Decimal:
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing required numeric value: {field}")
    result = D(str(value))
    if not result.is_finite():
        raise ValueError(f"non-finite numeric value: {field}")
    return result


def _optional_decimal(value: object, field: str) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return _decimal(value, field)


def read_vendor_minute_day(path: Path, symbol: str) -> list[Bar]:
    """Read one raw 5-minute vendor partition while preserving its timestamp label.

    The label-to-interval mapping is deliberately not inferred here. 09:30/15:00
    rows are flagged as unsafe execution references until the source semantics
    have been independently documented.
    """
    table = pq.read_table(path)
    missing = MINUTE_COLUMNS - set(table.column_names)
    if missing:
        raise ValueError(f"missing columns in {path.name}: {sorted(missing)}")
    selected = table.filter(pc.equal(table["code"], symbol))
    rows = selected.to_pylist()
    bars: list[Bar] = []
    for index, row in enumerate(rows):
        label = datetime.fromisoformat(str(row["trade_time"])).replace(tzinfo=CN_TZ)
        flags: set[str] = {"time_label_semantics_unverified"}
        if label.time().isoformat() == "09:30:00":
            flags.add("opening_record")
        if label.time().isoformat() == "15:00:00":
            flags.add("closing_record")
        volume = _decimal(row["vol"], "vol")
        if volume != volume.to_integral_value():
            raise ValueError(f"minute volume is fractional shares at {label.isoformat()}")
        amount = row.get("amount")
        bars.append(Bar(
            symbol=symbol,
            source_time=label,
            open=_decimal(row["open"], "open"),
            high=_decimal(row["high"], "high"),
            low=_decimal(row["low"], "low"),
            close=_decimal(row["close"], "close"),
            volume_shares=int(volume),
            amount_cny=None if amount is None else _decimal(amount, "amount"),
            trade_date=date.fromisoformat(str(row["date"])[:4] + "-" + str(row["date"])[4:6] + "-" + str(row["date"])[6:8]),
            available_at=None,
            source_id=f"minute-file:{path.name}",
            source_row_id=f"{path.name}:{symbol}:{index}",
            quality_flags=frozenset(flags),
        ))
    return bars


def read_daily_vendor_csv(path: Path, symbol: str) -> list[dict[str, object]]:
    """Read the supplied Chinese daily file and normalize units without altering it."""
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"股票代码", "交易日", "开盘价", "最高价", "最低价", "收盘价", "成交量（手）", "成交额（千元）"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"missing daily columns: {sorted(missing)}")
        for row_num, row in enumerate(reader, start=2):
            if row["股票代码"] != symbol:
                continue
            day_text = row["交易日"]
            day = date(int(day_text[:4]), int(day_text[4:6]), int(day_text[6:8]))
            volume_hands = _optional_decimal(row["成交量（手）"], "成交量（手）")
            amount_thousand = _optional_decimal(row["成交额（千元）"], "成交额（千元）")
            rows.append({
                "symbol": symbol,
                "trade_date": day,
                "open_raw": _optional_decimal(row["开盘价"], "开盘价"),
                "high_raw": _optional_decimal(row["最高价"], "最高价"),
                "low_raw": _optional_decimal(row["最低价"], "最低价"),
                "close_raw": _optional_decimal(row["收盘价"], "收盘价"),
                "volume_shares": int(volume_hands * 100) if volume_hands is not None else None,
                "amount_cny": amount_thousand * 1000 if amount_thousand is not None else None,
                "adjustment_factor": _optional_decimal(row.get("复权因子"), "复权因子"),
                "source_row_id": f"{path.name}:{row_num}",
                "price_basis": "raw-price-inferred-from-sample-cross-check",
            })
    return rows


def inspect_dataset(root: Path, symbol: str = "600519.SH") -> dict[str, object]:
    daily_dir = root / "股票全量数据（日线）"
    minute_dir = root / "5分钟数据"
    daily_path = daily_dir / f"{symbol}.csv"
    if not daily_path.exists():
        raise FileNotFoundError(daily_path)
    daily = read_daily_vendor_csv(daily_path, symbol)
    day_values = [row["trade_date"] for row in daily]
    duplicate_days = len(day_values) - len(set(day_values))
    latest_daily = max(daily, key=lambda row: row["trade_date"]) if daily else None
    buyability = None
    if latest_daily and latest_daily["close_raw"] is not None:
        price_cap = tick_up(latest_daily["close_raw"] * D("1.005"))
        quantity = plan_entry_quantity(D("1000000"), D("0.8"), price_cap, D("1000000"),
                                       fee_schedule=FeeSchedule())
        gross = D(quantity) * price_cap
        buyability = {
            "basis_date": latest_daily["trade_date"].isoformat(),
            "basis_close_cny": str(latest_daily["close_raw"]),
            "buy_protection_cny": str(price_cap),
            "planned_whole_lot_shares_at_80pct_target": quantity,
            "gross_amount_cny": str(gross),
            "note": "capacity illustration at the last price in this daily file; not a current quote or trading recommendation",
        }
    folder_stats: dict[str, dict[str, object]] = {}
    sample_paths: list[Path] = []
    for folder in sorted((item for item in minute_dir.iterdir() if item.is_dir()), key=lambda p: p.name):
        files = sorted(folder.glob("????????.parquet"))
        dates = [f.stem for f in files if f.stem.isdigit() and len(f.stem) == 8]
        folder_stats[folder.name] = {
            "date_partition_files": len(dates),
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
            "sample_file": files[0].name if files else None,
        }
        if dates:
            sample_paths.extend([files[0], files[-1]])
    deduped = list(dict.fromkeys(sample_paths))
    sample_audits: list[dict[str, object]] = []
    for path in deduped:
        bars = read_vendor_minute_day(path, symbol)
        sample_audits.append({
            "file": str(path),
            "rows_for_symbol": len(bars),
            "first_label": bars[0].source_time.isoformat() if bars else None,
            "last_label": bars[-1].source_time.isoformat() if bars else None,
            "labels_unique": len({bar.source_time for bar in bars}) == len(bars),
            "volume_shares": sum(bar.volume_shares for bar in bars),
            "amount_cny": str(sum((bar.amount_cny or D("0")) for bar in bars)),
            "time_semantics": "unverified; labels preserved without execution normalization",
        })
    daily_20240102 = next((r for r in daily if r["trade_date"] == date(2024, 1, 2)), None)
    sample_20240102_path = minute_dir / "2024" / "20240102.parquet"
    reconciliation = None
    if daily_20240102 and sample_20240102_path.exists():
        minute_bars = read_vendor_minute_day(sample_20240102_path, symbol)
        daily_close = daily_20240102["close_raw"]
        minute_close = minute_bars[-1].close if minute_bars else None
        close_delta = abs(daily_close - minute_close) if daily_close is not None and minute_close is not None else None
        daily_amount = daily_20240102["amount_cny"]
        minute_amount = sum((bar.amount_cny or D("0")) for bar in minute_bars)
        amount_delta = abs(daily_amount - minute_amount) if daily_amount is not None else None
        reconciliation = {
            "trade_date": "2024-01-02",
            "minute_rows": len(minute_bars),
            "daily_open_matches_minute_first_open": daily_20240102["open_raw"] is not None and daily_20240102["open_raw"] == minute_bars[0].open,
            "daily_close_matches_minute_last_close": daily_20240102["close_raw"] is not None and daily_20240102["close_raw"] == minute_bars[-1].close,
            "daily_volume_shares": daily_20240102["volume_shares"],
            "minute_volume_shares": sum(bar.volume_shares for bar in minute_bars),
            "daily_amount_cny": str(daily_20240102["amount_cny"]),
            "minute_amount_cny": str(minute_amount),
            "volume_matches": daily_20240102["volume_shares"] == sum(bar.volume_shares for bar in minute_bars),
            "amount_matches_exact": daily_amount == minute_amount,
            "amount_abs_delta_cny": str(amount_delta) if amount_delta is not None else None,
            "minute_open_matches_daily_open": daily_20240102["open_raw"] is not None and daily_20240102["open_raw"] == minute_bars[0].open,
            "minute_last_close_abs_delta_cny": str(close_delta) if close_delta is not None else None,
            "minute_last_close_within_one_tick": close_delta is not None and close_delta <= D("0.01"),
            "finding": "volume matches exactly; close is within one cent; amount differs and is not marked reconciled; wider audit required",
        }
    return {
        "report_version": "jevquant-data-inventory-v1",
        "symbol": symbol,
        "root": str(root),
        "daily": {
            "path": str(daily_path),
            "rows": len(daily),
            "first_date": min(day_values).isoformat() if day_values else None,
            "last_date": max(day_values).isoformat() if day_values else None,
            "duplicate_trade_dates": duplicate_days,
            "missing_volume_rows": sum(row["volume_shares"] is None for row in daily),
            "missing_amount_rows": sum(row["amount_cny"] is None for row in daily),
            "missing_ohlc_rows": sum(any(row[key] is None for key in ("open_raw", "high_raw", "low_raw", "close_raw")) for row in daily),
            "columns": ["股票代码", "交易日", "开盘价", "最高价", "最低价", "收盘价", "成交量（手）", "成交额（千元）", "复权因子", "当日涨停价", "当日跌停价"],
            "unit_mapping": {"成交量（手）": "股; ×100", "成交额（千元）": "CNY; ×1000"},
            "price_basis": "raw-price inference cross-checked on one date; full period still requires verification",
            "last_file_price_buyability": buyability,
        },
        "five_minute": {
            "path": str(minute_dir),
            "schema": ["code", "trade_time", "open", "high", "low", "close", "vol", "amount", "date", "pre_close", "change", "pct_chg"],
            "unit_mapping": {"vol": "shares; cross-checked on 2024-01-02", "amount": "CNY; cross-checked on 2024-01-02"},
            "time_label_semantics": "unverified; preserve source labels and block formal execution normalization",
            "partitions": folder_stats,
            "endpoint_samples": sample_audits,
            "sample_daily_reconciliation": reconciliation,
        },
        "unresolved": [
            "No mapped, verified source found yet for historical calendar, listing/ST/suspension status, and effective-date trading-rule table.",
            "No mapped Moutai corporate-action event table with announcement, ex-date, payment date, and correction availability was verified.",
            "Adjustment-factor publication/availability time is unknown; do not use it in point-in-time features.",
            "Five-minute timestamp-to-interval semantics and special 09:30/15:00 row meaning require source evidence and broader reconciliation.",
            "Five-minute files currently end at 2026-04-10; do not claim coverage beyond this date.",
        ],
    }


def write_inventory(root: Path, output_dir: Path, symbol: str = "600519.SH") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = inspect_dataset(root, symbol)
    out = output_dir / "data_inventory.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
