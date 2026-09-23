from datetime import date, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
from random import Random

import pytest

from jevquant.baselines import decide_baseline


def _history(values):
    start = date(2024, 1, 1)
    return [(start + timedelta(days=index), D(value)) for index, value in enumerate(values)]


def test_cash_and_buy_hold_baselines_have_fixed_intents():
    asof = date(2024, 2, 1)
    assert decide_baseline("CASH", asof=asof, daily_closes=[], has_position=False,
                           eligible_to_buy=True).action == "WAIT"
    cash_exit = decide_baseline("CASH", asof=asof, daily_closes=[], has_position=True,
                                eligible_to_buy=False)
    assert cash_exit.action == "SELL"
    bh80 = decide_baseline("BH80", asof=asof, daily_closes=[], has_position=False,
                           eligible_to_buy=True)
    assert (bh80.action, bh80.target_weight) == ("BUY", D("0.8"))
    bh50 = decide_baseline("BH50", asof=asof, daily_closes=[], has_position=True,
                           eligible_to_buy=True)
    assert (bh50.action, bh50.target_weight) == ("HOLD", D("0.5"))
    maximum = decide_baseline("BH_MAX", asof=asof, daily_closes=[], has_position=False,
                              eligible_to_buy=True)
    assert maximum.action == "BUY" and maximum.target_weight is None
    assert maximum.sizing_policy == "max_affordable_after_reserved_fees_and_cash_buffer"


def test_ma5_20_uses_only_completed_prior_closes_and_waits_for_warmup():
    asof = date(2024, 2, 1)
    rising = _history(range(1, 21))
    rising_with_future = rising + [(asof, D("1")), (date(2024, 2, 2), D("0.01"))]
    decision = decide_baseline("MA5_20", asof=asof, daily_closes=rising,
                               has_position=False, eligible_to_buy=True)
    perturbed = decide_baseline("MA5_20", asof=asof, daily_closes=rising_with_future,
                                has_position=False, eligible_to_buy=True)
    assert decision == perturbed
    assert decision.action == "BUY"
    assert decide_baseline("MA5_20", asof=asof, daily_closes=rising,
                           has_position=True, eligible_to_buy=True).action == "HOLD"
    falling = _history(range(20, 0, -1))
    assert decide_baseline("MA5_20", asof=asof, daily_closes=falling,
                           has_position=True, eligible_to_buy=True).action == "SELL"
    warmup = decide_baseline("MA5_20", asof=asof, daily_closes=rising[:19],
                             has_position=False, eligible_to_buy=True)
    assert warmup.action == "WAIT" and "20 completed" in warmup.reason


def test_random_baseline_requires_seeded_rng_and_repeats_exactly():
    asof = date(2024, 2, 1)

    def path(seed):
        rng = Random(seed)
        return [decide_baseline("RANDOM", asof=asof, daily_closes=[], has_position=False,
                                eligible_to_buy=True, rng=rng,
                                random_entry_probability=D("0.5")).action
                for _ in range(20)]

    with pytest.raises(ValueError, match="seeded RNG"):
        decide_baseline("RANDOM", asof=asof, daily_closes=[], has_position=False,
                        eligible_to_buy=True)
    assert path(42) == path(42)
    assert path(42) != path(7)


def test_baseline_rejects_duplicate_history_and_invalid_probability():
    asof = date(2024, 2, 1)
    duplicate = [(date(2024, 1, 1), D("10")), (date(2024, 1, 1), D("11"))]
    with pytest.raises(ValueError, match="duplicate daily dates"):
        decide_baseline("MA5_20", asof=asof, daily_closes=duplicate,
                        has_position=False, eligible_to_buy=True)
    with pytest.raises(ValueError, match="probability"):
        decide_baseline("RANDOM", asof=asof, daily_closes=[], has_position=False,
                        eligible_to_buy=True, rng=Random(0), random_entry_probability=D("1.1"))


def test_baseline_configuration_is_predeclared_and_matches_policy_defaults():
    config_path = Path(__file__).parents[1] / "configs" / "baseline_policies_v1.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["initial_cash_cny"] == "1000000.00"
    assert config["random"]["seeds"] == list(range(100))
    assert config["random"]["entry_probability"] == "0.05"
    assert config["random"]["exit_probability"] == "0.05"
    assert config["moving_average"]["signal_data"] == "previous_completed_daily_closes_only"
    assert config["status"] == "policy_specification_only_no_account_replay_yet"
