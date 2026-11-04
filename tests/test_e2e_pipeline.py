"""End-to-end integration test for the distillation pipeline."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.engine import AlchemyEngine


class TestEndToEndPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.vault_path = Path(self.temp_dir.name) / "vault"
        self.config = AlchemyConfig(
            env="test",
            gemini_api_key="mock_key",
            vault_path=self.vault_path,
            state_dir=Path(self.temp_dir.name) / ".notebooklm",
        )
        self.engine = AlchemyEngine(self.config)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_full_distillation_pipeline(self) -> None:
        report = self.engine.run_distillation(
            notebook_query="Portable Entrepreneur",
            force_offline=True,
            max_videos_per_volume=50,
            write_to_vault=True,
        )

        self.assertEqual(report.notebook_title, "Portable Entrepreneur")
        self.assertEqual(report.total_sources_scanned, 288)
        self.assertGreaterEqual(report.total_compendiums, 6)
        self.assertGreater(report.total_tokens, 5000)

        # Check vault files
        comp_dir = self.vault_path / "compendiums"
        self.assertTrue(comp_dir.exists())

        md_files = list(comp_dir.glob("*.md"))
        self.assertGreaterEqual(len(md_files), 7)  # At least 6 compendiums + INDEX.md

        index_file = comp_dir / "INDEX.md"
        self.assertTrue(index_file.exists())
        index_content = index_file.read_text(encoding="utf-8")
        self.assertIn("Master Compendium Index", index_content)
        self.assertIn("Domain Documents", index_content)
        self.assertIn("title-only source indexes", index_content)

        # Verify individual compendium files
        for comp in report.compendiums:
            comp_path = self.vault_path / comp.relative_path
            self.assertTrue(comp_path.exists(), f"Missing file: {comp_path}")
            txt = comp_path.read_text(encoding="utf-8")
            self.assertTrue(txt.startswith("---"))
            self.assertIn(comp.domain, txt)
            self.assertIn("Offline title-only index", txt)
            self.assertIn("## Source Index", txt)


if __name__ == "__main__":
    unittest.main()
