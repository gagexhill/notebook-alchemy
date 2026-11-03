"""Tests for dry-run analysis and topic clustering."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notebook_alchemy.analyzer import NotebookAnalyzer


class TestDryRunAnalysis(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = NotebookAnalyzer()

    def test_dry_run_fallback_catalog(self) -> None:
        report = self.analyzer.generate_dry_run("Example Notebook", use_fallback=True)

        self.assertEqual(report.notebook_title, "Example Notebook")
        self.assertEqual(report.total_sources, 288)
        self.assertEqual(len(report.youtube_videos), 288)
        self.assertGreater(report.total_estimated_tokens, 1_000_000)
        self.assertGreater(report.total_estimated_cost_usd, 0.05)
        self.assertLess(report.total_estimated_cost_usd, 0.50)

        # Check clusters
        clusters = report.domain_topic_clusters
        self.assertIn("Lead Generation & Client Acquisition", clusters)
        self.assertIn("Web Design & Agency Operations", clusters)
        self.assertIn("SEO & Local Search", clusters)
        self.assertIn("Pricing & Proposal Strategy", clusters)
        self.assertIn("Client Retention, Onboarding & QBRs", clusters)
        self.assertIn("Solo Operator Systems & Productivity", clusters)


if __name__ == "__main__":
    unittest.main()
