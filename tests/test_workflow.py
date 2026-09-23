import json

from jevquant.workflow import run_mock_round_trip
from jevquant.reconciliation import reconcile_cash


def test_synthetic_decision_order_fill_t1_nav_flow(tmp_path):
    result = run_mock_round_trip(tmp_path)
    assert result["mode"] == "synthetic_mock_only"
    assert result["real_api_calls"] == 0
    assert [item["action"] for item in result["decisions"]] == ["BUY", "SELL"]
    assert [item["side"] for item in result["fills"]] == ["BUY", "SELL"]
    assert result["ending_shares"] == 0
    assert result["account_ending_cash_cny"] == "1003401.14"
    assert result["ending_nav_cny"] == "1003401.14"
    usage = (tmp_path / "jev-usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(usage) == 2
    assert all(json.loads(line)["source"] == "mock" for line in usage)
    assert all(json.loads(line)["estimated_cost_usd"] is None for line in usage)


def test_independent_cash_reconciliation_detects_one_yuan_error(tmp_path):
    from jevquant.models import Fill, Side
    from datetime import date, datetime
    from decimal import Decimal as D

    buy = Fill("b", "bo", "600519.SH", Side.BUY, 100, D("10.00"), D("5.00"),
               date(2024, 1, 2), datetime(2024, 1, 2, 10, 0))
    sell = Fill("s", "so", "600519.SH", Side.SELL, 100, D("11.00"), D("5.00"),
                date(2024, 1, 3), datetime(2024, 1, 3, 10, 0))
    good = reconcile_cash(D("1000.00"), [buy, sell], D("1090.00"))
    bad = reconcile_cash(D("1000.00"), [buy, sell], D("1091.00"))
    assert good.passed and good.difference == D("0.00")
    assert not bad.passed and bad.difference == D("1.00")
