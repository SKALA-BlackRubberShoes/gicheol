from __future__ import annotations

import unittest
from types import SimpleNamespace

from LSH.marketEvaluation.web_search import OpenAIWebSearch


class FakeResponses:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        annotations = [
            SimpleNamespace(
                type="url_citation",
                title="Official market report",
                url="https://example.org/report",
            )
        ]
        content = SimpleNamespace(type="output_text", annotations=annotations)
        message = SimpleNamespace(type="message", content=[content])
        return SimpleNamespace(
            output_text="The market is growing according to the cited report.",
            output=[message],
        )


class FakeOpenAIClient:
    def __init__(self):
        self.responses = FakeResponses()


class OpenAIWebSearchTest(unittest.TestCase):
    def test_multiple_queries_use_one_api_call_and_keep_citation(self):
        search = OpenAIWebSearch()
        search._client = FakeOpenAIClient()

        results = search.search(["market size", "customer adoption", "ROI"])

        self.assertEqual(len(search._client.responses.calls), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://example.org/report")
        self.assertIn("market size", results[0].query)


if __name__ == "__main__":
    unittest.main()
