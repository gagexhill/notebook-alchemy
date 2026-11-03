"""Tests for AlchemyConfig."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notebook_alchemy.config import AlchemyConfig


class TestAlchemyConfig(unittest.TestCase):
    def test_default_config(self) -> None:
        config = AlchemyConfig()
        self.assertEqual(config.env, "development")
        self.assertEqual(config.log_level, "INFO")
        self.assertEqual(config.vault_path.name, "vault")
        self.assertEqual(config.state_dir.name, ".notebooklm")
        self.assertIsNone(config.session_file)

    def test_ensure_directories(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = AlchemyConfig(
                vault_path=tmp_path / "custom_vault",
                state_dir=tmp_path / "custom_state",
            )
            config.ensure_directories()
            self.assertTrue(config.vault_path.is_dir())
            self.assertTrue(config.state_dir.is_dir())


if __name__ == "__main__":
    unittest.main()
