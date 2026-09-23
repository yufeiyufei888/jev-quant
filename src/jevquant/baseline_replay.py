from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from random import Random
from typing import Iterable, Iterator, Mapping, Sequence

from .actions import CashDividend
from .baselines import BaselineName, decide_baseline
from .execution import make_protection_price, match_open_proxy
from .ledger import Account, FeeSchedule, money, plan_entry_quantity
from .liquidity import build_liquidity_reference
from .models import Bar, Fill, OrderIntent, Side
from .reconciliation import AccountEventReconciliation, reconcile_account_events

D = Decimal
SYMBOL = "600519.SH"


@dataclass(frozen=True, slots=True)
class SessionExecutionRules:
    suspended: bool
    limit_up: Decimal
    limit_down: Decimal

    def __post_init__(self) -> None:
        if self.limit_up <= 0 or self.limit_down <= 0 or self.limit_up < self.limit_down:
            raise ValueError("invalid session price limits")


@dataclass(frozen=True, slots=True)
class BaselineReplayConfig:
    baseline: BaselineName
    initial_cash: Decimal = D("1000000.00")
    cash_reserve: Decimal = D("1000.00")
    order_delay: timedelta = timedelta(minutes=5)
    protection_bps: int = 50
    min_slippage_bps: int = 5
    lot_size: int = 100
    tick: Decimal = D("0.01")
    random_seed: int | None = None
    random_entry_probability: Decimal = D("0.05")
    random_exit_probability: Decimal = D("0.05")
    liquidity_fraction: Decimal = D("0.01")

    def __post_init__(self) -> None:
        if self.initial_cash <= 0 or self.cash_reserve < 0 or self.order_delay < timedelta(0):
            raise ValueError("invalid baseline account or delay configuration")
        if self.protection_bps < 0 or self.min_slippage_bps < 0 or self.lot_size <= 0 or self.tick <= 0:
            raise ValueError("invalid baseline execution configuration")
        if self.baseline == "RANDOM" and self.random_seed is None:
            raise ValueError("RANDOM replay requires a fixed seed")


@dataclass(frozen=True, slots=True)
class BaselineReplayResult:
    baseline: str
    random_seed: int | None
    start_date: str
    end_date: str
    decisions: tuple[dict[str, object], ...]
    orders: tuple[dict[str, object], ...]
    fills: tuple[Fill, ...]
    nav_curve: tuple[dict[str, object], ...]
    ending_cash: Decimal
    ending_receivables: Decimal
    ending_shares: int
    ending_nav: Decimal
    corporate_action_events_used: int
    corporate_action_events_pre_start: int
    valuation_incomplete: bool
    reconciliation: AccountEventReconciliation
    status: str


def _liquidity_json(reference) -> dict[str, object]:
    return {
        "signal_date": reference.signal_date.isoformat(),
        "slot": reference.slot,
        "session_dates": [day.isoformat() for day in reference.session_dates],
        "session_volumes_shares": list(reference.session_volumes_shares),
        "median_volume_shares": str(reference.median_volume_shares),
        "fraction": str(reference.fraction),
        "cap_shares": reference.cap_shares,
    }


def run_baseline_replay(
    config: BaselineReplayConfig,
    *,
    bars: Iterable[Bar],
    trade_dates: Sequence[date],
    daily_closes: Iterable[tuple[date, Decimal]],
    buy_eligible_by_date: Mapping[date, bool],
    execution_schedule: Mapping[date, Sequence[datetime]],
    session_close_by_date: Mapping[date, datetime],
    next_trade_date_by_date: Mapping[date, date],
    execution_rules_by_date: Mapping[date, SessionExecutionRules],
    dividends: Sequence[CashDividend] = (),
) -> BaselineReplayResult:
    """Run one non-JEV account using only explicitly bounded, available bars.

    The caller must provide a point-in-time eligibility flag for every session
    it wants to permit new buys, plus the independently resolved session
    schedule. An absent/false flag blocks buys. Each signal gets one scheduled
    execution interval; rejected or missing intervals are recorded and retried
    only at the next complete-bar decision.
    """
    sessions = tuple(trade_dates)
    if not sessions or tuple(sorted(set(sessions))) != sessions:
        raise ValueError("trade_dates must be non-empty, unique, and ordered")
    required = set(sessions)
    if (not required.issubset(execution_schedule) or not required.issubset(session_close_by_date)
            or not required.issubset(next_trade_date_by_date)
            or not required.issubset(execution_rules_by_date)):
        raise ValueError("schedule, close, next trade date, and execution rules must cover every replay date")
    if any(next_trade_date_by_date[day] <= day for day in sessions):
        raise ValueError("each next trade date must follow the current session")

    by_day: dict[date, list[Bar]] = {day: [] for day in sessions}
    for bar in bars:
        if bar.trade_date not in by_day:
            raise ValueError(f"bar date outside replay calendar: {bar.trade_date}")
        if bar.symbol != SYMBOL:
            raise ValueError(f"unexpected symbol in single-instrument baseline: {bar.symbol}")
        if (bar.interval_start is None or bar.interval_end is None or bar.available_at is None
                or bar.available_at < bar.interval_end):
            raise ValueError("baseline replay requires explicit, complete bar intervals")
        by_day[bar.trade_date].append(bar)
    for day, day_bars in by_day.items():
        day_bars.sort(key=lambda bar: (bar.interval_start, bar.interval_end, bar.source_row_id))
        starts = [bar.interval_start for bar in day_bars]
        if len(starts) != len(set(starts)):
            raise ValueError(f"duplicate execution interval for {day}")
        allowed = set(execution_schedule[day])
        if any(bar.interval_start not in allowed for bar in day_bars):
            raise ValueError(f"bar interval is absent from the declared execution schedule on {day}")
        if any(left.interval_end > right.interval_start for left, right in zip(day_bars, day_bars[1:])):
            raise ValueError(f"overlapping execution intervals for {day}")
        if any(bar.interval_end > session_close_by_date[day] for bar in day_bars):
            raise ValueError(f"bar extends beyond the declared session close for {day}")
    closes = tuple((day, D(close)) for day, close in daily_closes)
    if len({day for day, _ in closes}) != len(closes):
        raise ValueError("daily closes contain duplicate dates")
    action_events = tuple(event for event in dividends
                          if sessions[0] <= event.record_date <= sessions[-1])
    if any(event.record_date not in required for event in action_events):
        raise ValueError("corporate-action record date is absent from the replay calendar")
    pre_start_actions = sum(event.record_date < sessions[0] for event in dividends)

    account = Account(config.initial_cash)
    decisions: list[dict[str, object]] = []
    orders: list[dict[str, object]] = []
    marks: dict[str, Decimal] = {}
    nav_curve: list[dict[str, object]] = []
    volume_history: list[tuple[date, str, int]] = []
    rng = Random(config.random_seed) if config.baseline == "RANDOM" else None
    order_counter = 0
    valuation_incomplete = False

    for day in sessions:
        day_bars = by_day[day]
        close_at = session_close_by_date[day]
        pending: tuple[OrderIntent, datetime, FeeSchedule, Decimal] | None = None
        for bar in day_bars:
            interval_start = bar.interval_start
            interval_end = bar.interval_end
            assert interval_start is not None and interval_end is not None and bar.available_at is not None
            if pending is not None:
                order, expected_slot, fee_schedule, reserved_amount = pending
                if interval_start == expected_slot:
                    result = match_open_proxy(
                        order, bar, min_slippage_bps=config.min_slippage_bps,
                        limit_up=execution_rules_by_date[day].limit_up,
                        limit_down=execution_rules_by_date[day].limit_down,
                        suspended=execution_rules_by_date[day].suspended,
                        tick=config.tick, prior_filled_gross=account.order_gross.get(order.order_id, D("0.00")),
                        fee_schedule=fee_schedule)
                    item: dict[str, object] = {
                        "order_id": order.order_id, "side": order.side.value,
                        "decision_at": order.decision_at.isoformat() if order.decision_at else None,
                        "arrival_at": order.arrival_at.isoformat() if order.arrival_at else None,
                        "expected_execution_start": expected_slot.isoformat(),
                        "execution_bar": bar.source_row_id or bar.source_time.isoformat(),
                        "requested_quantity": order.quantity, "limit_price": str(order.limit_price),
                        "status": result.reason,
                        "liquidity_reference": _liquidity_json(order.liquidity_reference),
                    }
                    if result.fill is not None:
                        fill = result.fill
                        if fill.side is Side.BUY:
                            account.buy(fill, fee_schedule, next_trade_date_by_date[day])
                            account.cancel_buy(order.order_id)
                        else:
                            account.sell(fill, fee_schedule)
                            account.cancel_sell(order.order_id)
                        item["fill_id"] = fill.fill_id
                        item["fill_price"] = str(fill.price)
                        item["fill_fee"] = str(fill.fee)
                        item["filled_at"] = fill.filled_at.isoformat()
                    else:
                        if order.side is Side.BUY:
                            account.cancel_buy(order.order_id)
                        else:
                            account.cancel_sell(order.order_id)
                    orders.append(item)
                    pending = None
                elif interval_start > expected_slot:
                    # The scheduled interval was absent; never fill later using
                    # a different slot's liquidity reference.
                    if order.side is Side.BUY:
                        account.cancel_buy(order.order_id)
                    else:
                        account.cancel_sell(order.order_id)
                    orders.append({
                        "order_id": order.order_id, "side": order.side.value,
                        "decision_at": order.decision_at.isoformat() if order.decision_at else None,
                        "arrival_at": order.arrival_at.isoformat() if order.arrival_at else None,
                        "expected_execution_start": expected_slot.isoformat(),
                        "status": "SCHEDULED_EXECUTION_BAR_MISSING",
                        "released_reserve": str(reserved_amount),
                    })
                    pending = None

            # The completed bar close is available to this decision only after
            # its recorded availability time. The policy receives prior daily
            # closes, the current bar close, and account state, never future OHLC.
            marks[SYMBOL] = bar.close
            nav_curve.append({
                "trade_date": day.isoformat(), "available_at": bar.available_at.isoformat(),
                "cash": str(account.cash_total), "shares": account.shares_total,
                "mark": str(bar.close), "receivables": str(account.receivables),
                "nav": str(account.nav(marks)), "stale_mark": False,
            })
            volume_history.append((day, interval_start.strftime("%H:%M"), bar.volume_shares))

            if pending is not None:
                continue
            decision_time = bar.available_at
            has_position = account.shares_total > 0
            decision = decide_baseline(
                config.baseline, asof=day, daily_closes=closes,
                has_position=has_position,
                eligible_to_buy=bool(buy_eligible_by_date.get(day, False)),
                rng=rng, random_entry_probability=config.random_entry_probability,
                random_exit_probability=config.random_exit_probability)
            record = {
                "trade_date": day.isoformat(), "decision_at": decision_time.isoformat(),
                "action": decision.action, "reason": decision.reason,
                "has_position": has_position,
                "buy_eligible": bool(buy_eligible_by_date.get(day, False)),
                "prior_daily_close_count": sum(1 for close_day, _ in closes if close_day < day),
            }
            if decision.action not in {"BUY", "SELL"}:
                record["status"] = "NO_ORDER"
                decisions.append(record)
                continue

            arrival = decision_time + config.order_delay
            schedule = sorted(execution_schedule[day])
            expected_slot = next((slot for slot in schedule if slot >= arrival), None)
            if expected_slot is None or expected_slot >= close_at:
                record["status"] = "NO_EXECUTION_SLOT_AFTER_DELAY"
                decisions.append(record)
                continue
            slot_name = expected_slot.strftime("%H:%M")
            try:
                liquidity = build_liquidity_reference(
                    volume_history, signal_date=day, slot=slot_name,
                    max_fraction=config.liquidity_fraction, lot_size=config.lot_size)
            except ValueError as exc:
                record["status"] = "LIQUIDITY_REFERENCE_UNAVAILABLE"
                record["detail"] = str(exc)
                decisions.append(record)
                continue

            if decision.action == "BUY":
                price_cap = make_protection_price(Side.BUY, bar.close, config.protection_bps)
                weight = D("1") if decision.target_weight is None else decision.target_weight
                fee_schedule = FeeSchedule.for_trade_date(day)
                quantity = plan_entry_quantity(
                    account.nav(marks), weight, price_cap, account.cash_available,
                    reserve=config.cash_reserve, lot_size=config.lot_size,
                    fee_schedule=fee_schedule)
                quantity = min(quantity, liquidity.cap_shares)
                if quantity <= 0:
                    record["status"] = "MIN_LOT_CASH_OR_LIQUIDITY_REJECTED"
                    decisions.append(record)
                    continue
                order_counter += 1
                order_id = f"{config.baseline.lower()}-{config.random_seed}-{order_counter:08d}"
                reserve_amount = money(D(quantity) * price_cap
                                       + fee_schedule.estimate(Side.BUY, D(quantity) * price_cap))
                account.reserve_buy(order_id, reserve_amount)
                order = OrderIntent(
                    order_id, SYMBOL, Side.BUY, quantity, price_cap, decision_time, close_at,
                    liquidity_reference=liquidity, decision_at=decision_time,
                    arrival_at=arrival)
                status = "ORDER_SUBMITTED"
            else:
                fee_schedule = FeeSchedule.for_trade_date(day)
                sellable_quantity = account.shares_sellable(day, SYMBOL)
                quantity = min(sellable_quantity, liquidity.cap_shares)
                if sellable_quantity <= 0:
                    record["status"] = "T1_NO_SELLABLE_SHARES"
                    decisions.append(record)
                    continue
                if quantity <= 0:
                    record["status"] = "LIQUIDITY_CAP_BELOW_ONE_LOT"
                    decisions.append(record)
                    continue
                price_cap = make_protection_price(Side.SELL, bar.close, config.protection_bps)
                order_counter += 1
                order_id = f"{config.baseline.lower()}-{config.random_seed}-{order_counter:08d}"
                account.reserve_sell(order_id, SYMBOL, quantity, day)
                order = OrderIntent(
                    order_id, SYMBOL, Side.SELL, quantity, price_cap, decision_time, close_at,
                    liquidity_reference=liquidity, decision_at=decision_time,
                    arrival_at=arrival)
                reserve_amount = D(quantity)
                status = "ORDER_SUBMITTED"
            record.update({"status": status, "order_id": order_id,
                           "quantity": quantity, "limit_price": str(price_cap),
                           "arrival_at": arrival.isoformat(),
                           "execution_slot": expected_slot.isoformat()})
            if decision.action == "SELL":
                record["sellable_before_order"] = sellable_quantity
                record["remaining_after_full_fill"] = sellable_quantity - quantity
            decisions.append(record)
            pending = (order, expected_slot, fee_schedule, reserve_amount)

        if pending is not None:
            order, expected_slot, _, reserved_amount = pending
            if order.side is Side.BUY:
                account.cancel_buy(order.order_id)
            else:
                account.cancel_sell(order.order_id)
            orders.append({
                "order_id": order.order_id, "side": order.side.value,
                "decision_at": order.decision_at.isoformat() if order.decision_at else None,
                "arrival_at": order.arrival_at.isoformat() if order.arrival_at else None,
                "expected_execution_start": expected_slot.isoformat(),
                "status": ("SCHEDULED_EXECUTION_BAR_MISSING"
                           if expected_slot not in {bar.interval_start for bar in day_bars}
                           else "UNFILLED_SESSION_END"),
                "released_reserve": str(reserved_amount),
            })

        # Effective-date action events are applied once at each session close.
        for event in action_events:
            account.apply_cash_dividend_event(event, day)
        if day_bars:
            last_bar = day_bars[-1]
            marks[SYMBOL] = last_bar.close
            covers_close = last_bar.interval_end == close_at
            if account.shares_total and not covers_close:
                valuation_incomplete = True
            nav_curve.append({
                "trade_date": day.isoformat(),
                "available_at": max(close_at, last_bar.available_at).isoformat(),
                "cash": str(account.cash_total), "shares": account.shares_total,
                "mark": str(last_bar.close), "receivables": str(account.receivables),
                "nav": str(account.nav(marks)), "stale_mark": not covers_close,
                "daily_close": covers_close,
            })
        elif account.shares_total:
            valuation_incomplete = True
            nav_curve.append({
                "trade_date": day.isoformat(), "available_at": close_at.isoformat(),
                "cash": str(account.cash_total), "shares": account.shares_total,
                "mark": str(marks[SYMBOL]), "receivables": str(account.receivables),
                "nav": str(account.nav(marks)), "stale_mark": True, "daily_close": True,
            })

    asof = sessions[-1]
    reconciliation = reconcile_account_events(
        config.initial_cash, account.fills, action_events, asof,
        account.cash_available, account.receivables,
        {SYMBOL: account.shares_total})
    final_nav = account.nav(marks) if marks or not account.shares_total else money(account.cash_total)
    return BaselineReplayResult(
        baseline=config.baseline, random_seed=config.random_seed,
        start_date=sessions[0].isoformat(), end_date=sessions[-1].isoformat(),
        decisions=tuple(decisions), orders=tuple(orders), fills=tuple(account.fills),
        nav_curve=tuple(nav_curve), ending_cash=account.cash_available,
        ending_receivables=account.receivables, ending_shares=account.shares_total,
        ending_nav=final_nav, corporate_action_events_used=len(action_events),
        corporate_action_events_pre_start=pre_start_actions,
        valuation_incomplete=valuation_incomplete, reconciliation=reconciliation,
        status=("ACCOUNT_RECONCILIATION_FAILED" if not reconciliation.passed else
                "ACCOUNT_REPLAY_INCOMPLETE_VALUATION" if valuation_incomplete else
                "ACCOUNT_REPLAY_PASS"))


def replay_to_jsonable(result: BaselineReplayResult) -> dict[str, object]:
    return {
        "schema": "jevquant-baseline-replay/v1",
        "baseline": result.baseline,
        "random_seed": result.random_seed,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "decisions": list(result.decisions),
        "orders": list(result.orders),
        "fills": [{
            "fill_id": fill.fill_id, "order_id": fill.order_id,
            "symbol": fill.symbol, "side": fill.side.value,
            "quantity": fill.quantity, "price": str(fill.price),
            "fee": str(fill.fee), "trade_date": fill.trade_date.isoformat(),
            "filled_at": fill.filled_at.isoformat(),
        } for fill in result.fills],
        "nav_curve": list(result.nav_curve),
        "ending_cash_cny": str(result.ending_cash),
        "ending_receivables_cny": str(result.ending_receivables),
        "ending_shares": result.ending_shares,
        "ending_nav_cny": str(result.ending_nav),
        "corporate_action_events_used": result.corporate_action_events_used,
        "corporate_action_events_pre_start": result.corporate_action_events_pre_start,
        "valuation_incomplete": result.valuation_incomplete,
        "reconciliation": {
            "passed": result.reconciliation.passed,
            "expected_cash_cny": str(result.reconciliation.expected_cash),
            "cash_difference_cny": str(result.reconciliation.cash_difference),
            "expected_receivables_cny": str(result.reconciliation.expected_receivables),
            "receivables_difference_cny": str(result.reconciliation.receivables_difference),
            "expected_shares": dict(result.reconciliation.expected_shares),
            "share_differences": dict(result.reconciliation.share_differences),
        },
        "status": result.status,
    }


def summarize_baseline_result(result: BaselineReplayResult, initial_cash: Decimal) -> dict[str, object]:
    daily = [row for row in result.nav_curve if row.get("daily_close")]
    navs = [D(str(row["nav"])) for row in daily]
    high_water = D(initial_cash)
    max_drawdown = D("0")
    for nav in navs:
        high_water = max(high_water, nav)
        if high_water > 0:
            max_drawdown = max(max_drawdown, (high_water - nav) / high_water)
    exposure_sessions = sum(int(row["shares"]) > 0 for row in daily)
    fees = sum((fill.fee for fill in result.fills), D("0"))
    return {
        "baseline": result.baseline,
        "random_seed": result.random_seed,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "initial_cash_cny": str(D(initial_cash)),
        "ending_nav_cny": str(result.ending_nav),
        "total_return": str((result.ending_nav - D(initial_cash)) / D(initial_cash)),
        "daily_close_marks": len(daily),
        "max_daily_drawdown": str(max_drawdown),
        "filled_trades": len(result.fills),
        "filled_buy_orders": sum(fill.side is Side.BUY for fill in result.fills),
        "filled_sell_orders": sum(fill.side is Side.SELL for fill in result.fills),
        "fees_cny": str(money(fees)),
        "exposure_sessions": exposure_sessions,
        "exposure_fraction": str(D(exposure_sessions) / D(len(daily))) if daily else "0",
        "ending_cash_cny": str(result.ending_cash),
        "ending_shares": result.ending_shares,
        "reconciliation_passed": result.reconciliation.passed,
        "valuation_incomplete": result.valuation_incomplete,
        "status": result.status,
    }


def iter_baseline_suite(
    *,
    bars: Iterable[Bar],
    trade_dates: Sequence[date],
    daily_closes: Iterable[tuple[date, Decimal]],
    buy_eligible_by_date: Mapping[date, bool],
    execution_schedule: Mapping[date, Sequence[datetime]],
    session_close_by_date: Mapping[date, datetime],
    next_trade_date_by_date: Mapping[date, date],
    execution_rules_by_date: Mapping[date, SessionExecutionRules],
    dividends: Sequence[CashDividend] = (),
    random_seeds: Iterable[int] = range(100),
    initial_cash: Decimal = D("1000000.00"),
) -> Iterator[tuple[str, BaselineReplayResult]]:
    """Yield independent account replays under one frozen execution setup."""
    common = {
        "bars": tuple(bars), "trade_dates": tuple(trade_dates),
        "daily_closes": tuple(daily_closes),
        "buy_eligible_by_date": buy_eligible_by_date,
        "execution_schedule": execution_schedule,
        "session_close_by_date": session_close_by_date,
        "next_trade_date_by_date": next_trade_date_by_date,
        "execution_rules_by_date": execution_rules_by_date,
        "dividends": tuple(dividends),
    }
    for name in ("CASH", "BH80", "BH50", "BH_MAX", "MA5_20"):
        config = BaselineReplayConfig(name, initial_cash=initial_cash)  # type: ignore[arg-type]
        yield name, run_baseline_replay(config, **common)
    for seed in random_seeds:
        config = BaselineReplayConfig("RANDOM", initial_cash=initial_cash, random_seed=int(seed))
        yield f"RANDOM-seed-{seed}", run_baseline_replay(config, **common)
