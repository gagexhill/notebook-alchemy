"""Check, against a real NotebookLM account, what `client.py` relies on from notebooklm-py.

Not part of the test suite: it needs a signed-in account and the network. Run it by hand before
raising the notebooklm-py version pin in `pyproject.toml`, and after NotebookLM changes something.

    python scripts/notebooklm_contract.py --profile NAME --confirm

It makes one throwaway notebook, adds a small file to it, waits, reads it back, renames it, reads
its text, removes it, and deletes the notebook. It exits 0 only when every check passed and the
notebook is gone. Nothing else in the account is touched.
"""

from __future__ import annotations

import argparse
import sys
import time

from notebook_alchemy.client import NotebookLMClient, SourceDocument, normalize_title
from notebook_alchemy.config import AlchemyConfig
from notebook_alchemy.errors import AlchemyError

TITLE = "Notebook Alchemy contract check (safe to delete)"
BOOK = "Contract check book"
RENAMED = "Contract check book, renamed"
TEXT = "# Contract check\n\nA synthetic test document. The quick brown fox jumps over the lazy dog.\n" * 20


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default=None, help="the notebooklm profile to check with")
    parser.add_argument("--confirm", action="store_true", help="really use this account")
    args = parser.parse_args()

    client = NotebookLMClient(AlchemyConfig(), args.profile)
    print(
        f"Profile [{client.profile}]; account recorded: {'yes' if client.upload_account().email else 'no'}."
    )
    if not args.confirm:
        print("Nothing was done. Add --confirm to run the check against this profile's account.")
        return 2

    failed: list[str] = []
    timings: list[tuple[str, float]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
        if not ok:
            failed.append(name)

    def timed(name: str, call):
        started = time.perf_counter()
        try:
            return call()
        finally:
            timings.append((name, time.perf_counter() - started))

    notebook_id = ""
    try:
        import notebooklm.exceptions as lib

        names = [
            "AuthError",
            "RateLimitError",
            "NotebookLimitError",
            "SourceTimeoutError",
            "SourceProcessingError",
            "SourceNotFoundError",
            "NetworkError",
            "RPCTimeoutError",
            "DecodingError",
            "NotebookLMError",
        ]
        check("the error types the adapter maps exist", all(hasattr(lib, n) for n in names))

        status = timed("open the session and list notebooks", client.check_session)
        check("the sign-in works", status.ok, status.detail)
        if not status.ok:
            return 1
        limits = timed("read the plan limits", client._read_plan_limits_live)
        check("the plan limits can be read", limits is not None and limits.source_limit is not None)

        notebook_id, created = timed("create a notebook", lambda: client.create_notebook(TITLE))
        check("a notebook is created with an id", bool(notebook_id) and created)
        listed = client.list_notebooks()
        check("a new notebook is listed with id and title", {"id": notebook_id, "title": TITLE} in listed)

        document = SourceDocument("contract", BOOK, TEXT)
        source_id = timed("upload a file", lambda: client.add_source_document(notebook_id, document))
        check("an upload returns a source id", bool(source_id))
        timed("wait for the source", lambda: client.wait_for_source(notebook_id, source_id, timeout=180))
        sources = timed("list the sources", lambda: client.list_sources(notebook_id))
        mine = next((s for s in sources if s["id"] == source_id), None)
        check("the source is listed", mine is not None)
        if mine:
            check(
                "a source has id, title, url, type and status",
                set(mine) == set(["id", "title", "url", "type", "status"]),
            )
            check("a ready source says so", mine["status"] == "ready", mine["status"])
            check("an uploaded book is not a video", mine["type"] != "youtube", mine["type"])
            print(f"       title after processing: {mine['title']!r}; type: {mine['type']!r}")

        timed("rename the source", lambda: client.rename_source(notebook_id, source_id, RENAMED))
        after = {s["id"]: s["title"] for s in client.list_sources(notebook_id)}
        check("a rename is read back", normalize_title(after.get(source_id, "")) == normalize_title(RENAMED))
        text = timed("read the source's text", lambda: client.source_text(notebook_id, source_id))
        check("the source's text is read back", "quick brown fox" in text)

        timed("remove the source", lambda: client.delete_source(notebook_id, source_id))
        check(
            "a removed source is gone", source_id not in {s["id"] for s in client.list_sources(notebook_id)}
        )
    except AlchemyError as exc:
        check("no step raised", False, f"{exc.message} ({type(exc.__cause__).__name__})")
    finally:
        if notebook_id:
            try:
                client._call("delete the notebook", lambda library: library.notebooks.delete(notebook_id))
                gone = notebook_id not in {n["id"] for n in client.list_notebooks()}
                check("the throwaway notebook is deleted", gone)
            except AlchemyError as exc:
                check("the throwaway notebook is deleted", False, exc.message)
                print(f"  Delete the notebook named {TITLE!r} in NotebookLM by hand.")
        client.close()

    print("\nSeconds per step:")
    for name, seconds in timings:
        print(f"  {seconds:6.1f}  {name}")
    print(f"\n{'All checks passed.' if not failed else f'{len(failed)} checks failed: ' + '; '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
