"""Offline by default; live checks are explicit and need configured services."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent / "unit/v2/dongwook"))
sys.path.insert(0, str(Path(__file__).parent / "acceptance/v2/dongwook"))
os.environ.setdefault("LANGSMITH_TRACING", "false")
os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
