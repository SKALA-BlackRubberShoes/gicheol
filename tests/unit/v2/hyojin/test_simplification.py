"""Shared-contract quality APIs and safe reuse, using a deterministic provider."""
import importlib.util
from pathlib import Path

import pytest

from main.agents.quality.claim_support import ClaimSupportVerifier
from main.agents.quality.rubrics import LLMClientJudge, record
from main.agents.supervisor import contracts as c

spec = importlib.util.spec_from_file_location(
    "simplification_cases", Path(__file__).resolve().parents[3] / "fixtures/v2/hyojin/cases.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


class Provider:
    def __init__(self):
        self.calls = 0
        self.status = "supported"

    async def generate(self, *, model, system_prompt, payload, response_model):
        self.calls += 1
        return response_model.model_validate({
            "support_status": self.status, "reason": "Explicit deterministic provider",
            "supported_evidence_ids": payload["claim"]["evidence_ids"] if self.status == "supported" else []})


def verifier_and_case():
    case = f.fixture()
    provider = Provider()

    async def original(source, evidence):
        body = case["bodies"][source["source_id"]]
        if c.sha256_text(body) != source["sha256"]:
            raise ValueError("Original source bytes changed")
        return body

    verifier = ClaimSupportVerifier(
        judge=LLMClientJudge(client=provider, model="fixture-judge"), excerpt_loader=original)
    return verifier, provider, case


async def verify(verifier, case, **kwargs):
    return await verifier.verify(case["claims"][0], evidence=case["evidence"],
        sources=case["sources"], company_id="alpha", as_of_date=kwargs.get("as_of_date", "2026-10-13"))


@pytest.mark.asyncio
async def test_verifier_returns_shared_dto_without_contract_factory():
    verifier, provider, case = verifier_and_case()
    result = await verify(verifier, case)
    assert isinstance(result, c.ClaimSupport)
    assert result.support_status == "supported"
    assert result.claim_id == "C-technology_maturity-0"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_unchanged_verified_claim_reuses_judgement():
    verifier, provider, case = verifier_and_case()
    first = await verify(verifier, case)
    second = await verify(verifier, case)
    assert record(first) == record(second)
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["claim", "source", "date", "model", "version"])
async def test_changed_verification_binding_requires_new_judgement(change):
    verifier, provider, case = verifier_and_case()
    await verify(verifier, case)
    options = {}
    if change == "claim":
        case["claims"][0]["limitations"] = ["Additional scope restriction"]
    elif change == "source":
        case["sources"][0]["title"] = "Updated original source provenance"
    elif change == "date":
        options["as_of_date"] = "2026-10-14"
    elif change == "model":
        verifier.judge.model = "fixture-other-model"
    else:
        verifier.verifier_version = "claim-support-2"
    result = await verify(verifier, case, **options)
    assert result.support_status == "supported"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_changed_original_bytes_reject_cached_support():
    verifier, provider, case = verifier_and_case()
    await verify(verifier, case)
    case["bodies"]["S-company"] += "\nUnexpected source edit"
    result = await verify(verifier, case)
    assert result.support_status == "unverified"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_prompt_change_requires_new_judgement(monkeypatch):
    verifier, provider, case = verifier_and_case()
    await verify(verifier, case)
    monkeypatch.setattr("main.agents.quality.claim_support.prompt", lambda name: "Changed verifier prompt")
    assert (await verify(verifier, case)).support_status == "supported"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_failed_judgement_is_not_reused():
    verifier, provider, case = verifier_and_case()
    provider.status = "unsupported"
    assert (await verify(verifier, case)).support_status == "unsupported"
    provider.status = "supported"
    assert (await verify(verifier, case)).support_status == "supported"
    assert provider.calls == 2
