"""LLM CSV 평가의 근거 검증과 검증 점수 보존을 확인합니다."""

import unittest

from main.agents.common.csv_judgment import (
    COMPETITION_CRITERIA, TECHNOLOGY_CRITERIA, CsvJudgmentScore,
    merge_csv_judgment, validate_csv_judgment,
)
from main.agents.common.evidence import Evidence, as_evidence
from main.rag.company.models import CompanyRecord, SourceRef


def company(company_id: str, *, patents: int | None = None) -> CompanyRecord:
    raw = {
        "대표제품": "피지컬 AI 모델", "서비스": "산업용로봇 AI 모델",
        "기술": "인공지능", "제품 형태": "소프트웨어 / 개발중",
        "특허 수": str(patents) if patents is not None else "NULL",
    }
    return CompanyRecord(
        company_id=company_id, company_name=f"회사{company_id}", raw=raw,
        values={"patent_count": patents}, content="회사 소개",
        source=SourceRef(csv_path="fixture.csv", record_number=2),
    )


def evidence(record: CompanyRecord) -> Evidence:
    return Evidence(id=f"CSV-{record.company_id}", company=record.company_name,
                    title="CSV", locator="fixture.csv#2", text="회사 소개")


def score(name: str, rating: int, ids: list[str], fields: list[str]) -> CsvJudgmentScore:
    return CsvJudgmentScore(criterion=name, rating=rating, rationale="CSV 근거를 해석한 이유",
                            evidence_ids=ids, source_fields=fields)


class CsvJudgmentTests(unittest.TestCase):
    def test_model_evidence_includes_csv_fields_used_for_judgment(self):
        text = as_evidence(company("17", patents=9))["text"]
        self.assertIn("특허 수: 9", text)
        self.assertIn("제품 형태: 소프트웨어 / 개발중", text)

    def test_model_chooses_rating_and_program_only_converts_points(self):
        target = company("17", patents=9)
        scores = [
            score("problem_solution", 3, ["CSV-17"], ["대표제품", "서비스"]),
            score("ai_role", 1, ["CSV-17"], ["기술"]),
            score("performance_validation", 0, [], []),
            score("maturity", 2, ["CSV-17"], ["제품 형태"]),
        ]
        result = validate_csv_judgment(scores, [target], [evidence(target)], TECHNOLOGY_CRITERIA)
        self.assertEqual([item["rating"] for item in result], [3, 1, 0, 2])
        self.assertEqual([item["points"] for item in result], [15, 5, 0, 10])

    def test_missing_field_and_wrong_company_cannot_support_score(self):
        target, competitor = company("17"), company("14")
        scores = [
            score("differentiation", 1, ["CSV-17", "CSV-14"], ["서비스"]),
            score("comparable_performance", 0, [], []),
            score("defensibility", 1, ["CSV-17"], ["특허 수"]),
            score("adoption_risk", 1, ["CSV-17"], ["제품 형태"]),
        ]
        with self.assertRaisesRegex(ValueError, "기재되지 않은 CSV 컬럼"):
            validate_csv_judgment(scores, [target, competitor],
                                  [evidence(target), evidence(competitor)], COMPETITION_CRITERIA)
        scores[2] = score("defensibility", 1, ["CSV-14"], ["제품 형태"])
        with self.assertRaisesRegex(ValueError, "대상 기업 CSV"):
            validate_csv_judgment(scores, [target, competitor],
                                  [evidence(target), evidence(competitor)], COMPETITION_CRITERIA)

    def test_verified_rating_is_preserved_when_csv_model_disagrees(self):
        csv = [
            {"criterion": name, "rating": rating, "points": rating * 5,
             "rationale": "모델 CSV 판단", "evidence_ids": ["CSV-17"]}
            for name, rating in zip(TECHNOLOGY_CRITERIA, (3, 1, 0, 2))
        ]
        verified = {
            "criteria": [
                {"criterion": name, "rating": rating, "points": rating * 5,
                 "rationale": "독립 자료 판단", "evidence_ids": ["DOC-1"] if rating else []}
                for name, rating in zip(TECHNOLOGY_CRITERIA, (0, 4, 0, 0))
            ],
            "total": 20, "max": 100, "status": "insufficient_evidence",
        }
        merged = merge_csv_judgment(verified, csv)
        self.assertEqual(merged["total"], 45)
        self.assertEqual(merged["criteria"][1]["rating"], 4)
        self.assertEqual(merged["verified_score"], verified)
        self.assertEqual(merged["status"], "provisional")

    def test_comparable_performance_requires_same_metric_and_conditions(self):
        target, competitor = company("17"), company("14")
        target.raw.update({"성능 지표": "정확도", "시험 결과": "90%", "시험 조건": "현장 A"})
        competitor.raw.update({"성능 지표": "정확도", "시험 결과": "88%", "시험 조건": "현장 B"})
        scores = [
            score("differentiation", 0, [], []),
            score("comparable_performance", 3, ["CSV-17", "CSV-14"],
                  ["성능 지표", "시험 결과", "시험 조건"]),
            score("defensibility", 0, [], []),
            score("adoption_risk", 0, [], []),
        ]
        result = validate_csv_judgment(scores, [target, competitor],
                                       [evidence(target), evidence(competitor)], COMPETITION_CRITERIA)
        self.assertEqual(result[1]["rating"], 0)
        self.assertIn("동일 지표·시험 조건", result[1]["rationale"])


if __name__ == "__main__":
    unittest.main()
