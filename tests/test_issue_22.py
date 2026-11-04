"""Regression coverage for honest, bounded synthesis."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from notebook_alchemy.cli import cli
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.engine import AlchemyEngine
from notebook_alchemy.synthesizer import Synthesizer


def test_offline_is_a_source_index_without_invented_content():
    videos = [{"id": "one", "title": "Quantum fields", "url": "https://example.com/one"}]
    result = Synthesizer().synthesize_cluster("Physics", videos, force_offline=True)
    assert "index" in result.content.lower()
    assert "Quantum fields" in result.content
    assert "https://example.com/one" in result.content
    assert "High-Yield Takeaway" not in result.content
    assert "SOP-01" not in result.content
    assert "notebooklm.google.com/notebook/" not in result.content
    assert "[[Lead_Generation" not in result.content


def test_live_covers_all_sources_and_surfaces_output_truncation():
    videos = [
        {
            "id": str(i),
            "title": f"Video {i}",
            "url": f"https://example.com/{i}",
            "transcript": "a" * 20000,
        }
        for i in range(50)
    ]
    response = MagicMock()
    response.json.return_value = {
        "candidates": [
            {"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial output"}]}}
        ]
    }
    with patch("notebook_alchemy.synthesizer.httpx.Client") as client:
        client.return_value.__enter__.return_value.post.return_value = response
        comp = Synthesizer(api_key="valid-test-key-123456").synthesize_cluster("Physics", videos)
        prompt = client.return_value.__enter__.return_value.post.call_args.kwargs["json"][
            "contents"
        ][0]["parts"][0]["text"]
    assert all(f"Video {i}" in prompt and f"Video {i}" in comp.content for i in range(50))
    assert len(prompt) < 250000
    assert any("MAX_TOKENS" in warning for warning in comp.warnings)


def test_offline_never_uses_gemini_for_clustering(tmp_path):
    engine = AlchemyEngine(
        AlchemyConfig(
            gemini_api_key="valid-test-key-123456",
            vault_path=tmp_path / "v",
            state_dir=tmp_path / "s",
        )
    )
    with patch.object(engine.analyzer, "_cluster_topics_with_gemini", return_value={}) as call:
        report = engine.run_distillation(
            "Portable Entrepreneur", force_offline=True, write_to_vault=False
        )
    call.assert_not_called()
    assert report.compendiums


def test_offline_related_links_resolve_for_colliding_domain_names(tmp_path):
    engine = AlchemyEngine(AlchemyConfig(vault_path=tmp_path / "v", state_dir=tmp_path / "s"))
    report = engine.analyzer.generate_dry_run("Portable Entrepreneur", allow_llm=False)
    title = report.youtube_videos[0].title
    report.domain_topic_clusters = {"A & B": [title], "A B": [title], "INDEX": [title]}
    with patch.object(engine.analyzer, "generate_dry_run", return_value=report):
        result = engine.run_distillation("example", force_offline=True)
    paths = [engine.vault.vault_path / comp.relative_path for comp in result.compendiums]
    assert len({str(path).lower() for path in paths}) == 3
    for comp, path in zip(result.compendiums, paths):
        assert path.read_text(encoding="utf-8") == comp.content
        for other in paths:
            if other != path:
                assert f"]({other.name})" in comp.content


def test_duplicate_titles_keep_distinct_sources_across_volumes(tmp_path):
    engine = AlchemyEngine(AlchemyConfig(vault_path=tmp_path / "v", state_dir=tmp_path / "s"))
    report = engine.analyzer.generate_dry_run("Portable Entrepreneur", allow_llm=False)
    first, second = report.youtube_videos[:2]
    second.title = first.title
    report.youtube_videos = [first, second]
    report.domain_topic_clusters = {"Domain": [first.title, second.title]}
    with patch.object(engine.analyzer, "generate_dry_run", return_value=report):
        result = engine.run_distillation(
            "example", force_offline=True, write_to_vault=False, max_videos_per_volume=1
        )
    assert [comp.source_ids[0] for comp in result.compendiums] == [
        first.source_id,
        second.source_id,
    ]


def test_cli_defaults_to_live_with_key_and_prints_model():
    runner = CliRunner()
    with (
        patch(
            "notebook_alchemy.engine.Synthesizer._call_gemini_api", return_value=("result", [])
        ) as call,
        patch(
            "notebook_alchemy.analyzer.NotebookAnalyzer._cluster_topics_with_gemini",
            return_value={},
        ),
    ):
        result = runner.invoke(
            cli,
            ["run", "Portable Entrepreneur", "--preview-only"],
            env={"GEMINI_API_KEY": "valid-test-key-123456"},
        )
    assert result.exit_code == 0, result.output
    assert call.called
    assert "gemini-2.5-flash" in result.output


def test_empty_live_response_falls_back_to_index():
    response = MagicMock()
    response.json.return_value = {"candidates": [{"content": {"parts": []}}]}
    with patch("notebook_alchemy.synthesizer.httpx.Client") as client:
        client.return_value.__enter__.return_value.post.return_value = response
        result = Synthesizer(api_key="valid-test-key-123456").synthesize_cluster(
            "Physics", [{"title": "No URL", "transcript": "text"}]
        )
    assert not result.is_live_synthesized
    assert "Offline title-only index" in result.content
    assert "https://youtube.com" not in result.content
    assert any("failed" in warning for warning in result.warnings)


def test_cli_offline_overrides_key_and_reports_truncation():
    runner = CliRunner()
    with (
        patch("notebook_alchemy.engine.Synthesizer._call_gemini_api") as call,
        patch("notebook_alchemy.analyzer.NotebookAnalyzer._cluster_topics_with_gemini") as cluster,
    ):
        result = runner.invoke(
            cli,
            ["run", "Portable Entrepreneur", "--preview-only", "--offline"],
            env={"GEMINI_API_KEY": "valid-test-key-123456"},
        )
    assert result.exit_code == 0, result.output
    call.assert_not_called()
    cluster.assert_not_called()
    assert "offline source index" in result.output

    with (
        patch(
            "notebook_alchemy.engine.Synthesizer._call_gemini_api",
            return_value=("partial", ["Gemini output was truncated (MAX_TOKENS)."]),
        ),
        patch(
            "notebook_alchemy.analyzer.NotebookAnalyzer._cluster_topics_with_gemini",
            return_value={},
        ),
    ):
        result = runner.invoke(
            cli,
            ["run", "Portable Entrepreneur", "--preview-only"],
            env={"GEMINI_API_KEY": "valid-test-key-123456"},
        )
    assert result.exit_code == 0, result.output
    assert "MAX_TOKENS" in result.output
