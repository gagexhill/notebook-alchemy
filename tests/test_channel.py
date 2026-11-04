"""Unit tests for the ChannelScanner and TranscriptManager modules."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from notebook_alchemy.channel import ChannelScanner, TranscriptManager


class TestChannelScanner(unittest.TestCase):
    def setUp(self) -> None:
        self.scanner = ChannelScanner()

    def test_normalize_url(self) -> None:
        self.assertEqual(
            self.scanner.normalize_url("@Portableentrepreneur"),
            "https://www.youtube.com/@Portableentrepreneur/videos",
        )
        self.assertEqual(
            self.scanner.normalize_url("https://www.youtube.com/@Portableentrepreneur"),
            "https://www.youtube.com/@Portableentrepreneur/videos",
        )
        self.assertEqual(
            self.scanner.normalize_url("https://www.youtube.com/@Portableentrepreneur/videos"),
            "https://www.youtube.com/@Portableentrepreneur/videos",
        )

    def test_is_youtube_url(self) -> None:
        self.assertTrue(self.scanner.is_youtube_url("https://www.youtube.com/@hubspot"))
        self.assertTrue(self.scanner.is_youtube_url("https://youtu.be/abc123xyz"))
        self.assertTrue(self.scanner.is_youtube_url("@Portableentrepreneur"))
        self.assertFalse(self.scanner.is_youtube_url("Portable Entrepreneur"))
        self.assertFalse(self.scanner.is_youtube_url("6fd9a6b5-5065-4c0f-ba7e-711c712fc3c9"))

    def test_scan_offline_fallback(self) -> None:
        res = self.scanner.scan(
            "https://www.youtube.com/@Portableentrepreneur", limit=50, force_offline=True
        )
        self.assertEqual(res.channel_name, "Portable Entrepreneur")
        self.assertEqual(len(res.videos), 50)
        self.assertEqual(res.total_videos, 50)
        self.assertFalse(res.is_live)
        self.assertTrue(res.videos[0].url.startswith("https://www.youtube.com"))


class TestTranscriptManager(unittest.TestCase):
    def test_transcript_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manager = TranscriptManager(cache_dir=Path(tmp_dir))
            cache_file = Path(tmp_dir) / "test_vid.json"
            cache_file.write_text(
                '{"video_id": "test_vid", "full_text": "Cached transcript sample."}',
                encoding="utf-8",
            )

            text = manager.fetch_transcript_text("test_vid")
            self.assertEqual(text, "Cached transcript sample.")

    def test_transcript_local_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create a mock .txt transcript file
            txt_file = tmp_path / "vid_123.txt"
            txt_file.write_text("Local text transcript for vid 123.", encoding="utf-8")

            manager = TranscriptManager(transcripts_dir=tmp_path)
            text = manager.fetch_transcript_text("vid_123")
            self.assertEqual(text, "Local text transcript for vid 123.")


if __name__ == "__main__":
    unittest.main()
