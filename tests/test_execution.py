from datetime import date, datetime, timedelta
from dataclasses import replace
from decimal import Decimal as D

import pytest

from jevquant.execution import match_open_proxy
from jevquant.models import Bar, OrderIntent, Side
from jevquant.liquidity import LiquidityReference, build_liquidity_reference


def _order(side: Side = Side.BUY, limit: str = "101") -> OrderIntent:
    created = datetime(2024, 1, 2, 9, 30)
    return OrderIntent("o1", "600519.SH", side, 100, D(limit), created,
                       created + timedelta(hours=1), liquidity_reference=_reference(created.date()))


def _reference(signal_day: date, cap: int = 100) -> LiquidityReference:
    dates = tuple(date(2023, 12, 1) + timedelta(days=index) for index in range(20))
    volumes = tuple(cap * 100 for _ in dates)
    return LiquidityReference(signal_day, "09:40", dates, volumes, D(cap * 100), D("0.01"), cap)


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


def test_liquidity_cap_uses_only_twenty_completed_same_slot_sessions():
    signal_day = date(2024, 2, 1)
    history = [(date(2024, 1, 1) + timedelta(days=index), "09:40", 100000 + 10000 * index)
               for index in range(20)]
    history.extend([(signal_day, "09:40", 999_999_999),
                    (date(2024, 1, 31), "10:10", 999_999_999)])
    reference = build_liquidity_reference(history, signal_date=signal_day, slot="09:40")
    assert len(reference.session_dates) == 20
    assert signal_day not in reference.session_dates
    assert reference.median_volume_shares == D("195000")
    assert reference.cap_shares == 1900
    with pytest.raises(ValueError, match="does not match"):
        replace(reference, cap_shares=2000)


def test_open_proxy_does_not_use_final_bar_volume_to_resize_fixed_order():
    created = datetime(2024, 1, 2, 9, 30)
    order = OrderIntent("o1", "600519.SH", Side.BUY, 100, D("101"), created,
                        created + timedelta(hours=1), liquidity_reference=_reference(created.date()))
    result = match_open_proxy(order, _bar(volume_shares=1))
    assert result.fill is not None
    assert result.fill.quantity == 100


def test_open_proxy_rejects_intent_larger_than_signal_time_cap_even_if_future_volume_is_large():
    created = datetime(2024, 1, 2, 9, 30)
    order = OrderIntent("o1", "600519.SH", Side.BUY, 200, D("101"), created,
                        created + timedelta(hours=1), liquidity_reference=_reference(created.date()))
    result = match_open_proxy(order, _bar(volume_shares=10_000_000))
    assert result.fill is None
    assert result.reason == "LIQUIDITY_REFERENCE_CAP"


def test_open_proxy_refuses_order_without_signal_time_liquidity_reference():
    created = datetime(2024, 1, 2, 9, 30)
    order = OrderIntent("o1", "600519.SH", Side.BUY, 100, D("101"), created,
                        created + timedelta(hours=1))
    result = match_open_proxy(order, _bar())
    assert result.fill is None
    assert result.reason == "MISSING_LIQUIDITY_REFERENCE"
