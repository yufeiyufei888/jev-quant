from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Iterable

D = Decimal


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
