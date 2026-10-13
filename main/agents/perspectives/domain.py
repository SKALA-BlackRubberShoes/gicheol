"""Neutral domain application research with no State or other worker calls."""
from main.agents.perspectives.market import _NeutralResearchAgent
from main.agents.supervisor.contracts import Perspective


class DomainAgent(_NeutralResearchAgent):
    perspective = Perspective.DOMAIN_APPLICATION
    prompt_directory = "domain"
    question_terms = ("documented real deployment use cases pilots operating results", "deployment data requirements operating conditions integration domain limitations")
