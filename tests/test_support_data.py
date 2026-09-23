from __future__ import annotations

import json
from pathlib import Path

from jevquant.support_data import audit_local_support_data, load_official_sse_annual_calendar


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_support_audit_separates_file_coverage_from_point_in_time_readiness(tmp_path: Path):
    root = tmp_path / "project-data"
    support = root / "股票项目-data" / "support-data"
    early = {
        "schema_version": "historical-support/v1",
        "role": "historical_status_tradestatus",
        "status": "CAPTURED_NOT_PIT_READY",
        "symbol": "600519",
        "coverage_start": "2023-01-03",
        "coverage_end": "2023-01-04",
        "source_timestamp_proven": False,
        "usable_for_pit": False,
        "status_fields": ["date", "code", "tradestatus", "isST"],
        "status_rows": [["2023-01-03", "sh.600519", "1", "0"],
                        ["2023-01-04", "sh.600519", "1", "0"]],
    }
    later = {
        **early,
        "coverage_start": "2024-01-02",
        "coverage_end": "2024-01-03",
        "status_rows": [["2024-01-02", "sh.600519", "1", "0"],
                        ["2024-01-03", "sh.600519", "1", "0"]],
    }
    calendar = {
        "schema_version": "official-calendar/v2",
        "role": "trading_calendar",
        "status": "PIT_READY",
        "coverage_start": "2024-01-02",
        "coverage_end": "2024-01-03",
        "source_timestamp": "2023-12-20T10:00:00+08:00",
        "usable_for_pit": True,
        "sessions": ["2024-01-02", "2024-01-03"],
    }
    _write(support / "status-industry-2022-2023-strict" / "symbols" / "600519.json", early)
    _write(support / "status-industry-v2" / "symbols" / "600519.json", later)
    _write(root / "股票项目-artifacts-support-data" / "2026-08-17" / "official-calendar-v2.json", calendar)

    report = audit_local_support_data(root)
    assert report["status_sources"]["2022_2023_strict_capture"]["rows"] == 2
    assert report["status_sources"]["2022_2023_strict_capture"]["rows_by_year"] == {"2023": 2}
    assert report["status_sources"]["2022_2023_strict_capture"]["trade_status_value_counts"] == {"1": 2}
    assert report["status_sources"]["2022_2023_strict_capture"]["classification"] == "RESEARCH_ONLY_NOT_PIT_READY"
    assert report["status_sources"]["2024_plus_capture"]["classification"] == "RESEARCH_ONLY_NOT_PIT_READY"
    assert report["calendar_source"]["classification"] == "PIT_READY"
    comparison = report["calendar_status_coverage_comparison"]
    assert comparison["same_date_coverage"] is True
    assert comparison["classification"] == "COVERAGE_MATCH_ONLY_NOT_PIT_STATUS"


def test_support_audit_flags_duplicate_status_dates_and_wrong_symbol(tmp_path: Path):
    root = tmp_path / "project-data"
    support = root / "股票项目-data" / "support-data"
    status = {
        "schema_version": "historical-support/v1",
        "role": "historical_status_tradestatus",
        "status": "CAPTURED_NOT_PIT_READY",
        "symbol": "600519",
        "coverage_start": "2023-01-03",
        "coverage_end": "2023-01-03",
        "source_timestamp_proven": False,
        "usable_for_pit": False,
        "status_fields": ["date", "code", "tradestatus", "isST"],
        "status_rows": [["2023-01-03", "sz.000001", "1", "0"],
                        ["2023-01-03", "sz.000001", "1", "0"]],
    }
    _write(support / "status-industry-2022-2023-strict" / "symbols" / "600519.json", status)
    report = audit_local_support_data(root)
    entry = report["status_sources"]["2022_2023_strict_capture"]
    assert entry["symbol_matches"] is False
    assert entry["duplicate_dates"] == ["2023-01-03"]
    assert entry["classification"] == "RESEARCH_ONLY_NOT_PIT_READY"


def test_official_sse_closure_schedules_derive_expected_2022_2023_sessions():
    calendar_2022 = load_official_sse_annual_calendar(2022)
    calendar_2023 = load_official_sse_annual_calendar(2023)
    assert calendar_2022["session_count"] == 242
    assert calendar_2022["first_session"] == "2022-01-04"
    assert calendar_2022["last_session"] == "2022-12-30"
    assert calendar_2023["session_count"] == 242
    assert calendar_2023["first_session"] == "2023-01-03"
    assert calendar_2023["last_session"] == "2023-12-29"
    assert "2023-04-23" not in calendar_2023["sessions"]
    assert "2023-05-06" not in calendar_2023["sessions"]
    assert calendar_2023["source_published_on"] == "2022-12-27"
