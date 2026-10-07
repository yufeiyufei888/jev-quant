"""Read-only multi-stock inputs for the T+1 short-swing diagnostic."""
from __future__ import annotations

import csv
import json
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .data import _raw_market_price
from .models import Bar

D = Decimal
TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class UniverseMember:
    symbol: str
    median_amount_cny: Decimal
    last_close_cny: Decimal
    listing_sessions: int
    action_path: Path
    status_path: Path


def supported_symbol(symbol: str) -> bool:
    code, _, suffix = symbol.partition(".")
    if len(code) != 6 or not code.isdigit():
        return False
    return ((suffix == "SH" and code.startswith(("600", "601", "603", "605", "688", "689")))
            or (suffix == "SZ" and code.startswith(("000", "001", "002", "003", "300", "301"))))


def _status_path(status_root: Path, symbol: str) -> Path:
    return status_root / "symbols" / f"{symbol[:6]}.json"


def status_by_date(path: Path) -> dict[date, tuple[bool, bool]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = {}
    for day, _, trading, st in payload.get("status_rows", []):
        result[date.fromisoformat(day)] = (trading == "1", st == "1")
    return result


def select_universe(*, daily_root: Path, minute_root: Path, status_root: Path,
                    action_root: Path, start: date, size: int = 5) -> tuple[list[UniverseMember], list[dict]]:
    """Choose a frozen year-start universe from exactly the preceding sessions."""
    warm_days = sorted(date(int(p.stem[:4]), int(p.stem[4:6]), int(p.stem[6:8]))
                       for p in minute_root.rglob("*.parquet")
                       if len(p.stem) == 8 and p.stem.isdigit() and p.stem < start.strftime("%Y%m%d"))[-120:]
    if len(warm_days) < 120:
        raise ValueError("120 pre-start minute partitions required for listing-age verification")
    last20 = set(warm_days[-20:])
    earliest = warm_days[0]
    last = warm_days[-1]
    candidates: list[UniverseMember] = []
    exclusions: list[dict] = []
    for path in sorted(daily_root.glob("*.csv")):
        symbol = path.stem
        if not supported_symbol(symbol):
            continue
        amounts: dict[date, Decimal] = {}
        close: Decimal | None = None
        age = 0
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                day_text = row.get("交易日", "")
                if len(day_text) != 8 or not day_text.isdigit():
                    continue
                day = date(int(day_text[:4]), int(day_text[4:6]), int(day_text[6:8]))
                if day > last:
                    continue
                if day < earliest:
                    break
                if row.get("收盘价"):
                    age += 1
                if day in last20:
                    if not row.get("成交额（千元）") or not row.get("收盘价"):
                        continue
                    amounts[day] = D(row["成交额（千元）"]) * 1000
                    if day == last:
                        close = D(row["收盘价"])
        if age < 120 or len(amounts) != 20 or close is None or not D(5) <= close <= D(200):
            continue
        median = D(str(statistics.median(amounts.values())))
        if median < D("100000000"):
            continue
        status_path = _status_path(status_root, symbol)
        action_path = action_root / "symbols" / f"{symbol[:6]}.json"
        if not status_path.is_file() or not action_path.is_file():
            exclusions.append({"symbol": symbol, "reason": "STATUS_OR_ACTION_SOURCE_MISSING"})
            continue
        status = status_by_date(status_path)
        if last not in status or status[last] != (True, False):
            exclusions.append({"symbol": symbol, "reason": "PRIOR_SESSION_STATUS_INELIGIBLE_OR_UNKNOWN"})
            continue
        action = json.loads(action_path.read_text(encoding="utf-8"))
        if action.get("failures") or action.get("coverage_end", "") < f"{start.year}-12-01":
            exclusions.append({"symbol": symbol, "reason": "ACTION_SOURCE_INCOMPLETE"})
            continue
        candidates.append(UniverseMember(symbol, median, close, age, action_path, status_path))
    candidates.sort(key=lambda item: (-item.median_amount_cny, item.symbol))
    return candidates[:size], exclusions


def load_action_events(path: Path, symbol: str, year: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    events = []
    for row in payload.get("dividends", []):
        record = row.get("dividRegistDate", "")
        ex_day = row.get("dividOperateDate", "")
        payment = row.get("dividPayDate") or ex_day
        if not record or not ex_day or not payment or int(ex_day[:4]) != year:
            continue
        announced = row.get("dividPlanDate") or row.get("dividPlanAnnounceDate")
        if not announced or announced > record:
            raise ValueError(f"unusable action announcement chronology for {symbol} {ex_day}")
        cash = D(row.get("dividCashPsBeforeTax") or "0")
        share_ratio = D(row.get("dividStocksPs") or "0") + D(row.get("dividReserveToStockPs") or "0")
        if cash < 0 or share_ratio < 0:
            raise ValueError(f"invalid corporate action for {symbol} {ex_day}")
        if cash == 0 and share_ratio == 0:
            continue
        events.append({"event_id": f"{symbol}:{record}:{ex_day}", "symbol": symbol,
                       "record_date": date.fromisoformat(record), "ex_date": date.fromisoformat(ex_day),
                       "payment_date": date.fromisoformat(payment), "cash_per_share": cash,
                       "share_ratio": share_ratio, "known_date": date.fromisoformat(announced),
                       "evidence": "BAOSTOCK_RECONSTRUCTED_PRETAX_NOT_PIT_PROVEN"})
    return sorted(events, key=lambda event: (event["ex_date"], event["event_id"]))


def read_multi_minute_day(path: Path, symbols: tuple[str, ...], *,
                          include_opening: bool = False) -> dict[str, list[Bar]]:
    """Decode one partition once; opening is opt-in for source-volume audits."""
    fields = ["code", "trade_time", "open", "high", "low", "close", "vol", "amount", "date"]
    table = pq.read_table(path, columns=fields)
    selected = table.filter(pc.is_in(table["code"], value_set=pa.array(symbols)))
    float32 = {key: pa.types.is_float32(table.schema.field(key).type)
               for key in ("open", "high", "low", "close")}
    output = {symbol: [] for symbol in symbols}
    for row in selected.to_pylist():
        symbol = row["code"]
        end = datetime.fromisoformat(str(row["trade_time"])).replace(tzinfo=TZ)
        if end.hour == 9 and end.minute == 30 and not include_opening:
            continue
        if end.hour == 9 and end.minute == 30:
            role = frozenset({"opening_record"})
        elif end.hour == 15 and end.minute == 0:
            # This final label is retained for closing valuation only.
            role = frozenset({"closing_record"})
        else:
            role = frozenset()
        start = end - timedelta(minutes=5)
        prices = {key: _raw_market_price(row[key], key, float32_source=float32[key])[0]
                  for key in ("open", "high", "low", "close")}
        shares = D(str(row["vol"]))
        if shares != shares.to_integral_value():
            raise ValueError(f"fractional volume for {symbol} {end}")
        output[symbol].append(Bar(symbol, end, prices["open"], prices["high"], prices["low"],
                                  prices["close"], int(shares),
                                  D(str(row["amount"])) if row["amount"] is not None else None,
                                  end.date(), end, f"minute-file:{path.name}",
                                  f"{path.name}:{symbol}:{end:%H%M}",
                                  role | {"assumed_end_labeled_retrospective_only",
                                          "availability_assumed_at_interval_end"}, start, end))
    for bars in output.values():
        bars.sort(key=lambda bar: bar.interval_end)
    return output
