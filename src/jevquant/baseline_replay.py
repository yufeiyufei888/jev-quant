from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_left
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
    bar_schedule_by_date: Mapping[date, Sequence[datetime]] | None = None,
    prior_volume_history: Iterable[tuple[date, str, int]] = (),
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
    bar_schedule = execution_schedule if bar_schedule_by_date is None else bar_schedule_by_date
    if (not required.issubset(execution_schedule) or not required.issubset(session_close_by_date)
            or not required.issubset(next_trade_date_by_date)
            or not required.issubset(execution_rules_by_date)
            or not required.issubset(bar_schedule)):
        raise ValueError("schedule, close, next trade date, and execution rules must cover every replay date")
    if any(not set(execution_schedule[day]).issubset(set(bar_schedule[day])) for day in sessions):
        raise ValueError("every eligible execution start must also be a supplied bar start")
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
        allowed = set(bar_schedule[day])
        if any(bar.interval_start not in allowed for cxïKh‘éì¶»§q«^w¢$D”täõ5D”5ôôäÅ•ôäõEôdõ$ÔÅô„•5Dõ$”4ÅôU„T5UD”ôåô44UDä4R"À¢'7–Ö&öÂ#¢5”Ô$ôÂÀ¢&–æ—F–Åö66…ö6ç’#¢#ã"À¢'7F'EöFFR#¢7F'Bæ—6öf÷&ÖB‚’Â&VæEöFFR#¢VæBæ—6öf÷&ÖB‚’À¢'6W76–öç2#¢ÆVâ‡6W76–öç2’Â'v&×W÷föÇVÖU÷6W76–öç2#¢¶Bæ—6öf÷&ÖB‚’f÷"B–âv&×W÷6W76–öç5ÒÀ¢&FF÷&ö÷E÷6÷W&6U÷F‡2#¢²&Ö–çWFU÷&ö÷B#¢7G"†Ö–çWFU÷&ö÷B’Â&F–Ç•ö77b#¢7G"†F–Ç•ö77b—ÒÀ¢'F–ÖW7F×ö‡—÷F†W6—2#¢°¢&ÖöFR#¢'7FæF&EöÆ&VÇ5ö&Uö–çFW'fÅöVæB"À¢&Ö–ær#¢'G&FU÷F–ÖRÆ&VÂÂÖ2Fò´ÂÓRÖ–çWFW2ÂÅÒ"À¢'7V6–Åó“3÷&V6÷&B#¢&W†6ÇVFVBg&öÒæ÷&ÖÆ—¦VBFV6—6–öâöW†V7WF–öâ&'2"À¢&f–Æ&–Æ—G’#¢&77VÖVB–ÖÖVF–FVÇ’B–çFW'fÅöVæC²7GVÂ&÷f–FW"&VÆV6RFVÆ’—2Væ¶æ÷vâ"À¢'7FGW5öFF#¢'&WG&÷7V7F—fR6GW&VBfÇVW3²æ÷Bö–çBÖ–â×F–ÖR"À¢&f–ÆÇ2#¢&&"Ö÷Vâ&÷‡’öæÇ“²æòVWVR÷"&VÂW†V7WF–öâ6Æ–Ò"À¢ÒÀ¢&VÆ–v–&ÆUöW†V7WF–öå÷7F'E÷v–æF÷w2#¢²#“£3RÓ£#R"Â#3£RÓC£S%ÒÀ¢&fVUöÆ&VÂ#¢&ÖöFVÆVBfVW2W"6öæf–wW&VB66†VGVÆS²æ÷BF‚Ö6W'F–f–VB"À¢&66÷VçE÷&V6öæ6–ÆVB#¢ÆÂ‡&÷u²'&V6öæ6–Æ–F–öå÷76VB%Òf÷"&÷r–â7VÖÖ'•÷&÷w2’À¢&ÆÅ÷fÇVF–öç5ö6ö×ÆWFR#¢ÆÂ†æ÷B&÷u²'fÇVF–öåö–æ6ö×ÆWFR%Òf÷"&÷r–â7VÖÖ'•÷&÷w2’À¢&66÷VçEö6÷VçB#¢ÆVâ‡7VÖÖ'•÷&÷w2’Â'7VÖÖ&–W2#¢7VÖÖ'•÷&÷w2À¢'6÷W&6Uöf–ÆW5÷6†#Sb#¢°¢&F–Ç•ö77b#¢†6†Æ–"ç6†#Sb†F–Ç•ö77bç&VEö'—FW2‚’’æ†W†F–vW7B‚’À¢&F—f–FVæEö6öæf–r#¢†6†Æ–"ç6†#Sb†F—f–FVæEö6öæf–rç&VEö'—FW2‚’’æ†W†F–vW7B‚’À¢'7FGW5öf–ÆW2#¢·7G"‡F‚“¢†6†Æ–"ç6†#Sb‡F‚ç&VEö'—FW2‚’’æ†W†F–vW7B‚’f÷"F‚–â7FGW5÷F‡7ÒÀ¢ÒÀ¢Ð¢†÷WGWBò'7VÖÖ'’æ§6öâ"’çw&—FU÷FW‡B†§6öâæGV×2‡–ÆöBÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"Â6÷'Eö¶W—3ÕG'VR’À¢Væ6öF–æsÒ'WFbÓ‚"ÂæWvÆ–æSÒ%Æâ"¢&WGW&â–Æö@  ¦FVbÖ–â‚’ÓâæöæS ¢'6W"Ò&w'6Rä&wVÖVçE'6W"†FW67&—F–öãÒ%B&WG&÷7V7F—fRÖöæÇ’Ö÷WF’&6VÆ–æRF–væ÷7F–72"¢'6W"æFEö&wVÖVçB‚"ÒÖÖ–çWFR×&ö÷B"ÂG—SÕF‚Â&WV—&VCÕG'VR¢'6W"æFEö&wVÖVçB‚"ÒÖF–Ç’Ö77b"ÂG—SÕF‚Â&WV—&VCÕG'VR¢'6W"æFEö&wVÖVçB‚"Ò×7FGW2Ó##"Ó##2"ÂG—SÕF‚Â&WV—&VCÕG'VR¢'6W"æFEö&wVÖVçB‚"Ò×7FGW2Ó##B×ÇW2"ÂG—SÕF‚Â&WV—&VCÕG'VR¢'6W"æFEö&wVÖVçB‚"ÒÖF—f–FVæG2"ÂG—SÕF‚ÂFVfVÇCÕF‚‚&6öæf–w2öÖ÷WF•ó##5ó##Eö66…öF—f–FVæG2æ§6öâ"’¢'6W"æFEö&wVÖVçB‚"Ò×7F'B"ÂG—SÖFFRæg&öÖ—6öf÷&ÖBÂ&WV—&VCÕG'VR¢'6W"æFEö&wVÖVçB‚"ÒÖVæB"ÂG—SÖFFRæg&öÖ—6öf÷&ÖBÂ&WV—&VCÕG'VR¢'6W"æFEö&wVÖVçB‚"ÒÖ÷WGWB"ÂG—SÕF‚Â&WV—&VCÕG'VR¢'6W"æFEö&wVÖVçB‚"ÒÖ6öæf—&ÒÖF–væ÷7F–2ÖVæBÖÆ&VÂÖ‡—÷F†W6—2"Â7F–öãÒ'7F÷&U÷G'VR"À¢†VÇÒ&W‡Æ–6—FÇ’66WBâVçfW&–f–VBVæBÖÆ&VÂ66Væ&–òf÷"F–væ÷7F–2÷WGWBöæÇ’"¢&w2Ò'6W"ç'6Uö&w2‚¢–bæ÷B&w2æ6öæf—&ÕöF–væ÷7F–5öVæEöÆ&VÅö‡—÷F†W6—3 ¢'6W"æW'&÷"‚&×W7B72ÒÖ6öæf—&ÒÖF–væ÷7F–2ÖVæBÖÆ&VÂÖ‡—÷F†W6—3²&W7VÇB—2æWfW"f÷&ÖÂ66WFæ6R"¢–ÆöBÒ'Vå÷&WG&÷7V7F—fUö&6VÆ–æW2€¢Ö–çWFU÷&ö÷CÖ&w2æÖ–çWFU÷&ö÷BÂF–Ç•ö77cÖ&w2æF–Ç•ö77bÀ¢7FGW5÷F‡3Ò†&w2ç7FGW5ó##%ó##2Â&w2ç7FGW5ó##E÷ÇW2’À¢F—f–FVæEö6öæf–sÖ&w2æF—f–FVæG2Â÷WGWCÖ&w2æ÷WGWBÂ7F'CÖ&w2ç7F'BÂVæCÖ&w2æVæBÀ¢&öw&W75ö6ÆÆ&6³ÖÆÖ&FÖW76vS¢&–çB†ÖW76vRÂfÇW6ƒÕG'VR’À¢¢&–çB†§6öâæGV×2‡°¢&÷WGWB#¢7G"†&w2æ÷WGWBç&W6öÇfR‚’’Â''Vå÷7FGW2#¢–ÆöE²''Vå÷7FGW2%ÒÀ¢'6W76–öç2#¢–ÆöE²'6W76–öç2%ÒÂ&66÷VçEö6÷VçB#¢–ÆöE²&66÷VçEö6÷VçB%ÒÀ¢&66÷VçE÷&V6öæ6–ÆVB#¢–ÆöE²&66÷VçE÷&V6öæ6–ÆVB%ÒÀ¢&ÆÅ÷fÇVF–öç5ö6ö×ÆWFR#¢–ÆöE²&ÆÅ÷fÇVF–öç5ö6ö×ÆWFR%ÒÀ¢'7VÖÖ&–W2#¢–ÆöE²'7VÖÖ&–W2%ÒÀ¢ÒÂVç7W&Uö66–“ÔfÇ6RÂ–æFVçCÓ"’  ¦–bõöæÖUõòÓÒ%õöÖ–åõò# ¢Ö–â‚