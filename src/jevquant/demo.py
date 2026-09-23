from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from .ledger import Account, FeeSchedule
from .models import Fill, Side

D = Decimal


def golden_round_trip(*, slippage_bps: int = 0) -> dict[str, str | int]:
    """Synthetic accounting fixture from the project specification, not market data."""
    fees = FeeSchedule.for_trade_date(date(2024, 1, 2))
    account = Account(D("1000000.00"))
    buy_price = D("1500.00")
    sell_price = D("1510.00")
    if slippage_bps:
        buy_price = (buy_price * (D("1") + D(slippage_bps) / D(10000))).quantize(D("0.01"))
        sell_price = (sell_price * (D("1") - D(slippage_bps) / D(10000))).quantize(D("0.01"))
    buy_gross = D(500) * buy_price
    buy_fee = fees.estimate(Side.BUY, buy_gross)
    buy = Fill("golden-buy", "golden-buy-order", "600519.SH", Side.BUY, 500, buy_price,
               buy_fee, date(2024, 1, 2), datetime(2024, 1, 2, 10, 0))
    account.buy(buy, fees, date(2024, 1, 3))
    try:
        account.sell(Fill("same-day-sell", "same-day-sell", "600519.SH", Side.SELL, 500,
                          sell_price, D("0"), date(2024, 1, 2), datetime(2024, 1, 2, 11, 0)), fees)
    except ValueError as exc:
        same_day_rejection = str(exc)
    else:
        raise AssertionError("T+1 must reject same-day sale")
    sell_gross = D(500) * sell_price
    sell_fee = fees.estimate(Side.SELL, sell_gross)
    sell = Fill("golden-sell", "golden-sell-order", "600519.SH", Side.SELL, 500, sell_price,
                sell_fee, date(2024, 1, 3), datetime(2024, 1, 3, 10, 0))
    account.sell(sell, fees)
    return {
        "initial_cash_cny": "1000000.00",
        "buy_price_cny": str(buy_price),
        "sell_price_cny": str(sell_price),
        "buy_fee_cny": str(buy_fee),
        "same_day_sell_rejection": same_day_rejection,
        "sell_fee_cny": str(sell_fee),
        "ending_cash_cny": str(account.cash_available),
        "ending_shares": account.shares_total,
        "net_profit_cny": str(account.cash_available - account.initial_cash),
    }
