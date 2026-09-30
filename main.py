"""저장소 루트에서 전체 투자 분석 그래프를 실행합니다."""

from pathlib import Path

# main/은 네임스페이스 패키지입니다. 동명의 main.py가 기존 패키지 import를 가리지 않도록 합니다.
__path__ = [str(Path(__file__).resolve().parent / "main")]
if __spec__ is not None:
    __spec__.submodule_search_locations = __path__


def main() -> int:
    from main.scripts.run_graph import main as run_graph

    return run_graph()


if __name__ == "__main__":
    raise SystemExit(main())
