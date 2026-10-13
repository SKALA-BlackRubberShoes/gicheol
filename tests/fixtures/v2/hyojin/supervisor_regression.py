"""Run issue #21 against original shared modules in an isolated subprocess.

HYOJIN_SUPERVISOR_ROOT optionally selects a four-file reference snapshot.
Without that override the integrated repository's actual Supervisor is used.
Source retrieval, Judge and storage are explicit fixtures; Supervisor is real.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    parent, _, leaf = name.rpartition(".")
    setattr(sys.modules[parent], leaf, module)
    return module


def bootstrap():
    repo = Path(__file__).resolve().parents[4]
    shared = Path(os.environ.get("HYOJIN_SUPERVISOR_ROOT", repo)).resolve()
    # Avoid importing the unrelated legacy graph's eager package __init__.
    for name, path in (
            ("main", repo / "main"), ("main.agents", repo / "main/agents"),
            ("main.agents.supervisor", shared / "main/agents/supervisor"),
            ("main.graph", shared / "main/graph")):
        package = ModuleType(name)
        package.__path__ = [str(path)]
        sys.modules[name] = package
        parent, _, leaf = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], leaf, package)
    contracts = load_module("main.agents.supervisor.contracts", shared / "main/agents/supervisor/contracts.py")
    load_module("main.agents.supervisor.policy", shared / "main/agents/supervisor/policy.py")
    state = load_module("main.graph.supervisor_state", shared / "main/graph/supervisor_state.py")
    agent = load_module("main.agents.supervisor.agent", shared / "main/agents/supervisor/agent.py")
    # Use this single original DTO module for fixtures and the real Supervisor.
    os.environ.pop("HYOJIN_CONTRACTS_PATH", None)
    spec = importlib.util.spec_from_file_location("hyojin_routing_cases", Path(__file__).with_name("cases.py"))
    cases = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cases)
    cases.COMMON = contracts
    return contracts, state, agent, cases


async def run_scenario(mode):
    c, state_module, agent_module, f = bootstrap()
    from main.agents.quality.rubrics import RUBRIC_VERSION
    case = f.fixture()

    class MemoryStore:
        def __init__(self):
            self.files = {source["content_ref"]["relative_path"]: case["bodies"][source["source_id"]].encode()
                          for source in case["sources"]}

        async def save(self, payload, *, key, media_type="application/json"):
            data = c.canonical_json(payload).encode()
            self.files[key] = data
            from hashlib import sha256
            return c.ArtifactRef(relative_path=key, sha256=sha256(data).hexdigest(), media_type=media_type)

        async def verify(self, ref):
            from hashlib import sha256
            assert sha256(self.files[ref.relative_path]).hexdigest() == ref.sha256

        async def load(self, ref):
            await self.verify(ref)
            return json.loads(self.files[ref.relative_path])

    class Resolver:
        async def resolve(self, evidence_ids):
            items = [item for item in case["evidence"] if item["evidence_id"] in evidence_ids]
            source_ids = {item["source_id"] for item in items}
            return c.EvidenceBundle(sources=[source for source in case["sources"] if source["source_id"] in source_ids],
                                    evidence=items)

    store = MemoryStore()
    judge = f.FixtureJudge()
    _, assessor, _, _ = f.services(case, verifier_judge=judge)
    assessor.save_claim_support = store.save
    config = c.RunConfig(supervisor_model="fixture-supervisor", research_model="fixture-research",
                         report_model="fixture-writer", judge_model="fixture-judge",
                         rubric_version=RUBRIC_VERSION, prompt_hash=c.sha256_text("fixture-prompts"),
                         corpus_version="corpus-1", index_version="index-1")
    run_input = c.RunInput(company_name="Alpha", official_url="https://alpha.example.org",
                          domain="Physical AI / Robotics", as_of_date="2026-10-13", corpus_manifest=[])
    now = datetime.now(timezone.utc)
    state = state_module.new_state(run_input, config,
        input_ref=await store.save(run_input.model_dump(mode="json"), key="input.json"),
        config_ref=await store.save(config.model_dump(mode="json"), key="config.json"),
        company_ref=await store.save({"company_id": "alpha"}, key="company.json"), now=now)
    target = c.EligibilityResult.model_validate(case["eligibility"])
    state["eligibility_ref"] = await store.save(target.model_dump(mode="json"), key="eligibility.json")
    state["eligibility_status"] = "eligible"
    state["eligibility_attempts"] = 1
    for perspective, value in case["perspective_results"].items():
        result = c.PerspectiveResult.model_validate(value)
        state["perspectives"][perspective] = c.PerspectiveSlot(
            status="insufficient", latest_result_ref=await store.save(result.model_dump(mode="json"), key=f"views/{perspective}.json"),
            result_hash=result.result_hash, attempt=1)
        state["research_attempts"][perspective] = 1
    state = state_module.validate_state(state)
    attempts = deepcopy(state["research_attempts"])
    research_refs = {p: slot.latest_result_ref for p, slot in state["perspectives"].items()}
    supervisor = agent_module.Supervisor(store=store, evidence_resolver=Resolver(), assess_evidence=assessor,
                                         now=lambda: now, limits=config)
    judge.support_status["C-eligibility"] = "unsupported"
    state = await supervisor.step(state)
    assert state["next_action"] == "eligibility", {
        "next_action": state["next_action"].value, "eligibility_status": state["eligibility_status"],
        "research_attempts": state["research_attempts"], "last_error": state["last_error"]}
    assert state["eligibility_status"] == "unknown"
    assert state["eligibility_attempts"] == 2
    assert state["research_attempts"] == attempts
    assert not state["evidence_gate_pass"] and state["gate_ref"] is None
    assert state["draft_report_ref"] is None
    request = await store.load(state["active_task"].payload_ref)
    assert request["gap_ids"]
    assert any("C-eligibility" in instruction and "Synthetic fixture entailment verdict" in instruction
               for instruction in request["instructions"]), request
    actions = [state["next_action"].value]

    async def supply_eligibility(current):
        task = current["active_task"]
        corrected = c.EligibilityResult.model_validate(target.model_dump(mode="json") | {
            "task_id": task.task_id, "result_hash": None})
        ref = await store.save(corrected.model_dump(mode="json"), key=f"eligibility/{task.task_id}.json")
        current["pending_result"] = c.ResultEnvelope(task_id=task.task_id, outcome="success", result_ref=ref)
        return await supervisor.step(current)

    if mode in {"repeat", "exhausted"}:
        state = await supply_eligibility(state)
        actions.append(state["next_action"].value)
        assert state["next_action"] == "eligibility" and state["eligibility_status"] == "unknown"
        assert state["eligibility_attempts"] == 3 and state["research_attempts"] == attempts
        assert not state["evidence_gate_pass"] and state["draft_report_ref"] is None
    if mode == "exhausted":
        state = await supply_eligibility(state)
        actions.append(state["next_action"].value)
        assert state["status"] == "incomplete" and state["terminal_reason"] == "ELIGIBILITY_UNKNOWN", state
        assert state["research_attempts"] == attempts
        assert not state["evidence_gate_pass"] and state["draft_report_ref"] is None
    else:
        judge.support_status.pop("C-eligibility")
        state = await supply_eligibility(state)
        actions.append(state["next_action"].value)
        assert state["status"] == "running" and state["next_action"] == "writer", state
        assert state["eligibility_status"] == "eligible" and state["evidence_gate_pass"]
        assert state["research_attempts"] == attempts
        assert all(slot.status == "sufficient" for slot in state["perspectives"].values())
        assert await agent_module.validate_current_gate(state, store, config, now=lambda: now)
        request = c.WriterRequest.model_validate(await store.load(state["active_task"].payload_ref))
        assert await agent_module.validate_writer_inputs(state, store, request)
    assert {p: slot.latest_result_ref for p, slot in state["perspectives"].items()} == research_refs
    return {"fixture_only": True, "scenario": mode, "actions": actions,
            "status": state["status"], "terminal_reason": state["terminal_reason"],
            "research_attempts": {p.value: count for p, count in state["research_attempts"].items()},
            "eligibility_attempts": state["eligibility_attempts"], "gate_pass": state["evidence_gate_pass"]}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_scenario(sys.argv[1])), ensure_ascii=False))
