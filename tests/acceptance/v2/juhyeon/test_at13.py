"""Submission rejection checks; these fixtures are NOT live AT13 evidence."""
import asyncio
from io import BytesIO
import json
from pathlib import Path
import sys
from zipfile import ZipFile

from PIL import Image
import pytest

from docs.v2.integration.package_submission import SubmissionNotReady, _write_archive, package_submission
from main.storage.artifacts import ArtifactIntegrityError, FileArtifactStore

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "unit/v2/juhyeon"))
from test_storage_artifacts import make_manifest


async def negative_fixture(root):
    store = FileArtifactStore(root / "run", "run-1")
    manifest = await make_manifest(store)
    await store.commit_manifest(manifest)
    # Synthetic runtime status solely for negative structural validation tests;
    # no test creates or claims authentic delivered LangSmith evidence.
    await store.save(dict(run_id=manifest.run_id, trace_id=manifest.trace_id,
        enabled=True, mock=False, root_created=True, trace_delivery="delivered",
        delivery_error_count=0), key="trace_status.json")
    readme = root / "README.md"
    readme.write_text("# Explicitly synthetic packaging test; not live evidence.\n")
    image = BytesIO()
    Image.new("RGB", (2, 2), "white").save(image, format="PNG")
    ref = await store.save_bytes(image.getvalue(), key="traces/synthetic.png", media_type="image/png")
    # Every test below injects a fault; this object is never packaged as live proof.
    evidence = dict(run_id=manifest.run_id, report_revision=manifest.report_revision,
        trace_id=manifest.trace_id, mock=False, paths=[dict(scenario=scenario,
            run_id=manifest.run_id, report_revision=manifest.report_revision,
            trace_id=manifest.trace_id, mock=False,
            trace_url=f"https://smith.langchain.com/r/{manifest.trace_id}",
            png_ref=ref.model_dump(mode="json"))
            for scenario in ("normal", "research_retry", "quality_repair")])
    arguments = dict(run_root=store.root, run_id=manifest.run_id, report_revision=manifest.report_revision,
        branch_url="https://github.com/SKALA-BlackRubberShoes/gicheol/tree/feature/V2/juhyeon/runtime-integration",
        readme_path=readme, trace_evidence_path=root / "trace-evidence.json",
        output_path=root / "Agent_서울_1반_주현.zip")
    return store, manifest, evidence, arguments


@pytest.mark.parametrize("fault", ["missing_manifest", "mock", "path_mock", "run", "revision", "trace",
    "path_run", "missing_path", "duplicate_path", "wrong_trace_url", "png_magic", "png_hash",
    "missing_readme", "repository_url", "extra_secret_field", "status_missing", "status_mock",
    "status_disabled", "status_root_missing", "status_failed", "status_errors", "status_run", "status_trace"])
def test_at13_refuses_missing_mock_or_mismatched_submission_evidence(tmp_path, fault):
    async def scenario():
        store, manifest, evidence, arguments = await negative_fixture(tmp_path)
        if fault == "missing_manifest":
            (store.root / "manifest.json").unlink()
        elif fault == "mock":
            evidence["mock"] = True
        elif fault == "path_mock":
            evidence["paths"][0]["mock"] = True
        elif fault == "run":
            evidence["run_id"] = "different-run"
        elif fault == "revision":
            evidence["report_revision"] = 2
        elif fault == "trace":
            evidence["trace_id"] = "different-trace"
        elif fault == "path_run":
            evidence["paths"][1]["run_id"] = "different-run"
        elif fault == "missing_path":
            evidence["paths"].pop()
        elif fault == "duplicate_path":
            evidence["paths"][2]["scenario"] = "normal"
        elif fault == "wrong_trace_url":
            evidence["paths"][0]["trace_url"] = "https://smith.langchain.com/r/another-trace"
        elif fault == "png_magic":
            bad = await store.save_bytes(b"not a PNG", key="traces/invalid.png", media_type="image/png")
            evidence["paths"][0]["png_ref"] = bad.model_dump(mode="json")
        elif fault == "png_hash":
            (store.root / "traces/synthetic.png").write_bytes(b"changed")
        elif fault == "missing_readme":
            arguments["readme_path"].unlink()
        elif fault == "repository_url":
            arguments["branch_url"] = "https://github.com/SKALA-BlackRubberShoes/gicheol"
        elif fault == "extra_secret_field":
            evidence["credentials"] = "must never be included"
        elif fault == "status_missing":
            (store.root / "trace_status.json").unlink()
        elif fault.startswith("status_"):
            status = json.loads((store.root / "trace_status.json").read_text())
            field, value = {
                "status_mock": ("mock", True), "status_disabled": ("enabled", False),
                "status_root_missing": ("root_created", False), "status_failed": ("trace_delivery", "failed"),
                "status_errors": ("delivery_error_count", 1), "status_run": ("run_id", "other-run"),
                "status_trace": ("trace_id", "other-trace"),
            }[fault]
            status[field] = value
            await store.save(status, key="trace_status.json")
        arguments["trace_evidence_path"].write_text(json.dumps(evidence))
        with pytest.raises((SubmissionNotReady, ArtifactIntegrityError)):
            await package_submission(**arguments)
        assert not arguments["output_path"].exists()
        assert not list(tmp_path.glob(".submission-*"))
    asyncio.run(scenario())


def test_zip_writer_only_includes_allowlisted_bytes_without_directory_scan(tmp_path):
    (tmp_path / ".env").write_text("SECRET=must-not-copy")
    (tmp_path / "corpus.pdf").write_bytes(b"raw corpus must not copy")
    path = tmp_path / "Agent_서울_1반_주현+동욱.zip"
    _write_archive(path, {"README.md": b"isolated archive writer fixture", "branch.txt": b"branch"})
    with ZipFile(path) as archive:
        assert set(archive.namelist()) == {"README.md", "branch.txt"}
        assert archive.testzip() is None
    previous = path.read_bytes()
    with pytest.raises(SubmissionNotReady, match="already exists"):
        _write_archive(path, {"README.md": b"replacement"})
    assert path.read_bytes() == previous


@pytest.mark.parametrize("name", ["submission.zip", "Agent_서울_1_주현.zip", "Agent_서울_1반_주현.zip\n", ".env"])
def test_zip_writer_enforces_explicit_sdd_filename(tmp_path, name):
    with pytest.raises(SubmissionNotReady):
        _write_archive(tmp_path / name, {})
