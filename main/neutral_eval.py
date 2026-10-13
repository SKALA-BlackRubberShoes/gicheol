"""Separate neutral evaluation CLI; the existing investment CLI is unchanged."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from langgraph.errors import GraphRecursionError
from langsmith import tracing_context
from pydantic import ValidationError

from main.agents.supervisor import contracts as c
from main.graph.supervisor_state import StateIntegrityError, SupervisorState, validate_state
from main.graph.supervisor_workflow import initialize_run, validate_resume_versions
from main.runtime.neutral.config import ConfigError, load_runtime_config, preflight, runtime_config_from_snapshot
from main.runtime.neutral.llm import StructuredLLMClient
from main.runtime.neutral.services import ServiceConfigurationError, assemble_graph, evaluate_retrieval, load_services
from main.runtime.neutral.tracing import TraceRecorder, redact_text
from main.storage.artifacts import FileArtifactStore
from main.storage.checkpoints import open_checkpointer

ROOT = Path(__file__).resolve().parents[1]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="SDD 1.0 neutral startup evaluation")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("validate", "ingest", "run", "resume", "evaluate-retrieval"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="configs/neutral-evaluation.yaml")
        command.add_argument("--env-file", help="Explicit environment file; credentials never enter snapshots")
        command.add_argument("--set", action="append", default=[], metavar="KEY=JSON",
                             help="Explicit configuration override; strings may omit JSON quoting")
        if name == "run":
            command.add_argument("--input", required=True)
        elif name == "resume":
            command.add_argument("--run-id", required=True)
            command.add_argument("--artifact-root", help="Locate the original run; never changes saved settings")
        elif name == "evaluate-retrieval":
            command.add_argument("--dataset", required=True)
    return result


def _overrides(values):
    result = {}
    for value in values:
        key, separator, raw = value.partition("=")
        if not separator or not key:
            raise ValueError("--set requires KEY=VALUE")
        try:
            result[key] = json.loads(raw)
        except json.JSONDecodeError:
            result[key] = raw
    return result


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _output(payload):
    print(c.canonical_json(payload))


def _exit(status):
    return {"completed": 0, "incomplete": 3, "failed": 1}.get(status, 1)


@asynccontextmanager
async def _service_scope(runtime, store, llm, run_input):
    bundle = await load_services(runtime, store, llm, run_input=run_input)
    try:
        yield bundle
    finally:
        close = getattr(bundle, "close", None)
        if close is not None:
            await close()


async def _execute_graph(*, state, runtime, store, bundle, checkpointer, resume=False):
    recorder = TraceRecorder(store.root, run_id=state["run_id"], trace_id=state["trace_id"],
        metadata={"spec_version": "1.0", "schema_version": state["versions"]["schema"],
                  "corpus_hash": state["versions"]["corpus"], "index_revision": state["versions"]["index"],
                  "config_hash": state["versions"]["config"], "prompt_hash": state["versions"]["prompt"],
                  "policy_version": state["versions"]["policy"], "model": state["versions"]["model"],
                  "models": {key: getattr(runtime.run_config, key) for key in
                             ("supervisor_model", "research_model", "report_model", "judge_model")}},
        enabled=runtime.langsmith_tracing, mock=bundle.mock,
        project_name=getattr(runtime, "langsmith_project", "neutral-evaluation-v2"))
    graph = assemble_graph(bundle=bundle, store=store, run_config=runtime.run_config,
        checkpointer=checkpointer, recorder=recorder, deadline_at=state["deadline_at"])
    invocation = {"configurable": {"thread_id": state["thread_id"]},
                  "recursion_limit": runtime.run_config.recursion_limit}
    await recorder.start()
    try:
        with tracing_context(enabled=False):
            result = await graph.ainvoke(None if resume else state, invocation)
    except GraphRecursionError:
        with tracing_context(enabled=False):
            snapshot = await graph.aget_state(invocation)
        result = validate_state(dict(snapshot.values) if snapshot.values else state)
        result.update(status="incomplete", terminal_reason=c.TerminalReason.BUDGET_EXHAUSTED,
                      final_manifest_ref=None)
        await store.save({"status": "incomplete", "terminal_reason": "BUDGET_EXHAUSTED",
                          "reason": "graph recursion limit", "run_id": state["run_id"]},
                         key="diagnostics.json")
        with tracing_context(enabled=False):
            await graph.aupdate_state(invocation, result, as_node="abort")
    except (KeyboardInterrupt, asyncio.CancelledError):
        await asyncio.shield(store.save({"status": "interrupted", "run_id": state["run_id"]},
                                       key="diagnostics.json"))
        await recorder.finish(outcome="interrupted")
        raise
    except Exception:
        # Provider internals can contain credentials or full prompts. Keep the
        # durable checkpoint; expose a classified summary, never raw payloads.
        await store.save({"status": "interrupted", "run_id": state["run_id"],
                          "reason": "runtime interrupted; inspect checkpoint before resume"},
                         key="diagnostics.json")
        await recorder.finish(outcome="failed")
        raise
    await recorder.finish(outcome=result["status"])
    if result["status"] == "completed":
        manifest_ref = await store.find_manifest(result["run_id"], result["report_revision"])
        if manifest_ref is None or manifest_ref != result["final_manifest_ref"]:
            raise StateIntegrityError("Completed result has no matching committed manifest")
        await store.approved_artifact_paths(result["run_id"], result["report_revision"])
    return result


async def _run(args, runtime):
    try:
        run_input = c.RunInput.model_validate(_read_json(args.input))
    except OSError:
        raise ValueError("Input JSON file cannot be read") from None
    if run_input.run_config is not None and run_input.run_config != runtime.run_config:
        raise ValueError("Input run_config conflicts with the effective --config settings")
    checked = await asyncio.to_thread(preflight, runtime, run_input=run_input)
    runtime = checked.config
    # Persist precisely the actual corpus that preflight checked.
    run_input = run_input.model_copy(update={"corpus_manifest": checked.corpus_manifest,
                                            "run_config": runtime.run_config})
    run_id = str(uuid4())
    store = FileArtifactStore(runtime.artifact_root / run_id, run_id=run_id)
    llm = StructuredLLMClient(timeout_seconds=runtime.run_config.llm_timeout_seconds,
                             max_retries=runtime.run_config.max_transient_retries)
    async with _service_scope(runtime, store, llm, run_input) as bundle:
        state = await initialize_run(run_input, runtime.run_config, store=store)
        state["run_id"] = run_id
        state = validate_state(state)
        runtime_ref = await store.save(runtime.snapshot(), key="snapshots/runtime.json")
        await store.save({"run_id": run_id, "thread_id": state["thread_id"],
                          "runtime_ref": runtime_ref.model_dump(mode="json"),
                          "input_ref": state["input_ref"].model_dump(mode="json"),
                          "config_file_hash": _file_hash(runtime.config_path)}, key="runtime.json")
        async with open_checkpointer(runtime.sqlite_path) as checkpointer:
            result = await _execute_graph(state=state, runtime=runtime, store=store,
                                         bundle=bundle, checkpointer=checkpointer)
    _output({"run_id": run_id, "status": result["status"], "mock": bundle.mock,
             "terminal_reason": result["terminal_reason"],
             "manifest": result["final_manifest_ref"]})
    return _exit(result["status"])


async def _resume(args):
    UUID(args.run_id)  # Run IDs are path components, not arbitrary file paths.
    if args.set:
        raise ValueError("resume uses the saved configuration; --set is not allowed")
    if args.artifact_root:
        root = Path(args.artifact_root)
        root = root if root.is_absolute() else ROOT / root
    else:
        # Only the location is read from current YAML. Execution settings below
        # are always restored from the immutable snapshot.
        root = load_runtime_config(args.config).artifact_root
    run_root = root / args.run_id
    if not run_root.is_dir() or run_root.is_symlink():
        raise ValueError("Run does not exist in the selected artifact root")
    store = FileArtifactStore(run_root, run_id=args.run_id)
    registry = _read_json(run_root / "runtime.json")
    if registry["run_id"] != args.run_id:
        raise StateIntegrityError("Run registry identity mismatch")
    runtime = runtime_config_from_snapshot(await store.load(c.ArtifactRef.model_validate(registry["runtime_ref"])))
    if _file_hash(runtime.config_path) != registry["config_file_hash"]:
        raise StateIntegrityError("Configuration source changed", "RESUME_VERSION_MISMATCH")
    run_input = c.RunInput.model_validate(await store.load(c.ArtifactRef.model_validate(registry["input_ref"])))
    async with open_checkpointer(runtime.sqlite_path) as checkpointer:
        checkpoint = await checkpointer.aget_tuple({"configurable": {"thread_id": registry["thread_id"]}})
        if checkpoint is None:
            raise StateIntegrityError("No durable checkpoint found")
        state = validate_state({key: value for key, value in checkpoint.checkpoint["channel_values"].items()
                                if key in SupervisorState.__annotations__})
        if state["run_id"] != args.run_id or state["thread_id"] != registry["thread_id"]:
            raise StateIntegrityError("Checkpoint identity differs from registry")
        # Terminal runs cannot trigger fresh provider requests during resume.
        validate_resume_versions(state, state["versions"])
        try:
            checked = await asyncio.to_thread(preflight, runtime, run_input=run_input)
        except ConfigError as exc:
            if exc.code in {"INDEX_MISMATCH", "ARTIFACT_INTEGRITY_ERROR"}:
                raise StateIntegrityError("Saved corpus or index changed", "RESUME_VERSION_MISMATCH") from None
            raise
        validate_resume_versions(state, checked.versions)
        llm = StructuredLLMClient(deadline_at=state["deadline_at"],
            timeout_seconds=runtime.run_config.llm_timeout_seconds,
            max_retries=runtime.run_config.max_transient_retries)
        async with _service_scope(runtime, store, llm, run_input) as bundle:
            result = await _execute_graph(state=state, runtime=runtime, store=store,
                                         bundle=bundle, checkpointer=checkpointer, resume=True)
    _output({"run_id": args.run_id, "status": result["status"], "mock": bundle.mock,
             "terminal_reason": result["terminal_reason"], "manifest": result["final_manifest_ref"]})
    return _exit(result["status"])


async def dispatch(args):
    if args.env_file:
        from dotenv import load_dotenv
        if not Path(args.env_file).is_file():
            raise ValueError("Environment file does not exist")
        load_dotenv(args.env_file, override=False)
    if args.command == "resume":
        return await _resume(args)
    runtime = load_runtime_config(args.config, overrides=_overrides(args.set))
    if args.command == "run":
        return await _run(args, runtime)
    checked = await asyncio.to_thread(preflight, runtime, require_index=args.command != "ingest")
    runtime = checked.config
    if args.command == "validate":
        _output({"status": "validated", "checks": checked.checks,
                 "note": "Configuration preflight only; owner service integration and live run are separate"})
        return 0
    store = FileArtifactStore(runtime.artifact_root / ("preflight-" + str(uuid4())))
    if runtime.services_factory == "main.runtime.neutral.production:create_production_services":
        from main.runtime.neutral.production import ingest_production_corpus, evaluate_production_retrieval
        async with asyncio.timeout(runtime.run_config.run_deadline_seconds):
            if args.command == "ingest":
                result = await ingest_production_corpus(runtime=runtime, store=store,
                    corpus_manifest=checked.corpus_manifest)
            else:
                result = await evaluate_production_retrieval(runtime=runtime, store=store,
                    corpus_manifest=checked.corpus_manifest, dataset=_read_json(args.dataset))
    else:
        bundle = await load_services(runtime, store, StructuredLLMClient(
            timeout_seconds=runtime.run_config.llm_timeout_seconds,
            max_retries=runtime.run_config.max_transient_retries))
        try:
            async with asyncio.timeout(runtime.run_config.run_deadline_seconds):
                if args.command == "ingest":
                    if bundle.ingest is None:
                        raise ValueError("No neutral ingestion adapter is registered")
                    result = await bundle.ingest(runtime=runtime, corpus_manifest=checked.corpus_manifest)
                else:
                    result = await evaluate_retrieval(bundle, _read_json(args.dataset))
        finally:
            close = getattr(bundle, "close", None)
            if close is not None:
                await close()
    _output(result)
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        _output({"status": "interrupted"})
        return 1
    except (ValidationError, json.JSONDecodeError) as exc:
        # ValidationError may include sensitive input values; emit locations only.
        fields = [".".join(map(str, item["loc"])) for item in exc.errors()] if isinstance(exc, ValidationError) else ["JSON"]
        _output({"status": "invalid_input", "fields": fields})
        return 2
    except Exception as exc:
        code = getattr(exc, "code", "CONFIG_OR_AUTH_ERROR")
        if isinstance(exc, ValueError) and not hasattr(exc, "code"):
            _output({"status": "invalid_input", "error": type(exc).__name__})
            return 2
        payload = {"status": "failed", "terminal_reason": code, "error": type(exc).__name__}
        if isinstance(exc, (ConfigError, ServiceConfigurationError, StateIntegrityError)):
            payload["message"] = redact_text(str(exc))
        _output(payload)
        return 1


if __name__ == "__main__":
    sys.exit(main())
