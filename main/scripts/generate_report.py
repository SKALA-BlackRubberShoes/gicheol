"""투자 판단 State/payload 또는 가상 예제를 최종 PDF 보고서로 출력합니다."""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from main.agents.report import generate_report
from main.paths import DEFAULT_REPORT_DIR, DEFAULT_REPORT_SAMPLE_PATH


def main() -> int:
    parser = argparse.ArgumentParser(description="최종 분석 State를 5장 이내 한글 PDF로 생성")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo", action="store_true", help="가상 데이터로 실행 (기본: API 호출 없음)")
    group.add_argument("--input", type=Path, help="앞선 에이전트의 결과 State JSON")
    parser.add_argument("--llm", action="store_true", help="LLM이 SUMMARY 원문 문장을 선택")
    parser.add_argument("--model", help="사용 가능한 모델 ID (또는 REPORT_MODEL 환경변수)")
    parser.add_argument("--env-file", type=Path, help="명시적으로 지정한 .env만 로드; 기존 환경값 유지")
    parser.add_argument("--font", help="한글 글리프를 지원하는 TTF 경로")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--name", default="RAG-Output", help="확장자를 제외한 제출 파일명")
    args = parser.parse_args()
    try:
        if args.env_file:
            if not args.env_file.is_file():
                parser.error("--env-file 파일이 없습니다.")
            load_dotenv(args.env_file, override=False)
        path = DEFAULT_REPORT_SAMPLE_PATH if args.demo else args.input
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("입력 JSON 최상위는 State 객체여야 합니다.")
        result = generate_report(state, output_dir=args.output_dir, use_llm=args.llm,
                                 model_name=args.model, font_path=args.font, filename=args.name)
    except (ValueError, OSError) as exc:
        print(f"보고서 생성 실패: {exc}", file=sys.stderr)
        return 1
    print(f"PDF ({result['report_page_count']}쪽): {result['report_pdf_path']}")
    print(f"Markdown: {result['report_markdown_path']}")
    print(f"검증 기록: {result['report_json_path']}")
    if result["report_warnings"]:
        print(f"확인이 필요한 항목 {len(result['report_warnings'])}개: 보고서의 분석 한계 항목을 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
