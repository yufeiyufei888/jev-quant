from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal

from jevquant import cli
from jevquant import p6_sample


def test_p6_cli_parses_timezone_aware_spend_baseline(monkeypatch, capsys):
    captured = {}

    def fake_run_p6_sample(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(p6_sample, "run_p6_sample", fake_run_p6_sample)
    monkeypatch.setattr(sys, "argv", [
        "jevquant", "p6-sample",
        "--minute-root", "minutes",
        "--daily-csv", "daily.csv",
        "--status-2022-2023", "status-a.json",
        "--status-2024-plus", "status-b.json",
        "--start", "2023-01-03",
        "--sessions", "1",
        "--output", "run",
        "--account-spend-baseline-usd", "2.4382",
        "--account-spend-baseline-at", "2026-09-24T05:52:21+00:00",
        "--acknowledge-historical-data-to-live-jev",
    ])

    cli.main()

    assert captured["account_spend_baseline_at"] == datetime(
        2026, 9, 24, 5, 52, 21, tzinfo=timezone.utc
    )
    assert captured["account_spend_baseline_usd"] == Decimal("2.4382")
    assert '"ok": true' in capsys.readouterr().out
