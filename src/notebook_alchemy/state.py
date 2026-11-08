"""Moving the working files to another computer: `export-state` and `import-state`.

The working files are what a rerun builds on: captions, the AI's labels and sorting answers, the
section layout, the upload records and which notebook belongs to which channel. Copying them lets
another computer carry on without reading or paying for anything again.

Left out on purpose: sign-in sessions, the Gemini key (`.env`), the upload confirmation (it belongs
to an account, so the other computer confirms again) and a Gemini batch still running.
"""

from __future__ import annotations

import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import __version__
from .errors import AlchemyError

if TYPE_CHECKING:
    from .engine import AlchemyEngine

FORMAT = "notebook-alchemy-state"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
NOTEBOOKS = "notebooks.json"
# Folders in the archive, and where each lives on this computer.
FOLDERS = ("captions", "knowledge", "library", "sync")
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
_MEMBER = re.compile(r"^(captions|knowledge|library|sync)/[A-Za-z0-9._@ ()\[\]&'+,=-]{1,200}\.json$")


@dataclass
class StateSummary:
    path: Path
    files: dict[str, int] = field(default_factory=dict)  # folder -> files written (or exported)
    kept: int = 0  # files already here, left as they were (import without --replace)

    @property
    def total(self) -> int:
        return sum(self.files.values())


def _folder(engine: AlchemyEngine, name: str) -> Path:
    return {
        "captions": engine.config.cache_dir,
        "knowledge": engine.knowledge_dir(),
        "library": engine.home / "library",
        "sync": engine.sync_records_dir(),
    }[name]


def _json_object(raw: bytes) -> dict | None:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def export_state(engine: AlchemyEngine, path: Path) -> StateSummary:
    """Write the working files to a zip archive at `path`."""
    if path.exists():
        raise AlchemyError(f"{path} already exists.", hint="Choose a new file name.")
    summary = StateSummary(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in FOLDERS:
                folder = _folder(engine, name)
                for f in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                    member = f"{name}/{f.name}"
                    if _MEMBER.match(member) and _json_object(f.read_bytes()) is not None:
                        archive.write(f, member)
                        summary.files[name] = summary.files.get(name, 0) + 1
            notebooks = engine.notebooks_path()
            if notebooks.is_file() and _json_object(notebooks.read_bytes()) is not None:
                archive.write(notebooks, NOTEBOOKS)
                summary.files["notebooks"] = 1
            manifest = {
                "format": FORMAT,
                "version": FORMAT_VERSION,
                "tool": __version__,
                "files": summary.files,
            }
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
    except OSError as exc:
        path.unlink(missing_ok=True)
        raise AlchemyError(
            f"Could not write {path}: {exc}", hint="Check the folder exists and is writable."
        ) from exc
    return summary


def import_state(engine: AlchemyEngine, path: Path, *, replace: bool = False) -> StateSummary:
    """Copy the working files from an archive made by `export_state`.

    Every member is checked before anything is written: an archive with a file outside the known
    folders, an unsafe name, or a file that is not a JSON object is refused whole. A file already
    here is kept unless `replace` is set; the channel-to-notebook list is merged, this computer's
    entries first.
    """
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise AlchemyError(
            f"{path} is not a Notebook Alchemy state file: {exc}",
            hint="Make one with `notebook-alchemy export-state FILE` on the other computer.",
        ) from exc
    with archive:
        manifest = _json_object(archive.read(MANIFEST)) if MANIFEST in archive.namelist() else None
        if not manifest or manifest.get("format") != FORMAT:
            raise AlchemyError(
                f"{path} is not a Notebook Alchemy state file.",
                hint="Make one with `notebook-alchemy export-state FILE` on the other computer.",
            )
        if not isinstance(manifest.get("version"), int) or manifest["version"] > FORMAT_VERSION:
            raise AlchemyError(
                f"{path} was made by a newer version of Notebook Alchemy ({manifest.get('tool', '?')}).",
                hint="Upgrade Notebook Alchemy on this computer, then import it again.",
            )
        members: list[tuple[str, bytes]] = []
        total = 0
        for info in archive.infolist():
            if info.is_dir() or info.filename == MANIFEST:
                continue
            name = info.filename
            safe = name == NOTEBOOKS or (
                _MEMBER.match(name) is not None and PurePosixPath(name).name not in {".", ".."}
            )
            if not safe:
                raise AlchemyError(
                    f"{path} holds a file this tool does not write ({name}). Nothing was imported.",
                    hint="Import only files made by `notebook-alchemy export-state`.",
                )
            total += info.file_size
            if info.file_size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise AlchemyError(
                    f"{path} is larger than a state file can be. Nothing was imported.",
                    hint="Import only files made by `notebook-alchemy export-state`.",
                )
            raw = archive.read(info)
            if _json_object(raw) is None:
                raise AlchemyError(
                    f"{name} in {path} is not a JSON object. Nothing was imported.",
                    hint="Make a new state file on the other computer.",
                )
            members.append((name, raw))

    summary = StateSummary(path)
    try:
        for name, raw in members:
            if name == NOTEBOOKS:
                here = engine.notebooks_path()
                current = _json_object(here.read_bytes()) if here.is_file() else None
                merged = {**json.loads(raw.decode("utf-8")), **(current or {})}
                if merged == current:
                    summary.kept += 1
                    continue
                here.parent.mkdir(parents=True, exist_ok=True)
                here.write_text(json.dumps(merged), encoding="utf-8")
                summary.files["notebooks"] = 1
                continue
            folder, file_name = name.split("/", 1)
            target = _folder(engine, folder) / file_name
            if target.exists() and not replace:
                summary.kept += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            summary.files[folder] = summary.files.get(folder, 0) + 1
    except OSError as exc:
        raise AlchemyError(
            f"Could not write the imported files: {exc}. {summary.total} were written before it stopped.",
            hint="Free some disk space or fix the folder's permissions, then import again.",
        ) from exc
    return summary
