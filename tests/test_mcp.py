"""Tests for service.py, mcp_server.py (stdio + Streamable HTTP), and mcp_install.py."""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from notebook_alchemy.channel import ChannelScanner, ChannelScanResult, ChannelVideo
from notebook_alchemy.cli import cli
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.errors import AlchemyError, SessionError
from notebook_alchemy.mcp_install import (
    _claude_desktop_path,
    build_json_block,
    install_client,
    merge_json_config,
    merge_toml_config,
    resolve_client_path,
)
from notebook_alchemy.mcp_server import DEFAULT_HTTP_PATH, StaticBearerVerifier, build_mcp_server, serve
from notebook_alchemy.service import (
    clean_service,
    get_status,
    list_notebooks_service,
    preview_channel,
    run_pipeline_service,
)


def _cfg(tmp_path: Path, **overrides: Any) -> AlchemyConfig:
    defaults: dict[str, Any] = {
        "cache_dir": tmp_path / ".notebook-alchemy" / "cache",
        "api_key": "",
        "gemini_api_key": "",
    }
    defaults.update(overrides)
    return AlchemyConfig(**defaults)


# --- service.py -----------------------------------------------------------------------------------


def test_service_status_and_preview_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from notebook_alchemy.client import AccountIdentity, NotebookLMClient

    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity("default", "u@example.test")
    )
    cfg = _cfg(tmp_path, api_key="sk-ant-TEST-ONLY-000000000", gemini_api_key="AIzaTESTonly00000000")
    st = get_status(cfg, check_live=False)
    assert st["claude"]["configured"] is True
    assert st["gemini_watch"]["configured"] is True
    assert st["live"] is None
    # Secrets must never appear in status output.
    serialized = json.dumps(st)
    assert "sk-ant-TEST-ONLY" not in serialized and "AIzaTESTonly" not in serialized

    prev = preview_channel(cfg, "sample", mock=True, watch_limit=1)
    assert prev["is_mock"] is True and prev["videos"] > 0


def test_service_run_mock_requires_save_dir_and_saves_books(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    with pytest.raises(AlchemyError, match="never uploaded"):
        run_pipeline_service(cfg, "sample", mock=True)
    out_dir = tmp_path / "out"
    res = run_pipeline_service(cfg, "sample", mock=True, save_dir=out_dir)
    assert res["status"] == "saved" and res["books_count"] >= 1
    assert any(Path(p).name == "_OVERVIEW.md" for p in res["saved_files"])


def test_service_run_upload_requires_confirm_upload_on_first_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.client import AccountIdentity, NotebookLMClient, SessionStatus
    from tests.conftest import FakeNotebookLM

    fake = FakeNotebookLM()
    monkeypatch.setattr("notebook_alchemy.client.default_api", fake)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 0))
    monkeypatch.setattr(
        NotebookLMClient, "upload_account", lambda self: AccountIdentity("default", "u@example.test")
    )
    monkeypatch.setattr(
        ChannelScanner,
        "scan",
        lambda self, url, limit=None, **kw: ChannelScanResult(
            "Chan", [ChannelVideo("vid00000001", "Topic A", "u", 300, "2026-03-01")]
        ),
    )
    from notebook_alchemy.channel import TranscriptManager, TranscriptResult

    monkeypatch.setattr(
        TranscriptManager,
        "fetch_many",
        lambda self, items, progress=None: {
            "vid00000001": TranscriptResult("vid00000001", "Detailed fact one.", "youtube")
        },
    )
    cfg = _cfg(tmp_path)
    with pytest.raises(SessionError, match="Upload confirmation required"):
        run_pipeline_service(cfg, "@chan", offline=True, confirm_upload=False)
    res = run_pipeline_service(cfg, "@chan", offline=True, confirm_upload=True)
    assert res["status"] == "uploaded" and res["uploaded"] >= 1


def test_service_clean_requires_confirm_for_labels_and_records(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    knowledge = cfg.cache_dir.parent / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "vid1.json").write_text('{"key": "k", "segments": []}', encoding="utf-8")
    with pytest.raises(AlchemyError, match="requires confirmation"):
        clean_service(cfg, labels=True, confirm=False)
    res = clean_service(cfg, labels=True, confirm=True)
    assert res["removed"] == 1 and not (knowledge / "vid1.json").exists()


def test_record_cleanup_removes_finished_run_receipts_only(tmp_path: Path) -> None:
    from notebook_alchemy.run_registry import RunRegistry, receipts_dir

    registry = RunRegistry(receipts_dir())
    done = "a" * 32
    active = "b" * 32
    registry._write({"run_id": done, "status": "completed", "result": {"status": "saved"}})
    registry._write({"run_id": active, "status": "running"})
    result = clean_service(_cfg(tmp_path), records=True, confirm=True)
    assert result["removed"] == 1
    assert not (receipts_dir() / f"{done}.json").exists()
    assert (receipts_dir() / f"{active}.json").exists()


def test_service_list_notebooks_reports_preparing_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from notebook_alchemy.client import NotebookLMClient, SessionStatus
    from tests.conftest import FakeNotebookLM

    fake = FakeNotebookLM()
    nb = fake.seed("My Channel", ["Book 1"])
    fake.sources[nb][0]["status"] = "preparing"
    monkeypatch.setattr("notebook_alchemy.client.default_api", fake)
    monkeypatch.setattr(NotebookLMClient, "is_authenticated", lambda self: True)
    monkeypatch.setattr(NotebookLMClient, "check_session", lambda self: SessionStatus(True, "ok", 1))
    res = list_notebooks_service(_cfg(tmp_path), notebook_query="My Channel", include_sources=True)
    assert res["selected_sources"] == [
        {
            "id": fake.sources[nb][0]["id"],
            "title": "Book 1",
            "url": "",
            "type": "markdown",
            "status": "preparing",
        }
    ]


# --- mcp_server.py --------------------------------------------------------------------------------


def test_mcp_server_tools_resources_prompts_and_stdout_purity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from notebook_alchemy import ui
    from notebook_alchemy.client import AccountIdentity, NotebookLMClient

    async def _run() -> None:
        ui.route_to_stderr()
        monkeypatch.setattr(
            NotebookLMClient, "upload_account", lambda self: AccountIdentity("default", "u@example.test")
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-TEST-ONLY-secret-key-0000")
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaTESTonlySecretGemini000000")
        server = build_mcp_server()
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert {
            "alchemy_status",
            "alchemy_preview",
            "alchemy_run",
            "alchemy_run_status",
            "alchemy_notebooks",
            "alchemy_clean",
        } <= names

        status_res = await server.call_tool("alchemy_status", {"check_live": False})
        assert status_res.is_error is False
        structured = status_res.structured_content or {}
        assert structured["claude"]["configured"] is True
        assert "sk-ant-TEST-ONLY" not in json.dumps(structured)
        assert "AIzaTESTonly" not in json.dumps(structured)

        save_dir = tmp_path / "mcp_books"
        run_tool_res = await server.call_tool(
            "alchemy_run", {"source": "sample", "mock": True, "save_dir": str(save_dir)}
        )
        assert run_tool_res.is_error is False
        run_res = run_tool_res.structured_content or {}
        assert run_res["status"] == "running"
        for _ in range(100):
            receipt = await server.call_tool("alchemy_run_status", {"run_id": run_res["run_id"]})
            run_res = receipt.structured_content or {}
            if run_res["status"] != "running":
                break
            await asyncio.sleep(0.01)
        assert run_res["status"] == "completed"
        assert run_res["result"]["status"] == "saved"

        resources = await server.list_resources()
        assert {str(r.uri) for r in resources} >= {"alchemy://status", "alchemy://notebooks"}
        prompt = await server.get_prompt("distill_youtube_channel", {"channel_url": "https://youtube.com/@x"})
        assert "alchemy_preview" in prompt.messages[0].content.text

        # Nothing was printed to stdout when running tools.
        captured = capsys.readouterr()
        assert captured.out == ""

    asyncio.run(_run())


def test_mcp_server_scrubs_secrets_in_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    async def _run() -> None:
        secret = "sk-ant-TEST-ONLY-scrub-me-123456"
        mcp_secret = "mcp-TEST-ONLY-bearer-999"
        monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
        monkeypatch.setenv("ALCHEMY_MCP_TOKEN", mcp_secret)
        monkeypatch.setattr(
            "notebook_alchemy.mcp_server.preview_channel",
            lambda *a, **kw: (_ for _ in ()).throw(AlchemyError(f"boom {secret} {mcp_secret}", hint="retry")),
        )
        server = build_mcp_server()
        with pytest.raises(ToolError) as err:
            await server.call_tool("alchemy_preview", {"source": "@x"})
        assert secret not in str(err.value) and mcp_secret not in str(err.value) and "***" in str(err.value)

    asyncio.run(_run())


def test_mcp_server_guards_youtube_urls_and_loopback_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from notebook_alchemy.errors import ConfigError

    async def _run() -> None:
        server = build_mcp_server()
        for bad_source in ("http://127.0.0.1:8080/secret", "file:///etc/passwd", "not a url"):
            with pytest.raises(ToolError, match="only accept YouTube"):
                await server.call_tool("alchemy_preview", {"source": bad_source})
            with pytest.raises(ToolError, match="only accept YouTube"):
                await server.call_tool("alchemy_run", {"source": bad_source})

        monkeypatch.delenv("ALCHEMY_MCP_TOKEN", raising=False)
        with pytest.raises(ConfigError, match="non-loopback host"):
            serve(transport="streamable-http", host="0.0.0.0", token="secret-token")  # noqa: S104
        with pytest.raises(ConfigError, match="requires an access token"):
            serve(transport="streamable-http", host="127.0.0.1", token="")

    asyncio.run(_run())


def test_mcp_client_session_memory_stream_roundtrip() -> None:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    async def _run() -> None:
        server = build_mcp_server()
        lowlevel = server._lowlevel_server
        async with create_client_server_memory_streams() as (client_streams, server_streams):
            c_read, c_write = client_streams
            s_read, s_write = server_streams
            async with anyio.create_task_group() as tg:
                tg.start_soon(lowlevel.run, s_read, s_write, lowlevel.create_initialization_options())
                async with ClientSession(c_read, c_write) as client:
                    await client.initialize()
                    tools = await client.list_tools()
                    assert "alchemy_preview" in {t.name for t in tools.tools}
                    call_res = await client.call_tool("alchemy_preview", {"source": "sample", "mock": True})
                    assert not call_res.is_error
                    assert call_res.structured_content and call_res.structured_content["is_mock"] is True
                    tg.cancel_scope.cancel()

    asyncio.run(_run())


def test_mcp_bearer_verifier_and_streamable_http_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from notebook_alchemy.client import AccountIdentity, NotebookLMClient

    async def _run() -> None:
        monkeypatch.setattr(
            NotebookLMClient, "upload_account", lambda self: AccountIdentity("default", "u@example.test")
        )
        verifier = StaticBearerVerifier("secret-token-123", f"http://127.0.0.1:8765{DEFAULT_HTTP_PATH}")
        assert await verifier.verify_token("wrong") is None
        tok = await verifier.verify_token("secret-token-123")
        assert tok is not None and tok.client_id == "notebook-alchemy-client"

        server = build_mcp_server(bearer_token="secret-token-123")
        app = server.streamable_http_app(streamable_http_path=DEFAULT_HTTP_PATH, json_response=True)
        assert any(getattr(r, "path", "") == DEFAULT_HTTP_PATH for r in app.routes)
        headers_ok = {
            "Authorization": "Bearer secret-token-123",
            "Accept": "application/json, text/event-stream",
        }
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
            ) as http:
                unauth = await http.post(
                    DEFAULT_HTTP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                )
                assert unauth.status_code == 401
                bad = await http.post(
                    DEFAULT_HTTP_PATH,
                    headers={"Authorization": "Bearer wrong-token"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert bad.status_code == 401

                init = await http.post(
                    DEFAULT_HTTP_PATH,
                    headers=headers_ok,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1.0"},
                        },
                    },
                )
                assert init.status_code == 200
                session_id = init.headers["mcp-session-id"]
                session_headers = {**headers_ok, "mcp-session-id": session_id}

                notif = await http.post(
                    DEFAULT_HTTP_PATH,
                    headers=session_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                assert notif.status_code == 202

                call = await http.post(
                    DEFAULT_HTTP_PATH,
                    headers=session_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "alchemy_status", "arguments": {"check_live": False}},
                    },
                )
                assert call.status_code == 200
                payload = call.json()
                assert payload["result"]["structuredContent"]["notebooklm_profile"] == "default"

    asyncio.run(_run())


# --- mcp_install.py -------------------------------------------------------------------------------


def test_mcp_install_resolves_per_os_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    win = resolve_client_path("claude-desktop", system="Windows", home=tmp_path)
    mac = resolve_client_path("claude-desktop", system="Darwin", home=tmp_path)
    lin = resolve_client_path("claude-desktop", system="Linux", home=tmp_path)
    assert win == tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    assert "Application Support" in str(mac)
    assert ".config" in str(lin)
    assert resolve_client_path("antigravity", home=tmp_path) == (
        tmp_path / ".gemini" / "config" / "mcp_config.json"
    )
    assert resolve_client_path("chatgpt-codex", home=tmp_path) == (tmp_path / ".codex" / "config.toml")

    # Windows Store / MSIX Claude Desktop LocalCache fallback vs standard APPDATA precedence.
    local_root = tmp_path / "Local"
    msix_dir = local_root / "Packages" / "Claude_testpkg" / "LocalCache" / "Roaming" / "Claude"
    msix_dir.mkdir(parents=True)
    assert _claude_desktop_path("Windows", tmp_path, localappdata=local_root) == (
        msix_dir / "claude_desktop_config.json"
    )
    std_cfg = tmp_path / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json"
    std_cfg.parent.mkdir(parents=True)
    std_cfg.write_text("{}", encoding="utf-8")
    assert _claude_desktop_path("Windows", tmp_path, localappdata=local_root) == std_cfg


def test_mcp_install_json_and_toml_preserve_existing_entries(tmp_path: Path) -> None:
    # JSON merge preserves unrelated servers & top-level keys.
    merged, action = merge_json_config(
        {"theme": "dark", "mcpServers": {"other": {"command": "node", "args": ["x.js"]}}},
        block={"command": "notebook-alchemy", "args": ["mcp", "serve"]},
    )
    assert action == "created"
    assert merged["theme"] == "dark" and "other" in merged["mcpServers"]
    assert merged["mcpServers"]["notebook-alchemy"] == {
        "command": "notebook-alchemy",
        "args": ["mcp", "serve"],
    }
    merged2, action2 = merge_json_config(
        merged, block={"command": "notebook-alchemy", "args": ["mcp", "serve"]}
    )
    assert action2 == "unchanged" and merged2 == merged

    # TOML merge preserves unrelated tables & comments and updates cleanly.
    initial_toml = '# user config\nmodel = "gpt-5"\n\n[mcp_servers.other]\ncommand = "echo"\n'
    updated_toml, t_act = merge_toml_config(
        initial_toml, block={"command": "C:\\bin\\notebook-alchemy.exe", "args": ["mcp", "serve"]}
    )
    assert t_act == "created"
    assert "# user config" in updated_toml
    parsed = tomllib.loads(updated_toml)
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["other"]["command"] == "echo"
    assert parsed["mcp_servers"]["notebook-alchemy"]["args"] == ["mcp", "serve"]
    # Update existing section in place.
    updated2, t_act2 = merge_toml_config(
        updated_toml,
        block={"command": sys.executable, "args": ["-m", "notebook_alchemy.cli", "mcp", "serve"]},
    )
    assert t_act2 == "updated"
    assert tomllib.loads(updated2)["mcp_servers"]["notebook-alchemy"]["command"] == sys.executable


def test_mcp_install_client_dry_run_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "antigravity.json"
    dry = install_client("antigravity", config_path=target, dry_run=True)
    assert dry["action"] == "created" and not target.exists()
    real = install_client("antigravity", config_path=target, dry_run=False)
    assert real["action"] == "created" and target.is_file()
    assert (
        build_json_block() == json.loads(target.read_text(encoding="utf-8"))["mcpServers"]["notebook-alchemy"]
    )

    runner = CliRunner()
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    res = runner.invoke(cli, ["mcp", "install", "all", "--dry-run"])
    assert res.exit_code == 0 and "[Dry run]" in res.output
    st = runner.invoke(cli, ["mcp", "status"])
    assert st.exit_code == 0 and "Claude Desktop" in st.output and "Antigravity" in st.output
