# 참고 결과

`company_17_vs_14.json`은 구조 정리 전에 생성된 예시 결과입니다. 현재 코드나 평가 점수의 검증 자료로 사용하지 않습니다.
새 실행 결과는 원하는 출력 경로에 별도로 저장하세요.

`report_sample_state.json`은 보고서 생성 에이전트의 데모·테스트용 입력입니다. 추천·보류·결측·상충 사례를 담은 가상 기업과 합성 자료이며 실제 투자 평가 결과가 아닙니다. 저장소 루트에서 `python -m main.scripts.generate_report --demo`로 실행하면 `outputs/reports/<실행 ID>/`에 PDF·Markdown·검증 기록 JSON을 생성합니다.
