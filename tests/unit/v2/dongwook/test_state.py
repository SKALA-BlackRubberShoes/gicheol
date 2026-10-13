"""State isolation, bounds, and stable execution identity."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from main.agents.supervisor.contracts import ArtifactRef, RunInput, RunConfig
from main.graph.supervisor_state import (
    StateIntegrityError, copy_state, new_state, stable_task_id,
    state_digest, validate_state,
)


NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def make_state():
    ref = ArtifactRef(relative_path="snapshots/input.json", sha256="a" * 64,
                      media_type="application/json", schema_version="1.0")
    run_input = RunInput(
        company_name="Acme", official_url="https://acme.test",
        domain="Physical AI / Robotics", as_of_date="2026-10-13", corpus_manifest=[],
    )
    config = RunConfig(
        supervisor_model="test-supervisor", research_model="test-research",
        report_model="test-writer", judge_model="test-judge", prompt_hash="b" * 64,
    )
    return new_state(run_input, config, input_ref=ref, config_ref=ref,
                     company_ref=ref, now=NOW)


def test_new_runs_have_four_isolated_slots_and_utc_deadline():
    first, second = make_state(), make_state()
    assert len(first["perspectives"]) == 4
    assert first["run_id"] != second["run_id"]
    assert (first["deadline_at"] - first["started_at"]).total_seconds() == 900
    first["research_attempts"]["market"] = 2
    assert second["research_attempts"]["market"] == 0


def test_copy_does_not_overwrite_other_perspectives_or_mutate_input():
    original = make_state()
    previous = deepcopy(original)
    changed = copy_state(original)
    changed["research_attempts"]["market"] = 1
    assert changed["research_attempts"]["technology_maturity"] == 0
    assert original == previous


def test_unknown_state_field_and_large_snapshot_are_rejected():
    state = make_state()
    with pytest.raises(StateIntegrityError):
        validate_state({**state, "raw_document": "unexpected"})
    state["versions"]["padding"] = "x" * 32768
    with pytest.raises(StateIntegrityError) as raised:
        validate_state(state)
    assert raised.value.code == "STATE_TOO_LARGE"


def test_duplicate_accepted_tasks_and_negative_counters_are_rejected():
    state = make_state()
    state["accepted_task_ids"] = ["task-1", "task-1"]
    with pytest.raises(StateIntegrityError):
        validate_state(state)
    state = make_state()
    state["step_count"] = -1
    with pytest.raises(StateIntegrityError):
        validate_state(state)


def test_stable_task_id_binds_attempt_and_node_and_is_replayable():
    run_id = "b91d6f5d-8454-4091-838b-d4fdc0306f63"
    task = stable_task_id(run_id, "market", "market", 1)
    assert task == stable_task_id(run_id, "market", "market", 1)
    assert task != stable_task_id(run_id, "market", "market", 2)
    assert task != stable_task_id(run_id, "writer", None, 1)


def test_digest_tracks_control_changes_without_mutating_state():
    state = make_state()
    saved = deepcopy(state)
    before = state_digest(state)
    assert state == saved
    state["evidence_epoch"] += 1
    assert state_digest(state) != before


def test_unconfigured_models_and_future_input_are_rejected_before_run():
    ref = ArtifactRef(relative_path="input.json", sha256="a" * 64,
                      media_type="application/json", schema_version="1.0")
    data = RunInput(company_name="Acme",
                    official_url="https://acme.test", domain="Physical AI / Robotics",
                    as_of_date="2026-10-13", corpus_manifest=[])
    with pytest.raises(ValueError, match="model"):
        new_state(data, RunConfig(), input_ref=ref, config_ref=ref,
                  company_ref=ref, now=NOW)
    data = data.model_copy(update={"as_of_date": NOW.date().replace(year=2027)})
    config = RunConfig(supervisor_model="s", research_model="r",
                       report_model="w", judge_model="j", prompt_hash="b" * 64)
    with pytest.raises(ValueError, match="as_of_date"):
        new_state(data, config, input_ref=ref, config_ref=ref,
                  company_ref=ref, now=NOW)
