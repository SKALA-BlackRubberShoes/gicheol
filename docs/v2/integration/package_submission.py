"""Build the SDD submission ZIP from explicitly supplied live evidence.

This is STRUCTURAL packaging validation, not proof of actual live execution.
An operator must review LangSmith and confirm screenshot/metadata authenticity.
No credentials, corpus files, directory glob, or automatic sending are involved.
Fixture runs do not satisfy AT13.

Trace metadata contains run_id, report_revision, trace_id, mock=false, and paths.
Paths is exactly three records (scenario=normal/research_retry/quality_repair),
each with the same run_id/report_revision/trace_id, mock=false, trace_url, and
png_ref (shared ArtifactRef JSON). They describe those paths in the selected
run's trace. The helper cannot infer execution paths from screenshot pixels.
"""
from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, urlsplit, unquote
from zipfile import ZIP_DEFLATED, ZipFile

from main.agents.supervisor.contracts import ArtifactRef, Manifest, canonical_json
from main.storage.artifacts import ArtifactIntegrityError, FileArtifactStore


class SubmissionNotReady(ValueError):
    pass


SCENARIOS = {"normal", "research_retry", "quality_repair"}


def _read_file(path: Path, *, label: str) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SubmissionNotReady(f"{label} must be an existing regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _require_url(value: str, *, kind: str, trace_id: str = "") -> None:
    if not isinstance(value, str):
        raise SubmissionNotReady(f"{kind} URL must be a string")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None:
        raise SubmissionNotReady(f"{kind} must be an HTTPS URL without embedded credentials")
    path = unquote(parsed.path)
    if kind == "branch":
        if parsed.hostname != "github.com" or parsed.query or parsed.fragment or not re.fullmatch(r"/[^/]+/[^/]+/tree/.+", path):
            raise SubmissionNotReady("branch_url must identify a GitHub branch, not a repository home")
    else:
        query = parse_qs(parsed.query)
        ids = {item for name in ("trace_id", "run_id", "run") for item in query.get(name, [])}
        if parsed.hostname != "smith.langchain.com" or trace_id not in {*path.split("/"), *ids}:
            raise SubmissionNotReady("trace URL must identify the same LangSmith trace_id")


def _check_png(data: bytes) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SubmissionNotReady("trace image is not a PNG")
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as picture:
            if picture.format != "PNG" or picture.width < 1 or picture.height < 1:
                raise ValueError("not a PNG image")
            picture.verify()
    except Exception as exc:
        raise SubmissionNotReady("trace PNG cannot be decoded") from exc


def _write_archive(output_path: Path, files: dict[str, bytes]) -> Path:
    if not re.fullmatch(r"Agent_[^_/\\]+_[^_/\\]+반_[^_/\\]+\.zip", output_path.name):
        raise SubmissionNotReady("ZIP name must be Agent_{campus}_{class}반_{names}.zip")
    if any(ord(char) < 32 for char in output_path.name):
        raise SubmissionNotReady("ZIP filename contains control characters")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() or output_path.is_symlink():
        raise SubmissionNotReady("output already exists; choose a new explicit ZIP filename")
    descriptor, temporary = tempfile.mkstemp(prefix=".submission-", suffix=".zip", dir=output_path.parent)
    os.close(descriptor)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for name, data in sorted(files.items()):
                archive.writestr(name, data)
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        # Never replace another producer's file if it appeared after validation.
        os.link(temporary, output_path)
        directory = os.open(output_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)
    return output_path


async def package_submission(*, run_root: str | Path, run_id: str,
                             report_revision: int, branch_url: str,
                             readme_path: str | Path, trace_evidence_path: str | Path,
                             output_path: str | Path) -> Path:
    """Refuse incomplete/mock/mismatched evidence before creating any ZIP."""
    _require_url(branch_url, kind="branch")
    readme = Path(readme_path)
    if readme.name.lower() != "readme.md":
        raise SubmissionNotReady("readme_path must name README.md")
    readme_bytes = await asyncio.to_thread(_read_file, readme, label="README")
    if not readme_bytes.strip():
        raise SubmissionNotReady("README is empty")
    store = FileArtifactStore(run_root, run_id)
    manifest_ref = await store.find_manifest(run_id, report_revision)
    if manifest_ref is None:
        raise SubmissionNotReady("no integrity-verified completed manifest for this report key")
    manifest = Manifest.model_validate(await store.load(manifest_ref))
    try:
        status_bytes = await asyncio.to_thread(store._read, "trace_status.json")
        trace_status = json.loads(status_bytes)
    except (ArtifactIntegrityError, ValueError, UnicodeError) as exc:
        raise SubmissionNotReady("runtime trace_status.json is missing or invalid") from exc
    status_fields = {"run_id", "trace_id", "enabled", "mock", "root_created", "trace_delivery", "delivery_error_count"}
    if not isinstance(trace_status, dict) or set(trace_status) != status_fields or (
        trace_status.get("run_id"), trace_status.get("trace_id")
    ) != (manifest.run_id, manifest.trace_id):
        raise SubmissionNotReady("runtime trace status differs from the committed run/trace identity")
    if (trace_status.get("mock") is not False or trace_status.get("enabled") is not True
        or trace_status.get("root_created") is not True
        or trace_status.get("trace_delivery") != "delivered"
        or type(trace_status.get("delivery_error_count")) is not int
        or trace_status["delivery_error_count"] != 0):
        raise SubmissionNotReady("runtime trace delivery is mock, disabled, incomplete, or failed")
    status_ref = store._ref("trace_status.json", status_bytes)
    raw = await asyncio.to_thread(_read_file, Path(trace_evidence_path), label="trace evidence metadata")
    try:
        evidence = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise SubmissionNotReady("trace evidence metadata is not JSON") from exc
    if not isinstance(evidence, dict) or set(evidence) != {"run_id", "report_revision", "trace_id", "mock", "paths"}:
        raise SubmissionNotReady("trace evidence metadata has missing or unexpected fields")
    identity = (manifest.run_id, manifest.report_revision, manifest.trace_id)
    if evidence["mock"] is not False:
        raise SubmissionNotReady("mock trace evidence cannot satisfy AT13")
    if (evidence["run_id"], evidence["report_revision"], evidence["trace_id"]) != identity:
        raise SubmissionNotReady("trace metadata does not match the committed manifest")
    if not isinstance(evidence["paths"], list) or len(evidence["paths"]) != 3:
        raise SubmissionNotReady("normal, research_retry, and quality_repair evidence are all required")
    files = {"README.md": readme_bytes, "branch.txt": (branch_url + "\n").encode(),
             "trace_status.json": status_bytes}
    provenance = {"trace_status.json": status_ref.model_dump(mode="json")}
    seen = set()
    for item in evidence["paths"]:
        expected = {"scenario", "run_id", "report_revision", "trace_id", "trace_url", "mock", "png_ref"}
        if not isinstance(item, dict) or set(item) != expected:
            raise SubmissionNotReady("trace path metadata has missing or unexpected fields")
        scenario = item["scenario"]
        if scenario not in SCENARIOS or scenario in seen:
            raise SubmissionNotReady("trace paths must identify each required scenario exactly once")
        seen.add(scenario)
        if item["mock"] is not False:
            raise SubmissionNotReady("mock trace path cannot satisfy AT13")
        if (item["run_id"], item["report_revision"], item["trace_id"]) != identity:
            raise SubmissionNotReady("trace path is bound to another run, report revision, or trace")
        _require_url(item["trace_url"], kind="trace", trace_id=manifest.trace_id)
        ref = ArtifactRef.model_validate(item["png_ref"])
        if ref.media_type != "image/png":
            raise SubmissionNotReady("trace screenshot reference must have image/png media type")
        data = await asyncio.to_thread(store._verify, ref)
        await asyncio.to_thread(_check_png, data)
        archive_name = f"traces/{scenario}.png"
        files[archive_name] = data
        provenance[archive_name] = ref.model_dump(mode="json")
    # Explicit allowlist: never copy arbitrary manifest.artifacts or raw sources.
    for name, ref in (("manifest.json", manifest_ref), ("report.pdf", manifest.report_pdf_ref),
                      ("report.md", manifest.artifacts["report_md"]),
                      ("report.json", manifest.artifacts["report_json"]),
                      ("evaluation.json", manifest.artifacts["evaluation_json"])):
        files[name] = await asyncio.to_thread(store._verify, ref)
        provenance[name] = ref.model_dump(mode="json")
    files["trace-evidence.json"] = canonical_json(evidence).encode()
    files["submission-index.json"] = canonical_json({
        "run_id": run_id, "report_revision": report_revision, "trace_id": manifest.trace_id,
        "branch_url": branch_url, "artifacts": provenance,
        "validation_scope": "Local structure and integrity only; an operator must verify authentic live execution in LangSmith.",
    }).encode()
    return await asyncio.to_thread(_write_archive, Path(output_path), files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--report-revision", required=True, type=int)
    parser.add_argument("--branch-url", required=True)
    parser.add_argument("--readme", required=True)
    parser.add_argument("--trace-evidence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        path = asyncio.run(package_submission(run_root=args.run_root, run_id=args.run_id,
            report_revision=args.report_revision, branch_url=args.branch_url,
            readme_path=args.readme, trace_evidence_path=args.trace_evidence, output_path=args.output))
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Submission is not ready: {exc}\n")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
