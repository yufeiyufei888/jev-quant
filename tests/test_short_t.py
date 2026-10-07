from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from jevquant.ledger import Account, FeeSchedule
from jevquant.models import Bar, Fill, Side
from jevquant.provider import decide_binary
from jevquant.short_t import (Candidate, Cycle, ShortTAccount, ShortTConfig, _bar_fill,
                              expected_round_trip_profit_bps, signal_features)

D = Decimal
TZ = ZoneInfo("Asia/Shanghai")


def _bar(day: date, hour: int, minute: int, price: str, *, volume=100000,
         symbol="000001.SZ") -> Bar:
    end = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    p = D(price)
    return Bar(symbol, end, p, p + D("0.20"), p - D("0.20"), p, volume,
               D(volume) * p, day, end, "synthetic", "synthetic-row",
               frozenset(), end - timedelta(minutes=5), end)


def test_signal_uses_only_latest_closed_bars():
    day = date(2023, 1, 3)
    bars = [_bar(day, 9 + (35 + i * 5) // 60, (35 + i * 5) % 60,
                 str(D("10") + D(i) / 100)) for i in range(13)]
    first = signal_features(bars)
    assert first is not None and first[2] > 0
    assert signal_features(bars[:-1]) is None
    bars.append(_bar(day, 10, 40, "20"))
    assert signal_features(bars)[2] > first[2]


def test_delayed_partial_fill_and_t_plus_one_old_lot_sale():
    day = date(2023, 1, 4)
    account = Account(D("1000000"))
    fees = FeeSchedule.for_trade_date(date(2023, 1, 3))
    prior = Fill("old", "old", "000001.SZ", Side.BUY, 1000, D("10"),
                 fees.estimate(Side.BUY, D("10000")), date(2023, 1, 3),
                 datetime(2023, 1, 3, 10, 0, tzinfo=TZ))
    account.buy(prior, fees, day)
    at = datetime(2023, 1, 4, 10, 30, tzinfo=TZ)
    current = _bar(day, 10, 30, "10")
    candidate = Candidate("c1", "000001.SZ", Side.BUY, 1000, "CYCLE_OPEN",
                          at, current.close, D("-2"), D("30"))
    execution = _bar(day, 10, 40, "10", volume=30000)
    fill, reason = _bar_fill(candidate, execution, remaining_bar_volume=30000,
                             limit_up=D("11"), limit_down=D("9"), account=account,
                             config=ShortTConfig())
    assert reason == "PARTIAL_FILL" and fill.quantity == 300
    account.buy(fill, FeeSchedule.for_trade_date(day), date(2023, 1, 5))
    assert account.shares_total == 1300
    assert account.shares_sellable(day, "000001.SZ") == 1000


def test_star_market_rejects_buy_below_200_shares():
    day = date(2023, 1, 4)
    at = datetime(2023, 1, 4, 10, 30, tzinfo=TZ)
    candidate = Candidate("star", "688001.SH", Side.BUY, 100,
                          "CYCLE_OPEN", at, D("100"), D("-2"), D("30"))
    execution = _bar(day, 10, 40, "100", volume=100000, symbol="688001.SH")
    fill, reason = _bar_fill(candidate, execution, remaining_bar_volume=100000,
                             limit_up=D("120"), limit_down=D("80"),
                             account=Account(D("1000000")), config=ShortTConfig())
    assert fill is None and reason == "STAR_MINIMUM_ORDER_200"


def test_fee_aware_spread_is_negative_for_tiny_move():
    assert expected_round_trip_profit_bps(D("10"), D("10.01"), 100,
                                          Side.BUY, date(2023, 1, 4)) < 0


class _Answer:
    def __init__(self, execute_probability):
        self.probabilities = {"EXECUTE": execute_probability,
                              "SKIP": 1 - execute_probability}
        self.confidence = 0.8
        self.choice = "EXECUTE"


class _Response:
    model = "jev-1.13.0"
    usage = None

    def __init__(self, p):
        self.answers = {"action": _Answer(p)}


class _Client:
    def __init__(self, p):
        self.p = p
        self.calls = 0

    def system_one(self, **kwargs):
        self.calls += 1
        return _Response(self.p)


def test_binary_review_tie_skips_and_cache_prevents_second_call(tmp_path: Path):
    state = {"policy_context": {"allowed_actions": ["EXECUTE", "SKIP"]}, "candidate": "fixed"}
    client = _Client(0.5)
    path = tmp_path / "cache.json"
    assert decide_binary(state, "review", path, client=client).action_requested == "SKIP"
    assert decide_binary(state, "review", path, client=client).action_requested == "SKIP"
    assert client.calls == 1


def test_mock_review_never_exceeds_three_per_day(tmp_path: Path):
    account = ShortTAccount("mock", ("000001.SZ",), ShortTConfig(),
                            output=tmp_path, mode="mock")
    at = datetime(2023, 1, 4, 10, 30, tzinfo=TZ)
    candidate = Candidate("c", "000001.SZ", Side.BUY, 100,
                          "CYCLE_OPEN", at, D("10"), D("-2"), D("50"))
    for _ in range(3):
        assert account._confirm(candidate, daily_call_allowance=3)
    assert not account._confirm(candidate, daily_call_allowance=3)
    assert account.reviews == 3


def test_open_sell_first_cycle_does_not_double_replenish_base(tmp_path: Path):
    symbol = "000001.SZ"
    account = ShortTAccount("pure", (symbol,), ShortTConfig(),
                            output=tmp_path, mode="pure")
    prior = date(2023, 1, 3)
    day = date(2023, 1, 4)
    fees = FeeSchedule.for_trade_date(prior)
    bought = Fill("base", "base", symbol, Side.BUY, 1000, D("10"),
                  fees.estimate(Side.BUY, D("10000")), prior,
                  datetime(2023, 1, 3, 10, 40, tzinfo=TZ))
    account.account.buy(bought, fees, day)
    sold = Fill("open", "open", symbol, Side.SELL, 200, D("10"),
                fees.estimate(Side.SELL, D("2000")), day,
                datetime(2023, 1, 4, 10, 40, tzinfo=TZ))
    account.account.sell(sold, FeeSchedule.for_trade_date(day))
    account.marks[symbol] = D("10")
    account.base_targets[symbol] = 1000
    cycle = Cycle("open", symbol, Side.SELL, day, 200, 200, D("10"), sold.fee)
    account.cycles["open"] = cycle
    assert account._base_candidate(symbol, _bar(day, 10, 30, "10")) is None
    cycle.status = "CLOSED"
    assert account._base_candidate(symbol, _bar(day, 10, 30, "10")).quantity == 200


def test_share_bonus_adjusts_open_cycle_and_base_target(tmp_path: Path):
    symbol = "000001.SZ"
    account = ShortTAccount("pure", (symbol,), ShortTConfig(),
                            output=tmp_path, mode="pure")
    prior = date(2023, 1, 3)
    ex_day = date(2023, 1, 4)
    fees = FeeSchedule.for_trade_date(prior)
    bought = Fill("base", "base", symbol, Side.BUY, 1000, D("10"),
                  fees.estimate(Side.BUY, D("10000")), prior,
                  datetime(2023, 1, 3, 10, 40, tzinfo=TZ))
    account.account.buy(bought, fees, ex_day)
    account.base_targets[symbol] = 1000
    account.cycles["open"] = Cycle("open", symbol, Side.BUY, prior,
                                    200, 200, D("10"), D("5"))
    account.apply_preopen_actions(ex_day, [{
        "event_id": "bonus", "symbol": symbol, "ex_date": ex_day,
        "cash_per_share": D("0"), "share_ratio": D("0.20"),
    }])
    cycle = account.cycles["open"]
    assert account._holdings(symbol) == 1200
    assert account.base_targets[symbol] == 1200
    assert cycle.first_quantity == cycle.remaining_quantity == 240
    assert cycle.first_price * cycle.first_quantity == D("2000")


def test_independent_audit_detects_one_yuan_cash_error(tmp_path: Path):
    import json
    from jevquant.short_t_audit import audit_account

    run = tmp_path / "pure"
    run.mkdir()
    (tmp_path / "manifest.json").write_text(json.dumps({"universe": []}), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({
        "account": "pure", "initial_cash_cny": "1000000", "start": "2023-01-03",
        "selected_symbols": ["000001.SZ"]}), encoding="utf-8")
    (run / "fills.jsonl").write_text("", encoding="utf-8")
    (run / "nav.jsonl").write_text(json.dumps({
        "trade_date": "2023-01-03", "cash_cny": "1000001.00",
        "receivables_cny": "0.00", "shares": {"000001.SZ": 0},
        "marks_cny": {"000001.SZ": "10"}, "nav_cny": "1000001.00"}) + "\n", encoding="utf-8")
    result = audit_account(run, tmp_path / "manifest.json")
    assert not result["passed"]
    assert result["mismatches"][0]["expected_cash"] == "1000000"
