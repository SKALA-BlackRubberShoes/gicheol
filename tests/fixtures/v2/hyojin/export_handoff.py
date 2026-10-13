"""Regenerate quality-boundary JSON fixtures without network or shared writes."""

import asyncio
import importlib.util
import json
from pathlib import Path

from main.agents.quality.rubrics import record

spec = importlib.util.spec_from_file_location("hyojin_export_cases", Path(__file__).with_name("cases.py"))
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


async def export():
    case = f.fixture()
    _, assessor, evaluator, stored = f.services(case)
    sufficient = [record(v) for v in await assessor(f.sufficiency_request(case))]
    good = record(await evaluator(f.evaluator_request(case)))
    judge = f.FixtureJudge()
    for target in ("market", "domain_application"):
        judge.findings.append({"criterion": "groundedness", "severity": "critical", "section_id": target,
                               "claim_id": None, "evidence_ids": [], "reason": f"Verify additional {target} scope",
                               "repair_kind": "research", "target_perspective": target})
    judge.findings.append({"criterion": "neutrality", "severity": "critical", "section_id": "SUMMARY",
                           "claim_id": None, "evidence_ids": [], "reason": "Remove persuasive expression",
                           "repair_kind": "rewrite", "target_perspective": None})
    _, _, evaluator, _ = f.services(case, report_judge=judge)
    multiple = record(await evaluator(f.evaluator_request(case)))
    recommendation = f.fixture()
    recommendation["report"]["sections"][0]["paragraphs"].append({"paragraph_id": "P-recommendation",
        "text": "투자 추천을 합니다.", "kind": "connective", "claim_ids": [], "evidence_ids": []})
    f.rehash(recommendation)
    _, _, evaluator, _ = f.services(recommendation)
    rewrite = record(await evaluator(f.evaluator_request(recommendation)))
    old_report = case["report"]["content_hash"]
    case["report"]["revision"] += 1
    case["report"]["evidence_epoch"] += 1
    case["report"]["sections"][6]["paragraphs"][0]["text"] += " Scale beyond the pilot remains unknown."
    f.rehash(case)
    _, _, evaluator, _ = f.services(case)
    new = record(await evaluator(f.evaluator_request(case)))
    output = {"fixture_only": True, "contract_commit": "2fb927db25d02e1bda5bdade02a9e25977a3b289",
              "scope": "Quality-boundary DTOs; no Supervisor, live Judge, RAG or publish run",
              "sufficiency_request": f.sufficiency_request(case), "claim_supports": stored,
              "sufficiency_verdicts": sufficient, "passing_evaluation": good,
              "multiple_research_and_rewrite_findings": multiple,
              "rewrite_only_evaluation": rewrite,
              "report_binding_change": {"old_report_hash": old_report, "old_evaluation": good,
                                        "new_report": case["report"], "new_evaluation": new}}
    path = Path(__file__).with_name("handoff_cases.json")
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {path}")


if __name__ == "__main__":
    asyncio.run(export())
