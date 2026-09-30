# MarketRAG 사용법

프로젝트 루트에서 `python -m pip install -r main/rag/market/requirements-market.txt`로 설치합니다.
`from main.rag.market import MarketRAG`로 가져옵니다. 기본 경로는 `main/paths.py`에서 관리합니다.

```bash
docker compose -p company-rag -f compose.qdrant.yml up -d
python -m main.scripts.build_market_index
# PDF 또는 sources.json 변경 후 전체 교체
python -m main.scripts.build_market_index --rebuild
```

PDF 추출·청킹은 `pdf_data.py`, 반환 모델은 `models.py`에 있습니다.
`MarketRAG.prepare_chunks()`는 로컬 PDF만 읽습니다. `build_index()`와 `retrieve()`는 Qdrant를 사용하며 새 임베딩에는 환경변수 `OPENAI_API_KEY`가 필요합니다.
기존 컬렉션은 차원을 확인해 재사용합니다. 파일을 바꾸면 명시적으로 다시 색인하세요.
기업 컬렉션 `companies_small_512`와 시장 컬렉션 `market_reference_pdf_512`는 별개입니다.
반환 `page`는 저장한 PDF 내부의 페이지 번호입니다. 발췌 PDF의 원본 보고서 페이지 번호로 해석하지 마세요.
생성한 RAG는 앱 종료 시 `close()`합니다. 주입받은 임베더의 종료는 호출자가 맡습니다.
