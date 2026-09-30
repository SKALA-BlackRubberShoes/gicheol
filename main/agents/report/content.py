"""검증된 입력을 보고서 목차로 연결하는 순수 함수 (검색/API/파일 쓰기 없음)."""
from __future__ import annotations

from collections import Counter
import re
from typing import Any

from .schemas import AnalysisResult, Evidence, InvestmentResult, ReportInput

STATUS = {"completed": "완료", "insufficient": "근거 부족", "failed": "실행 실패"}
ELIGIBILITY = {"eligible": "적격", "ineligible": "부적격", "unknown": "미확인"}
DIMENSIONS = {"technical": "기술", "market": "시장", "competition": "경쟁", "team": "팀"}


def score(value: float | None) -> str:
    if value is None:
        return "N/A"
    text = str(value)
    return text[:-2] if text.endswith(".0") else text


def section(title: str, *, paragraphs=None, bullets=None, tables=None) -> dict:
    return {"title": title, "paragraphs": paragraphs or [], "bullets": bullets or [], "tables": tables or []}


def source_text(e: Evidence) -> str:
    author = e.author or e.issuer or "작성자/기관 미확인"
    date = e.published_at or "발행일 미확인"
    if e.kind == "report":
        text = f"{author}({date[:4] if e.published_at else date}). {e.title}. {e.url or 'URL 미확인'}"
    elif e.kind == "paper":
        volume = e.volume + (f"({e.issue})" if e.issue else "")
        text = f"{author}({date[:4] if e.published_at else date}). {e.title}. {e.journal or '학술지 미확인'}, {volume or '권(호) 미확인'}, {e.pages or '페이지 미확인'}. {e.url}"
    elif e.kind == "csv":
        text = f"{author}({date}). {e.title}. {e.csv_path or '경로 미확인'}, 행 {e.record_number or '미확인'}."
    else:
        text = f"{author}({date}). {e.title}. {e.site_name or e.issuer or '사이트명 미확인'}, {e.url or 'URL 미확인'}"
    locators = [f"근거 ID: {e.evidence_id}"]
    page_label = "제공 PDF p." if e.kind == "report" else "제공 위치 p."
    for label, val in (("문서", e.doc_id), ("청크", e.chunk_id), (page_label, e.page),
                       ("기준연도", e.data_year), ("수집일", e.collected_at)):
        if val is not None and val != "":
            locators.append(f"{label} {val}")
    if e.locator and e.locator != e.url and not e.csv_path:
        locators.append("제공 위치 " + e.locator)
    if e.provenance_note:
        locators.append(e.provenance_note)
    labels = ["[가상 자료]" if e.is_mock else "[자료 유형 미확인]" if e.is_mock is None else ""]
    if e.content_kind == "generated_summary":
        labels.append("[AI 생성 요약·원문 인용 아님]")
    elif e.content_kind == "metadata_only":
        labels.append("[서지정보만 제공]")
    prefix = " ".join(label for label in labels if label)
    return (prefix + " " if prefix else "") + text.strip() + " (" + "; ".join(locators) + ")"


def _decision_reasons(result: InvestmentResult | None) -> str:
    """한계·위험 절에 따로 실린 동일 문장은 판정 사유에서 중복하지 않는다."""
    if result is None:
        return "판단 사유 미제공"
    elsewhere = set(result.missing_fields + result.conflicts + result.errors + result.risks)
    reasons = [part.strip() for reason in result.reasons for part in reason.split(";")
               if part.strip() and part.strip() not in elsewhere]
    return "; ".join(dict.fromkeys(reasons)) or "판단 사유는 아래 한계·위험 항목 참조"


def _compact_messages(messages: list[str]) -> list[str]:
    """같은 설명을 가진 항목은 식별자/주체를 모아 한 문장으로 표시한다."""
    groups: dict[str, list[str]] = {}
    singles = []
    for message in dict.fromkeys(messages):
        if ": " not in message:
            singles.append(message)
            continue
        subject, detail = message.split(": ", 1)
        groups.setdefault(detail, []).append(subject)
    return singles + [f"{', '.join(subjects)}: {detail}" for detail, subjects in groups.items()]


def _summary_excerpt(value: str, limit: int) -> str:
    """완결된 원문 문장/절만 발췌한다. 긴 문장 중간을 잘라 의미를 바꾸지 않는다."""
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    parts = re.split(r"(?<=[.!?。])\s+|;\s*", value)
    selected = []
    for part in parts:
        if len("; ".join([*selected, part])) > limit:
            break
        selected.append(part)
    return "; ".join(selected)


def _summary_fact(company, budget: int) -> str:
    """사업·결론·핵심 근거·위험을 요약한다. 원 점수와 판정을 그대로 사용한다."""
    inv = company.investment_result
    raw = (company.company_record or {}).get("raw") or {}
    parts = [f"{company.company_name}: {inv.decision if inv else '판단 미확인'}, 총점 {score(inv.final_score if inv else None)}."]
    business = raw.get("서비스") or raw.get("대표제품") or (company.technical_result.summary if company.technical_result else "")
    candidates = [("사업", _summary_excerpt(str(business), 110))]
    if inv:
        candidates.append(("판단 근거", _summary_excerpt(_decision_reasons(inv), 120)))
    analyses = [company.technical_result, company.market_result, company.competition_result, company.team_result]
    ratings = [f"{label} {score(result.scores[key])}" for result, key, label in zip(
        analyses, ("technical", "market", "competition", "team"), ("기술", "시장", "경쟁", "팀"))
        if result and key in result.scores]
    candidates.append(("원점수(기술·시장·경쟁 /100, 팀 /10)", ", ".join(ratings)))
    risks = list(inv.risks if inv else []) + [risk for result in analyses if result for risk in result.risks]
    missing = list(inv.missing_fields if inv else []) + [item for result in analyses if result for item in result.missing_fields]
    candidates.append(("주요 위험", next((text for item in risks if (text := _summary_excerpt(item, 220))), "")))
    candidates.append(("추가 확인", next((text for item in missing if (text := _summary_excerpt(item, 100))), "")))
    for label, text in candidates:
        line = f"{label}: {text}"
        if text and len(" ".join([*parts, line])) <= budget:
            parts.append(line)
    return " ".join(parts)


def _reference_paragraphs(refs: "Citations") -> list[str]:
    """출처별 서지정보를 유지하면서 동일한 출처 유형 설명만 한 번 싣는다."""
    shared = Counter(refs.catalog[eid].provenance_note for eid in refs.used
                     if refs.catalog[eid].provenance_note)
    paragraphs = []
    for eid, number in refs.used.items():
        item = refs.catalog[eid]
        if shared.get(item.provenance_note, 0) > 1:
            item = item.model_copy(update={"provenance_note": ""})
        paragraphs.append(f"[{number}] {source_text(item)}")
    for note, count in shared.items():
        if count > 1:
            ids = [f"[{refs.used[eid]}] {eid}" for eid in refs.used if refs.catalog[eid].provenance_note == note]
            paragraphs.append("공통 출처 설명 (" + ", ".join(ids) + "): " + note)
    return paragraphs


class Citations:
    def __init__(self, inputs: ReportInput, warnings: list[str]):
        self.catalog: dict[str, Evidence] = {}
        self.used: dict[str, int] = {}
        self.warnings = warnings
        evidence = list(inputs.evidence)
        for company in inputs.results_by_company.values():
            for result in (company.eligibility, company.technical_result, company.market_result,
                           company.competition_result, company.team_result, company.investment_result):
                if result:
                    evidence.extend(result.evidence)
        for item in evidence:
            self.add(item)

    def add(self, e: Evidence) -> None:
        if e.evidence_id in self.catalog and self.catalog[e.evidence_id] != e:
            raise ValueError(f"동일한 evidence_id에 다른 출처가 있습니다: {e.evidence_id}")
        self.catalog[e.evidence_id] = e

    def use(self, ids: list[str]) -> str:
        refs = []
        for eid in dict.fromkeys(ids):
            if eid not in self.catalog:
                self.warnings.append(f"출처를 찾을 수 없음: {eid}")
                refs.append(f"[근거 미확인: {eid}]")
            else:
                self.used.setdefault(eid, len(self.used) + 1)
                refs.append(f"[{self.used[eid]}]")
        return " ".join(refs)

    def result(self, result) -> str:
        return self.use([e.evidence_id for e in result.evidence] + result.evidence_ids)


def build_document(inputs: ReportInput) -> tuple[dict, list[str], dict[str, str]]:
    """문서와 품질 경고, SUMMARY 선택용 사실 목록을 만든다."""
    warnings: list[str] = []
    refs = Citations(inputs, warnings)
    companies = list(inputs.results_by_company.values())
    mock = inputs.config.is_mock or any(c.is_mock for c in companies)
    sections, facts, compare_rows = [], {}, []

    def analysis(name: str, label: str, result: AnalysisResult | None) -> list[str]:
        if result is None:
            warnings.append(f"{name}: {label} 분석 결과 미제공")
            return [f"{label}: 미확인 - 분석 결과가 전달되지 않았습니다."]
        citation = refs.result(result)
        if result.summary and not citation:
            warnings.append(f"{name}: {label} 요약의 출처 미제공")
        lines = [f"{label} ({STATUS[result.status]}): {result.summary or '요약 미제공'} {citation}".strip()]
        lines.extend(f"{title}: {text} {refs.use(result.detail_evidence_ids.get(title, []))}".strip()
                     for title, text in result.details.items())
        for label2, values in (("강점", result.strengths), ("위험", result.risks)):
            for value in dict.fromkeys(values):
                # 서술에 명시된 근거 ID는 개별 연결한다. 절 전체의 긴 인용 목록을
                # 모든 위험 문장 뒤에 되풀이하지 않는다.
                explicit = [eid for eid in refs.catalog if re.search(
                    r"(?<![\w-])" + re.escape(eid) + r"(?![\w-])", value)]
                related = refs.use(explicit) if explicit else ""
                lines.append(f"{label2}: {value} {related}".strip())
        if result.scores:
            values = []
            for key, value in result.scores.items():
                ids = result.score_evidence_ids.get(key, [])
                values.append(f"{key} {score(value)} {refs.use(ids)}".strip())
                if value is not None and not ids:
                    warnings.append(f"{name}: {label}/{key} 점수별 근거 연결 미제공")
            lines.append("전달된 세부 점수 (원 척도): " + "; ".join(values))
        return lines

    recommended = sum(c.investment_result is not None and c.investment_result.decision == "추천" for c in companies)
    held = sum(c.investment_result is not None and c.investment_result.decision == "보류" for c in companies)
    overview = f"평가 결과 {len(companies)}개 기업: 전달된 투자 판단은 추천 {recommended}개, 보류 {held}개, 미확인 {len(companies) - recommended - held}개입니다."
    if not companies:
        overview = "평가 가능한 기업 결과가 없어 투자 추천을 확정할 수 없습니다. " + (inputs.no_candidates_reason or "탐색 결과 또는 평가 결과가 전달되지 않았습니다.")
    elif recommended == 0:
        overview += " 추천 기업이 없습니다. 보류 사유와 미확인 항목을 우선 확인해야 합니다."
    pending = [cid for cid in inputs.candidate_company_ids if cid not in inputs.results_by_company]
    if pending:
        warnings.append("평가 결과가 없는 후보: " + ", ".join(pending))
    if inputs.config.as_of == "미확인":
        warnings.append("평가 기준일 미제공")

    for index, c in enumerate(companies, 1):
        name, inv = c.company_name, c.investment_result
        eligibility_refs = refs.result(c.eligibility)
        decision = inv.decision if inv else "미확인"
        total = score(inv.final_score if inv else None)
        reason = _decision_reasons(inv)
        inv_refs = refs.result(inv) if inv else ""
        compare_rows.append([name, ELIGIBILITY[c.eligibility.status], decision, total,
                             f"종합 평가·한계 항목 참조 {inv_refs}".strip()])
        summary_budget = max(220, (650 - len(overview)) // min(len(companies), 2))
        facts[f"company-{index}"] = _summary_fact(c, summary_budget)
        if inv and inv.decision == "추천" and (c.eligibility.status == "ineligible" or inv.status != "completed" or inv.final_score is None):
            warnings.append(f"{name}: 전달된 추천 판정과 적격성/완료 상태/총점이 일치하지 않음. 투자 판단 에이전트 재확인 필요")
        if inv and inv.reasons and not inv_refs:
            warnings.append(f"{name}: 투자 판단 근거 출처 미제공")
        if inv is None:
            warnings.append(f"{name}: 투자 판단 결과 미제공")

        paragraphs = [f"적격성: {ELIGIBILITY[c.eligibility.status]}. {c.eligibility.reason} {eligibility_refs}".strip()]
        record = c.company_record or {}
        raw, values = record.get("raw") or {}, record.get("values") or {}
        snapshot = []
        for label, value in (("핵심 제품", raw.get("대표제품")), ("제품/서비스", raw.get("서비스")),
                             ("최근 투자 단계", values.get("funding_stage")), ("최근 투자일", values.get("funding_latest_date")),
                             ("최근 투자금 (원)", values.get("funding_latest_won")), ("누적 투자금 (원)", values.get("funding_total_won"))):
            if value is not None and value != "" and value != "NULL":
                shown = f"{value:,}" if isinstance(value, (int, float)) else str(value)
                snapshot.append(f"{label}: {shown}")
        if snapshot:
            provenance = record.get("source") or {}
            snap_ref = ""
            if provenance.get("csv_path") and provenance.get("record_number"):
                eid = f"company-record:{c.company_id}"
                existing = refs.catalog.get(f"CSV-{c.company_id}")
                if (existing and existing.kind == "csv" and existing.csv_path == provenance["csv_path"]
                        and existing.record_number == provenance["record_number"]):
                    eid = existing.evidence_id
                else:
                    refs.add(Evidence(evidence_id=eid, title="기업 기본정보 CSV", kind="csv", issuer="팀 제공 자료",
                                      csv_path=provenance["csv_path"], record_number=provenance["record_number"],
                                      content_kind="metadata_only", provenance_note="팀 제공 CSV; 자료 진위 미검증",
                                      is_mock=True if c.is_mock else None))
                snap_ref = refs.use([eid])
            else:
                warnings.append(f"{name}: 기업 기본정보의 CSV 출처 미제공")
            paragraphs.append("사업 및 투자 현황: " + "; ".join(snapshot) + " " + snap_ref)
        else:
            paragraphs.append("사업 및 투자 현황: 기업 기본정보 미제공. 매출/투자액을 추정하지 않았습니다.")
        # 별도로 받은 조회 결과도 보존하되 임의로 출처를 붙이지 않는다.
        for label, data in (("기술 조회 정보", c.technology_data), ("재무 조회 정보", c.finance_data)):
            if data:
                paragraphs.append(label + ": " + "; ".join(f"{k}: {'미확인' if v is None else v}" for k, v in data.items()))
                warnings.append(f"{name}: {label}의 별도 출처 연결 확인 필요")
        paragraphs += analysis(name, "제품·기술", c.technical_result)
        sections.append(section(f"사업 아이디어 - {name}", paragraphs=paragraphs))
        sections.append(section(f"사업 리스크·시장 규모·경쟁 현황 - {name}", paragraphs=(
            analysis(name, "시장성", c.market_result) + analysis(name, "경쟁 및 시장에서의 위치", c.competition_result))))
        sections.append(section(f"팀 구성·기술 역량 - {name}", paragraphs=analysis(name, "창업자·팀", c.team_result)))

    if compare_rows:
        sections.insert(0, section("기업별 평가 비교", paragraphs=["총점은 투자 판단 에이전트가 전달한 100점 척도입니다. N/A는 미확인이며 0점과 다릅니다. 표의 순서는 입력 순서입니다."],
                                  tables=[{"headers": ["기업", "적격성", "판정", "총점", "판단 사유"], "rows": compare_rows,
                                           "column_weights": [2, 1, 1, 1, 5]}]))

    decisions, rows = [], []
    for c in companies:
        inv = c.investment_result
        if inv:
            decisions.append(f"{c.company_name}: {inv.decision} ({STATUS[inv.status]}). " + _decision_reasons(inv) + " " + refs.result(inv))
            decisions.extend(f"{c.company_name} 주요 위험: {risk}" for risk in inv.risks)
            for key, value in inv.weighted_scores.items():
                ids = inv.score_evidence_ids.get(key, [])
                rows.append([c.company_name, DIMENSIONS.get(key, key), score(value), refs.use(ids) or "근거 연결 미제공"])
                if value is not None and not ids:
                    warnings.append(f"{c.company_name}: 반영점수 {key}의 근거 연결 미제공")
            if inv.final_score is not None and not inv.score_evidence_ids.get("final_score"):
                warnings.append(f"{c.company_name}: 총점의 근거 연결 미제공")
            elif inv.score_evidence_ids.get("final_score"):
                decisions.append(f"{c.company_name} 총점 {score(inv.final_score)}: " + refs.use(inv.score_evidence_ids["final_score"]))
            scores = list(inv.weighted_scores.values())
            if scores and all(v is not None for v in scores) and inv.final_score is not None:
                if abs(sum(scores) - inv.final_score) > 0.01:
                    warnings.append(f"{c.company_name}: 분야별 반영점수 합계와 총점 불일치 (전달값 유지)")
            elif inv.final_score is not None and any(v is None for v in scores):
                warnings.append(f"{c.company_name}: 미확인 반영점수와 확정 총점이 함께 전달됨")
    sections.append(section("종합 평가 및 투자 판단", paragraphs=decisions or [inputs.no_candidates_reason or "투자 판단 결과 미제공"],
                            tables=[{"headers": ["기업", "영역", "반영점수", "근거"], "rows": rows,
                                     "column_weights": [2.5, 1, 1.5, 3]}] if rows else []))

    limits = ["점수·판정·위험은 이전 에이전트의 전달값이며 보고서 단계에서 새로 평가하거나 외부 검색하지 않았습니다.",
              "인용 번호는 전달된 근거와의 연결을 나타냅니다. 원문 진위, 수치 검산 및 비교 조건 검증은 앞선 분석 단계의 책임입니다."]
    for c in companies:
        issues: dict[tuple[str, str], list[str]] = {}
        statuses: dict[str, list[str]] = {}
        for label, result in (("기술", c.technical_result), ("시장", c.market_result), ("경쟁", c.competition_result),
                              ("팀", c.team_result), ("투자 판단", c.investment_result)):
            if result:
                for kind, vals in (("미확인", result.missing_fields), ("상충", result.conflicts), ("오류", result.errors)):
                    for value in vals:
                        labels = issues.setdefault((kind, value), [])
                        if label not in labels:
                            labels.append(label)
                if result.status != "completed":
                    statuses.setdefault(STATUS[result.status], []).append(label)
        groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for (kind, value), labels in issues.items():
            groups.setdefault((kind, tuple(labels)), []).append(value)
        for (kind, labels), values in groups.items():
            limits.append(f"{c.company_name}/{'·'.join(labels)} {kind}: " + "; ".join(_compact_messages(values)))
        limits.extend(f"{c.company_name}/{'·'.join(labels)}: {status}" for status, labels in statuses.items())
    mock = mock or any(refs.catalog[eid].is_mock for eid in refs.used)
    if mock:
        overview = "[가상 자료 포함 / 제출용 실제 분석 아님] " + overview
        limits.insert(0, "가상 자료를 포함한 실행 예시입니다. 실제 기업에 대한 투자 판단으로 사용할 수 없습니다.")
    for eid in refs.used:
        e = refs.catalog[eid]
        if not e.published_at or (e.kind != "csv" and not e.url):
            warnings.append(f"{eid}: 발행일 또는 원문 URL 미확인")
    for predicate, label in (
        (lambda e: e.content_kind == "metadata_only", "원문 미전달·서지정보만 제공된 출처"),
        (lambda e: e.content_kind == "generated_summary", "원문 인용이 아닌 AI 생성 요약 출처"),
        (lambda e: e.is_mock is None, "실제/가상 자료 유형 미확인 출처"),
    ):
        ids = [eid for eid in refs.used if predicate(refs.catalog[eid])]
        if ids:
            warnings.append(label + ": " + ", ".join(ids))
    warnings = list(dict.fromkeys(warnings))
    # 경고 원본 목록은 API/JSON에 모두 남긴다. 본문은 같은 설명의 주체를 모아
    # 줄바꿈과 중복 문구를 줄이며 어떤 경고나 근거 ID도 생략하지 않는다.
    sections.append(section("분석 한계 및 추가 확인사항", paragraphs=list(dict.fromkeys(limits)) +
                            (["출력 확인: " + "; ".join(_compact_messages(warnings))] if warnings else [])))
    reference = section("REFERENCE", paragraphs=_reference_paragraphs(refs)
                        or ["실제로 사용한 근거 자료가 전달되지 않았습니다. 출처를 임의로 생성하지 않았습니다."])
    # 과제의 서체 지정: 보고서·웹은 제목, 논문은 학술지명을 기울여 표시한다.
    emphasis = [refs.catalog[eid].journal if refs.catalog[eid].kind == "paper" else
                refs.catalog[eid].title if refs.catalog[eid].kind != "csv" else "" for eid in refs.used]
    reference["emphasis"] = emphasis + [""] * (len(reference["paragraphs"]) - len(emphasis))
    sections.append(reference)
    document = {"title": inputs.config.title, "subtitle": f"{inputs.config.domain} | 평가 기준일: {inputs.config.as_of}",
                "summary": overview, "sections": sections}
    return document, warnings, facts


def apply_summary(document: dict, facts: dict[str, str], selected_ids: list[str]) -> dict:
    """자유 생성 문장을 붙이지 않고 선택된 원문만 SUMMARY에 포함한다."""
    selected = []
    for fid in selected_ids:
        if fid not in facts:
            raise ValueError(f"존재하지 않는 SUMMARY 사실 ID: {fid}")
        if fid not in selected:
            selected.append(fid)
    summary = document["summary"]
    for fid in selected:
        candidate = summary + "\n" + facts[fid]
        if len(candidate) > 700:
            continue  # 세부 내용은 본문에 전부 남아 있다.
        summary = candidate
    return {**document, "summary": summary}
