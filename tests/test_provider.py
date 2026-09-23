from types import SimpleNamespace

import pytest

from jevquant.provider import JevResponseError, decide, decide_cached, has_api_key_configured, resolve_api_key


def _state(actions):
    return {"schema_version": "state_v1", "policy_context": {"allowed_actions": actions}}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def system_one(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return self.response


def _response(choice="BUY", probabilities=None, model="jev-1.13.0", confidence=0.8):
    probabilities = probabilities or {"BUY": 0.8, "WAIT": 0.2}
    answer = SimpleNamespace(choice=choice, probabilities=probabilities, confidence=confidence)
    return SimpleNamespace(model=model, answers={"action": answer},
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=0))


def test_forced_single_action_is_rule_driven_without_api_call():
    client = FakeClient(_response())
    result = decide(_state(["HOLD"]), "keep position", client=client)
    assert result.source == "rule"
    assert result.action_requested == "HOLD"
    assert client.calls == 0


def test_choice_probabilities_use_fixed_comparison_and_record_usage():
    client = FakeClient(_response())
    result = decide(_state(["BUY", "WAIT"]), "enter or wait", client=client)
    assert result.action_requested == "BUY"
    assert result.provider_choice == "BUY"
    assert result.input_tokens == 1000
    assert result.estimated_cost_usd == pytest.approx(0.000042)
    assert client.kwargs["model"] == "jev-1.13.0"


def test_tie_defaults_to_wait():
    client = FakeClient(_response(choice="BUY", probabilities={"BUY": 0.5, "WAIT": 0.5}))
    result = decide(_state(["BUY", "WAIT"]), "enter or wait", client=client)
    assert result.action_requested == "WAIT"


@pytest.mark.parametrize("probabilities", [
    {"BUY": 0.8},
    {"BUY": 1.2, "WAIT": -0.2},
    {"BUY": 0.6, "WAIT": 0.3},
])
def test_bad_probability_response_fails_closed(probabilities):
    client = FakeClient(_response(probabilities=probabilities))
    with pytest.raises(JevResponseError):
        decide(_state(["BUY", "WAIT"]), "enter or wait", client=client)


def test_unexpected_resolved_model_fails_closed():
    client = FakeClient(_response(model="jev-latest"))
    with pytest.raises(JevResponseError, match="model mismatch"):
        decide(_state(["BUY", "WAIT"]), "enter or wait", client=client)


def test_request_hash_changes_with_state_and_instructions():
    state = _state(["BUY", "WAIT"])
    client = FakeClient(_response())
    first = decide(state, "enter or wait", client=client)
    changed_state = _state(["BUY", "WAIT"])
    changed_state["account"] = {"cash_weight": 0.5}
    second = decide(changed_state, "enter or wait", client=client)
    third = decide(state, "different question", client=client)
    assert len({first.request_hash, second.request_hash, third.request_hash}) == 3


def test_cached_validated_decision_does_not_repeat_provider_call(tmp_path):
    client = FakeClient(_response())
    cache = tmp_path / "cache.json"
    state = _state(["BUY", "WAIT"])
    first = decide_cached(state, "enter?", cache, client=client)
    second = decide_cached(state, "enter?", cache, client=client)
    assert client.calls == 1
    assert first == second
    assert second.source == "jev"


def test_cache_separates_request_options(tmp_path):
    client = FakeClient(_response())
    cache = tmp_path / "cache.json"
    decide_cached(_state(["BUY", "WAIT"]), "enter?", cache, client=client)
    changed = _state(["BUY", "WAIT"])
    changed["account"] = {"cash": "99"}
    decide_cached(changed, "enter?", cache, client=client)
    assert client.calls == 2


def test_key_can_be_read_from_explicit_external_env_file_without_copying(tmp_path, monkeypatch):
    env_file = tmp_path / "private.env"
    env_file.write_text("UNRELATED=value\nTYPESAFE_API_KEY=masked-test-value\n", encoding="utf-8")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("JEVQUANT_TYPESAFE_ENV_FILE", str(env_file))
    assert resolve_api_key() == "masked-test-value"
    assert has_api_key_configured()


def test_process_key_takes_precedence_over_external_file(tmp_path, monkeypatch):
    env_file = tmp_path / "private.env"
    env_file.write_text("TYPESAFE_API_KEY=file-value\n", encoding="utf-8")
    monkeypatch.setenv("JEVQUANT_TYPESAFE_ENV_FILE", str(env_file))
    monkeypatch.setenv("TYPESAFE_API_KEY", "process-value")
    assert resolve_api_key() == "process-value"


def test_project_local_env_can_reference_private_external_key_file(tmp_path, monkeypatch):
    from jevquant import provider

    package_dir = tmp_path / "src" / "jevquant"
    package_dir.mkdir(parents=True)
    (tmp_path / "private.env").write_text("TYPESAFE_API_KEY=local-reference-test\n", encoding="utf-8")
    (tmp_path / ".env").write_text("JEVQUANT_TYPESAFE_ENV_FILE=private.env\n", encoding="utf-8")
    monkeypatch.setattr(provider, "__file__", str(package_dir / "provider.py"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEVQUANT_TYPESAFE_ENV_FILE", raising=False)
    assert provider.resolve_api_key() == "local-reference-test"
