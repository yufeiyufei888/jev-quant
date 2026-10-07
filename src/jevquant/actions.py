from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

D = Decimal


@dataclass(frozen=True, slots=True)
class CashDividend:
    event_id: str
    symbol: str
    cash_per_share: Decimal
    announcement_date: date
    known_at: datetime
    record_date: date
    ex_date: date
    payment_date: date
    evidence_level: str
    source_url: str


def load_cash_dividends(path: Path, symbol: str) -> list[CashDividend]:
    """Load and validate a sourced cash-dividend table without altering it."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "jevquant-cash-dividends/v1" or data.get("symbol") != symbol:
        raise ValueError("cash-dividend schema or symbol mismatch")
    events: list[CashDividend] = []
    seen: set[str] = set()
    for row in data.get("events", []):
        event = CashDividend(
            event_id=str(row["event_id"]),
            symbol=symbol,
            cash_per_share=D(str(row["cash_per_share"])),
            announcement_date=date.fromisoformat(row["announcement_date"]),
            known_at=datetime.fromisoformat(row["known_at"]),
            record_date=date.fromisoformat(row["record_date"]),
            ex_date=date.fromisoformat(row["ex_date"]),
            payment_date=date.fromisoformat(row["payment_date"]),
            evidence_level=str(row["evidence_level"]),
            source_url=str(row["source_url"]),
        )
        if event.event_id in seen:
            raise ValueError("duplicate cash-dividend event_id")
        seen.add(event.event_id)
        if event.cash_per_share <= 0:
            raise ValueError("cash dividend must be positive")
        if event.known_at.date() != event.announcement_date:
            raise ValueError("known_at must use the dated announcement timestamp policy")
        if event.known_at.tzinfo is None or event.known_at.utcoffset() is None:
            raise ValueError("known_at must include a timezone")
        if not (event.announcement_date <= event.record_date < event.ex_date <= event.payment_date):
            raise ValueError("invalid dividend announcement/record/ex/payment date order")
        if event.evidence_level != "OFFICIAL_IMPLEMENTATION_NOTICE":
            raise ValueError("cash-dividend event lacks official implementation evidence")
        if urlparse(event.source_url).scheme != "https" or not urlparse(event.source_url).netloc:
            raise ValueError("cash-dividend source must be an HTTPS URL")
        events.append(event)
    return sorted(events, key=lambda item: (item.ex_date, item.event_id))
