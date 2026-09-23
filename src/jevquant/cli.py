from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sys
from pathlib import Path

from . import __version__


def doctor() -> dict[str, object]:
    from .provider import has_api_key_configured, has_api_key_source_configured
    return {
        "jevquant_version": __version__,
        "python_version": platform.python_version(),
        "supported_python": (3, 11) <= sys.version_info[:2] < (3, 12),
        "pyarrow_available": importlib.util.find_spec("pyarrow") is not None,
        "data_root_configured": bool(os.environ.get("JEVQUANT_DATA_ROOT")),
        "data_root_exists": Path(os.environ["JEVQUANT_DATA_ROOT"]).exists() if os.environ.get("JEVQUANT_DATA_ROOT") else False,
        "typesafe_api_key_configured": has_api_key_configured(),
        "typesafe_key_source_configured": has_api_key_source_configured(),
        "live_broker_enabled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(prog="jevquant")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="show local environment readiness without secrets")
    inventory = sub.add_parser("inventory", help="read-only inventory for supplied local market data")
    inventory.add_argument("--root", type=Path, default=None)
    inventory.add_argument("--output", type=Path, default=Path("artifacts/preflight"))
    inventory.add_argument("--symbol", default="600519.SH")
    support_audit = sub.add_parser("audit-support", help="inventory existing calendar/status evidence without modifying it")
    support_audit.add_argument("--project-data-root", type=Path, required=True)
    support_audit.add_argument("--output", type=Path, default=Path("artifacts/preflight/support_data_audit.json"))
    support_audit.add_argument("--symbol", default="600519.SH")
    minute_audit = sub.add_parser("audit-minutes", help="audit all raw minute partitions for one symbol")
    minute_audit.add_argument("--root", type=Path, required=True)
    minute_audit.add_argument("--daily-csv", type=Path, default=None)
    minute_audit.add_argument("--supplemental-daily-parquet-root", type=Path, default=None)
    minute_audit.add_argument("--output", type=Path, default=Path("artifacts/preflight/moutai_minute_audit.json"))
    minute_audit.add_argument("--symbol", default="600519.SH")
    feature_sample = sub.add_parser("features-sample", help="write a local daily causal-feature sample")
    feature_sample.add_argument("--daily-csv", type=Path, required=True)
    feature_sample.add_argument("--actions", type=Path, required=True)
    feature_sample.add_argument("--symbol", default="600519.SH")
    feature_sample.add_argument("--start", type=lambda value: __import__("datetime").date.fromisoformat(value), required=True)
    feature_sample.add_argument("--sessions", type=int, default=20)
    feature_sample.add_argument("--output", type=Path, default=Path("artifacts/preflight/features_sample.json"))
    sub.add_parser("offline-demo", help="run the synthetic ledger golden example")
    workflow = sub.add_parser("mock-round-trip", help="run synthetic decision-to-NAV flow; no live API")
    workflow.add_argument("--output", type=Path, default=Path("artifacts/mock-round-trip"))
    args = parser.parse_args()

    if args.command == "doctor":
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        return
    if args.command == "offline-demo":
        from .demo import golden_round_trip
        result = golden_round_trip()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "mock-round-trip":
        from .workflow import run_mock_round_trip
        result = run_mock_round_trip(args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "inventory":
        configured_root = os.environ.get("JEVQUANT_DATA_ROOT")
        root = args.root or (Path(configured_root) if configured_root else None)
        if root is None:
            parser.error("provide --root or set JEVQUANT_DATA_ROOT")
        from .data import write_inventory
        path = write_inventory(root, args.output, args.symbol)
        print(path.resolve())
        return
    if args.command == "audit-support":
        from .support_data import audit_local_support_data
        report = audit_local_support_data(args.project_data_root, args.symbol)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "output": str(args.output.resolve()),
            "status_sources": {
                key: {"present": value["present"], "classification": value["classification"],
                      "rows": value.get("rows"), "coverage_start": value.get("coverage_start"),
                      "coverage_end": value.get("coverage_end")}
                for key, value in report["status_sources"].items()
            },
            "calendar": {"present": report["calendar_source"]["present"],
                         "classification": report["calendar_source"]["classification"],
                         "sessions": report["calendar_source"].get("sessions"),
                         "coverage_start": report["calendar_source"].get("coverage_start"),
                         "coverage_end": report["calendar_source"].get("coverage_end")},
            "calendar_status_coverage_comparison": report["calendar_status_coverage_comparison"],
            "formal_pit_status_gate": report["formal_pit_status_gate"],
        }, ensure_ascii=False, indent=2))
        return
    if args.command == "audit-minutes":
        from .data import audit_minute_partitions
        report = audit_minute_partitions(
            args.root, args.symbol, args.daily_csv,
            progress_callback=lambda done, total: print(f"audited {done}/{total} minute partitions", flush=True),
            supplemental_daily_parquet_root=args.supplemental_daily_parquet_root,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({
            "output": str(args.output.resolve()),
            "partition_files": report["partition_files"],
            "symbol_rows_per_file_distribution": report["symbol_rows_per_file_distribution"],
            "files_without_symbol_rows": len(report["files_without_symbol_rows"]),
            "invalid_rows_count": report["invalid_rows_count"],
            "nonstandard_time_grid_dates": len(report["nonstandard_time_grid_dates"]),
            "volume_unit_override_dates_applied": report["volume_unit_override_dates_applied"],
            "daily_crosscheck": report["daily_crosscheck"],
            "semantics": report["semantics"],
        }, ensure_ascii=False, indent=2))
        return
    if args.command == "features-sample":
        from datetime import datetime, time
        from zoneinfo import ZoneInfo
        from .actions import load_cash_dividends
        from .data import read_daily_vendor_csv
        from .features import daily_total_return_features_asof
        if args.sessions <= 0:
            parser.error("--sessions must be positive")
        rows = read_daily_vendor_csv(args.daily_csv, args.symbol)
        clean = [(row["trade_date"], row["close_raw"]) for row in rows
                 if row["trade_date"] >= args.start and row["close_raw"] is not None]
        selected = sorted(clean, key=lambda row: row[0])[:args.sessions]
        if len(selected) != args.sessions:
            parser.error(f"only {len(selected)} complete sessions found from --start")
        history = [(row["trade_date"], row["close_raw"]) for row in rows if row["close_raw"] is not None]
        actions = load_cash_dividends(args.actions, args.symbol)
        tz = ZoneInfo("Asia/Shanghai")
        samples = []
        for day, _ in selected:
            asof_at = datetime.combine(day, time(16, 0), tz)
            features = daily_total_return_features_asof(history, actions, asof_at)
            samples.append({key: (str(value) if hasattr(value, "as_tuple") else
                                  value.isoformat() if hasattr(value, "isoformat") else value)
                            for key, value in features.items()})
        result = {
            "schema": "jevquant-feature-sample/v1",
            "symbol": args.symbol,
            "start": selected[0][0].isoformat(),
            "end": selected[-1][0].isoformat(),
            "sessions": len(samples),
            "price_basis": "raw_close_plus_known_official_cash_dividends",
            "decision_policy": "daily close available at 15:05 Asia/Shanghai (simulation assumption); sample as-of 16:00",
            "scope": "feature pipeline smoke sample only; not strategy performance evidence",
            "samples": samples,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(args.output.resolve()), "sessions": len(samples),
                          "start": result["start"], "end": result["end"],
                          "scope": result["scope"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
