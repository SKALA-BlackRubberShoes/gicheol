"""경쟁사 후보가 CSV와 검색 색인 사이에서 누락되지 않는지 검증합니다."""

import json

from main.agents.competitor_selection import CompetitorSelectionAgent
from main.rag.company import CompanyRecord, SearchHit


def company(company_id, *, sector="로봇", age=5.0):
    return CompanyRecord(
        company_id=company_id,
        company_name=f"기업 {company_id}",
        raw={},
        values={"sector": sector, "company_age_years": age},
        content=f"기업 {company_id}의 제품과 고객",
        source={"csv_path": "fixture.csv", "record_number": 2},
    )


class PartialSearchRAG:
    """CSV 정확 조회는 유지하고 검색 hit만 지정한 부분집합으로 반환합니다."""

    def __init__(self, records, hit_ids=()):
        self.records = {record.company_id: record for record in records}
        self.hit_ids = hit_ids

    def get_company(self, company_id):
        record = self.records.get(company_id)
        return record.model_copy(deep=True) if record else None

    def list_companies(self, *, filters=None):
        records = sorted(self.records.values(), key=lambda item: item.company_id)
        for condition in filters or []:
            assert (condition.field, condition.op) == ("sector", "eq")
            records = [
                record for record in records
                if record.values[condition.field] == condition.value
            ]
        return [record.model_copy(deep=True) for record in records]

    def retrieve(self, query, n_results=5, *, filters=None, exclude_company_id=None):
        eligible = {record.company_id for record in self.list_companies(filters=filters)}
        return [
            SearchHit(company=self.get_company(company_id), score=1.0 - index / 100)
            for index, company_id in enumerate(self.hit_ids)
            if company_id in eligible and company_id != exclude_company_id
        ][:n_results]


class Selector:
    def __init__(self, selected_ids):
        self.selected_ids = selected_ids
        self.candidate_ids = None

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages, config=None):
        payload = json.loads(messages[1][1])
        self.candidate_ids = [item["company_id"] for item in payload["candidates"]]
        return {"competitor_ids": self.selected_ids}


def test_partial_search_backfills_missing_csv_peers_within_ten_candidate_limit():
    peers = [company(f"{number:02}") for number in range(1, 13)]
    rag = PartialSearchRAG([company("T"), *peers], hit_ids=["12"])
    selector = Selector(["09"])

    result = CompetitorSelectionAgent(rag, selector).select("T")

    assert selector.candidate_ids == ["12", "01", "02", "03", "04", "05", "06", "07", "08", "09"]
    assert result == {"competitor_ids": ["09"], "message": None}


def test_complete_search_keeps_ranked_hit_order():
    rag = PartialSearchRAG(
        [company("T"), company("A"), company("B"), company("C")],
        hit_ids=["C", "A", "B"],
    )
    selector = Selector(["A"])

    result = CompetitorSelectionAgent(rag, selector).select("T")

    assert selector.candidate_ids == ["C", "A", "B"]
    assert result == {"competitor_ids": ["A"], "message": None}


def test_empty_search_still_uses_csv_peers():
    rag = PartialSearchRAG([company("T"), company("A"), company("B")])
    selector = Selector([])

    result = CompetitorSelectionAgent(rag, selector).select("T")

    assert selector.candidate_ids == ["A", "B"]
    assert result["competitor_ids"] == ["A"]
    assert "'로봇' 업종" in result["message"]


def test_missing_sector_uses_nearest_age_with_accurate_message():
    rag = PartialSearchRAG([
        company("T", sector=None, age=4.0),
        company("A", age=4.6),
        company("B", age=4.2),
    ])
    selector = Selector([])

    result = CompetitorSelectionAgent(rag, selector).select("T")

    assert result["competitor_ids"] == ["B"]
    assert "CSV에 업종 정보가 없어" in result["message"]
    assert "대상 업력: 4.0년차" in result["message"]
    assert "동종 업종 경쟁사를 의미하지는 않습니다" in result["message"]
    assert "'None'" not in result["message"]
    assert selector.candidate_ids is None
