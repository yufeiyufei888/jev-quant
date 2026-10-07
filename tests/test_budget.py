import json
from datetime import datetime, timezone
from decimal import Decimal as D

import pytest

from jevquant.budget import (SpendBudgetExceeded, enforce_spend_budget,
                             estimate_request_cost_usd, recorded_project_spend)
from jevquant.p6_sample import _compact_state


def test_compact_input_drops_repeated_price_history_but_keeps_decision_features():
    state = {
        "schema_version": "state_v1",
        "instrument": {"asset_id": "ASSET_001"},
        "snapshot": {"decision_clock": "10:05", "session_index": 4,
                     "bar_slot_index": 7, "snapshot_mode": "retrospective_instant_snapshot_fill_diagnostic"},
        "market": {
            "current_close_index_100": "100.2",
            "current_bar": {"slot": "10:00", "open_index_100": "100.0",
                            "high_index_100": "100.3", "low_index_100": "99.9",
                            "close_index_100": "100.2", "relative_volume_same_slot": "1.1"},
            "completed_5m_bars": [{"close_index_100": str(i)} for i in range(24)],
            "features": {"return_1_bars": "0", "return_3_bars": "0.01", "return_6_bars": "0.01",
                         "return_12_bars": "0.02", "return_5_sessions_raw_unadjusted": "0.1",
                         "return_20_sessions_raw_unadjusted": "0.2", "return_60_sessions_raw_unadjusted": "0.3",
                         "price_vs_ma20_raw_unadjusted": "0.02", "price_vs_ma60_raw_unadjusted": "0.04",
                         "daily_volatility_20_raw_unadjusted": "0.02", "intraday_range_position": "0.5",
                         "gap_from_previous_session_raw_unadjusted": "0.01"},
            "prior_daily_close_indices_100": [str(i) for i in range(65)],
            "price_band_room_fraction": {"to_upper": "0.1", "to_lower": "-0.1"},
        },
        "account": {"position_state": "FLAT", "nav_ratio_to_initial": "1", "cash_weight": "1",
                    "position_weight": "0", "sellable_fraction": "0", "holding_sessions": 0,
                    "unrealized_return_before_costs": None, "receivables_ratio_to_initial": "0"},
        "policy_context": {"allowed_actions": ["BUY", "WAIT"], "entry_target_weight": "0.8",
                            "long_only": True, "leverage_allowed": False},
        "data_quality": {"five_minute_interval_end_is_assumed_not_provider_verified": True,
                         "availability_at_interval_end_is_assumed": True,
                         "historical_status_is_retrospective_not_point_in_time": True},
    }
    compact = _compact_state(state)
    encoded = json.dumps(compact, ensure_ascii=False)
    assert compact["schema_version"] == "compact_v1"
    assert compact["market"]["returns_5m"]["6"] == "0.01"
    assert compact["market"]["bar"]["ohlc_index_100"] == ["100.0", "100.3", "99.9", "100.2"]
    assert compact["policy_context"]["allowed_actions"] == ["BUY", "WAIT"]
    assert "completed_5m_bars" not in compact["market"]
    assert "prior_daily_close_indices_100" not in compact["market"]
    assert len(encoded) < len(json.dumps(state, ensure_ascii=False)) / 2


def test_request_cost_estimate_is_positive_and_budget_blocks_before_limit():
    estimate = estimate_request_cost_usd({"a": "b"}, "choose", {"BUY": "buy"})
    assert estimate > 0
    with pytest.raises(SpendBudgetExceeded, match="would exceed"):
        enforce_spend_budget(portal_baseline_usd=D("2.1493"),
            account_limit_usd=D("5.00"), local_recorded_usd=D("2.80"),
            next_call_reserve_usd=D("0.10"))


def test_project_spend_scanner_sums_successes_and_failed_attempt_estimates(tmp_path):
    run = tmp_path / "p7" / "trial"
    run.mkdir(parents=True)
    (run / "jev-usage.jsonl").write_text(
        '{"recorded_at":"2026-01-02T00:00:00+00:00","estimated_cost_usd":"0.20"}\n', encoding="utf-8")
    (run / "errors.jsonl").write_text(
        '{"recorded_at":"2026-01-03T00:00:00+00:00","estimated_cost_usd":"0.01"}\n', encoding="utf-8")
    assert recorded_project_spend(tmp_path, after=datetime(2026, 1, 1, tzinfo=timezone.utc)) == D("0.21")
    assert recorded_project_spend(tmp_path, after=datetime(2026, 1, 2, 12, tzinfo=timezone.utc)) == D("0.01")
