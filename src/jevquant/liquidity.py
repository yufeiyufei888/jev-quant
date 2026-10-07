from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from typing import Iterable

D = Decimal


@dataclass(frozen=True, slots=True)
class LiquidityReference:
    signal_date: date
    slot: str
    session_dates: tuple[date, ...]
    session_volumes_shares: tuple[int, ...]
    median_volume_shares: Decimal
    fraction: Decimal
    cap_shares: int
    sessions_required: int = 20
    lot_size: int = 100

    def __post_init__(self) -> None:
        if (not self.slot or self.sessions_required <= 0 or self.lot_size <= 0
                or len(self.session_dates) != self.sessions_required
                or len(self.session_dates) != len(self.session_volumes_shares)):
            raise ValueError("liquidity reference needs matching slot dates and volumes")
        if any(day >= self.signal_date for day in self.session_dates):
            raise ValueError("liquidity reference contains a non-prior session")
        if tuple(sorted(set(self.session_dates))) != self.session_dates:
            raise ValueError("liquidity reference session dates must be unique and ordered")
        if any(volume < 0 for volume in self.session_volumes_shares):
            raise ValueError("historical slot volume cannot be negative")
        if self.median_volume_shares <= 0 or not D("0") < self.fraction <= D("1") or self.cap_shares < 0:
            raise ValueError("invalid liquidity reference median, fraction, or cap")
        ordered = sorted(self.session_volumes_shares)
        middle = len(ordered) // 2
        computed_median = (D(ordered[middle]) if len(ordered) % 2
                           else D(ordered[middle - 1] + ordered[middle]) / D("2"))
        expected_cap = int(((computed_median * self.fraction / self.lot_size)
                            .to_integral_value(rounding=ROUND_FLOOR))) * self.lot_size
        if computed_median != self.median_volume_shares or expected_cap != self.cap_shares:
            raise ValueError("liquidity reference median or cap does not match its recorded source volumes")


def build_liquidity_reference(
    history: Iterable[tuple[date, str, int]],
    *,
    signal_date: date,
    slot: str,
    sessions: int = 20,
    max_fraction: Decimal = D("0.01"),
    lot_size: int = 100,
) -> LiquidityReference:
    """Freeze an order-size cap from complete prior sessions in the same slot."""
    if not slot or sessions <= 0 or lot_size <= 0 or not D("0") < max_fraction <= D("1"):
        raise ValueError("invalid liquidity reference parameters")
    visible = [(day, int(volume)) for day, source_slot, volume in history
               if source_slot == slot and day < signal_date]
    if any(volume < 0 for _, volume in visible):
        raise ValueError("historical slot volume cannot be negative")
    if len({day for day, _ in visible}) != len(visible):
        raise ValueError("duplicate historical session for execution slot")
    visible.sort(key=lambda item: item[0])
    selected = visible[-sessions:]
    if len(selected) < sessions:
        raise ValueError(f"need {sessions} completed prior sessions for slot {slot}")
    ordered_volumes = sorted(volume for _, volume in selected)
    middle = len(ordered_volumes) // 2
    reference = (D(ordered_volumes[middle]) if len(ordered_volumes) % 2
                 else D(ordered_volumes[middle - 1] + ordered_volumes[middle]) / D("2"))
    raw_cap = reference * max_fraction
    cap_shares = int((raw_cap / lot_size).to_integral_value(rounding=ROUND_FLOOR)) * lot_size
    return LiquidityReference(signal_date, slot, tuple(day for day, _ in selected),
                              tuple(volume for _, volume in selected), reference,
                              max_fraction, cap_shares, sessions_required=sessions,
                              lot_size=lot_size)
