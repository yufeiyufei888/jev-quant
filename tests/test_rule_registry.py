import json
from datetime import date
from pathlib import Path


CONFIG = Path(__file__).parents[1] / "configs" / "sse_mainboard_rules_v1.json"


def _epoch_for(day: str):
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    target = date.fromisoformat(day)
    for epoch in payload["epochs"]:
        start = date.fromisoformat(epoch["valid_from"])
        end = date.fromisoformat(epoch["valid_through"]) if epoch["valid_through"] else None
        if target >= start and (end is None or target <= end):
            return epoch
    return None


def test_sse_mainboard_rule_registry_has_nonoverlapping_covered_epochs():
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    epochs = payload["epochs"]
    assert payload["instrument_scope"]["symbol"] == "600519.SH"
    assert len(epochs) == 2
    assert date.fromisoformat(epochs[0]["valid_through"]) < date.fromisoformat(epochs[1]["valid_from"])
    assert all(epoch["source_url"].startswith("https://www.sse.com.cn/") for epoch in epochs)


def test_verified_rule_cutovers_and_explicit_early_2023_gap():
    assert _epoch_for("2023-04-09") is None
    assert _epoch_for("2023-04-10")["ordinary_daily_price_limit_pct"] == "10"
    assert _epoch_for("2026-07-05")["risk_warning_daily_price_limit_pct"] == "5"
    assert _epoch_for("2026-07-06")["risk_warning_daily_price_limit_pct"] == "10"
    assert _epoch_for("2026-07-06")["minimum_price_tick_cny"] == "0.01"


def test_registry_does_not_claim_cross_market_or_status_availability():
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    limits = " ".join(payload["application_limits"])
    assert "historical security-status field" in limits
    assert "cross-board or cross-exchange" in limits
