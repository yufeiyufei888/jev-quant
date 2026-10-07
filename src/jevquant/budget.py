"""Conservative local guard for live JEV input-token spend."""
from __future__ import annotations

import json
import math
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PRICE_USD_PER_MILLION_INPUT_TOKENS = Decimal("0.042")


class SpendBudgetExceeded(RuntimeError):
    pass


def estimate_request_cost_usd(state: Mapping[str, Any], instructions: str,
                              action_meanings: Mapping[str, str]) -> Decimal:
    """Return a conservative pre-call estimate, including a 1.5x safety factor.

    Tokenization is provider-specific. UTF-8 bytes / 2 intentionally errs high
    for the compact JSON payload used here; the multiplier covers prompt framing.
    """
    payload = json.dumps({"state": state, "instructions": instructions,
                          "action_meanings": action_meanings}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    estimated_tokens = math.ceil(len(payload) / 2)
    return (Decimal(estimated_tokens) * PRICE_USD_PER_MILLION_INPUT_TOKENS
            * Decimal("1.5") / Decimal(1_000_000))


def recorded_project_spend(artifacts_root: Path, *, after: datetime) -> Decimal:
    """Sum local costs recorded after the supplied portal baseline timestamp."""
    total = Decimal("0")
    if not artifacts_root.exists():
        return total
    after = after.astimezone(timezone.utc)
    for path in artifacts_root.rglob("jev-usage.jsonl"):
        total += _sum_cost_field(path, after=after)
    for path in artifacts_root.rglob("errors.jsonl"):
        total += _sum_cost_field(path, after=after)
    return total


def _sum_cost_field(path: Path, *, after: datetime) -> Decimal:
    total = Decimal("0")
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            value = row.get("estimated_cost_usd")
            recorded_at = row.get("recorded_at")
            if recorded_at:
                timestamp = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
                if timestamp.astimezone(timezone.utc) <= after:
                    continue
            if value is not None:
                total += Decimal(str(value))
    return total


def enforce_spend_budget(*, portal_baseline_usd: Decimal, account_limit_usd: Decimal,
                         local_recorded_usd: Decimal, next_call_reserve_usd: Decimal) -> None:
    projected = portal_baseline_usd + local_recorded_usd + next_call_reserve_usd
    if projected > account_limit_usd:
        raise SpendBudgetExceeded(
            "JEV call blocked by local spend guard: "
            f"portal baseline {portal_baseline_usd:.4f} + recorded project spend "
            f"{local_recorded_usd:.4f} + reserved call {next_call_reserve_usd:.6f} "
            f"would exceed ${account_limit_usd:.2f}"
        )
