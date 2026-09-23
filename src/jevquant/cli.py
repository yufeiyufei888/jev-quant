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
    return {
        "jevquant_version": __version__,
        "python_version": platform.python_version(),
        "supported_python": (3, 11) <= sys.version_info[:2] < (3, 12),
        "pyarrow_available": importlib.util.find_spec("pyarrow") is not None,
        "data_root_configured": bool(os.environ.get("JEVQUANT_DATA_ROOT")),
        "data_root_exists": Path(os.environ["JEVQUANT_DATA_ROOT"]).exists() if os.environ.get("JEVQUANT_DATA_ROOT") else False,
        "typesafe_api_key_configured": bool(os.environ.get("TYPESAFE_API_KEY")),
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


if __name__ == "__main__":
    main()
