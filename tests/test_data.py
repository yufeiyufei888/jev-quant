from datetime import date, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from jevquant.data import read_daily_vendor_csv, read_vendor_minute_day
from jevquant.features import daily_features_asof
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
