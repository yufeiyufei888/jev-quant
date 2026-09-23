from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path
import hashlib

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from jevquant.data import audit_daily_limit_fields, audit_minute_partitions, read_daily_vendor_csv, read_vendor_minute_day
from jevquant.actions import load_cash_dividends
from jevquant.features import daily_features_asof, daily_total_return_features_asof
from jevquant.models import Bar


def test_bar_rejects_inconsistent_ohlc():
    with pytest.raises(ValueError, match="OHLC"):
        Bar("600519.SH", datetime(2024, 1, 2, 9, 30), D("10"), D("9"), D("8"), D("10"), 100,
            trade_date=date(2024, 1, 2))


def test_bar_rejects_negative_volume():
    with pytest.raises(ValueError, match="volume"):
        Bar("600519.SH", datetime(2024, 1, 2, 9, 30), D("10"), D("11"), D("9"), D("10"), -1,
            trade_date=date(2024, 1, 2))


def test_daily_csv_units_are_normalized_without_mutating_source(tmp_path: Path):
    path = tmp_path / "600519.SH.csv"
    path.write_text(
        "股票代码,交易日,开盘价,最高价,最低价,收盘价,成交量（手）,成交额（千元）,复权因子\n"
        "600519.SH,20240102,1715,1718.19,1678.1,1685.01,32156.44,5440082.548,8.4464\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    row = read_daily_vendor_csv(path, "600519.SH")[0]
    assert row["volume_shares"] == 3215644
    assert row["amount_cny"] == D("5440082548.000")
    assert row["close_raw"] == D("1685.01")
    assert path.read_bytes() == before


def test_daily_limit_fields_are_preserved_and_checked_without_claiming_pit(tmp_path: Path):
    path = tmp_path / "600519.SH.csv"
    path.write_text(
        "股票代码,交易日,开盘价,最高价,最低价,收盘价,前收盘价,成交量（手）,成交额（千元）,当日涨停价,当日跌停价\n"
        "600519.SH,20240102,10,10.5,9.5,10,10,100,1000,11,9\n"
        "600519.SH,20240103,10,11,9,10,10,100,1000,11.01,9\n",
        encoding="utf-8",
    )
    rows = read_daily_vendor_csv(path, "600519.SH")
    assert rows[0]["previous_close_raw"] == D("10")
    assert rows[0]["limit_up_raw"] == D("11")
    assert rows[0]["limit_down_raw"] == D("9")
    audit = audit_daily_limit_fields(rows, date(2024, 1, 1), date(2024, 1, 4))
    assert audit["daily_rows"] == 2
    assert audit["complete_up_down_band_rows"] == 2
    assert audit["ohlc_within_band_rows"] == 2
    assert audit["previous_close_10pct_formula_match_rows"] == 1
    assert len(audit["previous_close_10pct_formula_mismatches"]) == 1
    assert "not certified" in audit["interpretation"]


def test_parquet_reader_preserves_labels_and_flags_special_endpoints(tmp_path: Path):
    path = tmp_path / "20240102.parquet"
    table = pa.table({
        "code": ["600519.SH", "600519.SH"],
        "trade_time": ["2024-01-02 09:30:00", "2024-01-02 15:00:00"],
        "open": [1715.0, 1686.8], "high": [1715.0, 1688.0],
        "low": [1714.0, 1685.0], "close": [1714.0, 1685.01],
        "vol": [52600.0, 53912.0], "amount": [90208704.0, 90895320.0],
        "date": ["20240102", "20240102"], "pre_close": [1726.0, 1686.6],
    })
    pq.write_table(table, path)
    bars = read_vendor_minute_day(path, "600519.SH")
    assert [b.source_time.hour for b in bars] == [9, 15]
    assert "opening_record" in bars[0].quality_flags
    assert "closing_record" in bars[1].quality_flags
    assert [b.volume_shares for b in bars] == [52600, 53912]


def test_float32_ohlc_roundoff_is_bounded_to_legal_cent_prices(tmp_path: Path):
    path = tmp_path / "20240102.parquet"
    pq.write_table(pa.table({
        "code": ["600519.SH", "600519.SH"],
        "trade_time": ["2024-01-02 09:30:00", "2024-01-02 09:35:00"],
        "open": pa.array([11.81, 11.80], type=pa.float32()),
        "high": pa.array([11.81, 11.82], type=pa.float32()),
        "low": pa.array([11.81, 11.79], type=pa.float32()),
        "close": pa.array([11.81, 11.81], type=pa.float32()),
        "vol": [100, 200], "amount": [1181.0, 2362.0], "date": ["20240102", "20240102"],
    }), path)
    bars = read_vendor_minute_day(path, "600519.SH")
    assert bars[0].open == D("11.81")
    assert bars[0].close == D("11.81")
    assert "source_ohlc_float32_tick_checked" in bars[0].quality_flags
    assert "source_ohlc_float32_tick_normalized" in bars[0].quality_flags


def test_float32_ohlc_outside_tick_error_bound_is_rejected(tmp_path: Path):
    path = tmp_path / "20240102.parquet"
    pq.write_table(pa.table({
        "code": ["600519.SH"], "trade_time": ["2024-01-02 09:30:00"],
        "open": pa.array([11.812], type=pa.float32()),
        "high": pa.array([11.812], type=pa.float32()),
        "low": pa.array([11.812], type=pa.float32()),
        "close": pa.array([11.812], type=pa.float32()),
        "vol": [100], "amount": [1181.2], "date": ["20240102"],
    }), path)
    with pytest.raises(ValueError, match="outside Float32 legal-tick tolerance"):
        read_vendor_minute_day(path, "600519.SH")


def test_minute_audit_reports_writer_but_does_not_mislabel_it_as_market_provider(tmp_path: Path):
    path = tmp_path / "20240102.parquet"
    pq.write_table(pa.table({
        "code": ["600519.SH"], "trade_time": ["2024-01-02 09:35:00"],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "vol": [100], "amount": [1000.0], "date": ["20240102"],
    }), path)
    report = audit_minute_partitions(tmp_path, "600519.SH")
    provenance = report["parquet_metadata_provenance"]
    assert provenance["files_examined"] == 1
    assert provenance["ohlc_storage_type_counts"] == {"open=float64,high=float64,low=float64,close=float64": 1}
    assert sum(provenance["writer_counts"].values()) == 1
    assert provenance["files_declaring_market_provider"] == 0
    assert provenance["files_declaring_bar_interval_semantics"] == 0
    assert "serialization only" in provenance["interpretation"]


def test_daily_features_ignore_data_after_the_requested_asof_date():
    history = [(date(2024, 1, day), D(str(day))) for day in range(1, 8)]
    asof = date(2024, 1, 5)
    baseline = daily_features_asof(history, asof, windows=(3, 5))
    perturbed_future = daily_features_asof(history[:5] + [(date(2024, 1, 6), D("999999"))],
                                           asof, windows=(3, 5))
    assert baseline == perturbed_future
    assert baseline["raw_sma_3"] == D("4")
    assert baseline["raw_sma_5"] == D("3")
    assert baseline["return_basis"] == "raw_close_unadjusted_actions_not_applied"


def test_full_partition_audit_counts_symbol_bars_and_reconciles_daily_values(tmp_path: Path):
    root = tmp_path / "minutes"
    year = root / "2024"
    year.mkdir(parents=True)
    pq.write_table(pa.table({
        "code": ["600519.SH", "600519.SH", "000001.SZ"],
        "trade_time": ["2024-01-02 09:30:00", "2024-01-02 09:35:00", "2024-01-02 09:30:00"],
        "open": [10.0, 10.0, 8.0], "high": [10.1, 10.2, 8.1],
        "low": [9.9, 9.9, 7.9], "close": [10.0, 10.1, 8.0],
        "vol": [100.0, 200.0, 100.0], "amount": [1000.0, 2020.0, 800.0],
        "date": ["20240102", "20240102", "20240102"],
    }), year / "20240102.parquet")
    daily = tmp_path / "600519.SH.csv"
    daily.write_text(
        "股票代码,交易日,开盘价,最高价,最低价,收盘价,成交量（手）,成交额（千元）,复权因子\n"
        "600519.SH,20240102,10,10.2,9.9,10.1,3,3.02,1\n",
        encoding="utf-8",
    )
    pq.write_table(pa.table({
        "code": ["600519.SH", "600519.SH"],
        "trade_time": ["2024-01-03 09:30:00", "2024-01-03 09:35:00"],
        "open": [11.0, 11.0], "high": [11.1, 11.2], "low": [10.9, 10.9],
        "close": [11.0, 11.1], "vol": [100.0, 200.0], "amount": [1100.0, 2220.0],
        "date": ["20240103", "20240103"],
    }), year / "20240103.parquet")
    supplemental = tmp_path / "supplement"
    supplemental.mkdir()
    pq.write_table(pa.table({
        "code": ["600519.SH"], "date": ["2024-01-03"], "open": [11.0],
        "high": [11.2], "low": [10.9], "close": [11.1], "volume": [300.0],
        "amount": [3320.0],
    }), supplemental / "daily.parquet")
    report = audit_minute_partitions(root, "600519.SH", daily, supplemental_daily_parquet_root=supplemental)
    assert report["partition_files"] == 2
    assert report["symbol_rows_per_file_distribution"] == {"2": 2}
    assert report["supplemental_daily_rows_loaded"] == 1
    assert report["daily_crosscheck"]["overlap_by_daily_source"] == {
        "user_daily_csv": 1, "baostock_raw_supplement_captured_2026": 1,
    }
    assert report["daily_crosscheck"]["volume_exact_match_days"] == 2
    assert report["daily_crosscheck"]["amount_exact_match_days"] == 2
    assert report["daily_crosscheck"]["first_row_open_exactly_equals_daily_open_days"] == 2
    assert report["daily_crosscheck"]["first_row_open_within_one_tick_of_daily_open_days"] == 2
    assert report["files_without_symbol_rows"] == []
    assert report["row_trade_date_mismatches"] == 0
    assert len(report["expected_raw_time_labels"]) == 49
    assert [row["date"] for row in report["nonstandard_time_grid_dates"]] == ["20240102", "20240103"]
    assert "interval role" in report["semantics"] and "unverified" in report["semantics"]


def test_minute_open_audit_distinguishes_float_representation_from_tick_difference(tmp_path: Path):
    minute = tmp_path / "2024" / "20240102.parquet"
    minute.parent.mkdir()
    pq.write_table(pa.table({
        "code": ["600519.SH", "600519.SH"],
        "trade_time": ["2024-01-02 09:30:00", "2024-01-02 09:35:00"],
        "open": [10.000001, 10.01], "high": [10.000001, 10.01],
        "low": [10.000001, 10.00], "close": [10.000001, 10.01],
        "vol": [100, 100], "amount": [1000, 1000],
        "date": ["20240102", "20240102"],
    }), minute)
    daily = tmp_path / "daily.csv"
    daily.write_text(
        "股票代码,交易日,开盘价,最高价,最低价,收盘价,前收盘价,成交量（手）,成交额（千元）,当日涨停价,当日跌停价\n"
        "600519.SH,20240102,10,10.01,10,10.01,10,2,2,11,9\n",
        encoding="utf-8",
    )
    report = audit_minute_partitions(minute.parent.parent, "600519.SH", daily)
    crosscheck = report["daily_crosscheck"]
    assert crosscheck["first_row_open_exactly_equals_daily_open_days"] == 0
    assert crosscheck["first_row_open_within_one_tick_of_daily_open_days"] == 1


def test_moutai_volume_unit_override_is_date_and_source_hash_bound(tmp_path: Path):
    path = tmp_path / "20240403.parquet"
    pq.write_table(pa.table({
        "code": ["600519.SH", "600519.SH"],
        "trade_time": ["2024-04-03 09:30:00", "2024-04-03 09:35:00"],
        "open": [1700.0, 1700.0], "high": [1710.0, 1710.0],
        "low": [1690.0, 1690.0], "close": [1700.0, 1705.0],
        "vol": [10000.0, 20000.0], "amount": [170000.0, 340000.0],
        "date": ["20240403", "20240403"],
    }), path)
    entry = {"raw_volume_to_shares": "0.01", "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    overrides = {("600519.SH", "2024-04-03"): entry}
    bars = read_vendor_minute_day(path, "600519.SH", volume_unit_overrides=overrides)
    assert [bar.volume_shares for bar in bars] == [100, 200]
    assert "source_volume_unit_corrected_by_hash_bound_rule" in bars[0].quality_flags
    path.write_bytes(path.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="source hash mismatch"):
        read_vendor_minute_day(path, "600519.SH", volume_unit_overrides=overrides)


def test_official_moutai_dividends_load_with_conservative_announcement_times():
    config = Path(__file__).parents[1] / "configs" / "moutai_2023_2024_cash_dividends.json"
    events = load_cash_dividends(config, "600519.SH")
    assert len(events) == 4
    assert [str(event.cash_per_share) for event in events] == ["25.911", "19.106", "30.876", "23.882"]
    assert all(event.known_at.hour == 23 and event.evidence_level == "OFFICIAL_IMPLEMENTATION_NOTICE"
               for event in events)


def test_total_return_feature_uses_known_dividend_and_only_visible_daily_closes():
    from datetime import time
    from zoneinfo import ZoneInfo
    from jevquant.actions import CashDividend

    tz = ZoneInfo("Asia/Shanghai")
    event = CashDividend("div-1", "600519.SH", D("2"), date(2024, 1, 2),
                         datetime(2024, 1, 2, 23, 59, 59, tzinfo=tz), date(2024, 1, 4),
                         date(2024, 1, 5), date(2024, 1, 5), "OFFICIAL_IMPLEMENTATION_NOTICE",
                         "https://example.test/dividend.pdf")
    history = [(date(2024, 1, 3), D("100")), (date(2024, 1, 4), D("100")),
               (date(2024, 1, 5), D("99")), (date(2024, 1, 8), D("500"))]
    before_ex = daily_total_return_features_asof(history, [event],
                                                  datetime.combine(date(2024, 1, 4), time(16), tz), windows=(2,))
    after_ex = daily_total_return_features_asof(history, [event],
                                                 datetime.combine(date(2024, 1, 5), time(16), tz), windows=(2,))
    assert before_ex["visible_bars"] == 2
    assert before_ex["cash_action_count_used"] == 0
    assert after_ex["visible_bars"] == 3
    assert after_ex["cash_action_count_used"] == 1
    assert after_ex["total_return_1d"] == D("0.01")


def test_total_return_feature_rejects_duplicate_daily_dates():
    from datetime import time
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    history = [(date(2024, 1, 2), D("100")), (date(2024, 1, 2), D("101"))]
    with pytest.raises(ValueError, match="duplicate dates"):
        daily_total_return_features_asof(
            history, [], datetime.combine(date(2024, 1, 2), time(16), tz), windows=(1,)
        )
