"""Moving the working files to another computer: `export-state` and `import-state`.

The working files are what a rerun builds on: captions, the AI's labels and sorting answers, the
section layout, the upload records and which notebook belongs to which channel. Copying them lets
another computer carry on without reading or paying for anything again.

Left out on purpose: sign-in sessions, the Claude API key (`.env`), the upload confirmation (it
belongs to an account, so the other computer confirms again) and a labelling batch still running.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__
from .config import WINDOWS_RESERVED_NAMES
from .errors import AlchemyError

if TYPE_CHECKING:
    from .engine import AlchemyEngine

FORMAT = "notebook-alchemy-state"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
NOTEBOOKS = "notebooks.json"
# Folders in the archive, and where each lives on this computer.
FOLDERS = ("captions", "knowledge", "library", "sync")
# A caption file for a 10-hour video is a few MB; these are far above any real state file and keep
# a crafted archive from filling memory or disk.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
# The names this tool writes: video ids, notebook ids and `engine._safe_name` of a channel, which
# keeps letters and digits of any script plus "-" and "_" (so "Café_Talks" and "日本語" are fine).
_MEMBER = re.compile(r"^(captions|knowledge|library|sync)/([\w-]{1,200})\.json$")


@dataclass
class StateSummary:
    path: Path
    files: dict[str, int] = field(default_factory=dict)  # folder -> files written (or exported)
    kept: int = 0  # files already here, left as they were (import without --replace)
    skipped: list[str] = field(default_factory=list)  # export: files left out, and why

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
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _member_ok(name: str) -> bool:
    """A name this tool writes, and one no system treats as a device (NUL, COM1, ...)."""
    if name == NOTEBOOKS:
        return True
    match = _MEMBER.match(name)
    return match is not None and match.group(2).lower() not in WINDOWS_RESERVED_NAMES


def _write_atomic(target: Path, raw: bytes) -> None:
    """Write via a temporary file in the same folder, so a stopped import leaves no half file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.importing")
    temporary.write_bytes(raw)
    os.replace(temporary, target)


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
                    if not _member_ok(member):
                        summary.skipped.append(f"{member} (a name this tool does not write)")
                    elif _json_object(f.read_bytes()) is None:
                        summary.skipped.append(f"{member} (not readable JSON)")
                    else:
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
    folders, an unsafe name, a file that is not a JSON object, or more data than a state file can
    hold is refused whole. Members are checked one at a time and read again to write, so memory
    holds one file at most, and each is written atomically. A file already here is kept unless
    `replace` is set; the channel-to-notebook list is merged, this computer's entries first.
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
        members: list[zipfile.ZipInfo] = []
        total = 0
        for info in archive.infolist():
            if info.is_dir() or info.filename == MANIFEST:
                continue
            name = info.filename
            if not _member_ok(name):
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
            if _json_object(_read_member(archive, info)) is None:
                raise AlchemyError(
                    f"{name} in {path} is not a JSON object. Nothing was imported.",
                    hint="Make a new state file on the other computer.",
                )
            members.append(info)

        summary = StateSummary(path)
        try:
            for info in members:
                _import_member(engine, info.filename, _read_member(archive, info), replace, summary)
        except OSError as exc:
            raise AlchemyError(
                f"Could not write the imported files: {exc}. {summary.total} were written before it "
                "stopped; each one written is complete.",
                hint="Free some disk space or fix the folder's permissions, then import again.",
            ) from exc
    return summary


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    """A member's bytes. zipfile reads no more than the size the archive declares (checked against
    MAX_FILE_BYTES first) and checks its CRC; a damaged member is a typed error, not a crash."""
    try:
        return archive.read(info)
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
        raise AlchemyError(
            f"{info.filename} in the state file is damaged ({exc}). Nothing was imported.",
            hint="Make a new state file on the other computer and copy it again.",
        ) from exc


def _import_member(
    engine: AlchemyEngine, name: str, raw: bytes, replace: bool, summary: StateSummary
) -> None:
    if name == NOTEBOOKS:
        here = engine.notebooks_path()
        current = _json_object(here.read_bytes()) if here.is_file() else None
        merged = {**json.loads(raw.decode("utf-8")), **(current or {})}
        if merged == current:
            summary.kept += 1
            return
        _write_atomic(here, json.dumps(merged).encode("utf-8"))
        summary.files["notebooks"] = 1
        return
    folder, file_name = name.split("/", 1)
    target = _folder(engine, folder) / file_name
    if target.exists() and not replace:
        summary.kept += 1
        return
    _write_atomic(target, raw)
    summary.files[folder] = summary.files.get(folder, 0) + 1
