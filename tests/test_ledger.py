from datetime import date, datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from jevquant.execution import make_protection_price
from jevquant.ledger import Account, FeeSchedule, plan_entry_quantity
from jevquant.models import Fill, PositionLot, Side
from jevquant.actions import CashDividend


def _fill(fill_id: str, side: Side, qty: int, price: str, fee: str, day: date) -> Fill:
    return Fill(fill_id, fill_id, "600519.SH", side, qty, D(price), D(fee), day,
                datetime.combine(day, datetime.min.time()))


def test_buy_is_t_plus_one_and_duplicate_event_is_idempotent():
    fees = FeeSchedule()
    account = Account(D("1000000"))
    fill = _fill("b1", Side.BUY, 200, "1500.00", "93.00", date(2024, 1, 2))
    account.buy(fill, fees, date(2024, 1, 3))
    account.buy(fill, fees, date(2024, 1, 3))
    assert account.cash_available == D("699907.00")
    assert account.shares_total == 200
    assert account.shares_sellable(date(2024, 1, 2)) == 0
    assert account.shares_sellable(date(2024, 1, 3)) == 200


def test_old_lots_remain_sellable_when_new_lot_is_locked():
    fees = FeeSchedule()
    account = Account(D("1000000"))
    account.buy(_fill("b1", Side.BUY, 200, "1500", "93", date(2024, 1, 2)), fees,
                date(2024, 1, 3))
    account.buy(_fill("b2", Side.BUY, 100, "1500", "46.50", date(2024, 1, 3)), fees,
                date(2024, 1, 4))
    assert account.shares_sellable(date(2024, 1, 3)) == 200
    with pytest.raises(ValueError, match=r"T\+1"):
        account.sell(_fill("s1", Side.SELL, 300, "1500", "0", date(2024, 1, 3)), fees)
    account.sell(_fill("s2", Side.SELL, 200, "1500", "243", date(2024, 1, 3)), fees)
    assert account.shares_total == 100


def test_cash_dividend_receivable_and_payment_are_separate_and_idempotent():
    account = Account(D("1000000"))
    account.apply_cash_dividend("div-ex", D("10000"), ex_date=True)
    account.apply_cash_dividend("div-ex", D("10000"), ex_date=True)
    assert account.cash_available == D("1000000.00")
    assert account.receivables == D("10000.00")
    account.apply_cash_dividend("div-pay", D("10000"), ex_date=False)
    assert account.cash_available == D("1010000.00")
    assert account.receivables == D("0.00")


def test_sourced_dividend_uses_record_date_holdings_and_books_payment_once():
    account = Account(D("1000000"))
    account.lots.append(PositionLot("lot1", "600519.SH", date(2024, 1, 2),
                                    date(2024, 1, 3), 100, D("100"), D("0")))
    event = CashDividend("div-1", "600519.SH", D("2"), date(2024, 1, 2),
                         datetime(2024, 1, 2, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai")), date(2024, 1, 4),
                         date(2024, 1, 5), date(2024, 1, 8), "OFFICIAL_IMPLEMENTATION_NOTICE",
                         "https://example.test/dividend.pdf")
    account.apply_cash_dividend_event(event, date(2024, 1, 4))
    account.apply_cash_dividend_event(event, date(2024, 1, 4))
    account.lots[0].quantity = 60  # disposal after the record-date snapshot
    account.apply_cash_dividend_event(event, date(2024, 1, 5))
    account.apply_cash_dividend_event(event, date(2024, 1, 5))
    assert account.dividend_receivables[event.event_id] == D("200.00")
    assert account.receivables == D("200.00")
    assert account.cash_available == D("1000000.00")
    account.apply_cash_dividend_event(event, date(2024, 1, 8))
    account.apply_cash_dividend_event(event, date(2024, 1, 8))
    assert account.receivables == D("0.00")
    assert account.cash_available == D("1000200.00")


def test_buy_reservation_is_an_asset_and_partial_fills_share_order_minimum_fee():
    fees = FeeSchedule()
    account = Account(D("1000000"))
    account.reserve_buy("o1", D("301000"))
    assert account.cash_total == D("1000000.00")
    first = Fill("f1", "o1", "600519.SH", Side.BUY, 100, D("1500"), D("46.50"),
                 date(2024, 1, 2), datetime(2024, 1, 2, 9, 35))
    account.buy(first, fees, date(2024, 1, 3))
    second = Fill("f2", "o1", "600519.SH", Side.BUY, 100, D("1500"), D("46.50"),
                  date(2024, 1, 2), datetime(2024, 1, 2, 9, 40))
    account.buy(second, fees, date(2024, 1, 3))
    assert account.order_gross["o1"] == D("300000.00")
    assert account.cash_reserved == D("907.00")
    assert account.cancel_buy("o1") == D("907.00")
    assert account.cash_total == D("699907.00")


def test_entry_quantity_rounds_down_to_whole_lots_and_respects_cash_reserve():
    quantity = plan_entry_quantity(D("1000000"), D("0.8"), D("1507.50"), D("1000000"))
    assert quantity == 500
    assert plan_entry_quantity(D("1000000"), D("0.8"), D("1507.50"), D("151000")) == 0


def test_price_protection_rounds_to_cent_away_from_signal():
    assert make_protection_price(Side.BUY, D("1500.001"), 50) == D("1507.51")
    assert make_protection_price(Side.SELL, D("1500.009"), 50) == D("1492.50")


def test_date_based_fee_rules_have_explicit_change_dates():
    assert FeeSchedule.for_trade_date(date(2022, 4, 28)).transfer_fee_rate == D("0.00002")
    assert FeeSchedule.for_trade_date(date(2022, 4, 29)).transfer_fee_rate == D("0.00001")
    assert FeeSchedule.for_trade_date(date(2023, 8, 27)).stamp_duty_sell_rate == D("0.001")
    assert FeeSchedule.for_trade_date(date(2023, 8, 28)).stamp_duty_sell_rate == D("0.0005")
    with pytest.raises(ValueError, match="before 2020"):
        FeeSchedule.for_trade_date(date(2019, 12, 31))
