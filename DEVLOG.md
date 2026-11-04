# Development Log

Completed material decisions, newest first. Current work is in GitHub issues.

## Audit remediation (#16)

- **Sample data is opt-in.** The scanner and analyzer used to substitute a bundled catalog whenever a
  lookup failed, which made a typo look like a run. They now raise, and `--mock` is the only way to use
  the sample. Why: a wrong-but-plausible result is worse than an error. (#19)
- **Output is namespaced and swapped atomically.** `vault/<source-slug>/compendiums/`, built in a staging
  directory and renamed into place. Why: one shared directory mingled sources and a failed run left it
  empty. (#20, #14)
- **Upload goes through a temp file.** `notebooklm source add <file> --type file`. Why: the CLI has no
  `--text` option, and one argument cannot carry a large document on Windows. (#17)
- **Gemini key in a header, one client for every call.** `gemini.py` owns retries, key scrubbing and the
  price table. Why: `?key=` URLs surfaced in exception text. (#21)
- **Default model is `gemini-2.5-flash-lite`.** `gemini-2.5-flash` shuts down on 2026-12-01 and its
  replacement is a different model; Flash-Lite is listed as not deprecated, costs $0.10 / $0.40 per 1M
  tokens, and handles clustering and per-domain synthesis. `ALCHEMY_GEMINI_MODEL` overrides it. Trade-off:
  weaker prose than a larger model; pick one with the variable if quality matters more than cost. (#26)
- **Offline mode is an index, not a summary.** It lists videos and links and says no summary was made.
  Why: canned text presented as synthesis misleads whoever uploads it. (#22)
- **Clustering is two-stage and index-based.** Domains from a sample, then every title assigned by
  number, with a visible Miscellaneous bucket. Why: the first 300 titles were all that was ever sent. (#23)
- **`notebooklm-py` is a declared dependency**, with the `browser` and `cookies` extras the login paths
  need. `pydantic` and `pytest-asyncio` were removed as unused. (#30)
- **Tests are hermetic.** An autouse fixture isolates the working directory and home and blocks real
  processes and sockets. (#31)
