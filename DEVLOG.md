# Development Log

Completed material decisions, newest first. Current work is in GitHub issues.

## Topic books from every word (0.3.0, 2026-11-08)

- **Books grow without reshuffling.**
  - Before: new subjects fell into Miscellaneous until 20% growth rebuilt the topic map, which moved passages and re-uploaded every book.
  - Now: passages that fit nowhere extend the map once (new sections or topics; nothing existing is renamed); each passage's section is stored by name; a section past 200 passages is divided once into named subsections. Only `--resort` rebuilds.
  - Only books whose text changed are uploaded (a content hash in the upload record), and a long topic's sections stay in their part, so one growing section does not re-upload every part.
  - NotebookLM's own append only adds text at the end of a source, so a passage that belongs mid-book needs the whole book replaced; changed-only upload keeps that to the books that changed.
- **Staying signed in.**
  - Live run 1 failed because both NotebookLM sign-ins had expired overnight: Google's `__Secure-1PSIDTS` cookie must be rotated, and notebooklm-py's guidance is to run `notebooklm auth refresh` every 15–20 minutes from the OS scheduler.
  - `login` offers, and `keep-signed-in` sets up, that refresh every 20 minutes per profile; `status` shows when it last ran and whether it worked. Runs also set `NOTEBOOKLM_HEADLESS_REAUTH=1`, so stale cookies are renewed from notebooklm's own sign-in browser profile while it is still signed in.
  - On Windows the task is registered from a task definition, not `schtasks /SC MINUTE`: the default refuses to start on battery, and in a live check on a laptop the task sat "Queued" and never ran. With the definition it ran on battery and recorded its result.
- **Review of the growth, sign-in, state and watch work (17 findings, all fixed).**
  - Uploads: a book whose title NotebookLM reset (or whose read-back failed) was recorded as unchanged and never fixed; now only a confirmed upload gets a hash, a drifted title on an unchanged book is renamed, and an old copy that could not be removed is kept as the tool's own and removed by the next run.
  - Sign-in refresh: the task called `notebooklm` through the scheduler's minimal PATH (launchd, cron, systemd), so it would not have been found; it now runs `python -m notebooklm` with the same Python. It reads back that the scheduler has the entry active, removes half-made entries, refuses paths a scheduler would rewrite (`%`, `$`, backquote), reuses the Linux scheduler it was made with, shows a refresh that stopped running as stale, refuses `ALCHEMY_SESSION_FILE` (which it could not keep fresh), and no longer makes `login` fail when only the offer failed.
  - Sorting: an unusable extension or subsection answer was paid for again on every run; it is now remembered for the passages it saw. Subsection names survive a quota stop, `--resort` forgets subsections, and passages placed nowhere are asked about again with the next new passages.
  - `--watch`: only a complete answer (`STOP`) is kept; a video that gave no words for good is remembered (`watched.json`), so the next run moves on; a watch is not retried within a run (a timed-out one may still be billed); the cost is shown and needs a yes (or `--yes`).
  - State: channel names in any script were silently left out of exports; skipped files are now named. Import refuses device names (`NUL`, `COM1`), deep JSON and damaged members with a typed error, holds one member in memory at a time, and writes each atomically.
  - Guided run: warns when a `GEMINI_API_KEY` in the environment would override the key it saved.
- **`--watch N` for videos no route can read.** NotebookLM itself failed on 4 of 15 test videos. Gemini reads a public YouTube URL directly (`fileData.fileUri`, about 100 tokens per second of video, read 2026-11-08), and is asked to write down what is said, word for word. It is opt-in and capped (N per run, 90 minutes per video, a quota stops it), cached like captions so a video is never paid for twice, and every such video is named in the overview because the words are Gemini's hearing, not captions.
- **A guided first run.** `notebook-alchemy` with no command, in a terminal, signs in, offers the background refresh and a free Gemini key (it opens the AI Studio key page), asks for a channel link and runs `run`; outside a terminal it prints the help. The one-line installer's shortcut starts it.
- **`export-state` / `import-state`** move the working files to another computer as a zip. Import checks every member (known folders, safe names, JSON objects, size) before writing any, and keeps this computer's files unless `--replace`.

- **The sources are the speakers' own words, not summaries.**
  - What went wrong: in a live run on a 754-video channel, 22 of 28 sources were title lists (YouTube blocked captions and the run stopped asking) and 6 were short briefs, one written from a single transcript.
  - Why it mattered: NotebookLM answers from the text it is given, so that gave it almost nothing.
  - Now: every video's words are read, repeated passages are kept once (newest copy), and Gemini only labels, files and orders passages. Its punctuation is copied onto the original words (`apply_punctuation`): every word stays, in order, and a word the model added is never used.
- **Gemini returns labels by chunk number, not text.**
  - Each answer is a few percent of the transcript.
  - Every paid answer is cached by what it saw: transcript + prompt version + model for labels; passages and taxonomy for placement; section contents + month for consolidation.
  - A rerun with no new videos makes no request.
  - Labels never come from the fallback model; a used-up daily quota stops the step and the next run resumes.
- **Batch mode first, standard calls when Google refuses it.** Google answers FAILED_PRECONDITION for a key whose project has no billing (seen live); the run then uses standard calls and says so. The batch success path is tested with fakes only, because the test key has no billing; the request shape follows aistudio.google.com/docs/batch-api.
- **Words when YouTube refuses.**
  - From this network YouTube refused both the captions API and yt-dlp's subtitle download (HTTP 429).
  - yt-dlp cannot read Chrome or Edge cookies on Windows (copy lock, DPAPI).
  - NotebookLM fetches a video's transcript on Google's side, so blocked videos go through a working notebook, are read back, and are removed. A first live test read 4 of 10, and a matching bug that left sources behind was fixed.
  - Requests are spaced 3–8 s apart, a block pauses only the refused route, and blocks are never cached.
- **Dates.** Channel listings carry approximate dates (yt-dlp `approximate_date`), shown as a year; yt-dlp subtitles give exact ones. Old news (12 months) is left out; old rules and figures (18 months) are kept and marked "As of". Undated passages are never judged.
- **Nothing kept locally.**
  - Removed: the vault, `sync`, `append`, `--no-sync`, `--max-per-vol`, `--preview-only`, and existing-notebook input; removed commands and flags print what replaced them.
  - A channel → notebook-id record keeps a rerun on the same notebook, even when the AI's title changes.
  - `--save DIR` writes a copy; `clean --vaults` removes 0.2 output for one release.
- **Removed modules.** `analyzer.py`, `synthesizer.py` and `vault.py` are gone; the title-word grouping moved to `offline.py` for the no-key path.

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
