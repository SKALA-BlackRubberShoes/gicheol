"""시장 보고서 PDF를 Qdrant에 색인하는 준비 스크립트입니다.

저장소 루트에서 다음과 같이 실행합니다.

    python -m main.scripts.build_market_index

PDF 또는 sources.json을 변경했다면 ``--rebuild``를 붙입니다.
"""

from __future__ import annotations

import argparse

from main.rag.market import MarketRAG


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or reuse the market PDF index")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Re-extract, re-embed, and replace the existing market collection",
    )
    args = parser.parse_args()

    rag = MarketRAG()
    try:
        count = rag.build_index(rebuild=args.rebuild)
        print(f"market_reference_pdf_512: {count} chunks ready")
    finally:
        rag.close()


if __name__ == "__main__":
    main()
