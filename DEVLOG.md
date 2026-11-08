# Development Log

Completed material decisions, newest first. Current work is in GitHub issues.

## One-line install (2026-11-08)

- **One command, no prerequisites.** `install/install.ps1` (`irm | iex`) and `install/install.sh`
  (`curl | sh`) install uv with Astral's installer if it is missing, install the tool with
  `uv tool install` on a Python 3.12 that uv downloads if needed, install Playwright's Chromium, add a
  shortcut and run `notebook-alchemy test`. The aim is the ease of a one-click browser extension for
  people who have never used Python.
- **The package source is the `main` source archive, not `git+https`,** so git is not a prerequisite
  either. It becomes the PyPI name once the package is published.
- **`--with-executables-from notebooklm-py` is required.** uv links only the package's own commands, and
  without `notebooklm` on PATH sign-in and uploads fail. **`--reinstall-package`** makes a rerun pick up
  new code even when the version number has not changed, so running the command again is the update.
- **The shortcut opens in `~/Notebook Alchemy`,** because `.env` and output are read from the working
  folder. It starts `notebook-alchemy` with no command, which is the guided first run, and keeps the
  terminal open afterwards. Its icon is downloaded from `docs/brand/icons`; a failed download never
  stops the install.
- **Nothing outside the home folder.** The shell script refuses to run as root, a PATH change that fails
  or that `UV_NO_MODIFY_PATH` forbids is a warning rather than a stop, and nothing asks for or stores an
  account or key.

## Icon (2026-11-08)

- **The icon belongs to NotebookLM's family on purpose.** Three nested blue arches share the left edge
  and face the same way as the NotebookLM mark, and a small gold alchemy bottle with a blue plus badge on its lower
  right stands under the innermost one. The Owner chose this after rejecting a flat purple set, a Google-Workspace-style
  emerald and amber set, and a mirrored set of arches. The geometry is drawn from scratch on a 192-unit
  grid, not traced. The gold bottle is the main thing that tells it apart from Google's mark, so the
  README's header now says the tool is unofficial and not affiliated with Google, and any listing
  should say the same.
- **One SVG, everything else generated.** `scripts/brand_icons.py` renders every size with Playwright's
  Chromium, which the `notebooklm-py[browser]` dependency already installs, and packs the .ico files
  itself from PNG entries, so the repository gains no image library. Opaque icons (apple-touch,
  maskable) put the mark on white, scaled to stay inside iOS's rounded corners and Android's 80%
  safe circle.

## Plain output, modelled on gcloud (2026-11-07)

- **What people read uses everyday words.** Terminal output, help text and the generated documents say
  topic, document, part and documents folder; summaries and video lists replace "Gemini synthesis" and
  "offline index". The code keeps domain, compendium, volume and vault, and the README says which is
  which. Why: the tool is meant for people who are not technical, and the old words described how it
  works rather than what it gives them.
- **Detail is behind `-v`, as gcloud keeps it behind `--verbosity=info`.** By default a run prints one
  line per result; paths, token counts, models, grouping method and per-document caption notes print
  with `-v`, which now also works after the command (`status -v`). `notebooklm`'s own sign-in progress
  (profile, cookies, file paths) is captured, not passed through; it explains a failure instead.
- **gcloud's message shapes.** Resource names in `[brackets]`, `Warning:` and `Error:` prefixes on
  stderr in place of boxed panels, next steps written as `To ..., run: $ command`, and confirmations as
  `Do you want to continue (y/N)?`. Google's developer style guide supplied "sign in" over "log in"
  and no "please"; its error-message page no longer exists, so the error shape follows gcloud's own
  output, read locally.
- **Older documents still work with `append`.** The related-topics heading was renamed; `append` finds
  either heading.

## Privacy, cleanup and future-proofing (2026-11-07)

- **The repository and the tool keep nothing that identifies a person.** The license, package metadata
  and README name the GitHub login; the README's "About the Owner" section is dropped (the README
  checker expects it; this entry records the deliberate drop). Contributor docs carry no company or
  maintainer-process references. The upload confirmation stores a random per-install salt and a salted
  SHA-256 of the account email instead of the email, `setup` saves the vault path as typed, `status`
  shows home paths as `~`, and `clean --records` removes the upload records. Why: this is open source,
  and neither the maintainer nor a user should leave personal data in the repository or on disk.
- **Dead code and the old vault layout are gone.** Write-only fields, unused members, the scanner-side
  mock path, `ALCHEMY_STATE_DIR` and the `INDEX.md` / `vault/compendiums` layout from before 2026-11-07
  were removed after a reference search showed nothing used them.
- **Python 3.11+, bounded dependencies.** yt-dlp 2026.10.26 dropped Python 3.10, and YouTube now needs
  yt-dlp's JavaScript solver (`yt-dlp[default]`). notebooklm-py (0.x) and youtube-transcript-api are
  capped below their next major so a breaking release arrives as a reviewed Dependabot pull request.
- **Prices carry their effective date.** `gemini-3.8-flash` doubles on 2027-01-01; the table lists both
  prices with dates instead of needing an edit on the day. The build summary also reports the tokens
  Gemini actually used, thinking included.
- **Vendor signals over vendor wording.** Quota and retry timing come from Google's structured
  `QuotaFailure` / `RetryInfo` details; every `CouldNotRetrieveTranscript` reason (including ones a
  later library adds) is classified; `PoTokenRequired` counts as blocked and stops the run's remaining
  caption requests.
- **The upload respects the account's own source limit.** notebooklm-py's `settings.get_account_limits()`
  (the CLI has no command for it) reports the plan's per-notebook source limit; live, a Pro account
  reads 300. `run` stops before building when the planned sources exceed it; `sync` refuses before
  uploading when the notebook would end over it, and frees the replaced copies first when only the
  add-then-delete peak would be over. An unreadable limit is reported as unknown and the run proceeds.
  (#43)
- **Large domains become named subtopics, not numbered volumes.** A live upload showed eleven
  "Government Contracting Fundamentals Vol N" sources that told a reader nothing. A domain larger than
  `--max-per-vol` is now split into subtopics named from its own titles (Gemini: discovered, then every
  title assigned by number; offline: grouped by distinctive title words), and NotebookLM titles come
  from the compendium's domain name, so "SAM.gov" keeps its dot. Cost: one request to name a domain's
  subtopics plus one per 150 of its titles, shown in the dry-run request count.
- **Channels include live streams.** A channel URL reads the Videos and Live tabs (`--tabs`; Shorts on
  request). Live streams are long and captioned; Shorts are usually under a minute. A live test showed
  why: a channel YouTube lists at 1.1K items had 508 videos, 246 streams and 440 Shorts, and the
  Videos tab alone missed the streams.
- **Faster where the time goes.** Uploads and waits run three `notebooklm` processes at a time (each
  takes seconds to start); titles are still corrected only after every wait. yt-dlp loads on first use,
  so `--help` went from about 1.5 s to 0.6 s.

## Upload account and setup (#39)

- **Review fixes (2026-11-07).** `run` reads the account again just before uploading and stops if it
  changed during the build. `ALCHEMY_SESSION_FILE` overrides any notebooklm profile, so an explicit
  profile with it set is refused, the account is labelled with the session file, and `login` writes that
  file. "No session" is read from `checks.storage_exists`, which notebooklm-py 0.8.6 always reports.
  `sync` checks the session before asking. `.env` reading tolerates a UTF-8 byte-order mark.

- **The upload account is read from the profile's own record, and shown as `unknown` otherwise.**
  `notebooklm --profile <p> auth check --json` reports the account email notebooklm-py 0.8.6 stored on the
  profile, without a network call. A sign-in window records it when the account is unambiguous, and
  `login --browser-cookies <b> --account <email>` picks one; with `--account`, either way, the login
  reads it back and fails if it differs. Why: uploads can go to a different Google account from the one
  that owns the Gemini key, and nothing said which.
  `notebooklm profile list` reads the same record; `status` (session context) and `auth inspect` (browser
  accounts) do not identify the profile's account, so they are not used for it.
- **First upload to a profile asks, per account.** The confirmation is stored per profile in
  `~/.notebook-alchemy/upload_accounts.json` (since 2026-11-07 as a salted hash, not the email); a
  different account, or an unreadable record, asks again. An
  unknown account is asked about on every upload and never recorded, because two unknown accounts cannot
  be told apart. `login --account` checks that the browser holds the email before it signs in, so a typo
  cannot overwrite the profile with another account.
  `--yes` skips the question without recording consent. Without a terminal and without `--yes`, the
  upload stops before any work.
- **`setup` writes `.env` in place.** Other lines are kept, a key is replaced where it stands, and the file
  is swapped in whole. The Gemini key is entered hidden and never printed.
- **A Google AI subscription plan does not change API quota.** Google's Google AI plans page (checked 2026-11-07) says plan
  benefits apply only in the AI Studio web interface and API-key usage is billed separately.

## Audit remediation (#16)

- **Sample data is opt-in.** The scanner and analyzer used to substitute a bundled catalog whenever a
  lookup failed, which made a typo look like a run. They now raise, and `--mock` is the only way to use
  the sample. Why: a wrong-but-plausible result is worse than an error. (#19)
- **Output is namespaced and swapped atomically.** `vault/<source-slug>/compendiums/`, built in a staging
  directory and renamed into place. Why: one shared directory mingled sources and a failed run left it
  empty. (#20, #14)
- **Upload goes through a temp file, then waits, corrects titles and reads back.**
  `notebooklm source add <file> --type file`. Why: the CLI has no `--text` option, and one argument cannot carry a
  large document on Windows. The first live re-sync left duplicates because NotebookLM resets some titles to the
  file name once processing ends (the upload response still echoes the title we sent), so the sync waits for every
  source, renames any whose title changed, removes older copies by a normalised title (no `.md`, underscores as
  spaces), and reads the notebook back to confirm each title appears once. A source the user added with another
  title is never touched. (#17)
- **Gemini key in a header, one client for every call.** `gemini.py` owns retries, key scrubbing and the
  price table. Why: `?key=` URLs surfaced in exception text. (#21)
- **Default model is `gemini-3.8-flash`, thinking set to low, with `gemini-3.5-flash-lite` as the fallback.**
  Google lists 3.8 Flash as its most capable Flash model at $0.75 input and $3.75 output per 1M tokens.
  `gemini-2.5-flash` shuts down on 2026-12-01, so it could not stay the default, and a synthesis tool is judged on
  the quality of its prose. The request mirrors what AI Studio shows for this model: `thinkingLevel: low`, and no
  temperature, because AI Studio offers none for Gemini 3.x. The first live run showed why a fallback is needed:
  3.8 Flash answered HTTP 503 ("high demand") for more than 40 seconds while `gemini-3.5-flash-lite` answered in
  2.5 seconds and accepted the same request, `gemini-2.5-flash-lite` returned 404 for a new project, and
  `gemini-3.1-flash-lite` spent a 64-token limit entirely on thinking. So the client switches to the fallback for
  the rest of a run when the default returns 404, 429 or a 5xx after its retries, and says so in the output;
  `ALCHEMY_GEMINI_FALLBACK_MODEL` changes it or, when empty, turns it off. Token limits are set high enough that
  thinking cannot consume them. Trade-off: thinking tokens still bill as output, so a real run costs somewhat more
  than the `dry-run` estimate (which says so). (#26)
- **A daily quota fails over at once, and the dry-run says how many requests a run needs.** A new AI Studio
  project starts on the free tier, and the live runs hit its limit of 20 requests per day for `gemini-3.8-flash`
  (HTTP 429, retry in about 12 hours). Waiting seconds cannot help, so the client treats a per-day 429 as final
  for that model and moves to the fallback immediately; the notice is one short line instead of Google's whole
  message. `dry-run` and `run` print the expected request count (one discovery call, one per 150 titles, one per
  volume) so a user can see whether the free tier is enough before starting. Larger channels need billing enabled
  on the key's project. (#26)
- **Offline mode is an index, not a summary.** It lists videos and links and says no summary was made.
  Why: canned text presented as synthesis misleads whoever uploads it. (#22)
- **Clustering is two-stage and index-based.** Domains from a sample, then every title assigned by
  number, with a visible Miscellaneous bucket. Why: the first 300 titles were all that was ever sent. (#23)
- **`notebooklm-py` is a declared dependency**, with the `browser` and `cookies` extras the login paths
  need. `pydantic` and `pytest-asyncio` were removed as unused. (#30)
- **Tests are hermetic.** An autouse fixture isolates the working directory and home and blocks real
  processes and sockets. (#31)
