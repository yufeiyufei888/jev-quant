from decimal import Decimal as D

from jevquant.demo import golden_round_trip


def test_zero_slippage_accounting_golden_example():
    result = golden_round_trip()
    assert result["buy_fee_cny"] == "232.50"
    assert result["sell_fee_cny"] == "611.55"
    assert result["ending_cash_cny"] == "1004155.95"
    assert result["ending_shares"] == 0
    assert D(result["net_profit_cny"]) == D("4155.95")


def test_five_bps_slippage_accounting_golden_example():
    result = golden_round_trip(slippage_bps=5)
    assert result["buy_price_cny"] == "1500.75"
    assert result["sell_price_cny"] == "1509.24"
    assert result["buy_fee_cny"] == "232.61"
    assert result["sell_fee_cny"] == "611.25"
    assert result["ending_cash_cny"] == "1003401.14"
