from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol

MODEL_ID = "jev-1.13.0"
SCHEMA_VERSION = "state_v1"
PRICE_USD_PER_MILLION_INPUT_TOKENS = 0.042


class SystemOneClient(Protocol):
    def system_one(self, *, state: Any, questions: Mapping[str, Any], model: str) -> Any: ...


class JevConfigurationError(RuntimeError):
    pass


class JevResponseError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecisionResult:
    action_requested: str
    option_probabilities: dict[str, float] | None
    distribution_confidence: float | None
    provider_choice: str | None
    model_requested: str | None
    model_resolved: str | None
    request_hash: str
    request_id: str | None
    source: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None


def create_client() -> Any:
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        raise JevConfigurationError("TYPESAFE_API_KEY is not configured")
    try:
        from typesafe_sdk import RetryPolicy, TypeSafeClient
    except ImportError as exc:
        raise JevConfigurationError("Install the pinned typesafe-sdk dependency") from exc
    return TypeSafeClient(model=MODEL_ID, timeout=15.0, retry=RetryPolicy(max_retries=0))


def request_hash(state: Any, actions: list[str], instructions: str, model: str = MODEL_ID) -> str:
    body = {
        "provider": "typesafe",
        "model": model,
        "schema_version": SCHEMA_VERSION,
        "state": state,
        "question": {"name": "action", "instructions": instructions, "criteria": actions},
        "request_options": {"timeout_seconds": 15, "sdk_retries": 0},
    }
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _result_from_response(response: Any, actions: list[str], digest: str,
                          model_id: str) -> DecisionResult:
    resolved = getattr(response, "model", None)
    if resolved != model_id:
        raise JevResponseError(f"resolved model mismatch: expected {model_id}")
    answers = getattr(response, "answers", None)
    answer = answers.get("action") if isinstance(answers, Mapping) else None
    if answer is None:
        raise JevResponseError("missing named action answer")
    probabilities = dict(getattr(answer, "probabilities", {}) or {})
    if set(probabilities) != set(actions):
        raise JevResponseError("action probability keys do not match allowed actions")
    values = list(probabilities.values())
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1 for value in values):
        raise JevResponseError("invalid action probability")
    if abs(sum(values) - 1.0) > 1e-4:
        raise JevResponseError("action probabilities do not sum to one")
    confidence = getattr(answer, "confidence", None)
    if not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise JevResponseError("invalid confidence")
    provider_choice = getattr(answer, "choice", None)
    if provider_choice not in actions:
        raise JevResponseError("provider choice is outside allowed actions")
    trade = "BUY" if "BUY" in actions else "SELL"
    no_trade = "WAIT" if "WAIT" in actions else "HOLD"
    action = trade if probabilities[trade] > probabilities[no_trade] else no_trade
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    cost = (input_tokens * PRICE_USD_PER_MILLION_INPUT_TOKENS / 1_000_000
            if isinstance(input_tokens, int) and input_tokens >= 0 else None)
    return DecisionResult(action, probabilities, float(confidence), provider_choice, model_id, resolved,
                          digest, None, "jev", input_tokens, output_tokens, cost)


def decide(state: dict[str, Any], instructions: str, *, client: SystemOneClient | None = None,
           model_id: str = MODEL_ID) -> DecisionResult:
    policy = state.get("policy_context") or {}
    actions = policy.get("allowed_actions")
    if actions not in (["WAIT"], ["HOLD"], ["BUY", "WAIT"], ["HOLD", "SELL"]):
        raise JevResponseError("illegal action set or action ordering")
    digest = request_hash(state, actions, instructions, model_id)
    if actions in (["WAIT"], ["HOLD"]):
        return DecisionResult(actions[0], None, None, None, model_id, None, digest, None, "rule")
    if client is None:
        client = create_client()
    try:
        from typesafe_sdk import Choice
    except ImportError as exc:
        raise JevConfigurationError("Install the pinned typesafe-sdk dependency") from exc
    meanings = {
        "BUY": "Request opening the preconfigured long position later.",
        "WAIT": "Keep the account in cash until a later review.",
        "HOLD": "Keep the existing share quantity unchanged.",
        "SELL": "Request a later exit of legally sellable shares.",
    }
    response = client.system_one(
        model=model_id,
        state=state,
        questions={"action": Choice(instructions=instructions, criteria={action: meanings[action] for action in actions})},
    )
    return _result_from_response(response, actions, digest, model_id)


def decide_cached(state: dict[str, Any], instructions: str, cache_path: Path, *,
                  client: SystemOneClient | None = None, model_id: str = MODEL_ID,
                  source_override: str | None = None) -> DecisionResult:
    """Reuse an identical validated response so restarts do not repeat a paid call."""
    actions = (state.get("policy_context") or {}).get("allowed_actions")
    if actions not in (["WAIT"], ["HOLD"], ["BUY", "WAIT"], ["HOLD", "SELL"]):
        raise JevResponseError("illegal action set or action ordering")
    digest = request_hash(state, actions, instructions, model_id)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        with cache_path.open("r", encoding="utf-8") as stream:
            cache = json.load(stream)
    else:
        cache = {}
    saved = cache.get(digest)
    if saved is not None:
        return DecisionResult(**saved)
    result = decide(state, instructions, client=client, model_id=model_id)
    if source_override is not None:
        if source_override not in {"mock", "jev"}:
            raise ValueError("source_override must be 'mock' or 'jev'")
        result = replace(result, source=source_override,
                         estimated_cost_usd=(result.estimated_cost_usd if source_override == "jev" else None))
    cache[digest] = {
        "action_requested": result.action_requested,
        "option_probabilities": result.option_probabilities,
        "distribution_confidence": result.distribution_confidence,
        "provider_choice": result.provider_choice,
        "model_requested": result.model_requested,
        "model_resolved": result.model_resolved,
        "request_hash": result.request_hash,
        "request_id": result.request_id,
        "source": result.source,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "estimated_cost_usd": result.estimated_cost_usd,
    }
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(cache, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temporary.replace(cache_path)
    return result
