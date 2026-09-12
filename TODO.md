# Consolidated TODO — sibling-project review + internal workflow review

This is the single, reprioritized list of everything found worth acting on
across five separate review efforts this session: four sibling open-source
forensics tools reviewed for practices worth considering, plus one internal
review of ios-ffs-browser's own GUI workflows. Nothing in this list has been
implemented yet — this is a planning document, not a change log.

Detailed reasoning for each finding lives in the two existing review docs at
the project root — `CRUSH_REVIEW.md` and `GUI_WORKFLOW_REVIEW.md` — this file
is the reprioritized action list drawn from those, plus three further reviews
(mf-scan, ALEAPP, FQLite) that were discussed in chat rather than written to
their own files, at the user's own request ("talk first"). Every item below
names which review it came from.

## Attribution — what was reviewed, and under what license

Every sibling project reviewed is open source. None of their code has been
copied into this project — every finding below is either (a) an idea/pattern
observed and reimplemented independently in this project's own style, or (b)
a design question raised by comparison, not a code transplant. Recorded here
for the same reason this project already names provenance on every vendored
file (`ccl_leveldb.py`, `ccl_abx.py`, `ccl_chromium_snss.py`, etc.): be
explicit about what was looked at and under what terms, even when nothing was
copied.

| Project | Author | License | Source |
|---|---|---|---|
| Crush (crush-forensics) | Marco Neumann (kalink0) | Apache License 2.0 | github.com/kalink0/crush-forensics |
| mf-scan (MobileForensic-zipgrep) | Ben0x0a | Apache License 2.0 | github.com/Ben0x0a/MobileForensic-zipgrep |
| ALEAPP | Alexis Brignoni | **MIT** (not Apache-2.0 — checked directly, corrected from an initial assumption that all four shared one license) | github.com/abrignoni/ALEAPP |
| FQLite | Christian Pawlaszczyk (pawlaszc) | Apache License 2.0 | staff.hs-mittweida.de/~pawlaszc/fqlite |

If any TODO item below is ever implemented, the resulting code should credit
the specific project it was informed by in its own comments — matching this
project's own existing convention for `ccl_*`/vendored-file provenance
headers — even though nothing here is a vendored copy the way those files are.

## TODO, in priority order

1. **[SHIPPED v1, 2026-09-12 — see CLAUDE.md's own Conventions entry for
   the full writeup] Search-hit → SQL-record interpretation.** Right-click
   a Keyword Search hit → "Interpret as SQL Record" — LAZY (only ever runs
   for a hit the examiner explicitly asks about, never eagerly for every
   hit) background resolution via `sqlite_carve.locate_offset` (item 5).
   Three real outcomes, shown as tree children under the hit with a
   "⏳ Computing…" placeholder visible the whole time it's running (the
   explicit usability requirement this was built to): **(a)** covered by
   an existing artifact report → names the report and shows that row's
   real field values inline; **(b)** live but not covered by any report →
   shows the real table name and every column's real name+value via
   `PRAGMA table_info` (no bespoke parser needed); **(c)** not
   attributable to any live row → an honest negative, never a guess.
   Verified end-to-end against real data: the report-match path correctly
   resolved a real Chrome History row to its own `chrome_web_history`
   report entry with matching field values; the live-row path correctly
   showed real column names/values for a table (`meta`) no parser covers.

   **Scoped deliberately narrower than the original design for v1, per
   direct instruction** (asked to ship the simpler shape first rather than
   build a whole new capability as part of this feature): case (a) NAMES
   the report and shows its row's values inline — it does NOT yet jump to
   and select that exact row inside the Artifact Viewer's Report table,
   since no "select this exact underlying row" mechanism exists there yet.
   Building that is a natural fast-follow, and should be shared work with
   item 11 (bookmark row-citation) below, which needs the identical
   capability. Also v1-scoped: only matches a record_source entry with a
   FIXED `table` key (skips a `table_field`-based entry, e.g. GroupMe's
   Chat-or-Group dynamic table — there's no live row read yet to
   determine which table that entry would even mean); stops at the first
   matching report rather than ranking several. **[UPDATED — partially
   built, same day]** The DELETED-row case from the original design was
   NOT built for a base-file hit (`locate_offset` remains deliberately
   scoped to the live b-tree only) — but a genuinely deleted row IS now
   recoverable via the separate WAL path (item 5's own WAL follow-up,
   shipped the same day): a hit landing in a `-wal` sidecar resolves
   against the sibling base file's schema and can surface a row the live
   database no longer has at all (real, confirmed example: a genuinely
   deleted LINE message, `docid=27`, recovered this way — see CLAUDE.md).
   What's STILL not built is the harder, narrower case: a base-file hit
   whose OWN file has no WAL to check, schema-matching a candidate in
   that file's own freed/freeblock space ad hoc (reusing
   `recover_deleted_rows`'s own carving primitives) — a real, separate,
   still-open follow-on, not silently dropped.
   *Source: user's own idea, refined through discussion of FQLite's
   whole-file scrap-scanning approach — see the chat discussion, not a file.*

   **[DONE, same day] Report an index-page hit by name/column instead of
   a flat negative.** An offset landing on a SQLite index page (type
   `0x0A`/`0x02`) now resolves via `sqlite_carve.identify_structure` to
   its owning index's real name and indexed table.column, instead of the
   generic "not attributable" negative every other non-table-leaf case
   still gets (freelist/interior/overflow/unattached). Confirmed live in
   the real running app: the `ServiceLogin` hit (page 44, real index
   `urls_url_index`) now reads "Index entry in urls_url_index — indexes
   urls.url" — see CLAUDE.md's own Conventions entry for the full
   writeup, including two real bugs found and fixed while building this
   (a `WITHOUT ROWID`-table false negative, and `sqlite_master` itself
   never being attributable to anything).
   *Source: user's own direct question during this session's live-testing
   of the feature above ("should we not just report it as coming from
   the index...") — not from any of the four sibling tools.*

   **A real, tested negative finding, prompted by the natural next
   question ("could a CLEARED index still hold recoverable content even
   after the table's own rows are gone?")**: tested directly against a
   real ground-truth case already in this project's test data —
   `androidVmGTD/packages/google-search-clear-history/2026-08-26T21-24-16.601Z`
   (a real Android device: Googled "crime is fun," opened an Instagram
   result, then genuinely used Chrome's own "Clear Web History"). The
   real post-clear `History` file confirms `urls`/`visits`/
   `keyword_search_terms` are genuinely 0 rows — but a raw byte search
   for the real visited content (`instagram.com`, `crimeisfunpodcast`,
   `crime is fun`) found NOTHING anywhere in the file, table or index.
   Checked why directly rather than assuming: `freelist_count` is 0 (no
   freed pages), the `urls` table's own leaf page has zero freeblocks,
   and `urls_url_index`'s own single page is 14 non-zero bytes out of
   4096 — and those 14 are just the page's own structural header,
   nothing else. A genuinely fresh, zeroed index page, not a
   row-unlinked-but-bytes-intact state. So for THIS specific real
   device/Chrome build, Chrome's "Clear Browsing Data" evidently rebuilds
   or fully zeroes BOTH the table and its index — "check the index after
   a clear" is a sound general instinct (table and index pages do get
   reused on independent schedules, per the Favicons parser's own
   documented rationale) but is NOT a free win here specifically. Recorded
   so this doesn't get silently re-derived and re-tested as a promising
   idea later without knowing it was already tried and came back empty —
   a single real test, though, not a claim that covers every Chrome
   version/clear-mechanism/table.
   *Source: user's own direct follow-up question during this session's
   live-testing of the feature above.*

   **[DONE 2026-09-12 — see CLAUDE.md's Conventions entry for the full
   design/verification writeup] WAL-file support for "Interpret as SQL
   Record."** Was: `SqlHitInterpretWorker`'s own magic-byte check
   classified any `-wal` file as `not_sqlite`, a technically-accurate but
   unhelpful negative for content that can hold genuinely recoverable
   historical page images the main db no longer has. Built
   `sqlite_carve.locate_wal_offset`/`identify_wal_structure` (the WAL
   counterparts of `locate_offset`/`identify_structure`, resolving a WAL
   frame's own page number against the sibling BASE file's cached page
   map — a WAL frame carries no schema of its own) plus a new `wal_row`
   result kind in the worker/renderer, clearly labeled as historical/WAL
   content rather than silently folded into the ordinary `live` case (no
   report cross-reference is attempted for a WAL-sourced row — a
   deliberate, documented v1 scope decision, not an oversight).

   **Real, striking proof of value found during verification, not just
   theorized**: tested against a real WAL file already in this project's
   own test data (`LINE — Recovered full-text search index`'s own
   `unencrypted_test_full_text_search_message.db-wal`) — recovered a
   genuinely DELETED message ("And this is your bad message. Let me know
   when you trash it.", docid=27) that does not exist anywhere in the
   live database at all (confirmed directly: `docid=27` is simply
   missing from the live table's own docid sequence). Also recovered 15+
   other real rows showing genuine historical FTS index states (segment
   b-tree structures from before the index's own internal
   merge/optimization passes) distinct from their current live content —
   confirmed live in the real running app via the exact same search-hit
   → interpret flow as every other case.
   *Source: user's own direct question ("does this work for wal files?"),
   asked mid-session while testing the base-file version of this same
   feature — not from any of the four sibling tools.*

2. **[DONE 2026-09-12 — see CLAUDE.md's `open_db_readonly` Conventions
   entry for the full fix/verification writeup] CRITICAL, CONFIRMED ACTIVE BUG — ios-ffs-browser, found via comparison
   with ALEAPP's own `attach_sqlite_db_readonly()` convention, then
   independently verified] Every `recoverable_tables`-declaring parser opens
   its evidence file with a bare, non-read-only `sqlite3.connect()`.**
   Empirically confirmed, not theoretical: a plain connect — even for a
   pure `SELECT`, no explicit write — triggers SQLite's own checkpoint-on-
   close behavior, which can silently delete the `-wal`/`-shm` files before
   the `recoverable_tables` carving pass (which reads those same files'
   raw bytes for deleted-row history) ever runs afterward. Verified directly
   against real WhatsApp/Google Messages/Viber WAL files: the WAL vanished
   in 2 of 3 real tests after nothing but a live parser run. Confirmed this
   affects `burner.py`, `chrome_web_history.py`, `google_messages.py`,
   `groupme.py`, `line.py`, `viber.py`, both `whatsapp.py`s, and
   `sms_messages.py` — every single one uses the unsafe connect. Also
   confirmed `chrome_shared.query_rows` (the existing "shared" Chrome helper)
   has the identical bug internally, so this isn't a "some parsers never got
   the shared helper" gap — no shared helper in this codebase does this
   safely yet. **Fix:** one shared, safe connect helper (read-only URI,
   `file:path?mode=ro`) in `artifact_runner.py`'s existing "Parser helpers"
   section, used by every parser and by `chrome_shared.query_rows`
   internally — confirmed via direct testing that read-only mode returns
   byte-identical live-row results to the current unsafe connect, so this is
   a pure safety fix with no behavior change to any report's own output.
   Also closes the specific `ATTACH DATABASE`-opens-read-write gap ALEAPP's
   own house rules flag, since `whatsapp.py`'s primary connection would
   already be read-only before the `ATTACH` ever runs. **Directly feeds
   into item 3 below** — this fix is the natural first piece of the same
   "one shared, safe SQL-opening helper for every parser" question.
   *Source: ALEAPP (`leapp-evidence-readonly.md`'s `attach_sqlite_db_readonly()`
   convention prompted the check); confirmed independently against
   ios-ffs-browser's own real data, not assumed from ALEAPP's own code.*

3. **[PARTIALLY DONE 2026-09-12 — the safe-connect half is shipped; the
   broader "app name + SQL query" minimal-parser design pass below is
   still open] Re-review the whole
   artifact-parser convention: why is there Chrome-specific shared code, and
   can there be one shared foundation for every SQL-based parser instead?**
   `chrome_shared.py`'s `query_rows` (connect/row_factory/close boilerplate)
   was not actually Chrome-specific in its own logic at all — it was factored
   out of a batch of Chrome parsers simply because that's where the
   duplication was first noticed, not because Chrome parsers have a
   different underlying need than WhatsApp/Viber/GroupMe/LINE/Google
   Messages/Burner/SMS Messages do. **The safe-connect half of this is now
   fixed** (2026-09-12, see item 2 and CLAUDE.md's `open_db_readonly`
   Conventions entry): a new `artifact_runner.open_db_readonly` is the one
   universal, safe (read-only) connect helper, and all 15 parsers that used
   to hand-roll a bare `sqlite3.connect()` — including every one of the
   seven named above — now import and call it directly; `chrome_shared.
   query_rows` itself is now a thin wrapper around the same universal
   helper, kept only so its existing Chrome-parser callers needed no code
   changes. Only `chrome_shared.py`'s other two functions (`url_set`,
   `history_visits`) are genuinely Chrome-schema-specific and correctly
   stay scoped to Chrome.

   **Still open — the broader ambition, not yet designed or built**: the
   user's real ask goes further than one shared connect helper — it should
   be possible to write a parser for a simple, single-table SQL-based app
   with little more than the app name and a SQL query, with shared code
   carrying the weight of shaping rows and (where declared) applying
   `timestamp_fields`/`core_fields`/etc., not just connecting safely. This
   still needs a real, deliberate design pass: (a) figure out where a
   genuinely universal declarative single-table-parser shortcut should live
   so a simple parser can opt into it, (b) do this WITHOUT losing or
   duplicating the project's own richer per-parser conventions
   (`record_source`, `media_fields`, `recoverable_tables`) that a genuinely
   complex parser (WhatsApp, Google Messages) still needs and shouldn't be
   forced around a too-simple shared shortcut. Worth explicitly comparing
   against ALEAPP's own `__artifacts_v2__` + `@artifact_processor`
   convention (a real, working example of a declarative metadata dict plus
   a thin decorated function, at 369-plugin scale) for how a "just the app
   name and the query" parser could look here, while keeping this
   project's own stricter, richer declarative fields intact for parsers
   that need them.
   *Source: user's own direct request, prompted by the item 2 investigation
   surfacing that `chrome_shared.py` is the only real shared-boilerplate
   convention in the whole `artifacts/` tree, and it's scoped to one app
   family rather than being universal.*

4. **[GUI_WORKFLOW_REVIEW.md, now further validated by ALEAPP] Add
   CSV/TSV export for Artifact Report tables.** The single largest gap found
   in the internal workflow review — no way to get a parsed report's own
   rows out of the tool at all today, not even a context menu. ALEAPP's own
   `output_types` convention (`html`/`tsv`/`timeline`/`lava`/`kml`, opt-in
   per plugin with zero extra code) is a real, working precedent at scale
   (369 plugins) for exactly this kind of declarative export — and notably
   uses **TSV, not CSV**, likely to avoid comma-escaping headaches; worth
   mirroring that choice given this project's own report rows often contain
   commas in decoded message text. `ArtifactTableModel` already has uniform
   row access for both DB-mode and list-mode reports — this is close to a
   plain iteration + a writer, no new data layer needed.
   *Source: GUI_WORKFLOW_REVIEW.md (Reporting workflow); TSV-over-CSV
   detail from ALEAPP.*

5. **[DONE 2026-09-12 — see CLAUDE.md's `sqlite_carve.locate_offset`
   Conventions entry for the full fix/verification writeup] Add
   `locate_offset` (byte-offset → row/column)
   reverse lookup**, mirroring Crush's own `CellLocator.locate_offset`
   alongside the existing `sqlite_carve.locate_live_row` (row → byte-offset).
   Was the direct technical foundation item 1 above depends on — item 1
   itself is now unblocked and ready to build on top of this.
   *Source: Crush (`core/cell_locator.py`'s two-directional `CellLocator`
   protocol).*

6. **[CRUSH_REVIEW.md] Verify real HEIC/HEIF photo rendering on an actual
   Windows frozen build.** Confirmed working on macOS via the OS's own
   codec, fed to Qt's plugin system — nothing bundles an equivalent codec
   for Windows, and nothing in `ffs_explorer.spec` or CI touches this at
   all. A real, common iOS evidence category (default photo format since
   iOS 11) may be silently unviewable on the actual shipped platform. If it
   fails, `pillow-heif` (or equivalent) is the concrete, portable fix.
   *Source: Crush (`pillow-heif` dependency prompted the direct check
   against this project's own Qt install).*

7. **[GUI_WORKFLOW_REVIEW.md] Surface WHY an app scored high, inline, in
   the Apps table** (`_populate_apps_table`/`_APPS_COLUMNS`) — the
   evidence-database/webview-storage detail is already computed by
   `app_intelligence.scan_apps()`, just not shown outside the AI-only MCP
   path today. Cheap, display-only.
   *Source: GUI_WORKFLOW_REVIEW.md (Triage workflow).*

8. **[GUI_WORKFLOW_REVIEW.md] Double-click an Apps-table row jumps the File
   Browser to that app's Data Folder** — closes the "found it, now what"
   gap Triage currently has (confirmed: clicking an Apps-table row does
   nothing today, `_art_current_mod` is explicitly `None` for it).
   *Source: GUI_WORKFLOW_REVIEW.md (Triage workflow).*

9. **[GUI_WORKFLOW_REVIEW.md] Add a "Validated" indicator column to the
   Apps table**, sourced from `validation_store.get()` per row — the
   lookup already exists, just not run table-wide.
   *Source: GUI_WORKFLOW_REVIEW.md (Validation workflow).*

10. **[GUI_WORKFLOW_REVIEW.md + mf-scan] Extend the search backend to also
    query already-populated `artifact_<name>` tables**, not just raw files
    — closes the "two separate search tools" gap (raw Keyword Search vs.
    each report's own separate, single-report-only filter box). While
    touching this: consider mf-scan's own base64-phase-aware literal search
    (compute the literal substrings that must appear in a base64 encoding
    of a needle at each of the 3 possible byte-alignment phases, search for
    those directly against raw bytes — no decode step needed) as a real,
    concretely portable enhancement to the same search backend, for hits
    currently invisible because they're base64-encoded inside a plist/JSON
    value.
    *Source: GUI_WORKFLOW_REVIEW.md (Searching workflow) for the base
    gap; mf-scan (`src/ops/search/base64.rs`) for the base64-phase technique.*

11. **[GUI_WORKFLOW_REVIEW.md] Let a bookmark entry optionally carry a
    report row's own citation** (record_source key), not just a file path
    — the most granular, most forensically specific thing this tool
    produces currently has no bookmark of its own.
    *Source: GUI_WORKFLOW_REVIEW.md (Bookmarking workflow).*

12. **[GUI_WORKFLOW_REVIEW.md] Decide, deliberately, how (or whether) to
    help a "new app, no parser" examiner without AI access** — partially
    answered by item 1 above (a generic live/deleted row viewer for an
    unsupported app's own table), but the broader question (should
    `list_apps`/`build_artifact_parser`-style guidance exist outside the
    MCP/AI path at all) is still a real, open design decision, not a quick
    fix.
    *Source: GUI_WORKFLOW_REVIEW.md ("New app not supported" workflow).*

13. **[CRUSH_REVIEW.md] A right-click "Search for this value" on any Report
    table cell**, pre-filling Keyword Search — closes most of the practical
    cross-report navigation gap cheaply, without attempting general
    cross-app identity resolution.
    *Source: CRUSH_REVIEW.md / GUI_WORKFLOW_REVIEW.md (Reviewing artifacts
    workflow).*

14. **[Four independent sibling tools now agree — Crush, mf-scan, ALEAPP,
    FQLite] Decide, deliberately, whether "never decrypt" already covers or
    excludes decryption with an examiner-supplied, independently-known key**
    (SQLCipher/WhatsApp-crypt/keychain-derived key style). All four sibling
    tools reviewed this session draw the same line: decrypt only with a key
    the examiner already has, never by attacking the evidence itself. This
    doesn't resolve the question for this project — it just means the
    forensic-tool norm has a fairly clear answer, worth weighing against
    this project's own stricter current stance.
    *Source: Crush (`sqlcipher3`), mf-scan (`src/decrypt/`), ALEAPP
    (`sqlcipher_decrypt.py`), FQLite (`SQLCipherForensicDecryptor.java`).*

15. **[GUI_WORKFLOW_REVIEW.md, depends on item 4] A hash manifest on
    report export** — trivial once CSV/TSV export exists, not worth
    building standalone first.
    *Source: GUI_WORKFLOW_REVIEW.md / CRUSH_REVIEW.md (Reporting; Crush's
    own integrity-mode hash manifest).*

16. **[GUI_WORKFLOW_REVIEW.md, per direct design discussion] Let the four
    center tabs detach into their own windows** (Qt dock widgets) for real
    multi-monitor support. Doesn't touch any tab's own content or existing
    per-tab state-preservation work, only the container.
    *Source: internal design discussion, prompted by comparing Cellebrite's
    own single-window model (rejected) against Crush's own window layout.*

17. **[CRUSH_REVIEW.md] Evaluate `av` (PyAV) as a replacement for the
    subprocess `ffmpeg` call in `media_viewer.py`** — needs a real
    side-by-side frozen-build test before switching, not a swap on the
    strength of the comparison alone.
    *Source: Crush (`av` dependency vs. this project's own subprocess
    `ffmpeg` call).*

18. **[ALEAPP, low cost] Consider a `sample_data`-style lightweight
    provenance note per parser** — a one-line "confirmed N rows on
    real device X, OS Y" string in each parser's own module, cheaper than
    a real automated test but a genuine, real discipline ALEAPP holds
    itself to across all 369 of its own plugins.
    *Source: ALEAPP (`__artifacts_v2__`'s `sample_data` dict).*

19. **[Crush + mf-scan confirm this is real and valuable; ALEAPP is a real
    counter-example] Stand up a real pytest suite with committed
    micro-fixtures.** Still a real, large gap (ios-ffs-browser has zero
    automated tests) — but the honest, updated picture after checking a
    third and fourth sibling tool: 2 of 4 mature siblings reviewed
    (Crush, mf-scan) have genuine fixture-backed test suites; ALEAPP
    (also mature, also widely used) has almost none; FQLite wasn't
    checked for this. Not "everyone but us has tests" — a real, if
    imperfect, norm, not a universal one. Start with what's already been
    manually verified this session and is cheapest to freeze:
    `sqlite_carve._cell_local_payload_size` (pure arithmetic, no archive
    needed) and `chrome_tabs.py`'s three parse functions (small real
    files already confirmed byte-for-byte this session).
    *Source: Crush (`crush/tests/`), mf-scan (`tests/`), with ALEAPP as an
    explicit counter-finding.*

## Considered and NOT recommended (kept here so they aren't silently lost)

- **A full Cellebrite-style single-unified-table rewrite of the four
  center tabs.** Rejected in direct discussion — this project's own
  per-app-specific column richness is exactly what a generic unified
  table would cost, and none of the real navigation gaps found are
  actually caused by tabs being tabs. See GUI_WORKFLOW_REVIEW.md's own
  "Window layout" section for the full reasoning.
- **General whole-file "scrap scanning" for unattributed deleted records**
  (FQLite's own core methodology, bottom-up rather than table-first).
  Rejected as a standing feature — no schema to check plausibility
  against means much less trustworthy output than everything else this
  project produces. The narrower, anchored version of this idea survives
  as item 1 above specifically because it starts from a known-good
  search hit instead of scanning blind.
- **General dropped-table recovery** (also FQLite). Real capability, but
  FQLite's own code admits it can't recover the table's own name/schema in
  the general case — doesn't map cleanly onto `recoverable_tables`'s own
  convention of naming a known table up front. Folded into item 1's own
  hardest fallback case instead of being a standalone feature.
- **CASE/UCO JSON-LD export** (FQLite). Real, standardized, genuine
  interoperability value if you're already in a CASE/UCO ecosystem — but
  real complexity (a whole ontology to map onto) relative to this
  project's own current, more basic gap (no export at all). CSV/TSV
  (item 4) should come first regardless of whether this is ever added
  later.
- **Cross-acquisition diff** (mf-scan's own `diff` command — comparing two
  full acquisitions of the same device at different points in time).
  Real, but a genuinely large new architectural concept (this project's
  whole design assumes one case, one archive) — not pursued without a
  concrete real casework need first; the user hasn't confirmed this is a
  real recurring need.
- **Broader archive-format support** (TAR/7z/adb backup/iTunes backup —
  Crush). This project's own narrower FFS-zip-only scope is a real asset,
  not a gap, per CLAUDE.md's own Conventions section.
- **`construct` (declarative binary-parsing library)** — considered as a
  possible improvement over hand-rolled `struct.unpack` parsing, then
  checked directly: Crush lists it as a dependency but never actually uses
  it anywhere in its own source. Not a real technique, correctly ruled
  out rather than adopted on the strength of a dependency list alone.
- **Two-library magic-byte detection** (`python-magic` + `filetype`,
  Crush) and **byte-level media format inspectors** (mf-scan's own
  `formats/inspect/media/*.rs`, which turned out to be classifiers only,
  not real structural inspectors) — both checked and found not to apply
  or not to exist as initially assumed.
