"""Deterministic approved Korean corpus; verifier and storage are test doubles."""
import hashlib
import os
from datetime import date, datetime, timezone
from pathlib import Path

from main.agents.supervisor import contracts as c

from main.agents.report.renderers import _FONT_CANDIDATES

FONT = os.environ.get("REPORT_FONT_PATH") or next((p for p in _FONT_CANDIDATES if Path(p).is_file()), "/missing/korean-font.ttf")
SOURCE_BYTES = b"Verified Korean report source fixture"
SOURCE_HASH = hashlib.sha256(SOURCE_BYTES).hexdigest()


def writer_request(*, long_text=0):
    source_ref = c.ArtifactRef(relative_path="sources/original.txt", sha256=SOURCE_HASH, media_type="text/plain")
    sources = [c.Source(source_id=f"source-{i}", canonical_url=f"https://example.org/source-{i}",
        title=f"로봇 기업의 검증 자료 {i}", publisher=f"검증 기관 {i}", publisher_group=f"group-{i}",
        source_type="institution", published_at=date(2026, 1, 1) if i else None,
        retrieved_at=datetime(2026, 10, 13, tzinfo=timezone.utc), corporate_controlled=False,
        sha256=SOURCE_HASH, content_ref=source_ref) for i in range(3)]
    evidence, results, supports = [], {}, []
    texts = {
        c.Perspective.TECHNOLOGY_MATURITY: ["로봇은 카메라와 센서 데이터를 결합한다.", "실험실 환경에서 작업 정확도를 검증했다."],
        c.Perspective.MARKET: ["고객은 반복 운반 작업의 자동화를 검토한다.", "도입 비용과 유지보수 조건을 함께 확인해야 한다."],
        c.Perspective.STAKEHOLDERS: ["고객과 현장 작업자가 주요 이해관계자다.", "작업자는 장비 교육과 안전 절차를 이행해야 한다."],
        c.Perspective.DOMAIN_APPLICATION: ["물류 현장에서 운반 작업을 시험했다.", "실제 도입에는 운영 데이터와 안전 검증이 필요하다."],
    }
    for p in c.PERSPECTIVES:
        claims = []
        for i, text in enumerate(texts[p]):
            eid, cid = f"e-{p.value}-{i}", f"c-{p.value}-{i}"
            text += " 검증 범위와 운영 조건을 보존한다." * (long_text if i == 1 else 0)
            ev = c.Evidence(evidence_id=eid, source_id=f"source-{i}", company_id="company-1",
                chunk_id=f"chunk-{p.value}-{i}", page_number=1, quote=text, event_date=date(2026, 1, 1))
            evidence.append(ev)
            claim = c.Claim(claim_id=cid, perspective=p, text=text, kind="fact", evidence_ids=[eid],
                            limitations=["자료의 검증 범위는 제공된 실증 환경으로 한정된다."])
            claims.append(claim)
            supports.append(c.ClaimSupport(claim_id=cid, claim_hash=c.canonical_hash(claim),
                evidence_hashes={eid: c.canonical_hash(ev)}, support_status="supported",
                reason="Deterministic fixture verification", verifier_version="fixture-1"))
        results[p] = c.PerspectiveResult(task_id=f"task-{p.value}", perspective=p, attempt=1,
            claims=claims, evidence_ids=[eid for claim in claims for eid in claim.evidence_ids],
            answered_questions={q: [claims[i].claim_id] for i, q in enumerate(c.REQUIRED_QUESTIONS[p])},
            gaps=[], limitations=["독립된 추가 현장 자료는 확인하지 못했다."], retrieval_receipts=[])
    elig = c.EligibilityResult(task_id="eligibility", unlisted=True, funding_stage=True,
        exit_status=True, verdict="eligible", claims=[], gaps=[])
    gate = c.GateCertificate(evidence_epoch=4, eligibility_hash=elig.result_hash,
        perspective_hashes={p: r.result_hash for p, r in results.items()}, rubric_version="1.0",
        passed=True, reason_ref=source_ref)
    return c.WriterRequest(task_id="writer-1", company_id="company-1", as_of_date=date(2026, 10, 13),
        report_revision=1, evidence_epoch=4, eligibility=elig, perspective_results=results,
        gate=gate, sources=sources, evidence=evidence, claim_supports=supports)


def evaluation(report):
    return c.EvalVerdict(report_revision=report.revision, report_hash=report.content_hash,
        evidence_epoch=report.evidence_epoch, structural_checks={"citations": True},
        criterion_scores={criterion: 3 for criterion in c.Criterion}, findings=[], overall_pass=True,
        repair_kind="none", rubric_version="1.0", judge_model="fixture-judge", prompt_hash="0" * 64)


def publish_request(request, report):
    return c.PublishRequest(task_id="publish-1", run_id="run-1", report=report,
        evaluation=evaluation(report), gate=request.gate, eligibility=request.eligibility,
        perspective_results=request.perspective_results)


class Resolver:
    def __init__(self, request):
        self.request = request

    async def resolve(self, ids):
        evidence = [ev for ev in self.request.evidence if ev.evidence_id in ids]
        source_ids = {ev.source_id for ev in evidence}
        return c.EvidenceBundle(evidence=evidence,
            sources=[s for s in self.request.sources if s.source_id in source_ids])


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.saved = {}
        self.verified = []
        path = self.root / "sources/original.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(SOURCE_BYTES)

    async def verify(self, ref):
        path = self.root / ref.relative_path
        if not path.resolve().is_relative_to(self.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != ref.sha256:
            raise ValueError("artifact integrity failed")
        self.verified.append(ref)

    async def save(self, payload, *, key, media_type="application/json"):
        self.saved[key] = payload
        return c.ArtifactRef(relative_path=key, sha256=c.canonical_hash(payload), media_type=media_type)

    async def commit_manifest(self, manifest):
        raise AssertionError("staging worker must never commit a manifest")
