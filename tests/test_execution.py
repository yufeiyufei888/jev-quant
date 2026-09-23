from datetime import date, datetime, timedelta
from decimal import Decimal as D

from jevquant.execution import match_open_proxy
from jevquant.models import Bar, OrderIntent, Side


def _order(side: Side = Side.BUY, limit: str = "101") -> OrderIntent:
    created = datetime(2024, 1, 2, 9, 30)
    return OrderIntent("o1", "600519.SH", side, 100, D(limit), created, created + timedelta(hours=1))


def _bar(**kwargs) -> Bar:
    fields = dict(symbol="600519.SH", source_time=datetime(2024, 1, 2, 9, 35), open=D("100"),
                  high=D("101"), low=D("99"), close=D("100"), volume_shares=100000,
                  trade_date=date(2024, 1, 2))
    fields.update(kwargs)
    return Bar(**fields)


def test_buy_uses_adverse_open_slippage_and_respects_limit():
    result = match_open_proxy(_order(limit="100.10"), _bar())
    assert result.fill is not None
    assert result.fill.price == D("100.05")


def test_buy_rejected_when_adverse_open_price_exceeds_limit():
    result = match_open_proxy(_order(limit="100.04"), _bar())
    assert result.fill is None
    assert result.reason == "PRICE_PROTECTION_REJECTED"


def test_unverified_open_or_close_record_cannot_be_used_as_fill_reference():
    result = match_open_proxy(_order(), _bar(quality_flags=frozenset({"opening_record"})))
    assert result.fill is None
    assert result.reason == "UNVERIFIED_BAR_ROLE"
