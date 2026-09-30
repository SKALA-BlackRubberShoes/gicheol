"""기업 CSV를 색인합니다. 데이터 변경 후에는 --rebuild를 지정합니다."""

import argparse
from main.rag.company import BaseRAG


def main():
    parser = argparse.ArgumentParser(description="Build or reuse the company CSV index")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    rag = BaseRAG()
    try:
        print(
            f"companies_small_512: {rag.build_index(rebuild=args.rebuild)} companies ready"
        )
    finally:
        rag.close()


if __name__ == "__main__":
    main()
