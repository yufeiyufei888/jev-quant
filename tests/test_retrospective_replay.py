from datetime import date, datetime, time
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from jevquant.models import Bar
from jevquant.retrospective_replay import (
    EXPECTED_END_LABELS, eligible_execution_starts, normalize_end_labeled_diagnostic,
)


TZ = ZoneInfo("Asia/Shanghai")


def _source_bars(day):
    labels = (time(9, 30), *EXPECTED_END_LABELS)
    return [Bar(
        "600519.SH", datetime.combine(day, label, TZ), D("10"), D("10.02"),
        D("9.98"), D("10"), 1000, trade_date=day, source_id="fixture",
        source_row_id=f"{day}:{label}", quality_flags=frozenset({"time_label_semantics_unverified"}),
    ) for label in labels]


def test_end_label_diagnostic_excludes_opening_record_and_builds_48_full_intervals():
    day = date(2023, 1, 3)
    normalized = normalize_end_labeled_diagnostic(day, _source_bars(day))
    assert len(EXPECTED_END_LABELS) == len(normalized) == 48
    assert normalized[0].interval_start == datetime.combine(day, time(9, 30), TZ)
    assert normalized[0].interval_end == datetime.combine(day, time(9, 35), TZ)
    assert normalized[-1].interval_start == datetime.combine(day, time(14, 55), TZ)
    assert normalized[-1].interval_end == datetime.combine(day, time(15, 0), TZ)
    assert all("assumed_end_labeled_retrospective_only" in bar.quality_flags for bar in normalized)
    assert all("time_label_semantics_unverified" not in bar.quality_flags for bar in normalized)


def test_execution_schedule_excludes_open_and_closing_segments_but_keeps_them_for_marks():
    day = date(2023, 1, 3)
    bars = normalize_end_labeled_diagnostic(day, _source_bars(day))
    starts = eligible_execution_starts(day, bars)
    assert len(starts) == 45
    assert starts[0].time() == time(9, 35)
    assert starts[-1].time() == time(14, 50)
    assert all(start.time() not in {time(9, 30), time(14, 55)} for start in starts)
