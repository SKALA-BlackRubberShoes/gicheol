"""SDD runtime configuration and fail-closed, independently testable preflight.

The Supervisor's RunConfig is the single control DTO. This wrapper contains
deployment paths and adapter settings only. Credentials are read from named
environment variables and never appear in snapshots or exception messages.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

import yaml
from pydantic import ValidationError

from main.agents.supervisor.contracts import CorpusDocument, Domain, RunConfig, RunInput, canonical_hash


class ConfigError(ValueError):
    def __init__(self, message: str, code: str = "CONFIG_OR_AUTH_ERROR") -> None:
        super().__init__(message)
        self.code = code


_ROOT = Path(__file__).resolve().parents[3]
_MODEL_FIELDS = ("supervisor_model", "research_model", "report_model", "judge_model")
_PATH_FIELDS = {"artifact_root", "sqlite_path", "report_font_path", "index_manifest_path"}
_LIST_FIELDS = {"corpus_paths", "corpus_domains", "prompt_paths", "credential_env_vars"}
_RUNTIME_DEFAULTS: dict[str, Any] = {
    "artifact_root": "outputs/juhyeon",
    "sqlite_path": "outputs/juhyeon/checkpoints.sqlite",
    "report_font_path": None,
    "qdrant_url": None,
    "qdrant_collection": None,
    "index_manifest_path": "outputs/juhyeon/index-manifest.json",
    "corpus_paths": [],
    "corpus_domains": [],
    "prompt_paths": [],
    "search_provider": None,
    "services_factory": "main.runtime.neutral.production:create_production_services",
    "langsmith_tracing": False,
    "langsmith_project": "neutral-evaluation-v2",
    "credential_env_vars": ["OPENAI_API_KEY", "TAVILY_API_KEY"],
}
_RUNTIME_ENV = {
    "artifact_root": "NEUTRAL_ARTIFACT_ROOT", "sqlite_path": "NEUTRAL_SQLITE_PATH",
    "report_font_path": "REPORT_FONT_PATH", "qdrant_url": "QDRANT_URL",
    "qdrant_collection": "QDRANT_COLLECTION", "index_manifest_path": "NEUTRAL_INDEX_MANIFEST_PATH",
    "corpus_paths": "NEUTRAL_CORPUS_PATHS", "corpus_domains": "NEUTRAL_CORPUS_DOMAINS",
    "prompt_paths": "NEUTRAL_PROMPT_PATHS", "search_provider": "SEARCH_PROVIDER",
    "services_factory": "NEUTRAL_SERVICES_FACTORY", "langsmith_tracing": "LANGSMITH_TRACING",
    "langsmith_project": "LANGSMITH_PROJECT", "credential_env_vars": "NEUTRAL_CREDENTIAL_ENV_VARS",
}
_SECRET_NAMES = ("OPENAI_API_KEY", "TAVILY_API_KEY", "QDRANT_API_KEY", "LANGSMITH_API_KEY")


def _path(value: Any, root: Path) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ConfigError("Configured paths must be nonempty strings")
    path = Path(value).expanduser()
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def _convert(key: str, value: Any) -> Any:
    if key in _LIST_FIELDS:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                raise ConfigError(f"{key} must be a JSON list when overridden") from None
        if not isinstance(value, (list, tuple)) or any(not isinstance(v, str) or not v.strip() for v in value):
            raise ConfigError(f"{key} must be a list of nonempty strings")
        return list(value)
    if key == "langsmith_tracing":
        if isinstance(value, str) and value.lower() in {"true", "false", "1", "0"}:
            return value.lower() in {"true", "1"}
        if type(value) is not bool:
            raise ConfigError("langsmith_tracing must be a boolean")
    if key in RunConfig.model_fields and isinstance(value, str):
        default = RunConfig.model_fields[key].default
        if type(default) is int or key == "embedding_dimension":
            if not re.fullmatch(r"[0-9]+", value):
                raise ConfigError(f"{key} must be an integer")
            return int(value)
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    run_config: RunConfig
    config_path: Path
    repo_root: Path
    artifact_root: Path
    sqlite_path: Path
    report_font_path: Path | None
    qdrant_url: str | None
    qdrant_collection: str | None
    index_manifest_path: Path
    corpus_paths: tuple[str, ...]
    corpus_domains: tuple[str, ...]
    prompt_paths: tuple[str, ...]
    search_provider: str | None
    services_factory: str
    langsmith_tracing: bool
    langsmith_project: str
    credential_env_vars: tuple[str, ...]
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    index_manifest_hash: str | None = None
    font_hash: str | None = None

    def snapshot(self) -> dict[str, Any]:
        result = {key: getattr(self, key) for key in _RUNTIME_DEFAULTS}
        result.update(config_path=str(self.config_path), repo_root=str(self.repo_root),
                      run_config=self.run_config.model_dump(mode="json"),
                      index_manifest_hash=self.index_manifest_hash, font_hash=self.font_hash)
        for key in _PATH_FIELDS:
            result[key] = str(result[key]) if result[key] is not None else None
        for key in _LIST_FIELDS:
            result[key] = list(result[key])
        return result


def _assemble(data: Mapping[str, Any], *, config_path: Path, root: Path, environ: Mapping[str, str]) -> RuntimeConfig:
    control = data.get("run_config", {})
    if not isinstance(control, dict):
        raise ConfigError("run_config must be a mapping")
    try:
        run_config = RunConfig.model_validate({k: _convert(k, v) for k, v in control.items()})
    except (ValidationError, TypeError):
        raise ConfigError("Invalid RunConfig fields or limits; check configuration keys and types") from None
    settings = {key: _convert(key, data.get(key, default)) for key, default in _RUNTIME_DEFAULTS.items()}
    for key in _PATH_FIELDS:
        settings[key] = _path(settings[key], root)
    for key in _LIST_FIELDS:
        settings[key] = tuple(settings[key])
    for key in ("corpus_paths", "prompt_paths"):
        settings[key] = tuple(str(_path(item, root)) for item in settings[key])
    for key in ("artifact_root", "sqlite_path", "index_manifest_path"):
        if settings[key] is None:
            raise ConfigError(f"{key} is required")
    for key in ("qdrant_url", "qdrant_collection", "search_provider", "services_factory", "langsmith_project"):
        value = settings[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"{key} must be a nonempty string")
    if settings["qdrant_url"]:
        url = urlsplit(settings["qdrant_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ConfigError("QDRANT_URL must be an HTTP(S) URL without credentials, query, or fragment")
    if not settings["services_factory"] or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", settings["services_factory"]):
        raise ConfigError("services_factory must use module:function syntax")
    for name in settings["credential_env_vars"]:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)", name):
            raise ConfigError("credential_env_vars must contain credential environment variable names")
    for domain in settings["corpus_domains"]:
        try:
            Domain(domain)
        except ValueError:
            raise ConfigError("Unknown corpus domain") from None
    secret_names = set(_SECRET_NAMES) | set(settings["credential_env_vars"])
    credentials = {name: environ[name] for name in secret_names if environ.get(name)}
    return RuntimeConfig(run_config=run_config, config_path=config_path, repo_root=root,
                         credentials=credentials, index_manifest_hash=data.get("index_manifest_hash"),
                         font_hash=data.get("font_hash"), **settings)


def load_runtime_config(path: str | Path, *, overrides: Mapping[str, Any] | None = None,
                        environ: Mapping[str, str] | None = None, repo_root: str | Path | None = None) -> RuntimeConfig:
    """Load YAML < environment < explicit CLI overrides; relative paths use repo root.

    RunConfig keys may be placed under ``run_config`` or supplied as flat CLI
    overrides. No implicit .env loading or START_AGENT_MODEL fallback occurs.
    """
    env = os.environ if environ is None else environ
    root = Path(repo_root).resolve() if repo_root else _ROOT
    config_path = _path(path, root)
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raise ConfigError("Cannot read configuration YAML") from None
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be a mapping")
    allowed = set(_RUNTIME_DEFAULTS) | {"run_config"}
    if set(data) - allowed:
        raise ConfigError("Unknown configuration keys; credentials belong in environment variables")
    if not isinstance(data.get("run_config", {}), dict):
        raise ConfigError("run_config must be a mapping")
    data = dict(data)
    data["run_config"] = dict(data.get("run_config", {}))
    for key, name in _RUNTIME_ENV.items():
        if name in env:
            data[key] = _convert(key, env[name])
    for key in RunConfig.model_fields:
        if key.upper() in env:
            data["run_config"][key] = _convert(key, env[key.upper()])
    for key, value in (overrides or {}).items():
        if key == "run_config":
            if not isinstance(value, Mapping):
                raise ConfigError("run_config override must be a mapping")
            data["run_config"].update(value)
        elif key in RunConfig.model_fields:
            data["run_config"][key] = _convert(key, value)
        elif key in _RUNTIME_DEFAULTS:
            data[key] = _convert(key, value)
        else:
            raise ConfigError("Unknown CLI configuration override")
    return _assemble(data, config_path=config_path, root=root, environ=env)


def runtime_config_from_snapshot(snapshot: Mapping[str, Any], *, environ: Mapping[str, str] | None = None) -> RuntimeConfig:
    """Restore frozen deployment settings; only credential values come from env."""
    allowed = set(_RUNTIME_DEFAULTS) | {"run_config", "repo_root", "config_path", "index_manifest_hash", "font_hash"}
    if set(snapshot) - allowed or not (set(_RUNTIME_DEFAULTS) | {"run_config", "repo_root", "config_path"}).issubset(snapshot):
        raise ConfigError("Invalid runtime configuration snapshot")
    return _assemble(snapshot, config_path=Path(snapshot["config_path"]), root=Path(snapshot["repo_root"]),
                     environ=os.environ if environ is None else environ)


def _expand_files(patterns: tuple[str, ...], *, label: str) -> list[Path]:
    import glob

    files: set[Path] = set()
    for pattern in patterns:
        matches = [Path(p).resolve() for p in glob.glob(pattern, recursive=True) if Path(p).is_file()]
        if not matches:
            raise ConfigError(f"{label} path has no readable files")
        files.update(matches)
    if not files:
        raise ConfigError(f"{label} paths are required")
    return sorted(files)


def inspect_corpus(config: RuntimeConfig, run_input: RunInput | None = None) -> list[CorpusDocument]:
    """Read real PDF pages and bytes, rejecting encryption, mismatches and >200."""
    from pypdf import PdfReader

    declared = {str(_path(d.file_path, config.repo_root)): d for d in run_input.corpus_manifest} if run_input else {}
    paths = _expand_files(config.corpus_paths, label="Corpus")
    if declared and not set(declared).issubset({str(path) for path in paths}):
        raise ConfigError("Run input corpus files differ from configured corpus", "ARTIFACT_INTEGRITY_ERROR")
    documents: list[CorpusDocument] = []
    hashes: set[str] = set()
    total_pages = 0
    for path in paths:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise ConfigError("Encrypted corpus PDF is not supported")
            pages = len(reader.pages)
            if pages < 1:
                raise ConfigError("Corpus PDF must contain at least one page")
            # Force each page to parse before ingest; header/page count alone is insufficient.
            for page in reader.pages:
                page.extract_text()
        except ConfigError:
            raise
        except Exception:
            raise ConfigError("Corpus PDF cannot be parsed") from None
        original = declared.get(str(path))
        if original and (original.sha256 != digest or original.page_count != pages):
            raise ConfigError("Run input PDF hash or actual page count differs", "ARTIFACT_INTEGRITY_ERROR")
        if declared and original is None:
            # Saved manifests omit duplicate hashes. A duplicate file that is
            # still configured must not break resume; an unlisted unique PDF
            # must never be silently added to the saved corpus.
            matching = next((item for item in declared.values() if item.sha256 == digest and item.page_count == pages), None)
            if matching is None:
                raise ConfigError("Run input omits a unique configured PDF", "ARTIFACT_INTEGRITY_ERROR")
            original = matching
        if digest in hashes:
            continue
        hashes.add(digest)
        total_pages += pages
        if total_pages > 200:
            raise ConfigError("Actual unique PDF corpus exceeds 200 pages")
        documents.append(original.model_copy(update={"file_path": str(_path(original.file_path, config.repo_root))}) if original else CorpusDocument(
            document_id="pdf-" + digest[:24], file_path=str(path), page_count=pages, sha256=digest))
    return documents


def _font_hash(path: Path | None) -> str:
    if path is None or not path.is_file():
        raise ConfigError("REPORT_FONT_PATH must identify an existing Korean TrueType font")
    try:
        from reportlab.pdfbase.ttfonts import TTFont
        font = TTFont("NeutralPreflight", str(path))
        if not all(ord(char) in font.face.charToGlyph for char in "한글보고서"):
            raise ConfigError("REPORT_FONT_PATH font lacks required Korean glyphs")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("REPORT_FONT_PATH is not a supported embeddable TrueType font") from None


def _sqlite_probe(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Existing checkpointer contents remain untouched; the transaction is rolled back.
        with sqlite3.connect(path, timeout=2) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("CREATE TABLE IF NOT EXISTS _neutral_preflight_probe (id INTEGER)")
            connection.execute("INSERT INTO _neutral_preflight_probe VALUES (1)")
            connection.rollback()
    except (OSError, sqlite3.Error):
        raise ConfigError("SQLite checkpoint path is not writable or accessible") from None


def _qdrant_probe(config: RuntimeConfig) -> dict[str, Any]:
    url = f"{config.qdrant_url.rstrip('/')}/collections/{quote(config.qdrant_collection, safe='')}"
    headers = {}
    if config.credentials.get("QDRANT_API_KEY"):
        headers["api-key"] = config.credentials["QDRANT_API_KEY"]
    try:
        request = urllib.request.Request(url, headers=headers)
        # Do not forward api-key to a redirected host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=config.run_config.tool_timeout_seconds) as response:
            payload = json.load(response)
        vectors = payload["result"]["config"]["params"]["vectors"]
        if not isinstance(vectors, dict) or "size" not in vectors:
            raise ConfigError("Qdrant collection must have a single dense vector", "INDEX_MISMATCH")
        return {"dimension": vectors["size"], "distance": vectors.get("distance")}
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("Qdrant collection is inaccessible or returned invalid metadata") from None


@dataclass(frozen=True)
class PreflightReport:
    config: RuntimeConfig
    corpus_manifest: list[CorpusDocument]
    checks: dict[str, str]
    versions: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {"checks": self.checks, "versions": self.versions,
                "corpus_manifest": [doc.model_dump(mode="json") for doc in self.corpus_manifest],
                "snapshot": self.config.snapshot()}


def preflight(config: RuntimeConfig, *, run_input: RunInput | None = None, require_index: bool = True,
              check_network: bool = True, qdrant_probe: Callable[[RuntimeConfig], Mapping[str, Any]] | None = None,
              font_probe: Callable[[Path | None], str] | None = None,
              sqlite_probe: Callable[[Path], None] | None = None) -> PreflightReport:
    """Validate files, credentials and index binding; skipped network is explicit.

    Injected probes are for adapter integration and deterministic tests. Callers
    must not label ``network_skipped`` reports as live service validation.
    """
    control = config.run_config
    missing = [name.upper() for name in _MODEL_FIELDS if name != "supervisor_model"
               and (not getattr(control, name) or not getattr(control, name).strip())]
    if missing:
        raise ConfigError("Required model settings are missing: " + ", ".join(missing))
    if not control.embedding_model or not control.embedding_revision or not control.embedding_dimension:
        raise ConfigError("EMBEDDING_MODEL, fixed EMBEDDING_REVISION and EMBEDDING_DIMENSION are required")
    if not re.fullmatch(r"[0-9a-f]{40}", control.embedding_revision):
        raise ConfigError("EMBEDDING_REVISION must be the immutable 40-character model commit SHA")
    if control.embedding_model not in {"BAAI/bge-m3", "intfloat/multilingual-e5-base"}:
        raise ConfigError("Only explicitly supported open-source embedding models can use this runtime")
    expected_dimension = {"BAAI/bge-m3": 1024, "intfloat/multilingual-e5-base": 768}[control.embedding_model]
    if control.embedding_dimension != expected_dimension:
        raise ConfigError("Embedding dimension does not match configured model", "INDEX_MISMATCH")
    missing_credentials = [name for name in config.credential_env_vars if not config.credentials.get(name)]
    if config.langsmith_tracing and not config.credentials.get("LANGSMITH_API_KEY"):
        missing_credentials.append("LANGSMITH_API_KEY")
    if missing_credentials:
        raise ConfigError("Required credential environment variables are missing: " + ", ".join(missing_credentials))
    if not config.search_provider:
        raise ConfigError("SEARCH_PROVIDER must be configured")
    if not config.qdrant_url or not config.qdrant_collection:
        raise ConfigError("QDRANT_URL and a dedicated QDRANT_COLLECTION are required")
    if not config.corpus_domains or (run_input and run_input.domain.value not in config.corpus_domains):
        raise ConfigError("No prepared corpus is declared for the requested domain", "CORPUS_DOMAIN_UNAVAILABLE")
    documents = inspect_corpus(config, run_input)
    corpus_hash = canonical_hash([document.model_dump(mode="json") for document in documents])
    prompt_files = _expand_files(config.prompt_paths, label="Prompt")
    try:
        prompt_hash = canonical_hash([{ "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                                      for path in prompt_files])
    except OSError:
        raise ConfigError("Configured prompt file cannot be read") from None
    font_hash = (font_probe or _font_hash)(config.report_font_path)
    (sqlite_probe or _sqlite_probe)(config.sqlite_path)
    checks = {name: "passed" for name in ("models", "credentials", "corpus", "prompts", "font", "sqlite")}
    index_hash = None
    if require_index:
        try:
            raw = config.index_manifest_path.read_bytes()
            manifest = json.loads(raw)
        except (OSError, ValueError):
            raise ConfigError("A valid index manifest is required; ingest the configured corpus first", "INDEX_MISMATCH") from None
        expected = {"corpus_hash": corpus_hash, "embedding_model": control.embedding_model,
                    "embedding_revision": control.embedding_revision, "embedding_dimension": control.embedding_dimension,
                    "collection": config.qdrant_collection, "index_version": control.index_version}
        if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in expected.items()):
            raise ConfigError("Index manifest does not match corpus, model, revision, dimension, collection or version", "INDEX_MISMATCH")
        if manifest.get("normalized") is not True or manifest.get("status") != "completed":
            raise ConfigError("Index manifest must record completed normalized dense embeddings", "INDEX_MISMATCH")
        index_hash = hashlib.sha256(raw).hexdigest()
        checks["index"] = "passed"
    else:
        checks["index"] = "not_required_for_ingest"
    if check_network:
        if not require_index and qdrant_probe is None:
            # Collection does not yet exist. Verify server access without creating it.
            try:
                request = urllib.request.Request(config.qdrant_url.rstrip("/") + "/collections", headers={
                    "api-key": config.credentials["QDRANT_API_KEY"]} if config.credentials.get("QDRANT_API_KEY") else {})
                class NoRedirect(urllib.request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None
                with urllib.request.build_opener(NoRedirect).open(request, timeout=control.tool_timeout_seconds) as response:
                    existing = json.load(response)["result"]["collections"]
                if any(item.get("name") == config.qdrant_collection for item in existing):
                    raise ConfigError("Ingest requires a new collection; choose an unused collection name", "INDEX_MISMATCH")
            except ConfigError:
                raise
            except Exception:
                raise ConfigError("Qdrant server is inaccessible") from None
        else:
            observed = (qdrant_probe or _qdrant_probe)(config)
            if require_index and observed.get("dimension") != control.embedding_dimension:
                raise ConfigError("Live Qdrant vector dimension differs from manifest", "INDEX_MISMATCH")
        checks["qdrant"] = "passed"
    else:
        checks["qdrant"] = "network_skipped"
    updated_control = control.model_copy(update={"corpus_hash": corpus_hash, "prompt_hash": prompt_hash, "config_hash": None})
    prepared = replace(config, run_config=updated_control, index_manifest_hash=index_hash, font_hash=font_hash)
    config_hash = canonical_hash(prepared.snapshot())
    prepared = replace(prepared, run_config=updated_control.model_copy(update={"config_hash": config_hash}))
    # Match exactly the versions initialized by the shared Supervisor.
    versions = {"schema": prepared.run_config.schema_version, "corpus": corpus_hash,
                "index": prepared.run_config.index_version, "config": canonical_hash(prepared.run_config.model_dump(mode="json")),
                "prompt": prompt_hash, "policy": prepared.run_config.policy_version,
                "model": canonical_hash({key: getattr(control, key) for key in _MODEL_FIELDS})}
    return PreflightReport(prepared, documents, checks, versions)
