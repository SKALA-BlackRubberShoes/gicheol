from __future__ import annotations

import unittest

from LSH.marketRAG import MarketRAG


class MarketRAGDataTest(unittest.TestCase):
    def test_six_pdfs_produce_page_aware_chunks(self):
        rag = MarketRAG()
        try:
            chunks = rag.prepare_chunks()
        finally:
            rag.close()

        self.assertGreater(len(chunks), 90)
        self.assertEqual(len({chunk.document_id for chunk in chunks}), 6)
        self.assertEqual(len({(chunk.document_id, chunk.page) for chunk in chunks}), 90)
        self.assertTrue(all(chunk.page >= 1 for chunk in chunks))
        self.assertTrue(all(chunk.title and chunk.publisher for chunk in chunks))
        self.assertTrue(all(chunk.text.strip() for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
