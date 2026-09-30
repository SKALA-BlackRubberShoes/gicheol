"""Run both BaseRAG-backed agents and save their validated results as JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    # The files have been copied into the team's main/ package.
    from main.agents import make_comparison_node, make_technology_node
except ModuleNotFoundError as exc:
    if exc.name != "main.agents":
        raise
    # Run the handoff folder in this checkout with: python -m HarryKim.run_agents
    from HarryKim.main.agents import make_comparison_node, make_technology_node
from main.baseRAG import BaseRAG


def main() -> None:
    parser = argparse.ArgumentParser(description="Run technology and competitor agents")
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--competitor-id", action="append", required=True)
    parser.add_argument("--model", default="openai:gpt-4.1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    state = {"company_id": args.company_id, "competitor_ids": args.competitor_id}
    rag = BaseRAG()
    try:
        state.update(make_technology_node(rag, model=args.model)(state))
        state.update(make_comparison_node(rag, model=args.model)(state))
    finally:
        rag.close()

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(output)


if __name__ == "__main__":
    main()
