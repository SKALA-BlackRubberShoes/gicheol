"""CSV 전체 회사를 평가한 뒤 총점 1위만 보고서에 전달한다."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable

from main.rag.company import CompanyRecord


def csv_order(records: list[CompanyRecord]) -> list[CompanyRecord]:
    """BaseRAG의 ID 정렬과 무관하게 원본 CSV 행 순서를 사용한다."""
    return sorted(records, key=lambda record: record.source.record_number)


def competitor_ids(
    target: CompanyRecord, records: list[CompanyRecord], limit: int = 2
) -> list[str]:
    """같은 세부 업종, 같은 업종, 나머지 순으로 CSV에서 앞선 회사를 고른다."""
    if limit < 1:
        raise ValueError("경쟁사 수는 1 이상이어야 합니다.")
    others = [record for record in records if record.company_id != target.company_id]
    if not others:
        raise ValueError("경쟁 비교에는 CSV에 다른 회사가 최소 한 곳 필요합니다.")
    sector = target.values.get("sector")
    subsector = target.values.get("subsector")

    def priority(record: CompanyRecord) -> int:
        if subsector and record.values.get("subsector") == subsector:
            return 0
        if sector and record.values.get("sector") == sector:
            return 1
        return 2

    return [record.company_id for record in sorted(
        others, key=lambda record: (priority(record), record.source.record_number)
    )[:limit]]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def run_csv_ranking(
    records: list[CompanyRecord],
    evaluate: Callable[[str, list[str]], dict[str, Any]],
    report: Callable[..., dict[str, Any]],
    output_dir: str | Path,
    *,
    competitors_per_company: int = 2,
) -> dict[str, Any]:
    """CSV 전 행을 시도하고 점수가 있는 회사 중 1위의 보고서 하나만 만든다.

    실패한 회사와 점수 없는 회사는 기록하되 순위에서는 제외한다.
    """
    ordered = csv_order(records)
    if len(ordered) < 2:
        raise ValueError("전체 평가와 경쟁 비교에는 회사가 최소 두 곳 필요합니다.")
    destination = Path(output_dir).expanduser().resolve()
    ranking_path = destination / "ranking.json"
    data: dict[str, Any] = {
        "status": "evaluating", "csv_path": ordered[0].source.csv_path,
        "company_count": len(ordered), "attempted_count": 0,
        "evaluated_count": 0, "failed_count": 0,
        "companies": [], "ranking": [], "winner_company_id": None,
        "report": None,
    }
    _write_json(ranking_path, data)

    for record in ordered:
        peers = competitor_ids(record, ordered, competitors_per_company)
        try:
            result = evaluate(record.company_id, peers)
            if not isinstance(result, dict) or result.get("company_id") != record.company_id:
                raise ValueError("평가 결과의 company_id가 CSV 회사와 다릅니다.")
            judgment = result.get("current_evaluation")
            if not isinstance(judgment, dict) or judgment.get("candidate_id") != record.company_id:
                raise ValueError("회사별 투자 판단 결과가 없거나 회사 ID가 다릅니다.")
            score = judgment.get("score")
            if score is not None and (
                type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100
            ):
                raise ValueError("총점은 0~100의 숫자 또는 null이어야 합니다.")
            if judgment.get("decision") not in ("invest", "hold"):
                raise ValueError("투자 판단의 decision은 invest 또는 hold여야 합니다.")
        except Exception as exc:
            data["companies"].append({
                "csv_row": record.source.record_number,
                "company_id": record.company_id,
                "company_name": record.company_name,
                "competitor_ids": peers,
                "score": None,
                "decision": None,
                "evaluation": None,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            })
            data["attempted_count"] += 1
            data["failed_count"] += 1
            _write_json(ranking_path, data)
            continue

        data["companies"].append({
            "csv_row": record.source.record_number,
            "company_id": record.company_id,
            "company_name": record.company_name,
            "competitor_ids": peers,
            "score": score,
            "decision": judgment["decision"],
            "evaluation": result,
        })
        data["evaluated_count"] += 1
        data["attempted_count"] += 1
        _write_json(ranking_path, data)

    ranked = [item for item in data["companies"] if item["score"] is not None]
    ranked.sort(key=lambda item: (-item["score"], item["csv_row"]))
    data["ranking"] = [
        {"rank": index, "company_id": item["company_id"], "company_name": item["company_name"],
         "score": item["score"], "decision": item["decision"], "csv_row": item["csv_row"]}
        for index, item in enumerate(ranked, start=1)
    ]
    if not ranked:
        data["status"] = "no_scored_company"
        _write_json(ranking_path, data)
        raise ValueError("모든 회사의 총점이 null이어서 1위를 선정할 수 없습니다.")

    winner = ranked[0]
    data["winner_company_id"] = winner["company_id"]
    data["status"] = "reporting"
    _write_json(ranking_path, data)
    _write_json(destination / "selected_report_input.json", winner["evaluation"])
    try:
        data["report"] = report(winner["evaluation"], output_dir=destination / "report", allow_hold=True)
    except Exception as exc:
        data["status"] = "report_failed"
        data["error"] = {"company_id": winner["company_id"], "type": type(exc).__name__, "message": str(exc)}
        _write_json(ranking_path, data)
        raise
    data["status"] = "completed"
    _write_json(ranking_path, data)
    return data
