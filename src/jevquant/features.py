from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Iterable
from zoneinfo import ZoneInfo

from .actions import CashDividend

D = Decimal
CN_TZ = ZoneInfo("Asia/Shanghai")


def daily_features_asof(history: Iterable[tuple[date, Decimal]], asof: date,
                        windows: tuple[int, ...] = (5, 20, 60)) -> dict[str, Decimal | date | None]:
    """Compute raw-close features from observations available on or before `asof`.

    This intentionally does not apply adjustment factors or actions. Consumers
    must use a separately verified causal total-return series for action-aware
    historical returns; this is suitable for schema/synthetic tests only.
    """
    visible = sorted(((day, close) for day, close in history if day <= asof), key=lambda x: x[0])
    if not visible:
        return {"asof": asof, "visible_bars": 0}
    if any(close <= 0 for _, close in visible):
        raise ValueError("daily closes must be positive")
    result: dict[str, Decimal | date | None] = {"asof": asof, "visible_bars": len(visible)}
    for window in windows:
        if window <= 0:
            raise ValueError("windows must be positive")
        values = [close for _, close in visible[-window:]]
        result[f"raw_sma_{window}"] = sum(values, D("0")) / D(len(values)) if len(values) == window else None
    result["raw_price_return_1d"] = (visible[-1][1] / visible[-2][1] - D("1")) if len(visible) > 1 else None
    result["return_basis"] = "raw_close_unadjusted_actions_not_applied"
    return result


def daily_total_return_features_asof(
    history: Iterable[tuple[date, Decimal]],
    dividends: Iterable[CashDividend],
    asof_at: datetime,
    windows: tuple[int, ...] = (5, 20, 60),
) -> dict[str, Decimal | date | int | str | None]:
    """Build causal close-based features using only known, realized cash actions.

    Daily close availability is conservatively modelled as 15:05 Asia/Shanghai;
    actual historical vendor publication times are not available in the source.
    A per-share cash dividend is included only on its ex-date, when its official
    implementation notice was already known by that time and by the decision.
    """
    if asof_at.tzinfo is None or asof_at.utcoffset() is None:
        raise ValueError("asof_at must be timezone-aware")
    decision_time = asof_at.astimezone(CN_TZ)
    visible: list[tuple[date, Decimal]] = []
    for day, close in history:
        bar_available_at = datetime.combine(day, time(15, 5), CN_TZ)
        if bar_available_at <= decision_time:
            visible.append((day, close))
    visible.sort(key=lambda item: item[0])
    if len({day for day, _ in visible}) != len(visible):
        raise ValueError("daily history contains duplicate dates")
    if not visible:
        return {"asof": decision_time.date(), "visible_bars": 0,
                "return_basis": "raw_close_plus_known_cash_dividends"}
    if any(close <= 0 for _, close in visible):
        raise ValueError("daily closes must be positive")
    visible_days = {day for day, _ in visible}
    dividends_by_ex_date: dict[date, Decimal] = {}
    for event in dividends:
        if event.known_at.tzinfo is None or event.known_at.utcoffset() is None:
            raise ValueError("dividend known_at must be timezone-aware")
        if (event.ex_date <= decision_time.date()
                and event.ex_date in visible_days
                and event.known_at.astimezone(CN_TZ) <= decision_time
                and event.announcement_date <= event.record_date < event.ex_date):
            dividends_by_ex_date[event.ex_date] = (
                dividends_by_ex_date.get(event.ex_date, D("0")) + event.cash_per_share
            )
    total_return_index: list[tuple[date, Decimal]] = [(visible[0][0], D("1"))]
    previous_close = visible[0][1]
    level = D("1")
    for day, close in visible[1:]:
        cash = dividends_by_ex_date.get(day, D("0"))
        level *= (close + cash) / previous_close
        total_return_index.append((day, level))
        previous_close = close
    result: dict[str, Decimal | date | int | str | None] = {
        "asof": decision_time.date(),
        "visible_bars": len(visible),
        "cash_action_count_used": sum(1 for day in dividends_by_ex_date if day <= decision_time.date()),
        "last_total_return_index": total_return_index[-1][1],
        "return_basis": "raw_close_plus_known_cash_dividends",
        "daily_close_availability": "15:05 Asia/Shanghai simulation assumption; vendor arrival time unobserved",
    }
    for window in windows:
        if window <= 0:
            raise ValueError("windows must be positive")
        values = [value for _, value in total_return_index[-window:]]
        result[f"total_return_sma_{window}"] = (
            sum(values, D("0")) / D(window) if len(values) == window else None
        )
    result["total_return_1d"] = (
        total_return_index[-1][1] / total_return_index[-2][1] - D("1")
        if len(total_return_index) > 1 else None
    )
    return result
