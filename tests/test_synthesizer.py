"""Unit tests for the Synthesizer module."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notebook_alchemy.synthesizer import Synthesizer


class TestSynthesizer(unittest.TestCase):
    def setUp(self) -> None:
        self.synthesizer = Synthesizer(api_key="mock_key")

    def test_safe_filename(self) -> None:
        name = self.synthesizer._safe_filename("SEO & Local Search: 101!")
        self.assertEqual(name, "SEO_Local_Search_101")

    def test_synthesize_cluster_offline(self) -> None:
        videos = [
            {
                "id": "yt_01",
                "title": "Google Business Profile Audit",
                "url": "https://youtube.com/watch?v=1",
            },
            {
                "id": "yt_02",
                "title": "Local Citation Building",
                "url": "https://youtube.com/watch?v=2",
            },
        ]

        compendium = self.synthesizer.synthesize_cluster(
            domain="SEO & Local Search",
            videos=videos,
            volume=1,
            force_offline=True,
            notebook_title="Portable Entrepreneur",
        )

        self.assertEqual(compendium.domain, "SEO & Local Search")
        self.assertEqual(compendium.volume, 1)
        self.assertEqual(len(compendium.titles), 2)
        self.assertEqual(len(compendium.source_ids), 2)
        self.assertTrue(compendium.relative_path.endswith("SEO_Local_Search.md"))
        self.assertIn("---", compendium.content)
        self.assertIn('title: "SEO & Local Search - Domain Compendium"', compendium.content)
        self.assertIn("## 1. Executive Summary & Strategic Core", compendium.content)
        self.assertIn("## 2. Foundational Mental Models & Principles", compendium.content)
        self.assertIn("## 3. Actionable Standard Operating Procedures (SOPs)", compendium.content)
        self.assertIn("## 4. Master Cross-Source Catalog & Video Breakdown", compendium.content)
        self.assertIn("Google Business Profile Audit", compendium.content)
        self.assertIn("Local Citation Building", compendium.content)
        self.assertFalse(compendium.is_live_synthesized)

    def test_generate_index(self) -> None:
        videos = [{"id": "yt_01", "title": "SEO Video", "url": "https://youtube.com/watch?v=1"}]
        c1 = self.synthesizer.synthesize_cluster("SEO & Local Search", videos, force_offline=True)
        c2 = self.synthesizer.synthesize_cluster(
            "Web Design & Agency Operations", videos, force_offline=True
        )

        index_md = self.synthesizer.generate_index(
            [c1, c2], notebook_title="Portable Entrepreneur", total_sources=2
        )
        self.assertIn("# Portable Entrepreneur — Master Compendium Index", index_md)
        self.assertIn("SEO & Local Search", index_md)
        self.assertIn("Web Design & Agency Operations", index_md)


if __name__ == "__main__":
    unittest.main()
