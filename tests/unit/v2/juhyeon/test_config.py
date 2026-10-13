"""Deployment preflight tests use explicit probe fixtures, never live services."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest
from pypdf import PdfWriter
import yaml

from main.agents.supervisor.contracts import RunInput, canonical_hash
from main.graph.supervisor_state import validate_run_settings
from main.runtime.neutral.config import (
    ConfigError, _font_hash, inspect_corpus, load_runtime_config, preflight,
    runtime_config_from_snapshot,
)


def pdf(path, count=1, password=None):
    writer = PdfWriter()
    for _ in range(count):
        writer.add_blank_page(width=612, height=792)
    if password:
        writer.encrypt(password)
    with path.open("wb") as output:
        writer.write(output)


@pytest.fixture
def configured(tmp_path):
    pdf(tmp_path / "corpus.pdf", 2)
    (tmp_path / "prompt.txt").write_text("Explicit fixture prompt", encoding="utf-8")
    (tmp_path / "font.ttf").write_bytes(b"probe-fixture-font-not-real")
    data = {
        "run_config": {
            "supervisor_model": "fixture-supervisor", "research_model": "fixture-research",
            "report_model": "fixture-report", "judge_model": "fixture-judge",
            "embedding_model": "BAAI/bge-m3", "embedding_revision": "a" * 40,
            "embedding_dimension": 1024, "index_version": "fixture-index-v1",
        },
        "corpus_paths": ["corpus.pdf"], "corpus_domains": ["Physical AI / Robotics"],
        "prompt_paths": ["prompt.txt"], "report_font_path": "font.ttf",
        "qdrant_url": "http://localhost:6333", "qdrant_collection": "neutral-fixture",
        "search_provider": "fixture", "credential_env_vars": ["OPENAI_API_KEY", "TAVILY_API_KEY"],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    config = load_runtime_config(path, repo_root=tmp_path, environ={
        "OPENAI_API_KEY": "fixture-secret-openai", "TAVILY_API_KEY": "fixture-secret-search"})
    corpus = inspect_corpus(config)
    config.index_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "completed", "normalized": True, "collection": config.qdrant_collection,
        "corpus_hash": canonical_hash([doc.model_dump(mode="json") for doc in corpus]),
        "embedding_model": config.run_config.embedding_model,
        "embedding_revision": config.run_config.embedding_revision,
        "embedding_dimension": config.run_config.embedding_dimension,
        "index_version": config.run_config.index_version,
    }
    config.index_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return config


def validated(config, **kwargs):
    return preflight(config, font_probe=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
                     qdrant_probe=lambda cfg: {"dimension": 1024}, **kwargs)


def test_precedence_and_repo_relative_paths(configured, monkeypatch, tmp_path):
    config = configured
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    loaded = load_runtime_config(config.config_path, repo_root=config.repo_root,
                                 environ={"SUPERVISOR_MODEL": "env", "MAX_GRAPH_STEPS": "42",
                                          "REPORT_FONT_PATH": "font.ttf"},
                                 overrides={"supervisor_model": "cli", "max_graph_steps": "43"})
    assert loaded.run_config.supervisor_model == "cli"
    assert loaded.run_config.research_model == "fixture-research"
    assert loaded.run_config.max_graph_steps == 43
    assert loaded.report_font_path == config.repo_root / "font.ttf"
    assert loaded.artifact_root == config.repo_root / "outputs/juhyeon"


def test_snapshot_never_contains_credentials_and_restoration_is_frozen(configured):
    report = validated(configured)
    snapshot = report.config.snapshot()
    assert "fixture-secret" not in json.dumps(snapshot)
    assert "fixture-secret" not in repr(report.config)
    restored = runtime_config_from_snapshot(snapshot, environ={
        "OPENAI_API_KEY": "rotated", "TAVILY_API_KEY": "rotated-search",
        "REPORT_MODEL": "wrong-new-model", "NEUTRAL_ARTIFACT_ROOT": "/wrong/path"})
    assert restored.snapshot() == snapshot
    assert restored.credentials["OPENAI_API_KEY"] == "rotated"
    assert validated(restored).versions == report.versions


def test_versions_match_shared_supervisor(configured):
    report = validated(configured)
    run_input = RunInput(company_name="Fixture", official_url="https://example.org",
                         domain="Physical AI / Robotics", as_of_date="2026-01-01",
                         corpus_manifest=report.corpus_manifest)
    assert validate_run_settings(run_input, report.config.run_config,
                                 datetime(2026, 1, 2, tzinfo=timezone.utc)) == report.versions
    assert report.checks["sqlite"] == "passed"
    assert configured.sqlite_path.is_file()


def test_rule_supervisor_accepts_missing_model(configured):
    config = replace(configured, run_config=configured.run_config.model_copy(
        update={"supervisor_model": None}))
    config = validated(config).config
    run_input = RunInput(company_name="Fixture", official_url="https://example.org",
                        domain="Physical AI / Robotics", as_of_date="2026-01-01",
                        corpus_manifest=inspect_corpus(config))
    versions = validate_run_settings(run_input, config.run_config,
                                     datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert config.run_config.supervisor_model is None
    assert versions["model"] == canonical_hash({
        "supervisor_model": None, "research_model": "fixture-research",
        "report_model": "fixture-report", "judge_model": "fixture-judge"})


def test_fingerprints_detect_prompt_and_font_changes(configured):
    first = validated(configured)
    Path(configured.prompt_paths[0]).write_text("Changed prompt", encoding="utf-8")
    second = validated(first.config)
    assert first.versions["prompt"] != second.versions["prompt"]
    assert first.versions["config"] != second.versions["config"]
    configured.report_font_path.write_bytes(b"other-font")
    third = validated(second.config)
    assert second.versions["config"] != third.versions["config"]


def test_manifest_content_change_affects_config_fingerprint(configured):
    first = validated(configured)
    manifest = json.loads(configured.index_manifest_path.read_text())
    manifest["ingestion_metadata"] = {"revision": "second"}
    configured.index_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert validated(first.config).versions["config"] != first.versions["config"]


@pytest.mark.parametrize("field", ["research_model", "report_model", "judge_model"])
def test_missing_models_fail_without_guessing(configured, field):
    config = replace(configured, run_config=configured.run_config.model_copy(update={field: None}))
    with pytest.raises(ConfigError, match=field.upper()):
        validated(config)


def test_missing_credentials_are_reported_by_name_only(configured):
    with pytest.raises(ConfigError, match="OPENAI_API_KEY") as error:
        validated(replace(configured, credentials={}))
    assert "fixture-secret" not in str(error.value)


@pytest.mark.parametrize("revision", [None, "main", "v1.0", "latest"])
def test_embedding_revision_must_be_explicit_immutable_commit(configured, revision):
    with pytest.raises(ConfigError, match="EMBEDDING_REVISION"):
        validated(replace(configured, run_config=configured.run_config.model_copy(update={"embedding_revision": revision})))


def test_incorrect_embedding_dimension_cannot_use_legacy_index(configured):
    with pytest.raises(ConfigError) as error:
        validated(replace(configured, run_config=configured.run_config.model_copy(update={"embedding_dimension": 512})))
    assert error.value.code == "INDEX_MISMATCH"


def test_real_page_count_and_sha_are_read(configured):
    actual = inspect_corpus(configured)
    assert len(actual) == 1 and actual[0].page_count == 2
    assert actual[0].sha256 == hashlib.sha256(Path(actual[0].file_path).read_bytes()).hexdigest()


def test_duplicate_pdf_hash_counts_once(configured):
    original = Path(configured.corpus_paths[0])
    duplicate = original.with_name("duplicate.pdf")
    duplicate.write_bytes(original.read_bytes())
    config = replace(configured, corpus_paths=(str(original), str(duplicate)))
    manifest = inspect_corpus(config)
    assert len(manifest) == 1
    run_input = RunInput(company_name="Fixture", official_url="https://example.org",
                         domain="Physical AI / Robotics", as_of_date="2026-01-01", corpus_manifest=manifest)
    assert inspect_corpus(config, run_input) == manifest


def test_actual_pdf_budget_rejects_201_pages_before_ingestion(configured):
    pdf(Path(configured.corpus_paths[0]), 201)
    with pytest.raises(ConfigError, match="200 pages"):
        inspect_corpus(configured)


def test_actual_pdf_budget_accepts_200_pages(configured):
    pdf(Path(configured.corpus_paths[0]), 200)
    assert inspect_corpus(configured)[0].page_count == 200


@pytest.mark.parametrize("kind", ["encrypted", "corrupt"])
def test_invalid_pdfs_fail(configured, kind):
    path = Path(configured.corpus_paths[0])
    if kind == "encrypted":
        pdf(path, password="secret")
    else:
        path.write_bytes(b"not a PDF")
    with pytest.raises(ConfigError):
        inspect_corpus(configured)


def test_input_cannot_forge_page_count(configured):
    actual = inspect_corpus(configured)[0]
    run_input = RunInput(company_name="Fixture", official_url="https://example.org",
                         domain="Physical AI / Robotics", as_of_date="2026-01-01",
                         corpus_manifest=[actual.model_copy(update={"page_count": 1})])
    with pytest.raises(ConfigError) as error:
        inspect_corpus(configured, run_input)
    assert error.value.code == "ARTIFACT_INTEGRITY_ERROR"


def test_unavailable_domain_fails(configured):
    run_input = RunInput(company_name="Fixture", official_url="https://example.org", domain="Energy",
                         as_of_date="2026-01-01", corpus_manifest=[])
    with pytest.raises(ConfigError) as error:
        validated(configured, run_input=run_input)
    assert error.value.code == "CORPUS_DOMAIN_UNAVAILABLE"


@pytest.mark.parametrize("key,value", [("embedding_dimension", 512), ("corpus_hash", "0" * 64),
                                       ("normalized", False), ("status", "staging"),
                                       ("collection", "legacy"), ("embedding_revision", "b" * 40)])
def test_manifest_must_bind_real_corpus_and_embedding(configured, key, value):
    manifest = json.loads(configured.index_manifest_path.read_text())
    manifest[key] = value
    configured.index_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        validated(configured)
    assert error.value.code == "INDEX_MISMATCH"


def test_live_qdrant_dimension_probe_is_checked(configured):
    with pytest.raises(ConfigError) as error:
        preflight(configured, font_probe=lambda path: "a" * 64, qdrant_probe=lambda cfg: {"dimension": 512})
    assert error.value.code == "INDEX_MISMATCH"


def test_offline_is_explicit_and_does_not_claim_network_success(configured):
    report = validated(configured, check_network=False)
    assert report.checks["qdrant"] == "network_skipped"


def test_ingest_can_validate_without_existing_index(configured):
    configured.index_manifest_path.unlink()
    report = validated(configured, require_index=False)
    assert report.checks["index"] == "not_required_for_ingest"
    assert report.config.index_manifest_hash is None


def test_missing_font_and_non_korean_font_fail(tmp_path):
    with pytest.raises(ConfigError, match="REPORT_FONT_PATH"):
        _font_hash(None)
    invalid = tmp_path / "invalid.ttf"
    invalid.write_bytes(b"invalid")
    with pytest.raises(ConfigError, match="REPORT_FONT_PATH"):
        _font_hash(invalid)
    import reportlab
    vera = Path(reportlab.__file__).parent / "fonts/Vera.ttf"
    with pytest.raises(ConfigError, match="Korean glyphs"):
        _font_hash(vera)


def test_invalid_limit_yaml_secret_and_url_credentials_rejected(configured):
    for overrides in ({"max_graph_steps": "bad"}, {"qdrant_url": "https://user:password@example.org"},
                      {"qdrant_url": "https://example.org?api_key=secret"}, {"api_key": "secret"}):
        with pytest.raises(ConfigError):
            load_runtime_config(configured.config_path, repo_root=configured.repo_root, environ={}, overrides=overrides)


def test_snapshot_does_not_accept_missing_immutable_fields(configured):
    snapshot = configured.snapshot()
    del snapshot["services_factory"]
    with pytest.raises(ConfigError):
        runtime_config_from_snapshot(snapshot, environ={})
