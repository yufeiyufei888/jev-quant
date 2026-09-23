from __future__ import annotations

from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _canonical_symbol(value: str) -> str:
    token = value.strip().upper()
    if "." not in token:
        return token
    left, right = token.split(".", 1)
    if left in {"SH", "SZ"}:
        return f"{right}.{left}"
    if right in {"SH", "SZ"}:
        return token
    return token


def _status_source(path: Path, expected_symbol: str) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "present": False, "classification": "NOT_FOUND"}

    payload, digest = _load_json(path)
    fields = payload.get("status_fields")
    rows = payload.get("status_rows")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise ValueError(f"status fields/rows missing in {path}")
    indices = {name: fields.index(name) for name in ("date", "code", "tradestatus", "isST")
               if name in fields}
    if set(indices) != {"date", "code", "tradestatus", "isST"}:
        raise ValueError(f"status schema missing required columns in {path}")

    dates: list[str] = []
    symbol_values: set[str] = set()
    trade_status_counts: Counter[str] = Counter()
    st_flag_counts: Counter[str] = Counter()
    rows_by_year: Counter[str] = Counter()
    malformed_rows = 0
    for row in rows:
        if not isinstance(row, list) or len(row) < len(fields):
            malformed_rows += 1
            continue
        try:
            date.fromisoformat(str(row[indices["date"]]))
            row_date = str(row[indices["date"]])
            dates.append(row_date)
            rows_by_year[row_date[:4]] += 1
            symbol_values.add(_canonical_symbol(str(row[indices["code"]])))
            trade_status_counts[str(row[indices["tradestatus"]])] += 1
            st_flag_counts[str(row[indices["isST"]])] += 1
        except (TypeError, ValueError):
            malformed_rows += 1

    duplicate_dates = sorted(d for d, count in Counter(dates).items() if count > 1)
    source_time_proven = payload.get("source_timestamp_proven") is True or bool(payload.get("source_timestamp"))
    source_pit = payload.get("usable_for_pit") is True and source_time_proven
    symbol_matches = symbol_values == {_canonical_symbol(expected_symbol)}
    return {
        "path": str(path),
        "present": True,
        "sha256": digest,
        "schema_version": payload.get("schema_version"),
        "role": payload.get("role"),
        "status": payload.get("status"),
        "symbol": payload.get("symbol"),
        "symbol_matches": symbol_matches,
        "coverage_start": payload.get("coverage_start"),
        "coverage_end": payload.get("coverage_end"),
        "rows": len(rows),
        "valid_date_rows": len(dates),
        "unique_dates": len(set(dates)),
        "duplicate_dates": duplicate_dates,
        "malformed_rows": malformed_rows,
        "rows_by_year": dict(sorted(rows_by_year.items())),
        "trade_status_value_counts": dict(sorted(trade_status_counts.items())),
        "st_flag_value_counts": dict(sorted(st_flag_counts.items())),
        "fetched_at": payload.get("fetched_at"),
        "known_gaps": payload.get("known_gaps", []),
        "source_timestamp_proven": source_time_proven,
        "usable_for_pit": payload.get("usable_for_pit") is True,
        "classification": "PIT_READY" if source_pit and symbol_matches and not malformed_rows and not duplicate_dates
        else "RESEARCH_ONLY_NOT_PIT_READY",
    }


def _calendar_source(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": str(path), "present": False, "classification": "NOT_FOUND"}
    payload, digest = _load_json(path)
    sessions = payload.get("sessions", payload.get("trading_days"))
    if not isinstance(sessions, list):
        raise ValueError(f"calendar sessions/trading_days missing in {path}")
    normalized: list[str] = []
    for item in sessions:
        value = item if isinstance(item, str) else item.get("date") if isinstance(item, dict) else None
        if not isinstance(value, str):
            raise ValueError(f"invalid calendar session row in {path}")
        normalized.append(date.fromisoformat(value).isoformat())
    duplicate_dates = sorted(d for d, count in Counter(normalized).items() if count > 1)
    source_time_proven = payload.get("source_timestamp_proven") is True or bool(payload.get("source_timestamp"))
    source_pit = payload.get("usable_for_pit") is True and source_time_proven
    return {
        "path": str(path),
        "present": True,
        "sha256": digest,
        "schema_version": payload.get("schema_version"),
        "role": payload.get("role"),
        "status": payload.get("status"),
        "coverage_start": payload.get("coverage_start") or (normalized[0] if normalized else None),
        "coverage_end": payload.get("coverage_end") or (normalized[-1] if normalized else None),
        "sessions": len(normalized),
        "unique_sessions": len(set(normalized)),
        "duplicate_dates": duplicate_dates,
        "source_timestamp_proven": source_time_proven,
        "usable_for_pit": payload.get("usable_for_pit") is True,
        "classification": "PIT_READY" if source_pit and not duplicate_dates else "RESEARCH_ONLY_NOT_PIT_READY",
    }


def audit_local_support_data(project_data_root: Path, symbol: str = "600519.SH") -> dict[str, Any]:
    """Inventory already-collected calendar and status evidence without modifying sources.

    This deliberately reports point-in-time readiness separately from file coverage.
    It is an audit aid; it does not promote retrospective status records into PIT evidence.
    """
    root = Path(project_data_root)
    support = root / "股票项目-data" / "support-data"
    status_paths = {
        "2022_2023_strict_capture": support / "status-industry-2022-2023-strict" / "symbols" / "600519.json",
        "2024_plus_capture": support / "status-industry-v2" / "symbols" / "600519.json",
    }
    calendar_path = root / "股票项目-artifacts-support-data" / "2026-08-17" / "official-calendar-v2.json"
    proxy_dir = root / "official-session-windows" / "asof=2026-08-21"
    proxy_paths = sorted(proxy_dir.glob("calendar-*.json"))

    statuses = {name: _status_source(path, symbol) for name, path in status_paths.items()}
    calendar = _calendar_source(calendar_path)
    proxies = [_calendar_source(path) for path in proxy_paths]

    comparison: dict[str, Any] = {"comparable": False}
    status_after_2024 = statuses["2024_plus_capture"]
    if calendar.get("present") and status_after_2024.get("present"):
        calendar_payload, _ = _load_json(calendar_path)
        status_payload, _ = _load_json(status_paths["2024_plus_capture"])
        calendar_sessions = calendar_payload.get("sessions", calendar_payload.get("trading_days", []))
        calendar_dates = {
            item if isinstance(item, str) else item.get("date")
            for item in calendar_sessions
        }
        status_rows = status_payload["status_rows"]
        status_fields = status_payload["status_fields"]
        date_index = status_fields.index("date")
        status_dates = {str(row[date_index]) for row in status_rows}
        overlap = sorted(calendar_dates & status_dates)
        comparison = {
            "comparable": True,
            "calendar_sessions": len(calendar_dates),
            "status_dates": len(status_dates),
            "overlap_sessions": len(overlap),
            "calendar_only": sorted(calendar_dates - status_dates),
            "status_only": sorted(status_dates - calendar_dates),
            "same_date_coverage": calendar_dates == status_dates,
            "classification": "COVERAGE_MATCH_ONLY_NOT_PIT_STATUS" if calendar_dates == status_dates
            else "DATE_COVERAGE_MISMATCH",
        }

    return {
        "schema": "jevquant-local-support-data-audit/v1",
        "symbol": symbol,
        "source_root": str(root),
        "read_only": True,
        "status_sources": statuses,
        "calendar_source": calendar,
        "calendar_status_coverage_comparison": comparison,
        "session_window_proxies": proxies,
        "formal_pit_status_gate": "BLOCKED_WHERE_SOURCE_IS_NOT_PIT_READY",
        "notes": [
            "A complete row/date inventory does not prove point-in-time availability.",
            "Official calendar availability does not establish historical per-security suspension, ST, listing, delisting, or limit-band state.",
            "No source file is modified or copied by this audit.",
        ],
    }
