from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .provider import DecisionResult


class UsageLedger:
    """Append local JEV usage receipts; reports spend without a hard cap."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, result: DecisionResult, attempted: bool = True) -> None:
        item = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "source": result.source,
            "request_hash": result.request_hash,
            "model_requested": result.model_requested,
            "model_resolved": result.model_resolved,
            "provider_request_id": result.request_id,
            "attempted": attempted,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "estimated_cost_usd": result.estimated_cost_usd,
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n")
