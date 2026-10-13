"""Neutral stakeholder research, sharing a helper rather than calling a worker."""
from main.agents.perspectives.market import _NeutralResearchAgent
from main.agents.supervisor.contracts import Perspective


class StakeholdersAgent(_NeutralResearchAgent):
    perspective = Perspective.STAKEHOLDERS
    prompt_directory = "stakeholders"
    question_terms = ("customers users team partners regulators stakeholder roles", "stakeholder expectations adoption burden costs training safety accountability")
