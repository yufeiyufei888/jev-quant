from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .ledger import FeeSchedule, tick_down, tick_up
from .models import Bar, Fill, OrderIntent, Side

D = Decimal


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    fill: Fill | None
    reason: str


def make_protection_price(side: Side, signal_price: Decimal, bps: int = 50) -> Decimal:
    factor = D(bps) / D(10000)
    if side is Side.BUY:
        return tick_up(signal_price * (D("1") + factor))
    return tick_down(signal_price * (D("1") - factor))


def match_open_proxy(order: OrderIntent, bar: Bar, *, min_slippage_bps: int = 5,
                     tick: Decimal = D("0.01"), limit_up: Decimal | None = None,
                     limit_down: Decimal | None = None, suspended: bool = False,
                     prior_filled_gross: Decimal = D("0.00"),
                     fee_schedule: FeeSchedule | None = None) -> ExecutionResult:
    if suspended:
        return ExecutionResult(None, "SUSPENDED")
    if bar.symbol != order.symbol:
        return ExecutionResult(None, "SYMBOL_MISMATCH")
    if order.arrival_at is None:
        return ExecutionResult(None, "MISSING_ORDER_ARRIVAL_TIME")
    if bar.interval_start is None or bar.interval_end is None:
        return ExecutionResult(None, "UNVERIFIED_BAR_INTERVAL")
    if bar.interval_start < order.arrival_at:
        return ExecutionResult(None, "BAR_STARTED_BEFORE_ORDER_ARRIVAL")
    if bar.interval_end > order.expires_at:
        return ExecutionResult(None, "OUTSIDE_ORDER_WINDOW")
    if bar.available_at is None:
        return ExecutionResult(None, "UNKNOWN_BAR_AVAILABILITY")
    if bar.available_at < bar.interval_end:
        return ExecutionResult(None, "BAR_NOT_COMPLETE_AT_AVAILABILITY")
    if bar.quality_flags.intersection({"opening_record", "closing_record", "auction_or_close_unverified"}):
        return ExecutionResult(None, "UNVERIFIED_BAR_ROLE")
    if order.liquidity_reference is None:
        return ExecutionResult(None, "MISSING_LIQUIDITY_REFERENCE")
    if order.quantity > order.liquidity_reference.cap_shares:
        return ExecutionResult(None, "LIQUIDITY_REFERENCE_CAP")
    if bar.volume_shares <= 0:
        return ExecutionResult(None, "NO_EXECUTABLE_VOLUME")
    # Open-proxy mode sizes orders only from the signal-time historical reference.
    # Execution-bar final volume is not used to retroactively resize the order.
    quantity = order.quantity
    slip = max(tick, bar.open * D(min_slippage_bps) / D(10000))
    price = bar.open + slip if order.side is Side.BUY else bar.open - slip
    price = (price / tick).to_integral_value(rounding=(ROUND_CEILING if order.side is Side.BUY else ROUND_FLOOR)) * tick
    if order.side is Side.BUY:
        if price > order.limit_price or (limit_up is not None and price > limit_up):
            return ExecutionResult(None, "PRICE_PROTECTION_REJECTED")
        if limit_up is not None and bar.high >= limit_up:
            return ExecutionResult(None, "LIMIT_UP_QUEUE_UNMODELED")
    else:
        if price < order.limit_price or (limit_down is not None and price < limit_down):
            return ExecutionResult(None, "PRICE_PROTECTION_REJECTED")
        if limit_down is not None and bar.low <= limit_down:
            return ExecutionResult(None, "LIMIT_DOWN_QUEUE_UNMODELED")
    if not bar.low <= price <= bar.high:
        return ExecutionResult(None, "FILL_OUTSIDE_BAR_RANGE")
    gross = D(quantity) * price
    fees = fee_schedule or FeeSchedule()
    fee = fees.incremental_fee(order.side, prior_filled_gross, gross)
    fill = Fill(f"{order.order_id}:{bar.source_time.isoformat()}", order.order_id, order.symbol,
                order.side, quantity, price, fee, bar.trade_date or bar.source_time.date(),
                bar.interval_start)
    return ExecutionResult(fill, "PARTIAL_FILL" if quantity < order.quantity else "FILLED")
