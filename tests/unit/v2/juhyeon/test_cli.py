"""CLI orchestration uses explicit service fixtures, real files and SQLite."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
import pytest
import yaml

from main import neutral_eval as cli
from main.agents.supervisor import contracts as c
from main.graph.supervisor_state import StateIntegrityError, SupervisorState
from main.graph.supervisor_workflow import initialize_run
from main.runtime.neutral.config import ConfigError, load_runtime_config
from main.runtime.neutral.services import ServiceBundle, ServiceConfigurationError, evaluate_retrieval
from main.storage.artifacts import FileArtifactStore
from main.storage.checkpoints import open_checkpointer


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"run_config": {
        "supervisor_model": "fixture-supervisor", "research_model": "fixture-research",
        "report_model": "fixture-report", "judge_model": "fixture-judge", "prompt_hash": "a" * 64,
    }}), encoding="utf-8")
    return load_runtime_config(path, repo_root=tmp_path, environ={})


def input_json(path):
    value = {"company_name": "Fixture", "official_url": "https://example.org", "domain": "Physical AI / Robotics",
             "as_of_date": "2026-01-01", "corpus_manifest": []}
    path.write_text(json.dumps(value), encoding="utf-8")
    return c.RunInput.model_validate(value)


@pytest.mark.parametrize("command,extra", [
    ("validate", []), ("ingest", []), ("run", ["--input", "input.json"]),
    ("resume", ["--run-id", str(uuid4())]), ("evaluate-retrieval", ["--dataset", "gold.json"]),
])
def test_all_five_commands_dispatch(runtime, tmp_path, monkeypatch, command, extra):
    calls = []
    def load(path, overrides=None):
        calls.append(("config", overrides))
        return runtime
    def preflight(cfg, **kwargs):
        calls.append(("preflight", kwargs))
        return SimpleNamespace(config=cfg, checks={"qdrant": "fixture"}, corpus_manifest=[])
    async def run(args, cfg):
        calls.append(("run", args.input))
        return 3
    async def resume(args):
        calls.append(("resume", args.run_id))
        return 3
    async def ingest(**kwargs):
        calls.append(("ingest", kwargs["corpus_manifest"]))
        return {"mock": True, "status": "ingested_fixture"}
    async def load_services(*args):
        return SimpleNamespace(ingest=ingest)
    async def retrieval(bundle, dataset):
        calls.append(("evaluate-retrieval", dataset))
        return {"mock": True, "query_count": 20}
    monkeypatch.setattr(cli, "load_runtime_config", load)
    monkeypatch.setattr(cli, "preflight", preflight)
    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(cli, "_resume", resume)
    monkeypatch.setattr(cli, "load_services", load_services)
    monkeypatch.setattr(cli, "evaluate_retrieval", retrieval)
    monkeypatch.setattr(cli, "_read_json", lambda path: {"fixture": True})
    args = cli.parser().parse_args([command, *extra])
    assert asyncio.run(cli.dispatch(args)) == (3 if command in {"run", "resume"} else 0)
    if command == "resume":
        assert [call[0] for call in calls] == ["resume"]
    elif command not in {"run"}:
        assert ("preflight", {"require_index": command != "ingest"}) in calls


@pytest.mark.parametrize("status,exit_code", [("completed", 0), ("incomplete", 3), ("failed", 1)])
def test_run_exit_codes_and_registry_identity_with_graph_fixture(runtime, tmp_path, monkeypatch, capsys, status, exit_code):
    path = tmp_path / "input.json"
    input_json(path)
    captured = {}
    monkeypatch.setattr(cli, "load_runtime_config", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(cli, "preflight", lambda cfg, **kwargs: SimpleNamespace(config=cfg, corpus_manifest=[]))
    async def load(*args):
        return SimpleNamespace(mock=True)
    async def execute(**kwargs):
        captured.update(kwargs)
        result = dict(kwargs["state"])
        result.update(status=status, terminal_reason=None, final_manifest_ref=None)
        return result
    monkeypatch.setattr(cli, "load_services", load)
    monkeypatch.setattr(cli, "_execute_graph", execute)
    assert cli.main(["run", "--input", str(path)]) == exit_code
    result = json.loads(capsys.readouterr().out)
    assert result["mock"] is True and result["status"] == status
    assert captured["state"]["run_id"] == result["run_id"]
    assert captured["store"].root.name == result["run_id"]
    registry = json.loads((captured["store"].root / "runtime.json").read_text())
    assert registry["run_id"] == result["run_id"]
    assert registry["thread_id"] == captured["state"]["thread_id"]


@pytest.mark.parametrize("kind", ["missing", "malformed", "dto"])
def test_invalid_run_input_exits_two_before_preflight(runtime, tmp_path, monkeypatch, capsys, kind):
    path = tmp_path / "input.json"
    if kind == "malformed":
        path.write_text("{broken", encoding="utf-8")
    elif kind == "dto":
        path.write_text('{"company_name": "secret-input"}', encoding="utf-8")
    monkeypatch.setattr(cli, "load_runtime_config", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(cli, "preflight", lambda *args, **kwargs: pytest.fail("No preflight for invalid input"))
    assert cli.main(["run", "--input", str(path)]) == 2
    output = capsys.readouterr().out
    assert "invalid_input" in output and "secret-input" not in output


def test_conflicting_embedded_input_config_is_rejected_before_preflight(runtime, tmp_path, monkeypatch):
    path = tmp_path / "input.json"
    value = input_json(path).model_dump(mode="json")
    value["run_config"] = runtime.run_config.model_copy(update={"report_model": "conflicting-model"}).model_dump(mode="json")
    path.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(cli, "load_runtime_config", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(cli, "preflight", lambda *args, **kwargs: pytest.fail("Conflicting control config must be rejected"))
    assert cli.main(["run", "--input", str(path)]) == 2


@pytest.mark.parametrize("error", [ConfigError("Required model settings are missing: JUDGE_MODEL"),
                                  ServiceConfigurationError("Owner services are missing")])
def test_configuration_failures_exit_one_with_safe_actionable_message(monkeypatch, capsys, error):
    async def dispatch(args):
        raise error
    monkeypatch.setattr(cli, "dispatch", dispatch)
    assert cli.main(["validate"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["terminal_reason"] == "CONFIG_OR_AUTH_ERROR"
    assert result["message"] == str(error)


def test_unknown_provider_exception_never_prints_secret(monkeypatch, capsys):
    async def dispatch(args):
        raise RuntimeError("key=VERY-SENSITIVE-SECRET payload=entire source body")
    monkeypatch.setattr(cli, "dispatch", dispatch)
    assert cli.main(["validate"]) == 1
    assert "VERY-SENSITIVE" not in capsys.readouterr().out


async def persisted_run(runtime, tmp_path, status="running"):
    run_id = str(uuid4())
    store = FileArtifactStore(runtime.artifact_root / run_id, run_id=run_id)
    state = await initialize_run(input_json(tmp_path / "input.json"), runtime.run_config, store=store)
    state["run_id"] = run_id
    if status != "running":
        state.update(status=status, terminal_reason=c.TerminalReason.BUDGET_EXHAUSTED)
    ref = await store.save(runtime.snapshot(), key="snapshots/runtime.json")
    await store.save({"run_id": run_id, "thread_id": state["thread_id"],
                      "runtime_ref": ref.model_dump(mode="json"),
                      "input_ref": state["input_ref"].model_dump(mode="json"),
                      "config_file_hash": cli._file_hash(runtime.config_path)}, key="runtime.json")
    graph = StateGraph(SupervisorState)
    graph.add_node("persist_fixture", lambda item: item)
    graph.add_edge(START, "persist_fixture")
    graph.add_edge("persist_fixture", END)
    async with open_checkpointer(runtime.sqlite_path) as saver:
        await graph.compile(checkpointer=saver).ainvoke(state, {"configurable": {"thread_id": state["thread_id"]}})
    return state, cli.parser().parse_args(["resume", "--run-id", run_id,
                                          "--artifact-root", str(runtime.artifact_root)])


def test_resume_uses_frozen_configuration_despite_environment_change(runtime, tmp_path, monkeypatch):
    async def scenario():
        state, args = await persisted_run(runtime, tmp_path)
        observed = []
        monkeypatch.setenv("REPORT_MODEL", "new-and-wrong")
        monkeypatch.setenv("NEUTRAL_ARTIFACT_ROOT", "/wrong")
        def preflight(cfg, **kwargs):
            observed.append(cfg)
            return SimpleNamespace(config=cfg, versions=state["versions"])
        async def services(cfg, *args):
            assert cfg.run_config.report_model == "fixture-report"
            return SimpleNamespace(mock=True)
        async def execute(**kwargs):
            assert kwargs["resume"] is True
            assert kwargs["state"]["deadline_at"] == state["deadline_at"]
            return dict(state, status="incomplete", terminal_reason=c.TerminalReason.BUDGET_EXHAUSTED)
        monkeypatch.setattr(cli, "preflight", preflight)
        monkeypatch.setattr(cli, "load_services", services)
        monkeypatch.setattr(cli, "_execute_graph", execute)
        assert await cli._resume(args) == 3
        assert observed[0].snapshot() == runtime.snapshot()
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["prompt", "config_file", "corpus", "terminal"])
def test_resume_rejects_changes_before_service_dispatch(runtime, tmp_path, monkeypatch, changed):
    async def scenario():
        state, args = await persisted_run(runtime, tmp_path, "incomplete" if changed == "terminal" else "running")
        if changed == "config_file":
            runtime.config_path.write_text("# changed source\n", encoding="utf-8")
        def preflight(cfg, **kwargs):
            if changed in {"config_file", "terminal"}:
                pytest.fail("Resume must reject before provider preflight")
            if changed == "corpus":
                raise ConfigError("Changed original PDF", "ARTIFACT_INTEGRITY_ERROR")
            return SimpleNamespace(config=cfg, versions=dict(state["versions"], prompt="b" * 64))
        async def services(*args):
            pytest.fail("Changed or terminal run cannot dispatch workers")
        monkeypatch.setattr(cli, "preflight", preflight)
        monkeypatch.setattr(cli, "load_services", services)
        with pytest.raises(StateIntegrityError) as error:
            await cli._resume(args)
        assert error.value.code == "RESUME_VERSION_MISMATCH"
    asyncio.run(scenario())


def balanced_gold():
    return {"queries": [{"query_id": f"q{i}", "query": f"query {i}",
                         "perspective": c.PERSPECTIVES[i // 5].value,
                         "language": "ko" if i < 10 else "en", "relevant_source_ids": ["gold-source"]}
                        for i in range(20)]}


def test_retrieval_uses_actual_ranks_holds_back_labels_and_reports_each_view():
    async def retrieve(*, query, top_k):
        assert "relevant_source_ids" not in query
        return c.RetrievalResult(sources=[], evidence=[], receipt=c.RetrievalReceipt(
            query_id=query["query_id"], query_text=query["query"], queries_hash="a" * 64,
            provider="fixture", corpus_version="v1", index_version="v1", filters={},
            ranked_hits=[c.RankedHit(source_id="gold-source", chunk_id="chunk", rank=5, score=0.1)],
            outcome="success", ts=datetime.now(timezone.utc)))
    result = asyncio.run(evaluate_retrieval(SimpleNamespace(retrieve=retrieve, mock=True), balanced_gold()))
    assert result["hit_rate_at_5"] == 1.0
    assert result["mrr_at_5"] == pytest.approx(0.2)
    assert len(result["by_perspective"]) == 4
    assert all(row["query_count"] == 5 for row in result["by_perspective"].values())
    assert result["p95_latency_seconds"] >= result["p50_latency_seconds"] >= 0
    assert result["memory_measurement"] == "process_lifetime_peak_rss"


@pytest.mark.parametrize("bad", ["receipt", "gold_type", "balance"])
def test_retrieval_rejects_invalid_gold_and_unrelated_receipt(bad):
    gold = balanced_gold()
    if bad == "gold_type":
        gold["queries"][0]["relevant_source_ids"] = "gold-source"
    if bad == "balance":
        gold["queries"][0]["language"] = "en"
    async def retrieve(*, query, top_k):
        return c.RetrievalResult(sources=[], evidence=[], receipt=c.RetrievalReceipt(
            query_id="wrong-query", query_text=query["query"], queries_hash="a" * 64,
            provider="fixture", corpus_version="v1", index_version="v1", filters={}, ranked_hits=[],
            outcome="empty", ts=datetime.now(timezone.utc)))
    with pytest.raises(ValueError):
        asyncio.run(evaluate_retrieval(SimpleNamespace(retrieve=retrieve, mock=True), gold))


def test_assessor_receives_original_deadline_and_logs_gate_metadata(tmp_path, monkeypatch):
    from datetime import timedelta
    from pydantic import BaseModel
    from main.runtime.neutral import services
    from main.runtime.neutral.llm import StructuredLLMClient
    from main.runtime.neutral.retries import current_scope
    from main.runtime.neutral.tracing import TraceRecorder

    class Response(BaseModel):
        checked: bool

    async def scenario():
        deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
        scopes = []
        async def transport(**kwargs):
            scopes.append(current_scope())
            return {"checked": True}
        client = StructuredLLMClient(transport=transport)  # No default deadline.
        async def noop(request):
            pass
        async def assess(request):
            response = await client.generate(model="fixture-judge", system_prompt="PRIVATE TEST PROMPT",
                                              payload={}, response_model=Response)
            assert response.checked
            return [c.SufficiencyVerdict(perspective=p, structural_pass=False, semantic_pass=False,
                                         gaps=[], evidence_ids=["fixture-evidence"], claim_support_refs=[],
                                         reason="insufficient fixture", rubric_version="1.0") for p in c.PERSPECTIVES]
        bundle = ServiceBundle(workers={name: noop for name in
            ["eligibility", "writer", "evaluator", *(p.value for p in c.PERSPECTIVES)]},
            evidence_resolver=SimpleNamespace(resolve=noop), assess_evidence=assess, publish_worker=noop, mock=True)
        recorder = TraceRecorder(tmp_path, run_id=str(uuid4()), trace_id=str(uuid4()), mock=True)
        monkeypatch.setattr(services, "build_supervisor_graph", lambda **kwargs: kwargs["supervisor"])
        supervisor = services.assemble_graph(bundle=bundle, store=None, run_config=c.RunConfig(),
                                             checkpointer=object(), recorder=recorder, deadline_at=deadline)
        request = c.SufficiencyRequest(eligibility=c.EligibilityResult(task_id="fixture", unlisted=True,
            funding_stage=True, exit_status=True, verdict="eligible", claims=[], gaps=[]),
            perspective_results={}, as_of_date="2026-01-01", evidence_epoch=0)
        await supervisor.assess_evidence(request)
        assert scopes[0].deadline_at == deadline and scopes[0].node == "assess_evidence"
        lines = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
        event = next(item for item in lines if item["event"] == "evidence_gate_assessment")
        assert event["gate_verdict"] is False and event["evidence_ids"] == ["fixture-evidence"]
        assert event["task_id"] == "sufficiency-" + c.canonical_hash(request)
        assert "PRIVATE TEST PROMPT" not in (tmp_path / "trace.jsonl").read_text()
    asyncio.run(scenario())
