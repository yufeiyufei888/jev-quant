"""Multi-stock T+1 short-swing research replay, never connected to a broker.

Raw minute timestamps and status availability remain retrospective assumptions.
The strategy's orders use a delayed adverse-price proxy and dated A-share fees.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
from typing import Any, Callable

from .actions import CashDividend
from .budget import enforce_spend_budget, estimate_request_cost_usd, recorded_project_spend
from .data import read_daily_vendor_csv
from .ledger import Account, FeeSchedule, money, plan_entry_quantity
from .models import Bar, Fill, Side
from .provider import DecisionResult, decide_binary, request_hash
from .short_t_data import UniverseMember, load_action_events, read_multi_minute_day, select_universe, status_by_date

D = Decimal
INSTRUCTIONS = (
    "你只审核已由本地规则确定方向、数量和价格的A股T+1短差候选。"
    "选择EXECUTE表示认可当前候选，选择SKIP表示放弃；不得修改订单。"
    "只评价给定快照的短期价格、量能和账户风险；不把动作概率解释为盈利概率。"
)
ACTION_MEANINGS = {
    "EXECUTE": "Approve this exact local candidate for a delayed limit order; never alter its fields.",
    "SKIP": "Decline the candidate and place no order.",
}


@dataclass(frozen=True)
class ShortTConfig:
    initial_cash: Decimal = D("1000000")
    base_weight_each: Decimal = D("0.10")
    tactical_weight: Decimal = D("0.02")
    max_stock_weight: Decimal = D("0.16")
    max_total_weight: Decimal = D("0.80")
    max_open_cycles: int = 3
    max_reviews_per_day: int = 3
    signal_z: Decimal = D("1.25")
    near_vwap_z: Decimal = D("0.25")
    min_net_profit_bps: Decimal = D("15")
    max_adverse_return: Decimal = D("0.015")
    max_cycle_sessions: int = 2
    stop_new_drawdown: Decimal = D("0.10")
    resume_new_drawdown: Decimal = D("0.08")
    terminal_drawdown: Decimal = D("0.15")
    order_delay_minutes: int = 5
    limit_offset_bps: int = 50
    slippage_bps: int = 5
    volume_fraction: Decimal = D("0.01")


def load_short_t_config(path: Path) -> ShortTConfig:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "jevquant-short-t-protocol/v1":
        raise ValueError("unknown short-T protocol version")
    account, signal, risk, execution, jev = (payload[key] for key in
        ("account", "signals", "risk", "execution", "jev"))
    return ShortTConfig(
        initial_cash=D(account["initial_cash_cny"]),
        base_weight_each=D(account["base_weight_per_symbol"]),
        tactical_weight=D(account["tactical_order_weight"]),
        max_stock_weight=D(account["max_symbol_weight"]),
        max_total_weight=D(account["max_total_weight"]),
        max_open_cycles=int(account["max_open_cycles"]),
        max_reviews_per_day=int(jev["maximum_reviews_per_day"]),
        signal_z=D(signal["open_absolute_z"]),
        near_vwap_z=D(signal["close_absolute_z"]),
        min_net_profit_bps=D(signal["minimum_estimated_net_profit_bps"]),
        max_adverse_return=D(signal["maximum_adverse_return"]),
        max_cycle_sessions=int(signal["maximum_cycle_sessions"]),
        stop_new_drawdown=D(risk["pause_new_at_drawdown"]),
        resume_new_drawdown=D(risk["resume_below_drawdown"]),
        terminal_drawdown=D(risk["terminal_exit_at_drawdown"]),
        order_delay_minutes=int(execution["delay_minutes"]),
        limit_offset_bps=int(execution["limit_offset_bps"]),
        slippage_bps=int(execution["adverse_slippage_bps_or_one_tick"]),
        volume_fraction=D(execution["max_share_of_execution_bar_volume"]),
    )


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    symbol: str
    side: Side
    quantity: int
    kind: str
    decision_at: datetime
    signal_price: Decimal
    z: Decimal
    edge_bps: Decimal
    cycle_id: str | None = None


@dataclass
class Cycle:
    cycle_id: str
    symbol: str
    opening_side: Side
    opened_on: date
    first_quantity: int
    remaining_quantity: int
    first_price: Decimal
    first_fee: Decimal
    closing_value: Decimal = D("0")
    closing_fees: Decimal = D("0")
    status: str = "OPEN"
    close_reason: str = ""

    def net_profit(self) -> Decimal:
        first = D(self.first_quantity) * self.first_price
        gross = ((self.closing_value - first) if self.opening_side is Side.BUY
                 else (first - self.closing_value))
        return money(gross - self.first_fee - self.closing_fees)


def signal_features(bars: list[Bar]) -> tuple[Decimal, Decimal, Decimal] | None:
    """Last 12 closed bars; ATR is the mean true range in raw price units."""
    if len(bars) < 13:
        return None
    window = bars[-12:]
    volume = sum(bar.volume_shares for bar in window)
    if volume <= 0:
        return None
    vwap = sum((bar.close * D(bar.volume_shares) for bar in window), D(0)) / D(volume)
    true_ranges = [max(bar.high - bar.low, abs(bar.high - previous.close),
                       abs(bar.low - previous.close))
                   for previous, bar in zip(bars[-13:-1], window)]
    atr = sum(true_ranges, D(0)) / D(12)
    if atr <= 0:
        return None
    z = (window[-1].close - vwap) / atr
    return vwap, atr, z


def expected_round_trip_profit_bps(first_price: Decimal, second_price: Decimal,
                                   quantity: int, opening_side: Side,
                                   on: date, slip_bps: int = 5) -> Decimal:
    """Conservative gross spread after two adverse proxies and explicit fees."""
    if quantity <= 0:
        return D("-Infinity")
    buy_reference = first_price if opening_side is Side.BUY else second_price
    sell_reference = second_price if opening_side is Side.BUY else first_price
    slip = D(slip_bps) / D(10000)
    buy = ((buy_reference + max(D("0.01"), buy_reference * slip)) / D("0.01")).to_integral_value(
        rounding=ROUND_CEILING) * D("0.01")
    sell = ((sell_reference - max(D("0.01"), sell_reference * slip)) / D("0.01")).to_integral_value(
        rounding=ROUND_FLOOR) * D("0.01")
    fees = FeeSchedule.for_trade_date(on)
    net = (sell - buy) * quantity - fees.estimate(Side.BUY, buy * quantity) - fees.estimate(Side.SELL, sell * quantity)
    return net / (D(quantity) * first_price) * D(10000)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Side):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _append(path: Path, payload: dict) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bar_fill(candidate: Candidate, bar: Bar, *, remaining_bar_volume: int,
              limit_up: Decimal | None, limit_down: Decimal | None,
              account: Account, config: ShortTConfig) -> tuple[Fill | None, str]:
    if bar.interval_start != candidate.decision_at + timedelta(minutes=config.order_delay_minutes):
        return None, "EXECUTION_SLOT_MISSING"
    if bar.quality_flags.intersection({"opening_record", "closing_record"}):
        return None, "UNVERIFIED_BAR_ROLE"
    if bar.volume_shares <= 0:
        return None, "ZERO_VOLUME"
    cap = int((D(remaining_bar_volume) * config.volume_fraction / 100).to_integral_value(rounding=ROUND_FLOOR)) * 100
    quantity = min(candidate.quantity, cap)
    if quantity <= 0:
        return None, "VOLUME_CAP_BELOW_LOT"
    if candidate.symbol.startswith(("688", "689")) and quantity < 200:
        remaining_sellable = account.shares_sellable(bar.interval_start.date(), candidate.symbol)
        if candidate.side is Side.BUY or quantity != remaining_sellable:
            return None, "STAR_MINIMUM_ORDER_200"
    fee_schedule = FeeSchedule.for_trade_date(bar.interval_start.date())
    slip = max(D("0.01"), bar.open * D(config.slippage_bps) / D(10000))
    direction = D(1) if candidate.side is Side.BUY else D(-1)
    proxy = bar.open + direction * slip
    rounding = ROUND_CEILING if candidate.side is Side.BUY else ROUND_FLOOR
    price = (proxy / D("0.01")).to_integral_value(rounding=rounding) * D("0.01")
    protection = D(config.limit_offset_bps) / D(10000)
    limit = candidate.signal_price * (D(1) + protection if candidate.side is Side.BUY
                                      else D(1) - protection)
    limit = (limit / D("0.01")).to_integral_value(rounding=rounding) * D("0.01")
    if candidate.side is Side.BUY:
        if price > limit or (limit_up is not None and (price > limit_up or bar.high >= limit_up)):
            return None, "BUY_PRICE_OR_LIMIT_UP_REJECTED"
        while quantity and D(quantity) * price + fee_schedule.estimate(Side.BUY, D(quantity) * price) > account.cash_available:
            quantity -= 100
    else:
        if price < limit or (limit_down is not None and (price < limit_down or bar.low <= limit_down)):
            return None, "SELL_PRICE_OR_LIMIT_DOWN_REJECTED"
        quantity = min(quantity, (account.shares_sellable(bar.interval_start.date(), candidate.symbol) // 100) * 100)
    if quantity <= 0:
        return None, "CASH_OR_T1_REJECTED"
    if not bar.low <= price <= bar.high:
        return None, "PROXY_OUTSIDE_BAR"
    fee = fee_schedule.estimate(candidate.side, D(quantity) * price)
    fill = Fill(candidate.candidate_id + ":fill", candidate.candidate_id, candidate.symbol,
                candidate.side, quantity, price, fee, bar.interval_start.date(), bar.interval_start)
    return fill, "PARTIAL_FILL" if quantity < candidate.quantity else "FILLED"


class ShortTAccount:
    def __init__(self, name: str, symbols: tuple[str, ...], config: ShortTConfig,
                 *, output: Path, mode: str, baseline: Decimal | None = None,
                 baseline_at: datetime | None = None, client: Any | None = None):
        self.name, self.symbols, self.config = name, symbols, config
        self.output, self.mode, self.client = output, mode, client
        self.baseline, self.baseline_at = baseline, baseline_at
        self.account = Account(config.initial_cash)
        self.base_targets: dict[str, int] = {}
        self.cycles: dict[str, Cycle] = {}
        self.pending: dict[str, Candidate] = {}
        self.marks: dict[str, Decimal] = {}
        self.peak = config.initial_cash
        self.max_drawdown = D(0)
        self.terminal_risk = False
        self.paused_new = False
        self.review_count_today = 0
        self.decisions: list[dict] = []
        self.orders: list[dict] = []
        self.fills: list[dict] = []
        self.nav_rows: list[dict] = []
        self.cycle_rows: list[dict] = []
        self.provider_errors: list[dict] = []
        self.reviews = 0
        self.cache_path = output / name / "jev-response-cache.json"

    def _holdings(self, symbol: str) -> int:
        return sum(lot.quantity for lot in self.account.lots if lot.symbol == symbol)

    def _nav(self) -> Decimal:
        missing = {lot.symbol for lot in self.account.lots if lot.symbol not in self.marks}
        if missing:
            raise ValueError(f"held symbols have no reliable mark: {sorted(missing)}")
        return self.account.nav(self.marks)

    def _drawdown(self) -> Decimal:
        nav = self._nav()
        self.peak = max(self.peak, nav)
        drawdown = (self.peak - nav) / self.peak
        self.max_drawdown = max(self.max_drawdown, drawdown)
        if drawdown >= self.config.terminal_drawdown:
            self.terminal_risk = True
        if drawdown >= self.config.stop_new_drawdown:
            self.paused_new = True
        elif drawdown < self.config.resume_new_drawdown and not self.terminal_risk:
            self.paused_new = False
        return drawdown

    def _base_candidate(self, symbol: str, bar: Bar) -> Candidate | None:
        if self.terminal_risk or bar.interval_end.time() != time(10, 30):
            return None
        # A sell-first T cycle temporarily uses old base shares. Its buy-back
        # restores the base; a separate base order would purchase them twice.
        if any(cycle.symbol == symbol and cycle.status == "OPEN" and
               cycle.opening_side is Side.SELL for cycle in self.cycles.values()):
            return None
        if symbol not in self.base_targets:
            target = plan_entry_quantity(self._nav(), self.config.base_weight_each,
                                         bar.close * D("1.005"), self.account.cash_available,
                                         fee_schedule=FeeSchedule.for_trade_date(bar.interval_end.date()))
            self.base_targets[symbol] = target
        remaining = self.base_targets[symbol] - self._holdings(symbol)
        if remaining < 100:
            return None
        if symbol.startswith(("688", "689")) and remaining < 200:
            return None
        return Candidate(f"{self.name}:{symbol}:{bar.interval_end:%Y%m%dT%H%M}:BASE", symbol,
                         Side.BUY, remaining, "BASE", bar.interval_end, bar.close, D(0), D(0))

    def _cycle_candidate(self, symbol: str, bar: Bar, history: list[Bar],
                         session_index: int) -> Candidate | None:
        if self.mode == "base" or self.terminal_risk:
            return None
        features = signal_features(history)
        if features is None:
            return None
        vwap, _, z = features
        day = bar.interval_end.date()
        cycle = next((item for item in self.cycles.values()
                      if item.symbol == symbol and item.status == "OPEN"), None)
        if cycle is not None:
            side = Side.SELL if cycle.opening_side is Side.BUY else Side.BUY
            edge = expected_round_trip_profit_bps(cycle.first_price, bar.close,
                                                   cycle.remaining_quantity, cycle.opening_side,
                                                   day, self.config.slippage_bps)
            adverse = ((cycle.first_price - bar.close) / cycle.first_price
                       if cycle.opening_side is Side.BUY else
                       (bar.close - cycle.first_price) / cycle.first_price)
            session_age = session_index - self._session_indices[cycle.opened_on]
            forced = adverse >= self.config.max_adverse_return or session_age >= self.config.max_cycle_sessions
            if not forced and not (abs(z) <= self.config.near_vwap_z and edge >= self.config.min_net_profit_bps):
                return None
            kind = "CYCLE_RISK_EXIT" if forced else "CYCLE_PROFIT_EXIT"
            return Candidate(f"{self.name}:{symbol}:{bar.interval_end:%Y%m%dT%H%M}:{kind}",
                             symbol, side, cycle.remaining_quantity, kind, bar.interval_end,
                             bar.close, z, edge, cycle.cycle_id)
        if self.paused_new or len([item for item in self.cycles.values() if item.status == "OPEN"]) >= self.config.max_open_cycles:
            return None
        if not time(10, 30) <= bar.interval_end.time() <= time(14, 30):
            return None
        if self._holdings(symbol) < self.base_targets.get(symbol, 0) or self.base_targets.get(symbol, 0) == 0:
            return None
        if abs(z) < self.config.signal_z:
            return None
        nav = self._nav()
        qty = int((nav * self.config.tactical_weight / bar.close / 100).to_integral_value(rounding=ROUND_FLOOR)) * 100
        if qty <= 0:
            return None
        if symbol.startswith(("688", "689")) and qty < 200:
            return None
        side = Side.BUY if z < 0 else Side.SELL
        if side is Side.BUY:
            if ((D(self._holdings(symbol)) + qty) * bar.close / nav > self.config.max_stock_weight
                    or sum(D(self._holdings(code)) * self.marks[code] for code in self.symbols) / nav
                    + D(qty) * bar.close / nav > self.config.max_total_weight):
                return None
        elif self.account.shares_sellable(day, symbol) < qty:
            return None
        edge = expected_round_trip_profit_bps(bar.close, vwap, qty, side, day,
                                               self.config.slippage_bps)
        if edge < self.config.min_net_profit_bps:
            return None
        return Candidate(f"{self.name}:{symbol}:{bar.interval_end:%Y%m%dT%H%M}:OPEN", symbol,
                         side, qty, "CYCLE_OPEN", bar.interval_end, bar.close, z, edge)

    def _confirm(self, candidate: Candidate, *, daily_call_allowance: int) -> bool:
        if self.mode == "pure" or candidate.kind in {"BASE", "CYCLE_RISK_EXIT", "ACCOUNT_RISK_EXIT"}:
            return True
        if self.mode == "base":
            return False
        if self.review_count_today >= daily_call_allowance:
            return False
        self.review_count_today += 1
        self.reviews += 1
        if self.mode == "mock":
            approve = abs(candidate.z) >= D("1.50") if candidate.kind == "CYCLE_OPEN" else candidate.edge_bps >= D(25)
            self.decisions.append({"candidate_id": candidate.candidate_id, "mode": "mock",
                                   "action": "EXECUTE" if approve else "SKIP"})
            return approve
        if self.mode != "jev" or self.baseline is None or self.baseline_at is None:
            raise ValueError("live JEV review needs a current spend baseline and timestamp")
        state = {"schema_version": "short_t_candidate_v1", "candidate": _jsonable(asdict(candidate)),
                 "account": {"cash_cny": str(self.account.cash_available),
                             "nav_cny": str(self._nav()), "shares": self._holdings(candidate.symbol)},
                 "policy_context": {"allowed_actions": ["EXECUTE", "SKIP"],
                                    "t_plus_one": True, "max_weight": str(self.config.max_stock_weight)}}
        digest = request_hash(state, ["EXECUTE", "SKIP"], INSTRUCTIONS,
                              action_meanings=ACTION_MEANINGS)
        cached = False
        reserve = D(0)
        if self.cache_path.exists():
            cached = digest in json.loads(self.cache_path.read_text(encoding="utf-8"))
        if not cached:
            reserve = estimate_request_cost_usd(state, INSTRUCTIONS, ACTION_MEANINGS)
            spent = recorded_project_spend(self.output.parent.parent, after=self.baseline_at)
            enforce_spend_budget(portal_baseline_usd=self.baseline,
                                 account_limit_usd=D(5), local_recorded_usd=spent,
                                 next_call_reserve_usd=reserve)
        try:
            answer = decide_binary(state, INSTRUCTIONS, self.cache_path,
                                   client=self.client, action_meanings=ACTION_MEANINGS)
        except Exception as exc:
            self.provider_errors.append({"candidate_id": candidate.candidate_id,
                                         "error_type": type(exc).__name__, "action": "SKIP_NO_ORDER"})
            if not cached:
                _append(self.output / self.name / "errors.jsonl", {
                    "recorded_at": datetime.now(timezone.utc), "candidate_id": candidate.candidate_id,
                    "estimated_cost_usd": reserve, "error_type": type(exc).__name__,
                    "action": "SKIP_NO_ORDER", "account_mutated": False})
            return False
        self.decisions.append({"candidate_id": candidate.candidate_id,
                               "action": answer.action_requested, "request_hash": answer.request_hash,
                               "probabilities": answer.option_probabilities,
                               "input_tokens": answer.input_tokens,
                               "estimated_cost_usd": answer.estimated_cost_usd})
        if answer.source == "jev" and not cached:
            _append(self.output / self.name / "jev-usage.jsonl", {
                "recorded_at": datetime.now(timezone.utc), "request_hash": answer.request_hash,
                "estimated_cost_usd": answer.estimated_cost_usd if answer.estimated_cost_usd is not None else reserve,
                "input_tokens": answer.input_tokens})
        return answer.action_requested == "EXECUTE"

    def _apply_fill(self, candidate: Candidate, fill: Fill, next_day: date, reason: str) -> None:
        fees = FeeSchedule.for_trade_date(fill.trade_date)
        if fill.side is Side.BUY:
            self.account.buy(fill, fees, next_day)
        else:
            self.account.sell(fill, fees)
        self.fills.append({"candidate_id": candidate.candidate_id, "fill_id": fill.fill_id,
                           "symbol": fill.symbol, "side": fill.side.value, "quantity": fill.quantity,
                           "price_cny": fill.price, "fee_cny": fill.fee,
                           "trade_date": fill.trade_date, "filled_at": fill.filled_at,
                           "kind": candidate.kind, "match_result": reason})
        if candidate.kind == "CYCLE_OPEN":
            self.cycles[candidate.candidate_id] = Cycle(candidate.candidate_id,
                fill.symbol, fill.side, fill.trade_date, fill.quantity, fill.quantity,
                fill.price, fill.fee)
        elif candidate.cycle_id:
            cycle = self.cycles[candidate.cycle_id]
            cycle.remaining_quantity -= fill.quantity
            cycle.closing_value += D(fill.quantity) * fill.price
            cycle.closing_fees += fill.fee
            if cycle.remaining_quantity <= 0:
                cycle.status = "CLOSED"
                cycle.close_reason = candidate.kind
                self.cycle_rows.append(_jsonable({**asdict(cycle), "net_profit_cny": cycle.net_profit()}))

    def step(self, at: datetime, bars: dict[str, Bar], histories: dict[str, list[Bar]],
             daily_rows: dict[str, dict], statuses: dict[str, dict],
             session_index: int, next_day: date) -> None:
        day = at.date()
        self._session_indices = self._session_indices if hasattr(self, "_session_indices") else {}
        self._session_indices[day] = session_index
        for symbol, bar in bars.items():
            self.marks[symbol] = bar.close
        # Every pending order gets one complete post-arrival bar; it never survives a gap.
        for symbol, candidate in list(self.pending.items()):
            expected_end = candidate.decision_at + timedelta(minutes=self.config.order_delay_minutes + 5)
            if at < expected_end:
                continue
            self.pending.pop(symbol)
            bar = bars.get(symbol)
            row = daily_rows[symbol]
            if at != expected_end or bar is None or statuses[symbol].get(day) != (True, False):
                result, fill = "MISSING_OR_INELIGIBLE_EXECUTION_BAR", None
            else:
                fill, result = _bar_fill(candidate, bar, remaining_bar_volume=bar.volume_shares,
                    limit_up=row["limit_up_raw"], limit_down=row["limit_down_raw"],
                    account=self.account, config=self.config)
            self.orders.append({"candidate_id": candidate.candidate_id, "symbol": symbol,
                                "side": candidate.side.value, "quantity": candidate.quantity,
                                "decision_at": candidate.decision_at,
                                "arrival_at": candidate.decision_at + timedelta(minutes=5),
                                "status": result, "filled_quantity": fill.quantity if fill else 0})
            if fill:
                self._apply_fill(candidate, fill, next_day, result)
        drawdown = self._drawdown()
        if at.time() == time(15, 0):
            return
        candidates = []
        for symbol in self.symbols:
            bar = bars.get(symbol)
            if bar is None or statuses[symbol].get(day) != (True, False) or symbol in self.pending:
                continue
            if self.terminal_risk:
                sellable = (self.account.shares_sellable(day, symbol) // 100) * 100
                if sellable:
                    candidates.append(Candidate(f"{self.name}:{symbol}:{at:%Y%m%dT%H%M}:ACCOUNT_RISK",
                        symbol, Side.SELL, sellable, "ACCOUNT_RISK_EXIT", at, bar.close, D(0), D("Infinity")))
                continue
            base = self._base_candidate(symbol, bar)
            if base:
                self.pending[symbol] = base
                continue
            signal = self._cycle_candidate(symbol, bar, histories[symbol], session_index)
            if signal:
                candidates.append(signal)
        if not candidates:
            return
        forced = [item for item in candidates if item.kind in {"CYCLE_RISK_EXIT", "ACCOUNT_RISK_EXIT"}]
        discretionary = [item for item in candidates if item not in forced]
        for item in forced:
            self.pending[item.symbol] = item
        if discretionary:
            top = sorted(discretionary, key=lambda item: (-item.edge_bps, item.symbol))[0]
            if self._confirm(top, daily_call_allowance=self.config.max_reviews_per_day):
                self.pending[top.symbol] = top

    def finish_day(self, day: date, actions: list[dict]) -> None:
        for event in actions:
            if event["record_date"] == day or event["payment_date"] == day:
                if event["cash_per_share"]:
                    self.account.apply_cash_dividend_event(CashDividend(
                        event["event_id"], event["symbol"], event["cash_per_share"],
                        event["known_date"], datetime.combine(event["known_date"], time(23, 59),
                            tzinfo=timezone(timedelta(hours=8))), event["record_date"],
                        event["ex_date"], event["payment_date"], event["evidence"],
                        "local-reconstructed-source"), day)
        nav = self._nav()
        self.nav_rows.append({"trade_date": day, "nav_cny": nav,
                              "cash_cny": self.account.cash_available,
                              "receivables_cny": self.account.receivables,
                              "marks_cny": dict(self.marks),
                              "shares": {symbol: self._holdings(symbol) for symbol in self.symbols},
                              "drawdown": self._drawdown()})
        self.review_count_today = 0

    def apply_preopen_actions(self, day: date, actions: list[dict]) -> None:
        for event in actions:
            if event["ex_date"] != day:
                continue
            if event["cash_per_share"]:
                self.account.apply_cash_dividend_event(CashDividend(
                    event["event_id"], event["symbol"], event["cash_per_share"],
                    event["known_date"], datetime.combine(event["known_date"], time(23, 59),
                        tzinfo=timezone(timedelta(hours=8))), event["record_date"],
                    event["ex_date"], event["payment_date"], event["evidence"],
                    "local-reconstructed-source"), day)
            if event["share_ratio"]:
                self.account.apply_share_change(event["event_id"] + ":bonus", event["symbol"],
                                                D(1) + event["share_ratio"])
                base = self.base_targets.get(event["symbol"])
                if base is not None:
                    self.base_targets[event["symbol"]] = int(D(base) * (D(1) + event["share_ratio"]))
                for cycle in self.cycles.values():
                    if cycle.symbol == event["symbol"] and cycle.status == "OPEN":
                        cycle.first_quantity = int(D(cycle.first_quantity) * (D(1) + event["share_ratio"]))
                        cycle.remaining_quantity = int(D(cycle.remaining_quantity) * (D(1) + event["share_ratio"]))
                        cycle.first_price = cycle.first_price / (D(1) + event["share_ratio"])


def run_short_t(*, data_root: Path, support_root: Path, output: Path,
                start: date, sessions: int, modes: tuple[str, ...] = ("base", "pure", "mock"),
                config: ShortTConfig = ShortTConfig(),
                spend_baseline_usd: Decimal | None = None,
                spend_baseline_at: datetime | None = None,
                client: Any | None = None,
                progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if sessions <= 0 or any(mode not in {"base", "pure", "mock", "jev"} for mode in modes):
        raise ValueError("invalid session count or short-T account mode")
    if "jev" in modes and (spend_baseline_usd is None or spend_baseline_at is None
                           or spend_baseline_at.tzinfo is None):
        raise ValueError("live JEV review requires a current portal spend and timestamp")
    if start.year > 2023:
        raise ValueError("2024+ action-source coverage is not yet independently verified")
    minute_root = data_root / "5分钟数据"
    daily_root = data_root / "股票全量数据（日线）"
    status_root = support_root / "support-data" / (
        "status-industry-2022-2023-strict" if start.year <= 2023 else "status-industry-v2")
    action_root = (support_root / "full-market" / "historical-support-2022-2023" / "actions-v2")
    members, exclusions = select_universe(daily_root=daily_root, minute_root=minute_root,
                                          status_root=status_root, action_root=action_root,
                                          start=start)
    if len(members) != 5:
        raise ValueError(f"only {len(members)} eligible year-start symbols; five required")
    symbols = tuple(member.symbol for member in members)
    paths = sorted((minute_root / str(start.year)).glob("*.parquet"))
    paths = [path for path in paths if path.stem >= start.strftime("%Y%m%d")][:sessions]
    if len(paths) != sessions:
        raise ValueError("not enough actual minute partitions for the requested period")
    days = [date(int(path.stem[:4]), int(path.stem[4:6]), int(path.stem[6:8])) for path in paths]
    daily = {symbol: {row["trade_date"]: row for row in
                      read_daily_vendor_csv(daily_root / f"{symbol}.csv", symbol)}
             for symbol in symbols}
    statuses = {item.symbol: status_by_date(item.status_path) for item in members}
    all_actions = [event for member in members for event in
                   load_action_events(member.action_path, member.symbol, start.year)]
    next_days = {}
    for day in days:
        following = sorted(candidate for candidate in daily[symbols[0]] if candidate > day)
        if not following:
            raise ValueError(f"missing next trading day needed for T+1 after {day}")
        next_days[day] = following[0]
    for day in days:
        for symbol in symbols:
            if day not in statuses[symbol] or day not in daily[symbol]:
                raise ValueError(f"incomplete status or daily price: {symbol} {day}")
    input_paths = [*(daily_root / f"{symbol}.csv" for symbol in symbols),
                   *(item.status_path for item in members),
                   *(item.action_path for item in members), *paths]
    input_hashes = {str(path): _sha256(path) for path in input_paths}
    output.mkdir(parents=True, exist_ok=True)
    arms = {mode: ShortTAccount(mode, symbols, config, output=output, mode=mode,
                                baseline=spend_baseline_usd, baseline_at=spend_baseline_at,
                                client=client) for mode in modes}
    for arm in arms.values():
        (output / arm.name).mkdir(parents=True, exist_ok=True)
        arm.marks.update({item.symbol: item.last_close_cny for item in members})
    quality_gaps = []
    volume_rounding_differences = []
    for index, (day, path) in enumerate(zip(days, paths)):
        source_data = read_multi_minute_day(path, symbols, include_opening=True)
        data = {symbol: [bar for bar in source_data[symbol]
                         if "opening_record" not in bar.quality_flags]
                for symbol in symbols}
        for symbol in symbols:
            expected_volume = daily[symbol][day]["volume_shares"]
            observed_volume = sum(bar.volume_shares for bar in source_data[symbol])
            difference = None if expected_volume is None else observed_volume - expected_volume
            if difference and abs(difference) <= 100:
                volume_rounding_differences.append({"trade_date": day.isoformat(),
                    "symbol": symbol, "difference_shares": difference})
            if len(data[symbol]) != 48 or expected_volume is None or abs(difference) > 100:
                quality_gaps.append({"trade_date": day.isoformat(), "symbol": symbol,
                                     "bar_count": len(data[symbol]),
                                     "minute_volume_shares": observed_volume,
                                     "daily_volume_shares": expected_volume})
        if quality_gaps:
            raise ValueError(f"minute coverage gap; account replay halted at {day}")
        actions_today = [event for event in all_actions
                         if day in (event["record_date"], event["ex_date"], event["payment_date"])]
        for arm in arms.values():
            arm.apply_preopen_actions(day, actions_today)
        histories = {symbol: [] for symbol in symbols}
        by_end = {bar.interval_end: {} for rows in data.values() for bar in rows}
        for symbol, rows in data.items():
            for bar in rows:
                by_end[bar.interval_end][symbol] = bar
        daily_rows = {symbol: daily[symbol][day] for symbol in symbols}
        for at in sorted(by_end):
            bars = by_end[at]
            for symbol, bar in bars.items():
                histories[symbol].append(bar)
            for arm in arms.values():
                arm.step(at, bars, histories, daily_rows, statuses, index, next_days[day])
        for arm in arms.values():
            arm.finish_day(day, actions_today)
        if progress and ((index + 1) % 10 == 0 or index + 1 == sessions):
            progress(f"short-T {index + 1}/{sessions} sessions; " +
                     ", ".join(f"{name} NAV={arm.nav_rows[-1]['nav_cny']} fills={len(arm.fills)}"
                               for name, arm in arms.items()))
    results = {}
    for name, arm in arms.items():
        run_dir = output / name
        for label, rows in (("decisions", arm.decisions), ("orders", arm.orders),
                            ("fills", arm.fills), ("nav", arm.nav_rows),
                            ("cycles", arm.cycle_rows), ("provider-errors", arm.provider_errors)):
            (run_dir / f"{label}.jsonl").write_text("".join(
                json.dumps(_jsonable(row), ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
        final_nav = arm.nav_rows[-1]["nav_cny"]
        completed = [cycle for cycle in arm.cycles.values() if cycle.status == "CLOSED"]
        tactical_fills = [fill for fill in arm.fills if fill["kind"].startswith("CYCLE_")]
        summary = {"schema": "jevquant-short-t-account/v1", "account": name,
                   "result_scope": "RETROSPECTIVE_DIAGNOSTIC_NOT_EXECUTABLE_RETURN_EVIDENCE",
                   "start": days[0], "end": days[-1], "sessions": sessions,
                   "selected_symbols": symbols, "initial_cash_cny": config.initial_cash,
                   "ending_nav_cny": final_nav, "total_return": final_nav / config.initial_cash - 1,
                   "max_drawdown": arm.max_drawdown, "fill_count": len(arm.fills),
                   "short_t_fill_count": len(tactical_fills),
                   "short_t_fill_days": len({fill["trade_date"] for fill in tactical_fills}),
                   "average_short_t_fills_per_session": D(len(tactical_fills)) / D(sessions),
                   "review_count": arm.reviews, "provider_error_count": len(arm.provider_errors),
                   "completed_cycles": len(completed),
                   "realized_cycle_profit_cny": sum((cycle.net_profit() for cycle in completed), D(0)),
                   "open_cycles": [cycle.cycle_id for cycle in arm.cycles.values() if cycle.status == "OPEN"],
                   "terminal_risk_failed": arm.terminal_risk,
                   "average_fills_per_session": D(len(arm.fills)) / D(sessions),
                   "ending_cash_cny": arm.account.cash_available,
                   "ending_receivables_cny": arm.account.receivables,
                   "ending_shares": {symbol: arm._holdings(symbol) for symbol in symbols},
                   "data_limits": ["minute interval-end labels and availability are assumptions",
                                   "historical security status is retrospective",
                                   "corporate actions are BaoStock reconstructed pre-tax records"]}
        (run_dir / "summary.json").write_text(json.dumps(_jsonable(summary), ensure_ascii=False,
                                                  indent=2) + "\n", encoding="utf-8")
        results[name] = summary
    manifest = {"schema": "jevquant-short-t-run/v1", "universe_asof": start,
                "universe": [asdict(item) for item in members], "excluded_source_candidates": exclusions,
                "source_mode": "READ_ONLY_D_DRIVE", "accounts": list(modes),
                "config": asdict(config), "action_count": len(all_actions),
                "quality_gaps": quality_gaps, "result_scope": "RETROSPECTIVE_DIAGNOSTIC"}
    manifest["volume_rounding_differences"] = volume_rounding_differences
    if any(_sha256(path) != input_hashes[str(path)] for path in input_paths):
        raise ValueError("a source file changed during the account replay")
    manifest["source_sha256"] = input_hashes
    (output / "manifest.json").write_text(json.dumps(_jsonable(manifest), ensure_ascii=False,
                                                indent=2) + "\n", encoding="utf-8")
    from .short_t_audit import audit_account
    for name in modes:
        audit = audit_account(output / name, output / "manifest.json")
        (output / name / "independent-audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        results[name]["independent_audit_passed"] = audit["passed"]
        (output / name / "summary.json").write_text(json.dumps(_jsonable(results[name]),
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not audit["passed"]:
            raise ValueError(f"independent account audit failed for {name}")
    return results
