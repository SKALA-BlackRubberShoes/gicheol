"""LangGraph-compatible node factories for selected BaseRAG company IDs."""

from __future__ import annotations

from typing import Any, Callable

from main.baseRAG import BaseRAG

from .competitor_comparison_agent import run_agent as run_comparison
from .evidence import as_evidence, get_selected_company
from .tech_summary_agent import run_agent as run_technology


def _target(rag: BaseRAG, state: dict[str, Any]):
    if not isinstance(state, dict):
        raise TypeError("state must be a dict")
    record = get_selected_company(rag, state.get("company_id"))
    for key in ("company", "company_name"):
        supplied = state.get(key)
        if supplied is not None and (
            not isinstance(supplied, str)
            or supplied.strip().casefold() != record.company_name.casefold()
        ):
            raise ValueError(f"state.{key} disagrees with state.company_id")
    return record


def make_technology_node(
    rag: BaseRAG, model: str | Any = "openai:gpt-4.1"
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a node that adds the technology report and its score to graph state."""
    if rag is None:
        raise ValueError("A BaseRAG instance is required")

    def technology_node(state: dict[str, Any]) -> dict[str, Any]:
        record = _target(rag, state)
        evidence = [as_evidence(record)]
        result = run_technology(
            {"company": record.company_name}, rag, model=model,
            search_company=lambda _name: evidence,
        )
        return {
            "technology_summary": result["technology_summary"],
            "technical_score": result["technical_score"],
        }

    return technology_node


def make_comparison_node(
    rag: BaseRAG, model: str = "openai:gpt-4.1"
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Return a node that compares the selected ID with competitor_ids."""
    if rag is None:
        raise ValueError("A BaseRAG instance is required")

    def comparison_node(state: dict[str, Any]) -> dict[str, Any]:
        target = _target(rag, state)
        competitor_ids = state.get("competitor_ids")
        if not isinstance(competitor_ids, list) or not competitor_ids:
            raise ValueError("state.competitor_ids must be a nonempty list of IDs")
        records = [target, *(get_selected_company(rag, item) for item in competitor_ids)]
        names = [record.company_name for record in records]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Target and competitor IDs must identify distinct companies")
        evidence_by_name = {record.company_name: [as_evidence(record)] for record in records}
        result = run_comparison(
            {"company": target.company_name, "competitors": names[1:]},
            rag,
            model=model,
            search_company=lambda name: evidence_by_name[name],
        )
        return {
            "competitor_comparison": result["competitor_comparison"],
            "competitor_score": result["competitor_score"],
        }

    return comparison_node
