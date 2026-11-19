"""Dual-transport MCP server (`stdio` and `streamable-http`) for Notebook Alchemy.

Usable across Claude Desktop, Antigravity, ChatGPT Codex (`stdio`), and ChatGPT Desktop Apps &
Connectors (`streamable-http` over HTTPS). All business logic delegates to `service.py`.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Literal

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl

from . import __version__, ui
from .channel import CHANNEL_TABS, DEFAULT_TABS, ChannelScanner
from .claude import scrub as scrub_claude
from .config import AlchemyConfig, validate_profile_name
from .errors import AlchemyError, ConfigError
from .service import (
    clean_service,
    get_status,
    list_notebooks_service,
    preview_channel,
    run_pipeline_service,
)
from .watch import scrub as scrub_gemini

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
DEFAULT_HTTP_PATH = "/mcp"


class StaticBearerVerifier(TokenVerifier):
    """Constant-time bearer token verifier for Streamable HTTP deployments."""

    def __init__(self, expected_token: str, resource_url: str) -> None:
        self._expected = expected_token
        self._resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self._expected or not hmac.compare_digest(
            token.encode("utf-8"), self._expected.encode("utf-8")
        ):
            return None
        return AccessToken(
            token=token,
            client_id="notebook-alchemy-client",
            scopes=[],
            resource=self._resource_url,
        )


def _scrub_error(exc: AlchemyError, config: AlchemyConfig) -> ToolError:
    msg = exc.message
    if exc.hint:
        msg = f"{msg} Next: {exc.hint}"
    msg = scrub_claude(scrub_gemini(msg, config.gemini_api_key), config.api_key)
    if config.mcp_token:
        msg = msg.replace(config.mcp_token, "***")
    return ToolError(msg)


def _validate_youtube_source(source: str, *, mock: bool) -> str:
    if mock:
        return source
    if not ChannelScanner().is_youtube_url(source):
        raise ConfigError(
            "MCP tools only accept YouTube channel, playlist, or video URLs (or @handles).",
            hint="Pass a youtube.com / youtu.be URL or @handle, or set mock=True for sample data.",
        )
    return source


def _config_for(profile: str | None = None) -> AlchemyConfig:
    config = AlchemyConfig.from_env()
    if profile:
        config.notebooklm_profile = validate_profile_name(profile, source="profile")
    return config


def _parse_tabs(tabs: list[str] | None) -> tuple[str, ...]:
    if not tabs:
        return DEFAULT_TABS
    cleaned = tuple(dict.fromkeys(t.strip().lower() for t in tabs if t and t.strip()))
    unknown = [t for t in cleaned if t not in CHANNEL_TABS]
    if unknown or not cleaned:
        raise ConfigError(
            f"Unknown channel tab(s): {', '.join(unknown) or '(none)'}.",
            hint=f"Use one or more of: {', '.join(CHANNEL_TABS)}.",
        )
    return cleaned


def build_mcp_server(
    *,
    bearer_token: str | None = None,
    resource_url: str = f"http://{DEFAULT_HTTP_HOST}:{DEFAULT_HTTP_PORT}{DEFAULT_HTTP_PATH}",
) -> MCPServer:
    """Create and configure the Notebook Alchemy MCPServer instance."""
    token = (bearer_token or "").strip()
    token_verifier = StaticBearerVerifier(token, resource_url) if token else None
    auth_settings = (
        AuthSettings(
            issuer_url=AnyHttpUrl(resource_url),
            resource_server_url=AnyHttpUrl(resource_url),
            required_scopes=[],
            validate_token_resource=False,
        )
        if token
        else None
    )

    server = MCPServer(
        name="notebook-alchemy",
        title="Notebook Alchemy",
        description="Distill YouTube channels into dated, deduplicated topic books in Google NotebookLM.",
        instructions=(
            "Use `alchemy_status` first to check NotebookLM sign-in and AI API key readiness. "
            "Use `alchemy_preview` before `alchemy_run` to show video counts and estimated API cost. "
            "On a first upload (or unrecorded account), confirm the target Google account with the user "
            "and pass `confirm_upload=True`."
        ),
        version=__version__,
        token_verifier=token_verifier,
        auth=auth_settings,
    )

    @server.tool(
        name="alchemy_status",
        title="Check Notebook Alchemy Status",
        description=(
            "Check NotebookLM authentication, target Google account, Claude/Gemini API key readiness, "
            "active batches, and mapped channel notebooks."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False),
    )
    def alchemy_status(profile: str | None = None, check_live: bool = False) -> dict[str, Any]:
        cfg = AlchemyConfig()
        try:
            cfg = _config_for(profile)
            return get_status(cfg, check_live=check_live)
        except AlchemyError as exc:
            raise _scrub_error(exc, cfg) from None

    @server.tool(
        name="alchemy_preview",
        title="Preview Channel Cost & Coverage",
        description=(
            "Scan a YouTube channel, playlist, or video URL and estimate word counts, cached videos, "
            "and Claude/Gemini --watch cost without modifying NotebookLM or spending API credits."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
    )
    def alchemy_preview(
        source: str,
        limit: int | None = None,
        tabs: list[str] | None = None,
        watch_limit: int = 0,
        now: bool = False,
        mock: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any]:
        cfg = AlchemyConfig()
        try:
            cfg = _config_for(profile)
            safe_source = _validate_youtube_source(source, mock=mock)
            return preview_channel(
                cfg,
                safe_source,
                limit=limit,
                tabs=_parse_tabs(tabs),
                mock=mock,
                now=now,
                watch_limit=watch_limit,
            )
        except AlchemyError as exc:
            raise _scrub_error(exc, cfg) from None

    @server.tool(
        name="alchemy_run",
        title="Build & Upload Topic Books",
        description=(
            "Build deduplicated, dated topic books from a YouTube channel/playlist/video and upload them "
            "to NotebookLM (or save locally to `save_dir`). Confirm the Google upload account with the "
            "user and set `confirm_upload=True` for first-time or unrecorded accounts."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True),
    )
    def alchemy_run(
        source: str,
        notebook_name: str | None = None,
        save_dir: str | None = None,
        limit: int | None = None,
        tabs: list[str] | None = None,
        offline: bool = False,
        mock: bool = False,
        now: bool = False,
        resort: bool = False,
        watch_limit: int = 0,
        allow_fewer: bool = False,
        confirm_upload: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any]:
        cfg = AlchemyConfig()
        try:
            cfg = _config_for(profile)
            safe_source = _validate_youtube_source(source, mock=mock)
            return run_pipeline_service(
                cfg,
                safe_source,
                notebook_name=notebook_name,
                save_dir=Path(save_dir).expanduser() if save_dir else None,
                limit=limit,
                tabs=_parse_tabs(tabs),
                offline=offline,
                mock=mock,
                now=now,
                resort=resort,
                watch_limit=watch_limit,
                allow_fewer=allow_fewer,
                confirm_upload=confirm_upload,
            )
        except AlchemyError as exc:
            raise _scrub_error(exc, cfg) from None

    @server.tool(
        name="alchemy_notebooks",
        title="List Mapped Notebooks & Sources",
        description=(
            "List channels mapped to NotebookLM notebooks and local sync records; optionally inspect "
            "live sources in NotebookLM (including any sources stuck in preparing/error)."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=True),
    )
    def alchemy_notebooks(
        notebook_query: str | None = None,
        include_sources: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any]:
        cfg = AlchemyConfig()
        try:
            cfg = _config_for(profile)
            return list_notebooks_service(cfg, notebook_query=notebook_query, include_sources=include_sources)
        except AlchemyError as exc:
            raise _scrub_error(exc, cfg) from None

    @server.tool(
        name="alchemy_clean",
        title="Clean Local Caches & Records",
        description=(
            "Remove local Notebook Alchemy caches (`cache`), AI labels/sorting (`labels`), or upload "
            "records (`records`). Destructive cleans (`labels`, `records`, custom cache) need `confirm=True`."
        ),
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
    )
    def alchemy_clean(
        cache: bool = False,
        labels: bool = False,
        records: bool = False,
        confirm: bool = False,
        profile: str | None = None,
    ) -> dict[str, Any]:
        cfg = AlchemyConfig()
        try:
            cfg = _config_for(profile)
            return clean_service(cfg, cache=cache, labels=labels, records=records, confirm=confirm)
        except AlchemyError as exc:
            raise _scrub_error(exc, cfg) from None

    @server.resource("alchemy://status", mime_type="application/json")
    def status_resource() -> str:
        cfg = _config_for()
        return json.dumps(get_status(cfg, check_live=False), indent=2)

    @server.resource("alchemy://notebooks", mime_type="application/json")
    def notebooks_resource() -> str:
        cfg = _config_for()
        return json.dumps(list_notebooks_service(cfg, include_sources=False), indent=2)

    @server.prompt(
        name="distill_youtube_channel",
        title="Distill YouTube Channel to NotebookLM",
        description="Safe workflow for previewing, confirming cost/account, and building a channel notebook.",
    )
    def distill_prompt(channel_url: str) -> str:
        return (
            f"Run `alchemy_status` first, then `alchemy_preview` for {channel_url}. "
            "Share the video count, estimated cost, and target Google account with the user and wait "
            "for confirmation before calling `alchemy_run` with `confirm_upload=True`."
        )

    return server


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def serve(
    *,
    transport: Literal["stdio", "streamable-http"] = "stdio",
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT,
    path: str = DEFAULT_HTTP_PATH,
    token: str | None = None,
) -> None:
    """Start the Notebook Alchemy MCP server over stdio or loopback-only Streamable HTTP."""
    ui.route_to_stderr()
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr
    )
    config = AlchemyConfig.from_env()
    resolved_token = (token if token is not None else config.mcp_token).strip()
    if transport == "streamable-http":
        if host.strip().lower() not in LOOPBACK_HOSTS:
            raise ConfigError(
                f"Streamable HTTP refuses non-loopback host {host!r}; use 127.0.0.1.",
                hint="For desktop LLMs, use `notebook-alchemy mcp install all` (automatic stdio).",
            )
        if not resolved_token:
            raise ConfigError(
                "Streamable HTTP requires an access token even on localhost.",
                hint="Set ALCHEMY_MCP_TOKEN in ~/.notebook-alchemy/.env or pass --token <secret>.",
            )

    scheme = "http"
    resource_url = f"{scheme}://{host}:{port}{path}"
    server = build_mcp_server(bearer_token=resolved_token or None, resource_url=resource_url)
    if transport == "stdio":
        server.run(transport="stdio")
        return

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
    )
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path=path,
        transport_security=security,
    )


def main() -> None:
    """Console script entry point (`notebook-alchemy-mcp`), defaulting to stdio transport."""
    transport = os.getenv("ALCHEMY_MCP_TRANSPORT", "stdio").strip().lower()
    if transport not in ("stdio", "streamable-http"):
        transport = "stdio"
    host = os.getenv("ALCHEMY_MCP_HOST", DEFAULT_HTTP_HOST).strip() or DEFAULT_HTTP_HOST
    port = int(os.getenv("ALCHEMY_MCP_PORT", str(DEFAULT_HTTP_PORT)))
    serve(transport=transport, host=host, port=port)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
