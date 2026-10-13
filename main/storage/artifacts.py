"""Durable, run-scoped filesystem implementation of the shared store contract.

All blocking filesystem work runs in a worker thread. File descriptors walk
directories with O_NOFOLLOW, so a substituted symlink cannot escape the root.
The small integrity records are written before their publication marker; the
completed manifest is the final atomic rename in a publication transaction.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from main.agents.supervisor.contracts import (
    ArtifactRef, DraftReport, EvalVerdict, GateCertificate, Manifest,
    ResultEnvelope, TaskEnvelope, canonical_json,
)
from main.graph.supervisor_state import StateIntegrityError


class ArtifactIntegrityError(StateIntegrityError):
    """Missing, modified, unsafe, or inconsistent persisted data."""


class FileArtifactStore:
    def __init__(self, root: str | Path, run_id: str | None = None):
        candidate = Path(root).expanduser().absolute()
        if candidate.is_symlink():
            raise ArtifactIntegrityError("artifact root cannot be a symlink")
        candidate.mkdir(parents=True, exist_ok=True)
        self.root = candidate.resolve(strict=True)
        self.run_id = run_id

    @staticmethod
    def _ref(key: str, data: bytes, media_type: str = "application/json") -> ArtifactRef:
        return ArtifactRef(relative_path=key, sha256=hashlib.sha256(data).hexdigest(),
                           media_type=media_type)

    @contextmanager
    def _parent(self, key: str, *, create: bool = False):
        # Reuse the shared DTO's exact path grammar even for not-yet-written files.
        ArtifactRef(relative_path=key, sha256="0" * 64, media_type="application/json")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(self.root, flags)
        try:
            for component in key.split("/")[:-1]:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                        os.fsync(descriptor)
                    except FileExistsError:
                        pass
                child = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            yield descriptor, key.split("/")[-1]
        except (OSError, ValueError) as exc:
            if isinstance(exc, ArtifactIntegrityError):
                raise
            raise ArtifactIntegrityError(f"unsafe or unavailable artifact path: {key}") from exc
        finally:
            os.close(descriptor)

    @contextmanager
    def _transaction(self):
        with self._parent(".storage/lock", create=True) as (directory, name):
            descriptor = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _read(self, key: str) -> bytes:
        with self._parent(key) as (directory, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ArtifactIntegrityError("artifact must be a regular file")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    return stream.read()
            finally:
                os.close(descriptor)

    def _exists(self, key: str) -> bool:
        # A dangling symlink is an integrity error rather than a missing result.
        try:
            with self._parent(key) as (directory, name):
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    return False
                if not stat.S_ISREG(info.st_mode):
                    raise ArtifactIntegrityError("artifact must be a regular file")
                return True
        except ArtifactIntegrityError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return False
            raise

    def _write(self, key: str, data: bytes, media_type: str = "application/json") -> ArtifactRef:
        ref = self._ref(key, data, media_type)
        with self._parent(key, create=True) as (directory, name):
            temporary = f".{name}.{uuid4().hex}.tmp"
            try:
                try:
                    existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    existing = None
                if existing is not None and not stat.S_ISREG(existing.st_mode):
                    raise ArtifactIntegrityError("refusing to replace a symlink or nonregular artifact")
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return ref

    def _verify(self, ref: ArtifactRef) -> bytes:
        ref = ArtifactRef.model_validate(ref.model_dump(mode="json"))
        data = self._read(ref.relative_path)
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError(f"artifact SHA-256 mismatch: {ref.relative_path}")
        return data

    def _load(self, ref: ArtifactRef) -> dict[str, Any]:
        if ref.media_type != "application/json" and not ref.media_type.endswith("+json"):
            raise ArtifactIntegrityError("load only accepts JSON artifacts; use verify for binary media")
        try:
            payload = json.loads(self._verify(ref), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ArtifactIntegrityError("artifact is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ArtifactIntegrityError("JSON artifact must contain an object")
        return payload

    def _verify_embedded_refs(self, value: Any, visited: set[tuple[str, str]] | None = None) -> None:
        """Check referenced inputs/results as well as the enclosing JSON bytes."""
        visited = visited if visited is not None else set()
        if isinstance(value, dict):
            if {"relative_path", "sha256", "media_type"} <= value.keys():
                ref = ArtifactRef.model_validate(value)
                identity = (ref.relative_path, ref.sha256)
                if identity not in visited:
                    visited.add(identity)
                    if ref.media_type == "application/json" or ref.media_type.endswith("+json"):
                        self._verify_embedded_refs(self._load(ref), visited)
                    else:
                        self._verify(ref)
            else:
                for item in value.values():
                    self._verify_embedded_refs(item, visited)
        elif isinstance(value, list):
            for item in value:
                self._verify_embedded_refs(item, visited)

    async def load(self, ref: ArtifactRef) -> dict[str, Any]:
        return await asyncio.to_thread(self._load, ref)

    async def verify(self, ref: ArtifactRef) -> None:
        await asyncio.to_thread(self._verify, ref)

    async def save(self, payload: dict[str, Any] | BaseModel, *, key: str,
                   media_type: str = "application/json") -> ArtifactRef:
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        if not isinstance(payload, dict):
            raise TypeError("save requires a JSON object; use save_bytes for binary data")
        return await self.save_bytes(canonical_json(payload).encode("utf-8"), key=key, media_type=media_type)

    async def save_bytes(self, data: bytes, *, key: str,
                         media_type: str = "application/octet-stream") -> ArtifactRef:
        if key == "manifest.json" or key.startswith(".storage/") or key.startswith("receipts/"):
            raise ArtifactIntegrityError("reserved artifact key requires its commit operation")
        if not isinstance(data, bytes):
            raise TypeError("save_bytes requires bytes")
        def save():
            if key.startswith("snapshots/"):
                with self._transaction():
                    if self._exists(key):
                        ref = self._ref(key, data, media_type)
                        self._verify(ref)
                        return ref
                    return self._write(key, data, media_type)
            return self._write(key, data, media_type)
        return await asyncio.to_thread(save)

    async def save_blob(self, data: bytes, key: str, media_type: str) -> ArtifactRef:
        return await self.save_bytes(data, key=key, media_type=media_type)

    async def find_receipt(self, task_id: str, input_hash: str) -> ResultEnvelope | None:
        from .receipts import ReceiptStore
        return await asyncio.to_thread(ReceiptStore(self).find, task_id, input_hash)

    async def save_receipt(self, task: TaskEnvelope, result: ResultEnvelope) -> None:
        from .receipts import ReceiptStore
        await asyncio.to_thread(ReceiptStore(self).save, task, result)

    def _validate_manifest(self, manifest: Manifest) -> None:
        if self.run_id is not None and manifest.run_id != self.run_id:
            raise ArtifactIntegrityError("manifest run_id differs from this store")
        gate = GateCertificate.model_validate(self._load(manifest.gate_ref))
        draft = DraftReport.model_validate(self._load(manifest.draft_ref))
        evaluation = EvalVerdict.model_validate(self._load(manifest.evaluation_ref))
        if not gate.passed or not evaluation.overall_pass or not manifest.pdf_validation_pass:
            raise ArtifactIntegrityError("publication requires passing gate, evaluation, and PDF checks")
        if (draft.revision, draft.content_hash, draft.evidence_epoch) != (
            manifest.report_revision, manifest.report_hash, manifest.evidence_epoch
        ) or (evaluation.report_revision, evaluation.report_hash, evaluation.evidence_epoch) != (
            draft.revision, draft.content_hash, draft.evidence_epoch
        ):
            raise ArtifactIntegrityError("manifest, draft, and evaluation bindings differ")
        if (gate.evidence_epoch, gate.eligibility_hash, gate.perspective_hashes) != (
            manifest.evidence_epoch, manifest.eligibility_hash, manifest.perspective_hashes
        ) or gate.rubric_version != evaluation.rubric_version:
            raise ArtifactIntegrityError("manifest evidence gate is stale or inconsistent")
        # A gate can bind sources/supports through its reason artifact. Verify
        # the entire referenced chain on both initial commit and every reopen,
        # so a still-valid outer JSON hash cannot conceal changed source bytes.
        self._verify_embedded_refs(manifest.model_dump(mode="json"))
        data = self._verify(manifest.report_pdf_ref)
        try:
            from pypdf import PdfReader
            document = PdfReader(BytesIO(data), strict=True)
            if document.is_encrypted or not 1 <= len(document.pages) <= 10:
                raise ValueError("PDF must be unencrypted and contain 1 to 10 pages")
        except Exception as exc:
            raise ArtifactIntegrityError("committed report is not a valid 1-to-10-page PDF") from exc

    def _find_manifest(self, run_id: str, report_revision: int) -> ArtifactRef | None:
        if self.run_id is not None and run_id != self.run_id:
            return None
        if not self._exists("manifest.json"):
            return None
        # The anchor precedes the final manifest rename. It is never sufficient
        # to authorize publication on its own after an interrupted commit.
        try:
            anchor = json.loads(self._read(".storage/manifest-ref.json"))
            ref = ArtifactRef.model_validate(anchor)
            if ref.relative_path != "manifest.json":
                raise ValueError("invalid manifest anchor")
            manifest = Manifest.model_validate(self._load(ref))
        except (ValueError, TypeError, KeyError) as exc:
            raise ArtifactIntegrityError("invalid completed manifest or integrity anchor") from exc
        self._validate_manifest(manifest)
        if (manifest.run_id, manifest.report_revision) != (run_id, report_revision):
            return None
        return ref

    async def find_manifest(self, run_id: str, report_revision: int) -> ArtifactRef | None:
        def find():
            with self._transaction():
                return self._find_manifest(run_id, report_revision)
        return await asyncio.to_thread(find)

    def _commit_manifest(self, manifest: Manifest) -> ArtifactRef:
        manifest = Manifest.model_validate(manifest.model_dump(mode="json"))
        with self._transaction():
            if self._exists("manifest.json"):
                existing = self._find_manifest(manifest.run_id, manifest.report_revision)
                if existing is None:
                    raise ArtifactIntegrityError("this run already has a different completed report key")
                previous = Manifest.model_validate(self._load(existing))
                if previous.model_dump(mode="json", exclude={"committed_at"}) != manifest.model_dump(mode="json", exclude={"committed_at"}):
                    raise ArtifactIntegrityError("completed report key cannot be overwritten with different artifacts")
                return existing
            self._validate_manifest(manifest)
            data = canonical_json(manifest).encode("utf-8")
            ref = self._ref("manifest.json", data)
            self._write(".storage/manifest-ref.json", canonical_json(ref).encode("utf-8"))
            # Publication's sole completion marker is always written LAST.
            return self._write("manifest.json", data)

    async def commit_manifest(self, manifest: Manifest) -> ArtifactRef:
        return await asyncio.to_thread(self._commit_manifest, manifest)

    async def approved_report_path(self, run_id: str, report_revision: int) -> Path | None:
        ref = await self.find_manifest(run_id, report_revision)
        if ref is None:
            return None
        manifest = Manifest.model_validate(await self.load(ref))
        await self.verify(manifest.report_pdf_ref)
        return self.root / manifest.report_pdf_ref.relative_path


FileSystemArtifactStore = FileArtifactStore
ArtifactStore = FileArtifactStore
