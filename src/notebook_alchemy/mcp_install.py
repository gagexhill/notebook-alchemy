"""Atomic MCP client installer for Claude Desktop, Antigravity, ChatGPT Codex, and editors.

Reads and merges only the `notebook-alchemy` server entry into each client's configuration file
without clobbering unrelated servers or settings, and prints exact Streamable HTTP + tunnel setup
guidance for standard ChatGPT Desktop ("Apps & Connectors").
"""

from __future__ import annotations

import copy
import json
import os
import platform as _platform
import re
import shutil
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

SERVER_KEY = "notebook-alchemy"


@dataclass(frozen=True)
class McpClientSpec:
    key: str
    display_name: str
    fmt: str  # "json" | "toml"
    config_key: str
    path_for: Callable[[str, Path], Path]


def _windows_appdata(home: Path) -> Path:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) if appdata else home / "AppData" / "Roaming"


def _claude_desktop_path(system: str, home: Path) -> Path:
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return _windows_appdata(home) / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _antigravity_path(_system: str, home: Path) -> Path:
    return home / ".gemini" / "antigravity" / "mcp_config.json"


def _chatgpt_codex_path(_system: str, home: Path) -> Path:
    return home / ".codex" / "config.toml"


def _claude_code_path(_system: str, home: Path) -> Path:
    return home / ".claude.json"


def _cursor_path(_system: str, home: Path) -> Path:
    return home / ".cursor" / "mcp.json"


MCP_CLIENTS: dict[str, McpClientSpec] = {
    "claude-desktop": McpClientSpec(
        "claude-desktop", "Claude Desktop", "json", "mcpServers", _claude_desktop_path
    ),
    "antigravity": McpClientSpec("antigravity", "Antigravity", "json", "mcpServers", _antigravity_path),
    "chatgpt-codex": McpClientSpec(
        "chatgpt-codex", "ChatGPT Codex", "toml", "mcp_servers", _chatgpt_codex_path
    ),
    "claude-code": McpClientSpec("claude-code", "Claude Code", "json", "mcpServers", _claude_code_path),
    "cursor": McpClientSpec("cursor", "Cursor", "json", "mcpServers", _cursor_path),
}

DESKTOP_ALL_CLIENTS = ("claude-desktop", "antigravity", "chatgpt-codex")
SUPPORTED_INSTALL_TARGETS = (*MCP_CLIENTS, "chatgpt-http", "all")


def resolve_server_command() -> tuple[str, list[str]]:
    """Executable + args for launching `notebook-alchemy mcp serve` reliably from desktop hosts."""
    if found := shutil.which("notebook-alchemy"):
        return found, ["mcp", "serve"]
    return sys.executable, ["-m", "notebook_alchemy.cli", "mcp", "serve"]


def build_json_block(command: str | None = None, args: list[str] | None = None) -> dict[str, Any]:
    cmd, default_args = resolve_server_command()
    return {
        "command": command or cmd,
        "args": list(args if args is not None else default_args),
    }


def resolve_client_path(client: str, *, system: str | None = None, home: Path | None = None) -> Path:
    spec = MCP_CLIENTS.get(client)
    if spec is None:
        raise ConfigError(
            f"Unknown MCP client {client!r}.",
            hint=f"Choose from: {', '.join(SUPPORTED_INSTALL_TARGETS)}.",
        )
    return spec.path_for(system or _platform.system(), home or Path.home())


def merge_json_config(
    existing: dict[str, Any],
    *,
    config_key: str = "mcpServers",
    server_key: str = SERVER_KEY,
    block: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Read-modify-merge `server_key` inside `config_key` without disturbing other entries."""
    new_cfg = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    servers = new_cfg.get(config_key)
    if not isinstance(servers, dict):
        servers = {}
    new_cfg[config_key] = servers
    desired = block if block is not None else build_json_block()
    current = servers.get(server_key)
    if current == desired:
        action = "unchanged"
    elif server_key in servers:
        action = "updated"
    else:
        action = "created"
    servers[server_key] = desired
    return new_cfg, action


def _toml_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_toml_section(command: str, args: list[str]) -> str:
    args_str = ", ".join(_toml_quote(a) for a in args)
    return f"[mcp_servers.{SERVER_KEY}]\ncommand = {_toml_quote(command)}\nargs = [{args_str}]\n"


def merge_toml_config(existing_text: str, *, block: dict[str, Any] | None = None) -> tuple[str, str]:
    """Merge `[mcp_servers.notebook-alchemy]` into TOML text, preserving other tables & comments."""
    if existing_text.strip():
        try:
            parsed = tomllib.loads(existing_text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"Existing TOML config is not valid TOML: {exc}",
                hint="Fix the syntax error in that config file, then run `mcp install` again.",
            ) from exc
    else:
        parsed = {}

    desired = block if block is not None else build_json_block()
    current = (parsed.get("mcp_servers") or {}).get(SERVER_KEY)
    if current == desired:
        return existing_text, "unchanged"

    section_text = _format_toml_section(str(desired["command"]), [str(a) for a in desired["args"]])
    header_re = re.compile(rf"(?ms)^\[mcp_servers\.{re.escape(SERVER_KEY)}\]\s*\r?\n.*?(?=^\[|\Z)")
    if header_re.search(existing_text):
        replaced = header_re.sub(lambda _m: section_text.rstrip() + "\n\n", existing_text, count=1)
        updated = replaced.rstrip() + "\n"
        action = "updated"
    else:
        if not existing_text or existing_text.endswith("\n\n"):
            sep = ""
        elif existing_text.endswith("\n"):
            sep = "\n"
        else:
            sep = "\n\n"
        updated = f"{existing_text}{sep}{section_text}"
        action = "created"

    # Verify the resulting TOML parses cleanly and matches desired values.
    verify = tomllib.loads(updated)
    if (verify.get("mcp_servers") or {}).get(SERVER_KEY) != desired:
        raise ConfigError(
            "Updated TOML configuration did not round-trip cleanly.",
            hint="Inspect the config file and run `mcp install` again.",
        )
    return updated, action


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def install_client(client: str, *, config_path: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Install/update the `notebook-alchemy` MCP server config for `client`."""
    spec = MCP_CLIENTS.get(client)
    if spec is None:
        raise ConfigError(
            f"Unknown MCP client {client!r}.",
            hint=f"Choose from: {', '.join(SUPPORTED_INSTALL_TARGETS)}.",
        )
    target = config_path or resolve_client_path(client)
    block = build_json_block()

    if spec.fmt == "json":
        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise ConfigError(
                    f"Could not read existing MCP config at {target}: {exc}",
                    hint="Fix the JSON syntax in that file so unrelated servers are not lost.",
                ) from exc
            if not isinstance(existing, dict):
                raise ConfigError(
                    f"{target} does not contain a JSON object.",
                    hint="Repair the file before running `mcp install`.",
                )
        else:
            existing = {}
        merged, action = merge_json_config(existing, config_key=spec.config_key, block=block)
        rendered = json.dumps(merged, indent=2) + "\n"
    else:
        existing_text = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
        rendered, action = merge_toml_config(existing_text, block=block)

    if not dry_run and action != "unchanged":
        _atomic_write(target, rendered)

    return {
        "client": client,
        "display_name": spec.display_name,
        "path": str(target),
        "action": action,
        "dry_run": dry_run,
        "block": block,
    }


def chatgpt_http_guide(*, port: int = 8765, has_token: bool = False) -> dict[str, Any]:
    """Setup instructions for ChatGPT Desktop Apps & Connectors (HTTPS Streamable HTTP)."""
    serve_cmd = f"notebook-alchemy mcp serve --transport streamable-http --port {port} --allow-tunnel"
    return {
        "client": "chatgpt-http",
        "display_name": "ChatGPT Desktop (Apps & Connectors)",
        "serve_command": serve_cmd,
        "local_endpoint": f"http://127.0.0.1:{port}/mcp",
        "auth_env": "ALCHEMY_MCP_TOKEN",
        "token_configured": has_token,
        "steps": [
            "Set ALCHEMY_MCP_TOKEN=<long-random-secret> in ~/.notebook-alchemy/.env.",
            f"Start the HTTP server: {serve_cmd}",
            f"Expose http://127.0.0.1:{port} over HTTPS (e.g. cloudflared tunnel or Tailscale Funnel).",
            (
                "In ChatGPT Desktop -> Settings -> Apps & Connectors -> Add custom MCP server, "
                "enter https://<your-tunnel-host>/mcp and your Bearer token."
            ),
        ],
    }
