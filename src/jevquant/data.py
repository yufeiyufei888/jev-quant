from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from .models import Bar
from .ledger import FeeSchedule, plan_entry_quantity, tick_up

D = Decimal
CN_TZ = ZoneInfo("Asia/Shanghai")
MINUTE_COLUMNS = {"code", "trade_time", "open", "high", "low", "close", "vol", "amount", "date"}
VOLUME_OVERRIDE_FILE = Path(__file__).resolve().parents[2] / "configs" / "moutai_minute_volume_unit_overrides.json"


def _load_volume_unit_overrides() -> dict[tuple[str, str], dict[str, str]]:
    if not VOLUME_OVERRIDE_FILE.is_file():
        return {}
    data = json.loads(VOLUME_OVERRIDE_FILE.read_text(encoding="utf-8"))
    if data.get("schema") != "jevquant-minute-volume-unit-overrides/v1":
        raise ValueError("unknown minute volume override schema")
    result: dict[tuple[str, str], dict[str, str]] = {}
    for item in data.get("corrections", []):
        key = (data["symbol"], item["date"])
        if key in result or D(item["raw_volume_to_shares"]) <= 0:
            raise ValueError("duplicate or invalid minute volume correction")
        result[key] = item
    return result


def _verify_volume_override(path: Path, symbol: str, day: str,
                            overrides: dict[tuple[str, str], dict[str, str]]) -> Decimal:
    item = overrides.get((symbol, day))
    if item is None:
        return D("1")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if item.get("source_file_sha256") != actual:
        raise ValueError(f"minute volume correction source hash mismatch for {day}")
    return D(item["raw_volume_to_shares"])


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


def read_vendor_minute_day(path: Path, symbol: str, *,
                           volume_unit_overrides: dict[tuple[str, str], dict[str, str]] | None = None) -> list[Bar]:
    """Read one raw 5-minute vendor partition while preserving its timestamp label.

    The label-to-interval mapping is deliberately not inferred here. 09:30/15:00
    rows are flagged as unsafe execution references until the source semantics
    have been independently documented.
    """
    overrides = _load_volume_unit_overrides() if volume_unit_overrides is None else volume_unit_overrides
    file_date = path.stem
    file_date_iso = f"{file_date[:4]}-{file_date[4:6]}-{file_date[6:8]}" if len(file_date) == 8 and file_date.isdigit() else None
    volume_scale = (_verify_volume_override(path, symbol, file_date_iso, overrides)
                    if file_date_iso else D("1"))
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
        if volume_scale != 1:
            flags.add("source_volume_unit_corrected_by_hash_bound_rule")
        volume = _decimal(row["vol"], "vol") * volume_scale
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
                "previous_close_raw": _optional_decimal(row.get("前收盘价"), "前收盘价"),
                "limit_up_raw": _optional_decimal(row.get("当日涨停价"), "当日涨停价"),
                "limit_down_raw": _optional_decimal(row.get("当日跌停价"), "当日跌停价"),
                "volume_shares": int(volume_hands * 100) if volume_hands is not None else None,
                "amount_cny": amount_thousand * 1000 if amount_thousand is not None else None,
                "adjustment_factor": _optional_decimal(row.get("复权因子"), "复权因子"),
                "source_row_id": f"{path.name}:{row_num}",
                "price_basis": "raw-price-inferred-from-sample-cross-check",
            })
    return rows


def audit_daily_limit_fields(rows: list[dict[str, object]], start: date = date(2023, 1, 1),
                             end: date = date(2024, 12, 31)) -> dict[str, object]:
    """Describe supplied daily limit-band fields; do not certify their PIT availability."""
    selected = [row for row in rows if start <= row["trade_date"] <= end]
    complete = [row for row in selected if row.get("limit_up_raw") is not None
                and row.get("limit_down_raw") is not None]
    within_band = 0
    formula_matches = 0
    formula_comparable = 0
    formula_mismatches: list[dict[str, str]] = []
    invalid_bands: list[str] = []
    missing: list[str] = []
    for row in selected:
        day = row["trade_date"].isoformat()
        up, down = row.get("limit_up_raw"), row.get("limit_down_raw")
        if up is None or down is None:
            missing.append(day)
            continue
        if up < down or up <= 0 or down <= 0:
            invalid_bands.append(day)
            continue
        low, high = row.get("low_raw"), row.get("high_raw")
        if low is not None and high is not None and down <= low <= high <= up:
            within_band += 1
        previous_close = row.get("previous_close_raw")
        if previous_close is not None and previous_close > 0:
            formula_comparable += 1
            expected_up = (previous_close * D("1.10")).quantize(D("0.01"), rounding=ROUND_HALF_UP)
            expected_down = (previous_close * D("0.90")).quantize(D("0.01"), rounding=ROUND_HALF_UP)
            if (up, down) == (expected_up, expected_down):
                formula_matches += 1
            else:
                formula_mismatches.append({
                    "date": day,
                    "previous_close": str(previous_close),
                    "observed_up": str(up), "observed_down": str(down),
                    "formula_up": str(expected_up), "formula_down": str(expected_down),
                })
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "daily_rows": len(selected),
        "complete_up_down_band_rows": len(complete),
        "missing_band_dates": missing,
        "invalid_band_dates": invalid_bands,
        "ohlc_within_band_rows": within_band,
        "previous_close_10pct_formula_comparable_rows": formula_comparable,
        "previous_close_10pct_formula_match_rows": formula_matches,
        "previous_close_10pct_formula_mismatches": formula_mismatches,
        "formula_rounding": "nearest CNY 0.01 using ROUND_HALF_UP",
        "interpretation": "source-field and arithmetic cross-check only; historical rule provenance and source availability are not certified",
    }


def audit_minute_partitions(root: Path, symbol: str, daily_csv: Path | None = None,
                            progress_callback=None,
                            supplemental_daily_parquet_root: Path | None = None) -> dict[str, object]:
    """Read every date partition and report symbol coverage and daily aggregates.

    This audit records raw labels and reconciles values, but deliberately does
    not infer whether labels mark bar starts/ends or whether 09:30 is executable.
    """
    files = sorted(p for p in root.rglob("*.parquet")
                   if p.stem.isdigit() and len(p.stem) == 8)
    daily_map: dict[date, dict[str, object]] = {}
    if daily_csv is not None:
        daily_map.update({row["trade_date"]: row for row in read_daily_vendor_csv(daily_csv, symbol)})
    supplemental_rows = 0
    if supplemental_daily_parquet_root is not None:
        supplement_files = list(supplemental_daily_parquet_root.rglob("*.parquet"))
        if supplement_files:
            supplement = ds.dataset(supplement_files, format="parquet")
            table = supplement.to_table(filter=(ds.field("code") == symbol),
                                        columns=["date", "open", "high", "low", "close", "volume", "amount"])
            for row in table.to_pylist():
                day_text = str(row["date"])
                day = (date(int(day_text[:4]), int(day_text[5:7]), int(day_text[8:10]))
                       if "-" in day_text else date(int(day_text[:4]), int(day_text[4:6]), int(day_text[6:8])))
                daily_map.setdefault(day, {
                    "trade_date": day,
                    "open_raw": _optional_decimal(row["open"], "open"),
                    "high_raw": _optional_decimal(row["high"], "high"),
                    "low_raw": _optional_decimal(row["low"], "low"),
                    "close_raw": _optional_decimal(row["close"], "close"),
                    "volume_shares": int(_decimal(row["volume"], "volume")) if row["volume"] is not None else None,
                    "amount_cny": _optional_decimal(row["amount"], "amount"),
                    "daily_source": "baostock_raw_supplement_captured_2026",
                })
                supplemental_rows += 1
    for row in daily_map.values():
        row.setdefault("daily_source", "user_daily_csv")
    daily_limit_audit = audit_daily_limit_fields(list(daily_map.values()))
    row_counts: Counter[int] = Counter()
    parquet_writer_counts: Counter[str] = Counter()
    parquet_metadata_key_counts: Counter[str] = Counter()
    files_with_provider_metadata: list[str] = []
    files_with_interval_metadata: list[str] = []
    missing_symbol_dates: list[str] = []
    duplicate_label_dates: list[str] = []
    nonstandard_time_grid_dates: list[dict[str, object]] = []
    invalid_rows: list[dict[str, str]] = []
    date_mismatch_rows = 0
    opening_label_days = 0
    closing_label_days = 0
    volume_override_dates_applied: list[str] = []
    volume_override_failures: list[dict[str, str]] = []
    unit_overrides = _load_volume_unit_overrides()
    daily_overlaps = 0
    daily_overlap_by_source: Counter[str] = Counter()
    volume_matches = 0
    volume_mismatches: list[dict[str, str]] = []
    amount_exact_matches = 0
    amount_deltas: list[tuple[str, Decimal, Decimal]] = []
    open_exact_matches = 0
    open_within_tick_matches = 0
    close_within_tick = 0
    first_label: str | None = None
    last_label: str | None = None
    required = {"code", "trade_time", "open", "high", "low", "close", "vol", "amount", "date"}
    expected_labels = (["09:30:00"]
                       + [f"{hour:02d}:{minute:02d}:00"
                          for hour, start, end in ((9, 35, 60), (10, 0, 60), (11, 0, 35),
                                                   (13, 5, 60), (14, 0, 60))
                          for minute in range(start, end, 5)]
                       + ["15:00:00"])

    for index, path in enumerate(files, start=1):
        parquet_metadata = pq.read_metadata(path)
        parquet_writer_counts[parquet_metadata.created_by or "<none>"] += 1
        metadata = parquet_metadata.metadata or {}
        metadata_text = " ".join(
            f"{key.decode('utf-8', 'replace')} {value.decode('utf-8', 'replace')}"
            for key, value in metadata.items()
        ).lower()
        metadata_keys = {key.decode("utf-8", "replace") for key in metadata}
        parquet_metadata_key_counts.update(metadata_keys)
        if any(token in metadata_text for token in ("provider", "vendor", "source_url", "data_source")):
            files_with_provider_metadata.append(path.name)
        if any(token in metadata_text for token in ("interval_start", "interval_end", "bar_type", "timestamp_semantics")):
            files_with_interval_metadata.append(path.name)
        schema = pq.read_schema(path)
        missing = required - set(schema.names)
        if missing:
            invalid_rows.append({"file": path.name, "reason": "missing_columns:" + ",".join(sorted(missing))})
            continue
        table = pq.read_table(path, columns=sorted(required))
        selected = table.filter(pc.equal(table["code"], symbol))
        rows = selected.to_pylist()
        row_counts[len(rows)] += 1
        if not rows:
            missing_symbol_dates.append(path.stem)
            continue
        try:
            date_iso = f"{path.stem[:4]}-{path.stem[4:6]}-{path.stem[6:8]}"
            try:
                volume_scale = _verify_volume_override(path, symbol, date_iso, unit_overrides)
                if volume_scale != 1 and date_iso not in volume_override_dates_applied:
                    volume_override_dates_applied.append(date_iso)
            except ValueError as exc:
                volume_override_failures.append({"date": date_iso, "reason": str(exc)})
                invalid_rows.append({"file": path.name, "reason": "unit_override_rejected_source_hash_mismatch"})
                volume_scale = D("1")
            stamps = [datetime.fromisoformat(str(r["trade_time"])) for r in rows]
            labels = [stamp.strftime("%H:%M:%S") for stamp in stamps]
            if len(set(stamps)) != len(stamps):
                duplicate_label_dates.append(path.stem)
            if labels != expected_labels:
                nonstandard_time_grid_dates.append({
                    "date": path.stem,
                    "actual_count": len(labels),
                    "expected_count": len(expected_labels),
                    "first_difference_index": next((i for i, pair in enumerate(zip(labels, expected_labels))
                                                     if pair[0] != pair[1]),
                                                    min(len(labels), len(expected_labels))),
                })
            if "09:30:00" in labels:
                opening_label_days += 1
            if "15:00:00" in labels:
                closing_label_days += 1
            if first_label is None:
                first_label = stamps[0].isoformat()
            last_label = stamps[-1].isoformat()
            for row, stamp in zip(rows, stamps):
                date_text = str(row["date"])
                row_date = (date(int(date_text[:4]), int(date_text[4:6]), int(date_text[6:8]))
                            if len(date_text) == 8 else date.fromisoformat(date_text[:10]))
                if row_date.isoformat() != date_iso:
                    date_mismatch_rows += 1
                try:
                    low, high = _decimal(row["low"], "low"), _decimal(row["high"], "high")
                    op, close = _decimal(row["open"], "open"), _decimal(row["close"], "close")
                    vol = _decimal(row["vol"], "vol") * volume_scale
                    amount = _optional_decimal(row.get("amount"), "amount")
                    if (low > min(op, close) or high < max(op, close) or low > high or vol < 0
                            or vol != vol.to_integral_value() or (amount is not None and amount < 0)):
                        invalid_rows.append({"file": path.name, "reason": f"ohlc_or_quantity_invalid:{stamp.isoformat()}"})
                except (ValueError, ArithmeticError) as exc:
                    invalid_rows.append({"file": path.name, "reason": f"invalid_numeric:{stamp.isoformat()}:{type(exc).__name__}"})
        except (TypeError, ValueError, OverflowError) as exc:
            invalid_rows.append({"file": path.name, "reason": f"invalid_time:{type(exc).__name__}"})
            continue

        day = date.fromisoformat(path.stem[:4] + "-" + path.stem[4:6] + "-" + path.stem[6:8])
        daily = daily_map.get(day)
        if daily is not None:
            daily_overlaps += 1
            daily_overlap_by_source[str(daily["daily_source"])] += 1
            daily_volume = daily["volume_shares"]
            minute_volume = sum(int(_decimal(r["vol"], "vol") * volume_scale) for r in rows)
            if daily_volume is not None:
                if minute_volume == daily_volume:
                    volume_matches += 1
                else:
                    volume_mismatches.append({"date": day.isoformat(), "daily_shares": str(daily_volume),
                                              "minute_shares": str(minute_volume),
                                              "delta_shares": str(minute_volume - daily_volume),
                                              "daily_source": str(daily["daily_source"])})
            daily_amount = daily["amount_cny"]
            minute_amounts = [_optional_decimal(r.get("amount"), "amount") for r in rows]
            if daily_amount is not None and all(v is not None for v in minute_amounts):
                amount = sum(minute_amounts, D("0"))
                delta = abs(daily_amount - amount)
                amount_deltas.append((day.isoformat(), delta, daily_amount))
                if delta == 0:
                    amount_exact_matches += 1
            if daily["open_raw"] is not None:
                open_delta = abs(daily["open_raw"] - _decimal(rows[0]["open"], "open"))
                if open_delta == 0:
                    open_exact_matches += 1
                if open_delta <= D("0.01"):
                    open_within_tick_matches += 1
            if daily["close_raw"] is not None and abs(daily["close_raw"] - _decimal(rows[-1]["close"], "close")) <= D("0.01"):
                close_within_tick += 1
        if progress_callback is not None and (index % 100 == 0 or index == len(files)):
            progress_callback(index, len(files))

    return {
        "report_version": "jevquant-minute-library-audit-v1",
        "symbol": symbol,
        "partition_root": str(root),
        "partition_files": len(files),
        "parquet_metadata_provenance": {
            "files_examined": len(files),
            "writer_counts": dict(parquet_writer_counts),
            "metadata_key_counts": dict(parquet_metadata_key_counts),
            "files_declaring_market_provider": len(files_with_provider_metadata),
            "files_declaring_bar_interval_semantics": len(files_with_interval_metadata),
            "interpretation": "Parquet writer identity describes serialization only; zero provider/interval declarations here does not imply the source data itself is absent",
        },
        "symbol_rows_per_file_distribution": {str(k): v for k, v in sorted(row_counts.items())},
        "files_without_symbol_rows": missing_symbol_dates,
        "volume_unit_override_dates_applied": sorted(volume_override_dates_applied),
        "volume_unit_override_failures": volume_override_failures,
        "duplicate_timestamp_dates": duplicate_label_dates,
        "nonstandard_time_grid_dates": nonstandard_time_grid_dates,
        "expected_raw_time_labels": expected_labels,
        "invalid_rows_count": len(invalid_rows),
        "invalid_rows_sample": invalid_rows[:20],
        "row_trade_date_mismatches": date_mismatch_rows,
        "supplemental_daily_rows_loaded": supplemental_rows,
        "days_with_0930_label": opening_label_days,
        "days_with_1500_label": closing_label_days,
        "raw_first_label": first_label,
        "raw_last_label": last_label,
        "daily_crosscheck": {
            "overlap_days": daily_overlaps,
            "overlap_by_daily_source": dict(daily_overlap_by_source),
            "volume_exact_match_days": volume_matches,
            "volume_mismatch_days": volume_mismatches,
            "amount_exact_match_days": amount_exact_matches,
            "amount_delta_max_cny": str(max((x[1] for x in amount_deltas), default=0)) if amount_deltas else None,
            "amount_delta_max_date": max(amount_deltas, key=lambda x: x[1])[0] if amount_deltas else None,
            "amount_delta_median_cny": str(sorted(x[1] for x in amount_deltas)[len(amount_deltas)//2]) if amount_deltas else None,
            "amount_delta_max_relative_bps": str(max((x[1] / x[2] * D("10000") for x in amount_deltas if x[2]), default=0)) if amount_deltas else None,
            "first_row_open_exactly_equals_daily_open_days": open_exact_matches,
            "first_row_open_within_one_tick_of_daily_open_days": open_within_tick_matches,
            "last_row_close_within_one_tick_days": close_within_tick,
            "opening_and_closing_comparisons_are_descriptive_only": True,
            "daily_limit_fields": daily_limit_audit,
        },
        "semantics": "validated clock-label sequence only; timestamp interval role and endpoint execution semantics remain unverified",
    }


def inspect_dataset(root: Path, symbol: str = "600519.SH") -> dict[str, object]:
    daily_dir = root / "股票全量数据（日线）"
    minute_dir = root / "5分钟数据"
    daily_path = daily_dir / f"{symbol}.csv"
    if not daily_path.exists():
        raise FileNotFoundError(daily_path)
    daily = read_daily_vendor_csv(daily_path, symbol)
    daily_limit_audit = audit_daily_limit_fields(daily)
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
            "price_limit_field_audit": daily_limit_audit,
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
            "Moutai status files exist for 2022–2026, but their source-time metadata explicitly prevents PIT use; listing/delisting lifecycle and daily suspension/ST eligibility still require an accepted point-in-time source.",
            "2022–2023 sessions are now derived from pre-published SSE closure schedules; 2024–2026 sessions come from the mapped official calendar. The full planned interval still requires the generated calendar to match supplied daily/status dates before use.",
            "Daily source limit-up/down fields cover the 2023–2024 window and match a rounded 10% previous-close arithmetic check; source provenance and effective-date rule evidence still require verification before they can serve as formal execution constraints.",
            "Four official 2023–2024 Moutai cash-dividend events are mapped; announcement-correction availability, tax treatment, non-cash actions and full event completeness remain open.",
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
