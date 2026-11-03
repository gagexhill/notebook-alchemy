"""Tests for AlchemyEngine."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notebook_alchemy.client import SourceDocument  # noqa: E402
from notebook_alchemy.config import AlchemyConfig  # noqa: E402
from notebook_alchemy.engine import AlchemyEngine  # noqa: E402


class TestAlchemyEngine(unittest.TestCase):
    def test_engine_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AlchemyConfig(
                vault_path=tmp_path / "test_vault",
                state_dir=tmp_path / "test_state",
            )
            engine = AlchemyEngine(config)
            status = engine.status()

            self.assertEqual(status["environment"], "development")
            self.assertFalse(status["authenticated"])
            self.assertEqual(status["sources_count"], 0)

    def test_process_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AlchemyConfig(
                vault_path=tmp_path / "test_vault",
                state_dir=tmp_path / "test_state",
            )
            engine = AlchemyEngine(config)

            doc = SourceDocument(
                source_id="src_001",
                title="Intro to Synthesis",
                content="# Overview\nThis is a test synthesis.",
            )

            result = engine.process_and_export(doc, "synthesis/intro.md")

            self.assertEqual(result.artifact_id, "art_src_001")
            self.assertEqual(result.title, "Intro to Synthesis")

            exported_file = config.vault_path / "synthesis" / "intro.md"
            self.assertTrue(exported_file.exists())
            content = exported_file.read_text(encoding="utf-8")
            self.assertIn("source_id: src_001", content)
            self.assertIn("# Overview", content)


if __name__ == "__main__":
    unittest.main()
