"""Independent event arithmetic for multi-stock short-T account outputs."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

from .short_t_data import load_action_events

D = Decimal


def _cent(value: Decimal) -> Decimal:
    return value.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def audit_account(run_dir: Path, manifest_path: Path) -> dict:
    """Derive cash, receivables and lots from source fills/actions, not Account."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    nav_rows = [json.loads(line) for line in (run_dir / "nav.jsonl").read_text(encoding="utf-8").splitlines() if line]
    fills = [json.loads(line) for line in (run_dir / "fills.jsonl").read_text(encoding="utf-8").splitlines() if line]
    symbols = tuple(summary["selected_symbols"])
    year = date.fromisoformat(summary["start"]).year
    action_rows = [event for member in manifest["universe"]
                   for event in load_action_events(Path(member["action_path"]), member["symbol"], year)]
    actions_by_record = defaultdict(list)
    actions_by_ex = defaultdict(list)
    actions_by_payment = defaultdict(list)
    for event in action_rows:
        actions_by_record[event["record_date"]].append(event)
        actions_by_ex[event["ex_date"]].append(event)
        actions_by_payment[event["payment_date"]].append(event)
    fills_by_date = defaultdict(list)
    for fill in fills:
        fills_by_date[date.fromisoformat(fill["trade_date"])].append(fill)
    cash, receivables = D(summary["initial_cash_cny"]), D(0)
    lots: dict[str, list[int]] = {symbol: [] for symbol in symbols}
    entitlements: dict[str, Decimal] = {}
    rows = []
    passed = True
    for observed in nav_rows:
        day = date.fromisoformat(observed["trade_date"])
        for event in actions_by_ex[day]:
            amount = entitlements.get(event["event_id"], D(0))
            receivables = _cent(receivables + amount)
            if event["share_ratio"]:
                ratio = D(1) + event["share_ratio"]
                lots[event["symbol"]] = [int((D(q) * ratio).to_integral_value(rounding=ROUND_FLOOR))
                                          for q in lots[event["symbol"]]]
        for fill in sorted(fills_by_date[day], key=lambda item: (item["filled_at"], item["fill_id"])):
            symbol, quantity = fill["symbol"], int(fill["quantity"])
            gross, fee = D(fill["price_cny"]) * quantity, D(fill["fee_cny"])
            if fill["side"] == "BUY":
                cash = _cent(cash - gross - fee)
                lots[symbol].append(quantity)
            else:
                cash = _cent(cash + gross - fee)
                remaining = quantity
                for index, held in enumerate(lots[symbol]):
                    take = min(remaining, held)
                    lots[symbol][index] -= take
                    remaining -= take
                    if not remaining:
                        break
                if remaining:
                    raise ValueError(f"source fills sell unavailable shares on {day}")
                lots[symbol] = [q for q in lots[symbol] if q]
        for event in actions_by_record[day]:
            entitlements[event["event_id"]] = _cent(D(sum(lots[event["symbol"]])) * event["cash_per_share"])
        for event in actions_by_payment[day]:
            amount = entitlements.get(event["event_id"], D(0))
            receivables = _cent(receivables - amount)
            cash = _cent(cash + amount)
        shares = {symbol: sum(lots[symbol]) for symbol in symbols}
        if "marks_cny" in observed:
            expected_nav = _cent(cash + receivables + sum(
                (D(observed["marks_cny"][symbol]) * shares[symbol] for symbol in symbols), D(0)))
            nav_ok = expected_nav == D(observed["nav_cny"])
        else:
            expected_nav = None
            nav_ok = None
        row_ok = (cash == D(observed["cash_cny"]) and
                  receivables == D(observed["receivables_cny"]) and
                  shares == observed["shares"] and nav_ok is not False)
        passed &= row_ok
        if not row_ok:
            rows.append({"day": day.isoformat(), "expected_cash": str(cash),
                         "observed_cash": observed["cash_cny"],
                         "expected_receivables": str(receivables),
                         "observed_receivables": observed["receivables_cny"],
                         "expected_shares": shares, "observed_shares": observed["shares"],
                         "expected_nav": str(expected_nav) if expected_nav is not None else None,
                         "observed_nav": observed["nav_cny"]})
    return {"schema": "jevquant-short-t-independent-audit/v1", "account": summary["account"],
            "passed": passed, "audited_sessions": len(nav_rows), "fill_count": len(fills),
            "ending_cash_cny": str(cash), "ending_receivables_cny": str(receivables),
            "ending_shares": {symbol: sum(lots[symbol]) for symbol in symbols},
            "mismatches": rows[:20], "complete_nav_marks": all("marks_cny" in row for row in nav_rows)}
