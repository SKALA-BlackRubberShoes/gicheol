"""저장소 위치를 기준으로 공유 데이터 경로를 계산합니다."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV_PATH = PROJECT_ROOT / "docs/data/base/raw/thevc_startups_30_updated.csv"
DEFAULT_PDF_DIR = PROJECT_ROOT / "docs/data/market/raw"
DEFAULT_MANIFEST_PATH = PROJECT_ROOT / "docs/data/market/sources.json"
