"""Unit and integration tests for the CLI commands."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notebook_alchemy.cli import cli


class TestCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_cli_version(self) -> None:
        result = self.runner.invoke(cli, ["--version"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("0.1.0", result.output)

    def test_cli_status(self) -> None:
        result = self.runner.invoke(cli, ["status"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Notebook Alchemy Status", result.output)

    def test_cli_dry_run(self) -> None:
        result = self.runner.invoke(cli, ["dry-run", "-n", "Portable Entrepreneur"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Dry-Run Cost & Token Sizing", result.output)
        self.assertIn("Total Estimated Cost", result.output)
        self.assertIn("SEO & Local Search", result.output)

    def test_cli_run_preview(self) -> None:
        result = self.runner.invoke(cli, ["run", "-n", "Portable Entrepreneur", "--preview-only"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Generated Domain Compendiums", result.output)
        self.assertIn("Distillation Completed", result.output)

    def test_cli_automated_test(self) -> None:
        result = self.runner.invoke(cli, ["test"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Pipeline Verification Results", result.output)
        self.assertIn("All automated pipeline verification checks PASSED", result.output)

    def test_cli_append(self) -> None:
        result = self.runner.invoke(
            cli,
            [
                "append",
                "-n",
                "Portable Entrepreneur",
                "--title",
                "How to Price SEO Services ($2,500/mo Retainer Guide)",
                "--url",
                "https://youtube.com/watch?v=mock_new_01",
                "--topic",
                "Pricing & Proposal Strategy",
            ],
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Successfully appended to Master Catalog", result.output)

    def test_cli_sync(self) -> None:
        result = self.runner.invoke(cli, ["sync", "-n", "Portable Entrepreneur"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Successfully synced compendiums to notebook", result.output)


if __name__ == "__main__":
    unittest.main()
