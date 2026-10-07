from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from .liquidity import LiquidityReference


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    source_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_shares: int
    amount_cny: Decimal | None = None
    trade_date: date | None = None
    available_at: datetime | None = None
    source_id: str = ""
    source_row_id: str = ""
    quality_flags: frozenset[str] = field(default_factory=frozenset)
    interval_start: datetime | None = None
    interval_end: datetime | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("OHLC range is inconsistent")
        if self.low > self.high or self.volume_shares < 0:
            raise ValueError("invalid low/high or volume")
        if (self.interval_start is None) != (self.interval_end is None):
            raise ValueError("bar interval requires both start and end")
        if (self.interval_start is not None and self.interval_end is not None
                and self.interval_start >= self.interval_end):
            raise ValueError("bar interval start must precede end")
        if self.interval_end is not None and self.available_at is not None and self.available_at < self.interval_end:
            raise ValueError("available_at precedes bar interval end")
        if self.available_at is not None and self.available_at < self.source_time:
            raise ValueError("available_at precedes source_time")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_id: str
    symbol: str
    side: Side
    quantity: int
    limit_price: Decimal
    created_at: datetime
    expires_at: datetime
    liquidity_reference: LiquidityReference | None = None
    decision_at: datetime | None = None
    arrival_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0 or self.limit_price <= 0:
            raise ValueError("quantity and limit price must be positive")
        if (self.liquidity_reference is not None
                and self.liquidity_reference.signal_date != self.created_at.date()):
            raise ValueError("liquidity reference signal date must match order creation date")
        if self.expires_at <= self.created_at:
            raise ValueError("order must expire after creation")
        if self.decision_at is not None and self.decision_at > self.created_at:
            raise ValueError("decision_at cannot follow order creation")
        if self.arrival_at is not None and self.arrival_at < self.created_at:
            raise ValueError("arrival_at cannot precede order creation")
        if self.arrival_at is not None and self.arrival_at >= self.expires_at:
            raise ValueError("order arrival must precede expiration")


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    quantity: int
    price: Decimal
    fee: Decimal
    trade_date: date
    filled_at: datetime


@dataclass(slots=True)
class PositionLot:
    lot_id: str
    symbol: str
    acquired_on: date
    sellable_on: date
    quantity: int
    entry_price: Decimal
    entry_fee_remaining: Decimal
