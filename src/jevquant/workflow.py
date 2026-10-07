from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .execution import make_protection_price, match_open_proxy
from .ledger import Account, FeeSchedule
from .liquidity import LiquidityReference
from .models import Bar, OrderIntent, Side
from .provider import DecisionResult, SystemOneClient, decide_cached
from .usage import UsageLedger

D = Decimal


def _mock_liquidity_reference(signal_date: date, slot: str) -> LiquidityReference:
    dates = tuple(signal_date - timedelta(days=20 - index) for index in range(20))
    volumes = tuple(100_000 for _ in dates)
    return LiquidityReference(signal_date, slot, dates, volumes, D("100000"), D("0.01"), 1000)


def _liquidity_receipt(reference: LiquidityReference) -> dict[str, Any]:
    return {
        "source": "synthetic_fixture",
        "signal_date": reference.signal_date.isoformat(),
        "slot": reference.slot,
        "session_dates": [day.isoformat() for day in reference.session_dates],
        "session_volumes_shares": list(reference.session_volumes_shares),
        "median_volume_shares": str(reference.median_volume_shares),
        "max_fraction": str(reference.fraction),
        "cap_shares": reference.cap_shares,
    }


class _MockJevClient:
    """Deterministic local mock; it does not connect to TypeSafe."""

    def system_one(self, *, state: Any, questions: Any, model: str) -> Any:
        actions = state["policy_context"]["allowed_actions"]
        choice = "BUY" if actions == ["BUY", "WAIT"] else "SELL"
        probabilities = {choice: 0.8, ("WAIT" if choice == "BUY" else "HOLD"): 0.2}
        answer = SimpleNamespace(choice=choice, probabilities=probabilities, confidence=0.8)
        return SimpleNamespace(model=model, answers={"action": answer},
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


def _decision_state(actions: list[str], cash: Decimal, shares: int) -> dict[str, Any]:
    return {
        "schema_version": "state_v1",
        "instrument": {"symbol": "600519.SH", "price_basis": "raw"},
        "account": {"cash_cny": str(cash), "shares": shares},
        "policy_context": {"allowed_actions": actions},
        "data": {"synthetic_fixture": True},
    }


def run_mock_round_trip(output_dir: Path) -> dict[str, Any]:
    """P5/P6 synthetic two-session path: JEV-shaped decision -> order -> fill -> NAV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    fee_schedule = FeeSchedule.for_trade_date(date(2024, 1, 2))
    account = Account(D("1000000.00"))
    client = _MockJevClient()
    usage = UsageLedger(output_dir / "jev-usage.jsonl")
    cache = output_dir / "decision-cache.json"
    decisions: list[DecisionResult] = []
    orders: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []

    first_state = _decision_state(["BUY", "WAIT"], account.cash_available, account.shares_total)
    buy_decision = decide_cached(first_state, "仅在预设范围内决定是否请求开仓", cache,
                                 client=client, source_override="mock")
    decisions.append(buy_decision)
    usage.record(buy_decision)
    if buy_decision.action_requested != "BUY":
        raise RuntimeError("mock fixture expected a BUY decision")

    buy_at = datetime.combine(date(2024, 1, 2), time(10, 5))
    buy_order = OrderIntent("mock-buy", "600519.SH", Side.BUY, 500,
                            make_protection_price(Side.BUY, D("1500.00")), buy_at,
                            datetime.combine(date(2024, 1, 2), time(15, 0)),
                            liquidity_reference=_mock_liquidity_reference(date(2024, 1, 2), "10:10"),
                            decision_at=buy_at, arrival_at=buy_at + timedelta(minutes=5))
    account.reserve_buy(buy_order.order_id, D("800000.00"))
    buy_bar = Bar("600519.SH", datetime.combine(date(2024, 1, 2), time(10, 15)),
                  D("1500.00"), D("1510.00"), D("1490.00"), D("1505.00"), 100000,
                  trade_date=date(2024, 1, 2), available_at=datetime.combine(date(2024, 1, 2), time(10, 15)),
                  source_id="synthetic-fixture", interval_start=datetime.combine(date(2024, 1, 2), time(10, 10)),
                  interval_end=datetime.combine(date(2024, 1, 2), time(10, 15)))
    matched_buy = match_open_proxy(buy_order, buy_bar, fee_schedule=fee_schedule)
    if matched_buy.fill is None:
        account.cancel_buy(buy_order.order_id)
        raise RuntimeError(f"mock BUY failed: {matched_buy.reason}")
    account.buy(matched_buy.fill, fee_schedule, date(2024, 1, 3))
    # The synthetic order is intentionally closed after its valid fill window;
    # any unused protection reserve must return to available cash.
    account.cancel_buy(buy_order.order_id)
    orders.append({"order_id": buy_order.order_id, "side": "BUY", "quantity": 500,
                   "limit_price": str(buy_order.limit_price), "status": matched_buy.reason,
                   "decision_at": buy_order.decision_at.isoformat(),
                   "order_arrival_at": buy_order.arrival_at.isoformat(),
                   "liquidity_reference": _liquidity_receipt(buy_order.liquidity_reference)})
    fills.append({"fill_id": matched_buy.fill.fill_id, "side": "BUY", "quantity": 500,
                  "price": str(matched_buy.fill.price), "fee": str(matched_buy.fill.fee),
                  "filled_at": matched_buy.fill.filled_at.isoformat()})

    second_state = _decision_state(["HOLD", "SELL"], account.cash_available,
                                   account.shares_total)
    sell_decision = decide_cached(second_state, "仅决定是否请求退出可卖持仓", cache,
                                  client=client, source_override="mock")
    decisions.append(sell_decision)
    usage.record(sell_decision)
    sell_at = datetime.combine(date(2024, 1, 3), time(10, 5))
    sell_order = OrderIntent("mock-sell", "600519.SH", Side.SELL,
                             account.shares_sellable(date(2024, 1, 3), "600519.SH"),
                             make_protection_price(Side.SELL, D("1510.00")), sell_at,
                             datetime.combine(date(2024, 1, 3), time(15, 0)),
                             liquidity_reference=_mock_liquidity_reference(date(2024, 1, 3), "10:10"),
                             decision_at=sell_at, arrival_at=sell_at + timedelta(minutes=5))
    sell_bar = Bar("600519.SH", datetime.combine(date(2024, 1, 3), time(10, 15)),
                   D("1510.00"), D("1520.00"), D("1500.00"), D("1515.00"), 100000,
                   trade_date=date(2024, 1, 3), available_at=datetime.combine(date(2024, 1, 3), time(10, 15)),
                   source_id="synthetic-fixture", interval_start=datetime.combine(date(2024, 1, 3), time(10, 10)),
                   interval_end=datetime.combine(date(2024, 1, 3), time(10, 15)))
    matched_sell = match_open_proxy(sell_order, sell_bar, fee_schedule=fee_schedule)
    if matched_sell.fill is None:
        raise RuntimeError(f"mock SELL failed: {matched_sell.reason}")
    account.sell(matched_sell.fill, fee_schedule)
    orders.append({"order_id": sell_order.order_id, "side": "SELL", "quantity": 500,
                   "limit_price": str(sell_order.limit_price), "status": matched_sell.reason,
                   "decision_at": sell_order.decision_at.isoformat(),
                   "order_arrival_at": sell_order.arrival_at.isoformat(),
                   "liquidity_reference": _liquidity_receipt(sell_order.liquidity_reference)})
    fills.append({"fill_id": matched_sell.fill.fill_id, "side": "SELL", "quantity": 500,
                  "price": str(matched_sell.fill.price), "fee": str(matched_sell.fill.fee),
                  "filled_at": matched_sell.fill.filled_at.isoformat()})

    summary = {
        "mode": "synthetic_mock_only",
        "real_api_calls": 0,
        "mock_decisions": len(decisions),
        "hard_api_budget_limit": None,
        "api_usage_cap_enabled": False,
        "api_usage_receipts": len(decisions),
        "account_initial_cash_cny": str(account.initial_cash),
        "account_ending_cash_cny": str(account.cash_available),
        "ending_shares": account.shares_total,
        "ending_nav_cny": str(account.nav({})),
        "decisions": [{"action": item.action_requested, "request_hash": item.request_hash,
                       "source": item.source} for item in decisions],
        "orders": orders,
        "fills": fills,
        "status": "synthetic_flow_passed_not_strategy_evidence",
    }
    import json
    with (output_dir / "mock-round-trip.json").open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    return summary
