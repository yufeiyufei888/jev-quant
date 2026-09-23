from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from .actions import CashDividend
from .models import Fill, Side

D = Decimal


@dataclass(frozen=True, slots=True)
class CashReconciliation:
    reconstructed_cash: Decimal
    recorded_cash: Decimal
    difference: Decimal
    passed: bool


@dataclass(frozen=True, slots=True)
class AccountEventReconciliation:
    expected_cash: Decimal
    recorded_cash: Decimal
    cash_difference: Decimal
    expected_receivables: Decimal
    recorded_receivables: Decimal
    receivables_difference: Decimal
    expected_shares: Mapping[str, int]
    recorded_shares: Mapping[str, int]
    share_differences: Mapping[str, int]
    passed: bool


def reconcile_account_events(
    initial_cash: Decimal,
    fills: Iterable[Fill],
    dividends: Iterable[CashDividend],
    asof: date,
    recorded_cash: Decimal,
    recorded_receivables: Decimal,
    recorded_shares: Mapping[str, int],
    initial_shares: Mapping[str, int] | None = None,
) -> AccountEventReconciliation:
    """Independently reconstruct a cash-dividend account from dated source events.

    This intentionally does not call Account methods. Record-date holdings are
    reconstructed from fills through that date; cash and receivables are then
    derived from dividend effective dates.
    """
    ordered_fills = sorted((fill for fill in fills if fill.trade_date <= asof),
                           key=lambda item: (item.trade_date, item.filled_at, item.fill_id))
    expected_cash = D(initial_cash)
    holdings = dict(initial_shares or {})
    holdings_at_close: dict[date, dict[str, int]] = {}
    fill_index = 0
    dividend_items = list(dividends)
    record_dates = {event.record_date for event in dividend_items if event.record_date <= asof}
    days = sorted({fill.trade_date for fill in ordered_fills} | record_dates | {asof})
    for day in days:
        while fill_index < len(ordered_fills) and ordered_fills[fill_index].trade_date == day:
            fill = ordered_fills[fill_index]
            delta = fill.quantity if fill.side is Side.BUY else -fill.quantity
            holdings[fill.symbol] = holdings.get(fill.symbol, 0) + delta
            gross = D(fill.quantity) * fill.price
            expected_cash += (-gross - fill.fee) if fill.side is Side.BUY else (gross - fill.fee)
            if holdings[fill.symbol] < 0:
                raise ValueError(f"source fills produce a short holding for {fill.symbol}")
            fill_index += 1
        if day in record_dates:
            holdings_at_close[day] = dict(holdings)

    expected_receivables = D("0")
    for event in dividend_items:
        if event.record_date > asof:
            continue
        if event.record_date not in holdings_at_close:
            raise ValueError(f"record-date source history unavailable for dividend {event.event_id}")
        entitled = holdings_at_close[event.record_date].get(event.symbol, 0)
        amount = (D(entitled) * event.cash_per_share).quantize(D("0.01"), rounding=ROUND_HALF_UP)
        if event.ex_date <= asof < event.payment_date:
            expected_receivables += amount
        if event.payment_date <= asof:
            expected_cash += amount

    expected_cash = expected_cash.quantize(D("0.01"), rounding=ROUND_HALF_UP)
    recorded_cash = D(recorded_cash).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    expected_receivables = expected_receivables.quantize(D("0.01"), rounding=ROUND_HALF_UP)
    recorded_receivables = D(recorded_receivables).quantize(D("0.01"), rounding=ROUND_HALF_UP)
    expected_share_map = {symbol: quantity for symbol, quantity in holdings.items() if quantity}
    recorded_share_map = {symbol: quantity for symbol, quantity in recorded_shares.items() if quantity}
    share_differences = {
        symbol: recorded_share_map.get(symbol, 0) - expected_share_map.get(symbol, 0)
        for symbol in expected_share_map.keys() | recorded_share_map.keys()
        if recorded_share_map.get(symbol, 0) != expected_share_map.get(symbol, 0)
    }
    cash_difference = recorded_cash - expected_cash
    receivables_difference = recorded_receivables - expected_receivables
    return AccountEventReconciliation(
        expected_cash, recorded_cash, cash_difference,
        expected_receivables, recorded_receivables, receivables_difference,
        expected_share_map, recorded_share_map, share_differences,
        cash_difference == 0 and receivables_difference == 0 and not share_differences,
    )


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
