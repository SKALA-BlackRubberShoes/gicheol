# 공통 DTO fixture

`valid-run-input.json`은 RunInput 형식 검사에만 쓰는 가상 기업 입력이다. 빈 corpus이므로 live preflight에는 통과하지 않는다. `invalid-run-input.json`은 빈 기업명, 잘못된 URL·domain·날짜·미등록 필드의 거절을 확인할 입력이다.

테스트용 모델·검색·Judge 결과는 mock으로 표시한다. 실제 기업 증빙이나 RAG gold를 이 fixture에서 생성하지 않는다. 저장소와 graph 결합 fixture는 `tests/integration/v2/test_runtime_graph.py`에 있으며 실제 파일·SQLite를 쓰되 업무 서비스는 가짜 응답을 주입한다.
