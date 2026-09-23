from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Iterable

from .models import Fill, OrderIntent, PositionLot, Side

D = Decimal
CENT = D("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def tick_up(value: Decimal, tick: Decimal = CENT) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def tick_down(value: Decimal, tick: Decimal = CENT) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    commission_rate: Decimal = D("0.0003")
    min_commission: Decimal = D("5.00")
    transfer_fee_rate: Decimal = D("0.00001")
    stamp_duty_sell_rate: Decimal = D("0.0005")

    @classmethod
    def for_trade_date(cls, trade_date: date) -> "FeeSchedule":
        if trade_date < date(2020, 1, 1):
            raise ValueError("historical fee rules before 2020-01-01 are not yet verified")
        return cls(
            transfer_fee_rate=D("0.00002") if trade_date < date(2022, 4, 29) else D("0.00001"),
            stamp_duty_sell_rate=D("0.001") if trade_date < date(2023, 8, 28) else D("0.0005"),
        )

    def cumulative_fee(self, side: Side, gross: Decimal) -> Decimal:
        if gross <= 0:
            return D("0.00")
        commission = max(self.min_commission, money(gross * self.commission_rate))
        transfer = money(gross * self.transfer_fee_rate)
        stamp = money(gross * self.stamp_duty_sell_rate) if side is Side.SELL else D("0.00")
        return money(commission + transfer + stamp)

    def estimate(self, side: Side, gross: Decimal) -> Decimal:
        return self.cumulative_fee(side, gross)

    def incremental_fee(self, side: Side, prior_gross: Decimal, new_gross: Decimal) -> Decimal:
        """Allocate an order-level minimum commission over partial fills."""
        if prior_gross < 0 or new_gross < 0:
            raise ValueError("gross amounts cannot be negative")
        return money(self.cumulative_fee(side, prior_gross + new_gross) - self.cumulative_fee(side, prior_gross))


@dataclass(slots=True)
class Account:
    initial_cash: Decimal
    cash_available: Decimal = field(init=False)
    cash_reserved: Decimal = D("0.00")
    receivables: Decimal = D("0.00")
    liabilities: Decimal = D("0.00")
    lots: list[PositionLot] = field(default_factory=list)
    seen_event_ids: set[str] = field(default_factory=set)
    fills: list[Fill] = field(default_factory=list)
    buy_reservations: dict[str, Decimal] = field(default_factory=dict)
    sell_reservations: dict[str, tuple[str, int]] = field(default_factory=dict)
    order_gross: dict[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.initial_cash = money(self.initial_cash)
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        self.cash_available = self.initial_cash

    @property
    def shares_total(self) -> int:
        return sum(lot.quantity for lot in self.lots)

    def shares_sellable(self, on: date, symbol: str | None = None) -> int:
        return sum(lot.quantity for lot in self.lots
                   if lot.sellable_on <= on and (symbol is None or lot.symbol == symbol))

    @property
    def cash_total(self) -> Decimal:
        return money(self.cash_available + self.cash_reserved)

    def nav(self, marks: dict[str, Decimal]) -> Decimal:
        market_value = sum((D(lot.quantity) * marks[lot.symbol] for lot in self.lots), D("0"))
        return money(self.cash_total + market_value + self.receivables - self.liabilities)

    def reserve_buy(self, order_id: str, amount: Decimal) -> None:
        amount = money(amount)
        if not order_id or amount <= 0:
            raise ValueError("valid order id and positive reserve are required")
        if order_id in self.buy_reservations:
            if self.buy_reservations[order_id] != amount:
                raise ValueError("order already has a different cash reservation")
            return
        if amount > self.cash_available:
            raise ValueError("insufficient available cash for reservation")
        self.cash_available = money(self.cash_available - amount)
        self.cash_reserved = money(self.cash_reserved + amount)
        self.buy_reservations[order_id] = amount

    def cancel_buy(self, order_id: str) -> Decimal:
        released = self.buy_reservations.pop(order_id, D("0.00"))
        self.cash_reserved = money(self.cash_reserved - released)
        self.cash_available = money(self.cash_available + released)
        return released

    def reserve_sell(self, order_id: str, symbol: str, quantity: int, on: date) -> None:
        if order_id in self.sell_reservations:
            if self.sell_reservations[order_id] != (symbol, quantity):
                raise ValueError("order already has a different share reservation")
            return
        reserved = sum(qty for held_symbol, qty in self.sell_reservations.values() if held_symbol == symbol)
        if quantity <= 0 or quantity > self.shares_sellable(on, symbol) - reserved:
            raise ValueError("insufficient T+1 sellable shares to reserve")
        self.sell_reservations[order_id] = (symbol, quantity)

    def cancel_sell(self, order_id: str) -> int:
        item = self.sell_reservations.pop(order_id, None)
        return item[1] if item else 0

    def buy(self, fill: Fill, fee_schedule: FeeSchedule, next_trade_date: date) -> None:
        if fill.fill_id in self.seen_event_ids:
            return
        self._validate_fill(fill, Side.BUY)
        gross = money(D(fill.quantity) * fill.price)
        prior_gross = self.order_gross.get(fill.order_id, D("0.00"))
        fee = fee_schedule.incremental_fee(Side.BUY, prior_gross, gross)
        if money(fill.fee) != fee:
            raise ValueError("fill fee does not match the configured schedule")
        reserve = self.buy_reservations.get(fill.order_id)
        if reserve is not None:
            if gross + fee > reserve:
                raise ValueError("fill exceeds order cash reservation")
            self.buy_reservations[fill.order_id] = money(reserve - gross - fee)
            self.cash_reserved = money(self.cash_reserved - gross - fee)
            if self.buy_reservations[fill.order_id] == 0:
                self.buy_reservations.pop(fill.order_id)
        else:
            if gross + fee > self.cash_available:
                raise ValueError("insufficient available cash")
            self.cash_available = money(self.cash_available - gross - fee)
        self.lots.append(PositionLot(fill.fill_id, fill.symbol, fill.trade_date, next_trade_date,
                                     fill.quantity, fill.price, fee))
        self._record(fill)

    def sell(self, fill: Fill, fee_schedule: FeeSchedule) -> None:
        if fill.fill_id in self.seen_event_ids:
            return
        self._validate_fill(fill, Side.SELL)
        reservation = self.sell_reservations.get(fill.order_id)
        if reservation is not None and (reservation[0] != fill.symbol or fill.quantity > reservation[1]):
            raise ValueError("sell fill exceeds its reserved shares")
        if fill.quantity > self.shares_sellable(fill.trade_date, fill.symbol):
            raise ValueError("sell quantity exceeds T+1 sellable shares")
        gross = money(D(fill.quantity) * fill.price)
        prior_gross = self.order_gross.get(fill.order_id, D("0.00"))
        fee = fee_schedule.incremental_fee(Side.SELL, prior_gross, gross)
        if money(fill.fee) != fee:
            raise ValueError("fill fee does not match the configured schedule")
        remaining = fill.quantity
        for lot in sorted(self.lots, key=lambda item: (item.acquired_on, item.lot_id)):
            if lot.symbol != fill.symbol or lot.sellable_on > fill.trade_date:
                continue
            take = min(lot.quantity, remaining)
            lot.quantity -= take
            remaining -= take
            if remaining == 0:
                break
        self.lots = [lot for lot in self.lots if lot.quantity]
        self.cash_available = money(self.cash_available + gross - fee)
        if reservation is not None:
            remainder = reservation[1] - fill.quantity
            if remainder:
                self.sell_reservations[fill.order_id] = (fill.symbol, remainder)
            else:
                self.sell_reservations.pop(fill.order_id)
        self._record(fill)

    def apply_cash_dividend(self, event_id: str, amount: Decimal, ex_date: bool) -> None:
        if event_id in self.seen_event_ids:
            return
        amount = money(amount)
        if amount < 0:
            raise ValueError("dividend amount cannot be negative")
        if ex_date:
            self.receivables = money(self.receivables + amount)
        else:
            if amount > self.receivables:
                raise ValueError("payment exceeds recorded receivable")
            self.receivables = money(self.receivables - amount)
            self.cash_available = money(self.cash_available + amount)
        self.seen_event_ids.add(event_id)

    def apply_share_change(self, event_id: str, symbol: str, ratio: Decimal) -> None:
        if event_id in self.seen_event_ids:
            return
        if ratio <= 0:
            raise ValueError("share change ratio must be positive")
        for lot in self.lots:
            if lot.symbol == symbol:
                new_quantity = int((D(lot.quantity) * ratio).to_integral_value(rounding=ROUND_FLOOR))
                lot.quantity = new_quantity
                lot.entry_price = money(lot.entry_price / ratio)
        self.lots = [lot for lot in self.lots if lot.quantity]
        self.seen_event_ids.add(event_id)

    def _validate_fill(self, fill: Fill, side: Side) -> None:
        if fill.side is not side or fill.quantity <= 0 or fill.price <= 0:
            raise ValueError("invalid fill")
        if fill.fill_id == "":
            raise ValueError("fill_id is required")

    def _record(self, fill: Fill) -> None:
        self.seen_event_ids.add(fill.fill_id)
        self.fills.append(fill)
        self.order_gross[fill.order_id] = money(self.order_gross.get(fill.order_id, D("0.00")) +
                                                D(fill.quantity) * fill.price)


def plan_entry_quantity(nav: Decimal, weight: Decimal, price_cap: Decimal,
                        cash_available: Decimal, reserve: Decimal = D("1000.00"),
                        lot_size: int = 100, fee_schedule: FeeSchedule | None = None) -> int:
    if nav <= 0 or not D("0") < weight <= D("1") or price_cap <= 0 or lot_size <= 0:
        raise ValueError("invalid entry sizing inputs")
    fees = fee_schedule or FeeSchedule()
    budget = min(money(nav * weight), money(cash_available - reserve))
    quantity = int((budget / price_cap / lot_size).to_integral_value(rounding=ROUND_FLOOR)) * lot_size
    while quantity > 0:
        gross = money(D(quantity) * price_cap)
        if gross + fees.estimate(Side.BUY, gross) <= cash_available - reserve:
            return quantity
        quantity -= lot_size
    return 0
