from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from random import Random
from typing import Iterable, Literal

D = Decimal
BaselineName = Literal["CASH", "BH80", "BH50", "BH_MAX", "MA5_20", "RANDOM"]
Action = Literal["BUY", "WAIT", "HOLD", "SELL"]


@dataclass(frozen=True, slots=True)
class BaselineDecision:
    baseline: BaselineName
    action: Action
    target_weight: Decimal | None
    sizing_policy: str
    reason: str


def decide_baseline(
    baseline: BaselineName,
    *,
    asof: date,
    daily_closes: Iterable[tuple[date, Decimal]],
    has_position: bool,
    eligible_to_buy: bool,
    rng: Random | None = None,
    random_entry_probability: Decimal = D("0.05"),
    random_exit_probability: Decimal = D("0.05"),
) -> BaselineDecision:
    """Return a baseline intent using only completed daily data before `asof`.

    This creates a decision only. Order sizing, fees, T+1, execution and cash
    accounting remain the responsibility of the common simulator.
    """
    visible = sorted(((day, D(close)) for day, close in daily_closes if day < asof),
                     key=lambda item: item[0])
    if len({day for day, _ in visible}) != len(visible):
        raise ValueError("baseline input contains duplicate daily dates")
    if any(close <= 0 for _, close in visible):
        raise ValueError("baseline daily closes must be positive")
    if not D("0") <= random_entry_probability <= D("1"):
        raise ValueError("random entry probability must be between zero and one")
    if not D("0") <= random_exit_probability <= D("1"):
        raise ValueError("random exit probability must be between zero and one")

    if baseline == "CASH":
        return BaselineDecision(baseline, "SELL" if has_position else "WAIT", D("0"),
                                "cash_only", "cash baseline never opens a position")
    if baseline in ("BH80", "BH50", "BH_MAX"):
        weight = {"BH80": D("0.8"), "BH50": D("0.5"), "BH_MAX": None}[baseline]
        sizing = "max_affordable_after_reserved_fees_and_cash_buffer" if baseline == "BH_MAX" else "fixed_nav_weight"
        if has_position:
            return BaselineDecision(baseline, "HOLD", weight, sizing, "buy-and-hold after first fill")
        action = "BUY" if eligible_to_buy else "WAIT"
        return BaselineDecision(baseline, action, weight, sizing,
                                "first eligible entry opportunity" if eligible_to_buy else "entry not currently eligible")
    if baseline == "MA5_20":
        if len(visible) < 20:
            return BaselineDecision(baseline, "HOLD" if has_position else "WAIT", D("0.8"),
                                    "fixed_nav_weight", "need 20 completed prior daily closes")
        closes = [close for _, close in visible]
        ma5 = sum(closes[-5:], D("0")) / D("5")
        ma20 = sum(closes[-20:], D("0")) / D("20")
        if ma5 > ma20:
            action = "HOLD" if has_position else ("BUY" if eligible_to_buy else "WAIT")
            reason = "prior-session MA5 above MA20"
        elif ma5 < ma20:
            action = "SELL" if has_position else "WAIT"
            reason = "prior-session MA5 below MA20"
        else:
            action = "HOLD" if has_position else "WAIT"
            reason = "prior-session MA5 equals MA20"
        return BaselineDecision(baseline, action, D("0.8"), "fixed_nav_weight", reason)
    if baseline == "RANDOM":
        if rng is None:
            raise ValueError("RANDOM baseline requires a caller-owned seeded RNG")
        if has_position:
            action = "SELL" if D(str(rng.random())) < random_exit_probability else "HOLD"
        else:
            action = "BUY" if eligible_to_buy and D(str(rng.random())) < random_entry_probability else "WAIT"
        return BaselineDecision(baseline, action, D("0.8"), "fixed_nav_weight",
                                "seeded legal-action random baseline")
    raise ValueError(f"unsupported baseline: {baseline}")
