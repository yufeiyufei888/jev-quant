from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from .models import Fill, Side

D = Decimal


@dataclass(frozen=True, slots=True)
class CashReconciliation:
    reconstructed_cash: Decimal
    recorded_cash: Decimal
    difference: Decimal
    passed: bool


def reconcile_cash(initial_cash: Decimal, fills: Iterable[Fill], recorded_cash: Decimal,
                   cash_events: Iterable[Mapping[str, object]] = ()) -> CashReconciliation:
    """Recompute cash from source events without calling the account ledger reducer."""
    expected = D(initial_cash)
    for fill in fills:
        gross = D(fill.quantity) * fill.price
        expected += (-gross - fill.fee) if fill.side is Side.BUY else (gross - fill.fee)
    for event in cash_events:
        kind = event.get("kind")
        amount = D(str(event["amount"]))
        if kind == "dividend_paid":
            expected += amount
        elif kind == "cash_debit":
            expected -= amount
        elif kind in {"dividend_ex_date", "note"}:
            continue
        else:
            raise ValueError(f"unsupported independent cash event: {kind!r}")
    expected = expected.quantize(D("0.01"), rounding=ROUND_HALF_UP)
    recorded = D(recorded_cash).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    difference = recorded - expected
    return CashReconciliation(expected, recorded, difference, difference == 0)
