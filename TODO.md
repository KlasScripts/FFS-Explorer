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
   capability. **[DONE 2026-09-12] Jump-to-exact-row, built as this
   fast-follow.** `_find_report_match` (keyword_search.py) now also
   captures the matched row's own implicit SQLite `rowid` in
   `caseresults.db`'s `artifact_<script>` table (`SELECT rowid AS
   "_report_rowid", * FROM ...` — the exact same rowid space
   `ArtifactTableModel`'s DB mode already keys its rows by, so no new
   identity concept was needed). A "→ Jump to this row in the report"
   child item appears under a `report`-kind result, carrying
   `(script_name, report_rowid)` on a new `_REPORT_JUMP_ROLE`; double-
   clicking it calls the new `ArtifactViewerMixin._art_jump_to_report_row`,
   which switches to the Artifact Viewer tab, opens the report, selects
   the matching tree node, and selects+scrolls to that exact row — clearing
   the "Hide likely false positives" filter first (inline, via the
   filter's own synchronous fast path) if that's specifically what's
   hiding the target row, without touching the free-text or date filters
   (both deliberate examiner choices this jump shouldn't silently
   override). Verified end-to-end in the real running app (in-process,
   same methodology as the rest of this feature): a real "Ohtani" search
   hit inside Chrome History resolved to `chrome_web_history` rowid 10,
   and the jump correctly switched tabs, selected that exact model row,
   and the row's own displayed content (title/url/from_url/etc.) matched
   the interpreted result exactly.
   *Source: user's own "go for it" confirmation of this item as the
   recommended next piece of work — not from any of the four sibling
   tools.*

   Also v1-scoped: only matches a record_source entry with a
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

   **[DONE, 2026-09-13] Auto-detect + opt-in bulk interpretation, to fix a
   real discoverability gap.** Right-click "Interpret as SQL Record" (above) has no visual
   affordance at all — an examiner who hasn't been told it exists would
   never find it. Planned fix: after a keyword search finishes, run a
   fast background pass over the search hits' own DISTINCT files (not
   per-hit — a search commonly returns dozens of hits inside the same
   db) and sniff each one's real header, then offer a modal: "N of these
   M hits are inside SQLite/WAL files — interpret them all now?"
   [Yes]/[No — I'll do it manually per hit]. **Yes** drives the SAME
   `SqlHitInterpretWorker`/rendering path the right-click already uses,
   just sequentially over every qualifying hit, with a progress dialog
   ("Interpreting hit 7 of 14…", cancellable) — nothing new is built for
   the actual interpretation, only the offer-and-drive layer around it.
   **No** leaves right-click as the only path for that search, unchanged.
   Deliberately opt-in per search (never automatic/eager) — preserves the
   original "LAZY, only for a hit the examiner explicitly asks about"
   design brief for the interpretation work itself; this only asks
   whether to run it in bulk for hits already known to be SQL/WAL-shaped.

   Three constraints fixed in the design before any code was written, all
   per direct instruction:

   - **Extension-first, byte-peek only as a bounded fallback — the SAME
     convention `app_intelligence.find_evidence_databases`'s own
     "Row-merge + magic-byte fallback" already established** (see that
     entry's Conventions writeup: "Scoped to dot-less filenames only —
     bounded cost; most real files carry a self-describing extension").
     Even a cheap 16-byte direct-offset read still costs something at the
     scale a keyword search can produce — hundreds or thousands of
     DISTINCT hit files, not just hits — so the discovery pass must not
     read a header off every one of them unconditionally just to build
     the prompt's own count. A hit file's own name is checked FIRST, for
     free (`.db`/`.sqlite`/`.sqlite3`, or a `-wal` suffix — pure string
     matching, no bytes touched at all); only a file with genuinely NO
     extension falls through to an actual header read. This project has
     already confirmed, repeatedly, that real extensionless SQLite/WAL
     files are common in FFS data (`History`, `viber_messages`,
     `naver_line`, ...) — so this fallback is real, not vestigial — but
     they're still the minority of files in a typical hit set, not the
     majority, which is what keeps the expensive path bounded.
   - **Main-archive and nested-archive hits need two separate branches
     for the byte-peek fallback, mirroring `SqlHitInterpretWorker.
     _read_raw_bytes`'s own existing split** (`if self.stored_path and
     self.entry_path: return read_nested_entry(...)` vs. the main-archive
     `physical`/`zip_cd_cache` path) rather than one unified read — a
     nested archive is always an ALREADY-EXTRACTED local file under
     `case_dir/nested_archives/` (extraction is a separate, manual/
     parser-triggered step, never automatic — see `nested_archive.py`'s
     own Conventions entry), so there is no `zip_cd_cache` offset to
     compute for it at all; its byte-peek has to go through
     `read_nested_entry`/a local file open instead. The free
     extension-first check is identical either way, just against
     `entry_path`'s own filename for a nested hit rather than `ui_path`.
   - **The discovery-pass header sniff must go through this project's
     own existing offset-read convention** (`zip_reader.ZipReader.read_at`
     against `zip_cd_cache`-computed offsets — the exact mechanism
     `header_scan.py` already uses for the SAME kind of magic-byte
     classification, and the same "never raw
     `zipfile.ZipFile(...).read(name)` on the main archive" rule already
     in this file's Conventions section) — never a full decompress/
     extract per candidate file. This isn't just style consistency: the
     main archive can be **network-hosted**, so downloading/decompressing
     every candidate file purely to read its first 16 bytes would scale
     badly on a large search result set; a direct-offset partial read
     costs the same whether the archive is local or remote. One read per
     DISTINCT file that actually needs it (extensionless only, per the
     bullet above), not per hit and not per every hit file regardless of
     name.
   - **A magic-byte header check is a classification heuristic, not an
     integrity guarantee, and this needs to be stated explicitly
     wherever the check is described** — not glossed over the way a
     passive, already-accepted convenience feature
     (`header_scan.classify_magic`'s existing uses elsewhere) can get
     away with. Because this check now DRIVES a user-facing decision
     (whether to bulk-run interpretation at all), its failure modes carry
     more weight: a deliberately altered/corrupted header byte sequence
     (accidental or intentionally anti-forensic) could make a real
     SQLite/WAL file silently NOT count toward the prompt — the
     auto-offer would simply never appear for it, and the examiner would
     have no way of knowing to check manually unless they already
     suspected something. The reverse (a forged header falsely counting
     a non-database file) is lower-stakes — it just wastes one
     interpretation attempt, which already fails cleanly as
     `not_sqlite`. This limitation must ship in the feature's own
     docstring/description and in this file, not left implicit.

   **What was actually built (app/keyword_search.py, no new module)**:
   `_looks_like_sqlite_by_name` — the free extension/WAL-suffix check, a
   deliberately small LOCAL copy of ffs-explorer.py's own DATABASE_
   EXTENSIONS logic rather than an import from it (app/ modules never
   import from the top-level script). `SqlHitDiscoveryWorker` — the
   background pass itself: extension-first → this case's own already-
   loaded `_header_type_overrides` cache (main-archive hits only) → a
   real header byte-peek only for a file still genuinely unresolved,
   exactly the three-layer design above. `_collect_hit_files_for_sql_
   discovery` groups by `self._search_file_items`' own existing per-file
   dict, so "distinct file" already means what the results tree itself
   already means by it — no new grouping concept. `_on_sql_discovery_
   done` shows the confirm prompt (stating the header-byte-check caveat
   directly in its own body text, not just in this file) and, on Yes,
   `_start_bulk_sql_interpret`/`_advance_bulk_sql_interpret` drive the
   SAME `_interpret_search_hit_as_sql` the right-click path already uses,
   one hit at a time — a new optional `on_done` callback on that method
   (called on every exit path, including its own early-return, so a
   malformed hit item can never silently stall the queue) is the only
   change to the existing single-hit path. `BulkSqlInterpretProgressDialog`
   mirrors this file's own pre-existing `SearchProgressDialog` visual
   style (label + `QProgressBar` + Cancel) rather than introducing
   `QProgressDialog`, a widget this file doesn't otherwise use.
   Cancelling stops queuing further hits — an already-running
   `SqlHitInterpretWorker` isn't interrupted mid-flight (no cancellation
   support in that worker today), it's just the last one started.
   Wired in as the deliberate LAST step of `_on_search_finished`, never
   interleaved with the search itself and never for a search with zero
   hit files.

   **A real, second bug found and fixed before this ever ran against
   real data, not after**: the first cut of `SqlHitDiscoveryWorker.run()`
   only ever built a `CachedZipView` from an already-existing local
   `.zcd` cache (`_zcd_load`), with no fallback — meaning on a genuinely
   brand-new case (or one where the `.zcd` copy hadn't happened yet),
   `z` would be `None` and every main-archive byte-peek would silently
   return `None`/unresolved, undercounting real SQLite/WAL hits for no
   real reason. Caught directly by testing against a real synthetic zip
   with no case_dir, not assumed correct from reading the code alone.
   Fixed by mirroring the exact same "fall back to a plain
   `zipfile.ZipFile` when the cache isn't built yet" pattern
   `HeaderScanWorker.run()` already established, rather than inventing a
   different one.

   **A second real mistake, made and caught by testing during THIS same
   pass**: while adding the `on_done` callback and the bulk-runner
   methods, an `Edit` to `_on_sql_hit_interpreted`'s own tail
   (`item.appendRow(...)` for the report-jump child) was made without
   re-reading far enough to see that method's own REAL closing two lines
   (`self.search_results_view.expand(item.index())` / `.resizeColumn
   ToContents(0)`, which expand and resize the just-populated result so
   the examiner sees it without an extra click) — the new methods landed
   between that `appendRow` call and those two lines, silently detaching
   them from `_on_sql_hit_interpreted` and making them dangling trailing
   statements of the unrelated `_advance_bulk_sql_interpret` instead
   (harmless there only by accident, since `item`/`self` still resolved
   to something, but not what those lines were ever meant to act on).
   Caught immediately by a real functional test (a fake, non-Qt-widget
   mixin instance triggered the exact `AttributeError` this produced,
   since the misplaced lines referenced `self.search_results_view`,
   which the test double never had) rather than assumed correct from a
   clean import/syntax check — a syntax-valid file is not the same claim
   as a behaviorally-correct one, confirmed the hard way here. Fixed by
   moving both lines back to `_on_sql_hit_interpreted`'s real end and
   confirmed via `git diff` that the restored method is byte-for-byte
   identical to the pre-edit original, just followed by genuinely new
   code rather than interleaved with it.

   Verified directly, not just compiled: `SqlHitDiscoveryWorker` against
   a real synthetic zip — an extensionless real SQLite file (byte-peek
   finds it), a `-wal`-suffixed file (free extension match, no peek
   needed), a plain-text decoy (correctly rejected), and a file already
   marked non-database by a prior header scan (correctly skipped without
   a redundant peek) — plus the identical extensionless-file case
   through the nested-archive branch (`read_nested_entry`). The bulk-
   runner's own sequencing was verified with a real `QWidget`-based test
   double standing in for `FastZipBrowser`: hits interpreted in the
   correct order one at a time, and a mid-queue cancellation correctly
   stops after the in-flight hit rather than continuing to the rest.

   **The zipfile-fallback fix above was itself corrected the same day,
   per direct instruction, and extended to older, already-shipped code
   too.** Falling back to raw `zipfile.ZipFile` on the main archive at
   all — even scoped to a metadata-only `.getinfo()` call, as both sites
   below did — wasn't what the examiner wanted, full stop; asked
   directly not to touch `zipfile` on the main archive rather than debate
   whether the existing narrow "metadata-only" carve-out technically
   covered it. `SqlHitDiscoveryWorker.run()`'s own fallback (added minutes
   earlier this same session) was removed outright — when the local
   `.zcd` isn't available, `z` stays `None` and `_peek_header`'s existing
   `if z is None: return None` already handles it honestly, costing no
   real coverage in practice since a keyword search (the only thing that
   ever constructs this worker) can't run before the case has fully
   loaded, and `.zcd` creation is the very first step of that load.

   The identical pattern was ALSO found, once flagged, in
   `SqlHitInterpretWorker._read_raw_bytes` — the ORIGINAL, already-
   shipped single-hit interpreter behind the whole "Interpret as SQL
   Record" feature (2026-09-12, not part of today's work at all). Fixed
   the same way, with the user's explicit confirmation before touching
   already-verified-adjacent functionality rather than assuming the same
   preference extended there silently: `zf = CachedZipView(self.zip_path,
   infos) if infos is not None else zipfile.ZipFile(...)` became a plain
   `CachedZipView(self.zip_path, infos)` — a `None` `infos` now fails
   inside the existing `try/except Exception: return None` this method
   already had for every other failure mode, the same honest "couldn't
   read it" outcome, never a raw zipfile touch.

   Verified directly: re-ran the full `SqlHitDiscoveryWorker` regression
   with NO `.zcd` present at all — an extensionless real SQLite file
   correctly comes back unresolved now (no zipfile touch to find it with)
   while a `-wal`-suffixed file still resolves for free (pure filename
   match, no I/O either way) — then re-ran it again after building a REAL
   `.zcd` via `zip_cd_cache.save`, confirming the extensionless file
   resolves correctly again once the cache exists, exactly as it does in
   real use. `SqlHitInterpretWorker._read_raw_bytes` was independently
   re-verified the same way: a real `.zcd`-backed read returns the exact
   original bytes unchanged (the normal, always-true-in-practice case),
   and a request with no case_dir at all now fails honestly (`None`)
   instead of silently reaching for `zipfile`.
   **Not yet click-tested in the live running GUI** (the actual confirm
   prompt and progress dialog on screen, and a real end-to-end search →
   discovery → bulk-interpret run against a real archive) — everything
   short of that was checked against real code paths, not simulated in
   isolation.

   **The REAL bug behind the whole zipfile back-and-forth above, found
   only once the user pushed past "fix the stray fallback" to ask why
   this kept recurring at all.** `KeywordSearchWorker` — the actual
   live-search engine, not a narrow helper — never had a `case_dir`
   parameter in the first place, so `_build_entries()`'s own call to
   `_build_zip_entries(self.zip_path, self._stop)` could NEVER pass one,
   regardless of anything else fixed above. `_resolve_search_scope`
   already caches `self._search_entries` after the FIRST search in a
   session, so this was never visible on a SECOND search — but the very
   first keyword search of every single session, for every case, always
   took the (now-removed) raw-zipfile branch and re-read the ENTIRE
   central directory over the network a second time, despite `.zcd`
   already existing by then — exactly the cost `.zcd` exists to
   eliminate, on what is arguably the single most common, everyday
   action in the whole app. This was the actual thing worth being
   frustrated about — not one leftover fallback line, but a real,
   silent, first-search-of-every-case tax that had nothing to do with
   whether `.zcd` was "ready" and everything to do with a parameter that
   was simply never wired through.

   Fixed at the source: `KeywordSearchWorker.__init__` gained `case_dir`/
   `delta` parameters (threaded through to `_build_entries`), and its one
   real construction site now passes `case_dir=self._case_dir,
   delta=self._local_extra_delta` — the exact same two values
   `ZipMetadataWorker`'s own already-correct header-scan candidate
   collection already uses elsewhere in this project. `_build_zip_entries`
   itself lost its raw-zipfile branch entirely (no `if/else`, just
   `if not case_dir: return entries` then `_zcd_load` or an honest empty
   return) — mirroring the exact same "the .zcd is guaranteed present by
   the time this can ever run" reasoning as `SqlHitDiscoveryWorker`/
   `SqlHitInterpretWorker` above, now true for a third, far more heavily
   used call site. `app/keyword_search.py` now has ZERO calls to
   `zipfile.ZipFile(...)` against the main archive anywhere in the file —
   confirmed by a full grep sweep, not assumed from having fixed the
   three known instances.

   Verified directly: `_build_zip_entries` with a real `.zcd` present
   correctly finds every real STORED entry via the cache (no zipfile
   touch); with no `case_dir` at all, correctly returns empty rather than
   falling back to a network central-directory read; `KeywordSearchWorker`
   itself confirmed to thread `case_dir` through to `_build_entries`
   correctly in both states. Re-ran the full regression suite for all
   three fixed workers (`SqlHitDiscoveryWorker`, `SqlHitInterpretWorker`,
   `KeywordSearchWorker`) together against one real synthetic archive —
   all three correct.
   *Source: user's own direct pushback on right-click discoverability,
   refined into this design during discussion; then a same-day follow-up
   flatly ruling out any raw-zipfile fallback on the main archive at all;
   then direct frustration at the pattern recurring, which led to finding
   the real, previously-invisible root cause — `KeywordSearchWorker` never
   had a `case_dir` parameter at all — rather than continuing to patch
   symptoms one at a time, 2026-09-13 — not from any of the four sibling
   tools.*

   **Project-wide comprehensive sweep, same day, per direct instruction
   ("just fix it and stop it please" — one final pass, not more
   incremental patching).** A subagent classified all remaining
   `zipfile.ZipFile(...)` call sites project-wide; every real violation
   against the MAIN archive it found — plus one it missed
   (`app/nested_archive.py`'s `extract_one`, caught in a final manual
   sweep) — was fixed the same way each time: read metadata via a local
   `.zcd`-backed `CachedZipView`, read actual bytes via `app/zip_entry.
   ZipEntry` (or `CachedZipView.open(name).read()` for a metadata-object
   caller), never a raw `zipfile.ZipFile` on the network-hosted main
   archive. Fixed, each verified against real or synthetic archive bytes
   before moving to the next:
   - `app/device_timezone.py`'s `detect_handset_zone` — removed the
     `zipfile.ZipFile` fallback entirely; now `None` if no `case_dir`,
     matching the one real caller's own guaranteed-real-case_dir context.
   - `app/media_viewer.py`'s `ThumbnailWorker` — removed the fallback;
     confirmed the existing per-item `try/except` already handles a
     `None` zip handle the same as any other unloadable thumbnail.
   - `app/hex_viewer.py`'s `HexLoadWorker` — deleted outright, not
     patched: it was a full duplicate reimplementation of `ZipEntry`'s
     own already-sanctioned DEFLATED fallback, the exact thing this
     project's Convention says never to reimplement elsewhere. Replaced
     with a direct call to `entry.read(limit=...)`.
   - `app/artifact_viewer.py`'s `ArtifactRunnerWorker.run()` — replaced
     `zipfile.ZipFile(self._zip_path)` with a `.zcd`-backed
     `CachedZipView`, raising honestly if the cache isn't available;
     confirmed via `app/artifact_runner.py` that every consumer only
     ever calls `.getinfo()`/`.namelist()` on it (metadata-only), with
     actual byte reads already routed through `ZipEntry`.
   - `ffs-explorer.py`: `_read_device_info` (honest empty return, no
     case_dir), `ExtractorWorker` (the Export feature — batched
     `CachedZipView` metadata + `compute_data_offsets`, `fallback_zf`
     removed), `_resolve_file_candidates` (no `z` → no candidates,
     fallback logic removed), `SingleFileScanWorker` (now takes
     `case_dir`, builds its own `CachedZipView`),
     `HeaderScanWorker.run()` (fallback removed), `ZipMetadataWorker`
     (its "no case_dir" branch now raises — confirmed genuinely
     unreachable, gated by an earlier `if case_dir is None: return`),
     `_get_zip_handle()` (lazy `CachedZipView` fallback instead of a
     background `zipfile.ZipFile` submit), and `_DetectFormatWorker`
     (built for the "Show scan locations" button earlier the same
     session — found during this same audit to have introduced a
     brand-new unconditional `zipfile.ZipFile` call, owned directly and
     fixed via `zip_cd_cache._extract_cd_payload` + an in-memory
     `zipfile.ZipFile(io.BytesIO(payload))` parse, the same sanctioned
     pattern `zip_cd_cache.load()` itself already uses).
   - `app/nested_archive.py`'s `extract_one` — the site the subagent's
     own report missed entirely (its table only covered this file's
     two legitimate, already-local repack lines, 170/171); found by a
     final manual sweep after the subagent's list was otherwise clean.
     Now reads the one entry being extracted via `.zcd` + `CachedZipView`
     rather than opening the main archive raw a second time per
     extraction.
   - `app/keyword_search.py`: `KeywordSearchWorker` gained a real
     `case_dir`/`delta` parameter (previously missing entirely — the
     actual root cause described above, this is the fix landing),
     `_build_zip_entries` lost its zipfile fallback outright,
     `SqlHitDiscoveryWorker`/`SqlHitInterpretWorker` already fixed
     earlier the same day (see above).
   - `app/adapters/ffs.py` — found in a follow-up pass after the main
     sweep, missed by the subagent's own file list entirely:
     `_load_launchservices_store` (the LaunchServices csstore reader
     behind `build_app_registry`, run on every real Cellebrite/GrayKey
     iOS case load) was doing a genuine DATA read
     (`_z.read(entry)`) via a raw `zipfile.ZipFile` whenever its caller
     passed no `z` — and both of its real call sites
     (`app/ffs_metadata.py`'s subprocess path and `ffs-explorer.py`'s
     in-thread `ZipMetadataWorker` path) *did* pass no `z`, despite each
     already holding a live `CachedZipView` (`z_ctx`) moments earlier —
     both comments even said "opens its own zip handle... rather than
     reusing z_ctx (already closed above)", a false premise:
     `CachedZipView.__exit__` is a no-op, so `z_ctx` was never actually
     closed and was safe to reuse the whole time. Fixed by passing
     `z=z_ctx` at both call sites, and by changing
     `_load_launchservices_store`'s own `_z.read(entry)` to
     `_z.open(entry).read()` (the one method both `zipfile.ZipFile` and
     `CachedZipView` share — `CachedZipView` deliberately has no bare
     `.read(name)` convenience method). This was a real, previously-
     silent violation on the MAIN parsing path, not an edge case — it
     ran on every iOS Cellebrite/GrayKey case load, every time.
     `_build_guid_bundle_map`'s own analogous `z=None` fallback (same
     file) was checked and left as-is: its one real call site
     (`build_ui_metadata`) already asserts `z is not None` before
     calling it, so that fallback is genuinely dead code in the app's
     own usage, matching this project's own documented exception for a
     fallback that only fires when `.zcd` "genuinely isn't available
     yet." `app/adapters/graykey.py`'s own `extract()`/`extract_metadata()`
     `z=None` fallback was checked the same way and left alone too — its
     one real in-app call site (`FfsAdapter.load_metadata`) always
     passes a real `z`; the fallback only fires from that file's own
     standalone `__main__` CLI mode (`python graykey.py <zip>`), a
     genuinely different invocation with no case_dir/`.zcd` concept at
     all — the same already-accepted class of exception as
     `scripts/validate_evidence_ranking.py`'s own top-level
     `zipfile.ZipFile` opens.
   Verified against real and synthetic archives throughout, not just
   compiled: a full cross-cutting regression script exercised
   `_resolve_file_candidates` (both with a real `CachedZipView` and with
   `z=None`), `SingleFileScanWorker.run()`, `HeaderScanWorker.run()`, and
   `_read_device_info` (with and without `case_dir`) together against one
   synthetic FORMAT_CELLEBRITE archive — all six passed (one initial
   failure was a test-data leading-slash mismatch against
   `FfsAdapter.resolve()`'s own output, not a bug in the fix, confirmed
   by direct diagnosis before correcting the test). `adapters/ffs.py`'s
   fix was separately verified against a synthetic csstore-named entry:
   `zipfile.ZipFile.open(entry).read()` and `CachedZipView.open(entry)
   .read()` returned byte-identical content, and
   `build_app_registry(..., z=cached_view)` completed cleanly end to end.

   **Final project-wide grep sweep result**: every remaining
   `zipfile.ZipFile(` call site in `app/`/`ffs-explorer.py` is one of the
   two sanctioned-internal cases (`app/zip_entry.py`'s own DEFLATED
   fallback; `app/zip_cd_cache.py`'s own in-memory CD-payload parse, the
   mechanism `.zcd` is built on — also reused directly by
   `ffs-explorer.py`'s own `_DetectFormatWorker`), a sanctioned-local-
   nested case (`app/nested_archive.py`'s own repack of already-extracted
   bytes), or a dead-in-practice fallback matching this project's own
   documented "only when `.zcd` genuinely isn't available yet" exception
   (`app/adapters/ffs.py`'s `_build_guid_bundle_map`,
   `app/adapters/graykey.py`'s CLI-only `z=None` path). `scripts/
   validate_evidence_ranking.py`'s own two top-level opens are a
   standalone, offline CLI validation tool with no case_dir/`.zcd`
   concept at all — explicitly out of scope, same as `graykey.py`'s own
   `__main__` block.
   *Source: direct user instruction, 2026-09-13 ("just fix it and stop
   it please") — comprehensive, not incremental.*

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

6. **[FIXED IN CODE, PENDING REAL WINDOWS VERIFICATION, 2026-09-14] HEIC/HEIF
   photo rendering — thumbnails AND the full-size viewer.** Picked back up
   directly ("remember we will need this for the thumb and full size since
   heic cannot be used on a pc via qt ntvlly") once the video/PyAV work
   made the underlying gap concrete: Qt's `QImage.loadFromData()` only
   decodes HEIC when the OS itself supplies a codec — macOS does (Apple's
   ImageIO framework), Windows (this project's primary shipped target)
   doesn't by default, confirmed directly (grepped `ffs_explorer.spec`/the
   Windows CI workflow for any bundled HEIC codec: zero hits).

   New shared `media_viewer._load_qimage(data, ext)` — the one entry point
   both `ThumbnailWorker` (thumbnail generation) and
   `MediaFullViewDialog._build_image` (full-size view) now call instead of
   constructing a bare `QImage` directly. Tries Qt's native decode first
   (every format Qt already handles on any platform — JPEG/PNG/etc. — is
   completely unaffected, never reaches the fallback); falls back to
   `pillow_heif` only for a `.heic`/`.heif` extension once Qt's own decode
   has already failed.

   **A real, non-obvious bug was found and fixed before this shipped, not
   assumed correct from the library's own name**: `pillow_heif` reads a
   HEIC file's real EXIF orientation tag internally but resets the
   STANDARD orientation tag it exposes back to 1 ("no rotation needed"),
   stashing the true value separately under
   `img.info['original_orientation']` — so `PIL.ImageOps.exif_transpose()`
   (which only reads the standard tag) silently never rotates a real
   portrait photo, landing it sideways (width/height transposed). Found by
   cross-checking pillow_heif's raw decode against Qt's own correct
   macOS decode of 20 real portrait HEIC photos from the IOS17 JoshHickman
   archive: 9/20 came out transposed before the fix, 0/20 after — fixed by
   writing the real `original_orientation` value back into the image's own
   exif data before calling `exif_transpose`, so well-tested standard
   library code does the actual rotation rather than a hand-rolled
   transform table.

   Verified against real data at every step, not assumed: since Qt already
   decodes HEIC natively on this macOS dev machine, the fallback path
   itself was force-exercised by monkeypatching `QImage.loadFromData` to
   always fail (the exact condition Windows is always in, having no native
   HEIC codec at all) — 20/20 real HEIC photos then matched Qt's own
   ground-truth dimensions through the forced `pillow_heif` path, and both
   real integration points (`MediaFullViewDialog`, `ThumbnailWorker`) were
   independently confirmed to produce correct output under the same forced
   condition — a real portrait/landscape photo's thumbnail came out with
   the exact correct aspect ratio (matching Qt's own ground-truth
   orientation) after scaling and JPEG re-encoding, not just the raw
   decode. A real local PyInstaller build confirmed `pillow_heif`'s own
   bundled native libraries (`libheif`/`libde265`/`libx265` — bundled
   inside its own wheel, unlike `av`, so no from-source build is needed
   for this one) are picked up automatically with zero exclusion warnings,
   and the frozen macOS exe launches cleanly.

   **NOT yet verified**: an actual Windows machine, where Qt has no native
   HEIC codec at all and every real HEIC file MUST go through this
   fallback (not just the forced-test condition) — the user has Parallels
   available for this, same as the video/PyAV work above.
   *Source: Crush (`pillow-heif` dependency originally prompted the
   check against this project's own Qt install) for the idea; the
   EXIF-orientation bug and its fix were found via this session's own
   direct investigation, not from Crush or any other sibling tool.*

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

17. **[SHIPPED IN CODE, PENDING REAL WINDOWS VERIFICATION, 2026-09-14]
    Replace `media_viewer.py`'s subprocess `ffmpeg` video-thumbnail call
    with `av` (PyAV), built from source.** Picked back up after the
    project-wide zipfile-elimination sweep's own real-archive speed
    testing prompted the user to ask "is there any way we can be faster?",
    narrowed by direct instruction to "speed and quality are important
    but I want simple ... I like the idea of it being in the exe."

    **Two real, independent bugs were found and fixed, not just a
    speed/packaging swap** — confirmed directly against real video files
    from the IOS17 JoshHickman test archive at every step, never assumed:

    - **The OLD implementation had its own pre-existing correctness bug**:
      piping video bytes to ffmpeg via `pipe:0` (unseekable) failed on
      13/15 real sampled videos with "Invalid data found when processing
      input" — real camera-original MOV/MP4 files commonly store their
      index (`moov` atom) at the END of the file, which a pipe can't seek
      back to. Confirmed via a real seekable temp file: same bytes, same
      ffmpeg, 100% success. `av.open(io.BytesIO(...))` gives PyAV a
      genuinely seekable in-memory stream — the identical fix, no temp
      file needed.
    - **PyAV's own official PyPI wheel cannot decode HEVC** (Apple's
      default recording codec since iOS 11): 6/6 real HEVC test videos
      demuxed every packet in the file (matching each file's own declared
      frame count exactly) with ZERO frames ever decoded, via the
      library's own documented high-level API, no exception raised. The
      decoder registers fine (`Codec('hevc','r')` succeeds, real 223-byte
      hvcC extradata present) but silently never produces output — a real
      gap in the wheel's bundled FFmpeg build, confirmed NOT a fundamental
      HEVC limitation (the exact same files decode correctly via a real
      Homebrew system FFmpeg, `libavcodec 63.1.101`). User asked directly
      "can we fix pyav" rather than accept the gap — fixed by building
      PyAV from source (`pip install --no-binary av`) linked against that
      real FFmpeg via `pkg-config`: all 6 previously-failing files then
      decoded with EXACT frame counts matching their own container
      metadata (176/901/470/360/360/360).

    This means `av` cannot just be `pip install`ed for this project's real
    needs — `requirements.txt` now documents this, and
    `.github/workflows/build-windows-exe.yml` was updated to install a
    real Windows FFmpeg dev package via chocolatey (`ffmpeg-shared` +
    `pkgconfiglite`) and build `av` from source against it
    (`pip install --no-binary av`), rather than pulling the prebuilt wheel.
    `ffs_explorer.spec` gained an explicit DLL-glob safety net (bundling
    every DLL from the same chocolatey ffmpeg install `av` was built
    against) since the community `pyinstaller-hooks-contrib` `av` hook is
    tuned for the OFFICIAL wheel's own DLL-bundling layout, not a
    from-source build linked against an external install directory.

    **A third real bug was caught before shipping**, by actually running a
    local PyInstaller build rather than trusting the dev-venv result would
    carry over: `av.VideoFrame.to_image()` needs PIL/Pillow, present in
    the dev venv only as an incidental transitive dependency of something
    else — never declared in `requirements.txt`, AND `ffs_explorer.spec`
    had `'PIL'` sitting in its own `excludes=` list from before this
    project had any real use for it (a "trim things you definitely don't
    need" list, now stale). A clean rebuild's own warning log
    (`excluded module named PIL - imported by av.video.frame (delayed)`)
    caught this directly, not assumed fixed just because `Pillow` was
    added to `requirements.txt` — both are now fixed.

    **Verified**: dev-venv correctness against real archive data (14/15
    and 39/40 real videos decoded correctly across two independent random
    samples — the only 2 "failures" were a genuine 0-byte file and a
    genuine audio-only MP4 with zero video streams, both correct
    negatives); a local macOS PyInstaller build correctly bundled the
    from-source `av`'s linked FFmpeg dylibs (PyInstaller's own dependency
    walker found `libavcodec.63.dylib` etc. automatically) and PIL's
    compiled extensions with zero remaining exclusion warnings; the frozen
    macOS exe launches and stays running with no import-time crash.
    **The Windows from-source build was attempted for real and reverted,
    2026-09-14, same day** — two consecutive real CI failures, not
    theoretical risk: (1) `choco install ffmpeg-shared` failed outright —
    its own pinned checksum (`8030dc46...`, expecting v8.0.1) no longer
    matched what gyan.dev's live "latest" URL actually served
    (`cb4d5e8d...`, v9.0.1) — a real, structural fragility in that
    specific chocolatey package (it wraps a rolling upstream URL behind a
    stale pinned hash), not something a retry fixes. (2) Before that was
    even found, the FIRST attempt failed at a different step: the
    hardcoded assumption about where the package extracts its files
    (`...\tools\ffmpeg-*-shared\lib\pkgconfig`) was simply wrong — fixed
    to search recursively instead, which is what surfaced failure (1).
    Investigated a direct (non-chocolatey) alternative — a real, pinned,
    checksummed mirror exists at `github.com/GyanD/codexffmpeg/releases`
    (confirmed via the GitHub API: a real `9.0.1` tag with a
    `ffmpeg-9.0.1-full_build-shared.zip` asset) — but downloading and
    inspecting it directly surfaced a THIRD real problem: this standard
    Windows FFmpeg distribution ships `.lib`/`.dll.a` import libraries and
    headers, but **no pkg-config `.pc` files at all** — meaning the
    `PKG_CONFIG_PATH`-based discovery this whole recipe was built around
    can never work against it regardless of which download source is
    used. PyAV's own `setup.py` does have a documented `--ffmpeg-dir=`
    flag for exactly this no-pkgconfig case, but reliably getting that
    flag through a modern `pip install`'s build-isolation subprocess
    turned out to be genuinely uncertain even after direct research (would
    likely need a raw `setup.py build_ext --ffmpeg-dir=... install`
    invocation instead of `pip install`, itself unverified).

    Per direct instruction, rather than keep engineering an increasingly
    complex, still-unverified recipe, **reverted to a plain
    `pip install av` for Windows CI** to test a cheaper hypothesis first:
    the HEVC-decode gap was only ever confirmed against the *macOS*
    prebuilt wheel — the Windows wheel is a separately-built artifact and
    Windows FFmpeg builds are often compiled with a more complete codec
    set than Apple's own bundling choices, so it may simply not have the
    same gap. `ffs_explorer.spec`'s DLL-glob safety net (needed only for
    a from-source build) was reverted alongside it — the plain wheel
    bundles its own DLLs, which `pyinstaller-hooks-contrib`'s `av` hook is
    already built for.
    **NOT yet verified**: the actual Windows CI build under this reverted,
    simpler recipe — specifically, whether the plain Windows wheel plays
    a real HEVC video thumbnail correctly or not. If it doesn't, the
    from-source path above will need to be revisited with the
    `--ffmpeg-dir=`/raw-`setup.py` approach, now that the pkg-config path
    is confirmed to be a dead end for this distribution.
    *Source: Crush (`av` dependency vs. this project's own subprocess
    `ffmpeg` call) for the original idea; the HEVC-decode bug, its
    from-source fix (macOS), the pipe-seekability bug, and the full
    Windows CI investigation (all three real failures, and the decision
    to revert and test the simpler hypothesis first) were all found via
    this session's own direct investigation and real CI runs, not from
    Crush or any other sibling tool.*

18. **[ALEAPP, low cost] Consider a `sample_data`-style lightweight
    provenance note per parser** — a one-line "confirmed N rows on
    real device X, OS Y" string in each parser's own module, cheaper than
    a real automated test but a genuine, real discipline ALEAPP holds
    itself to across all 369 of its own plugins.
    *Source: ALEAPP (`__artifacts_v2__`'s `sample_data` dict).*

19. **[DONE 2026-09-12 — first cut] Stand up a real pytest suite with
    committed micro-fixtures.** `requirements-dev.txt` + `tests/`
    (`conftest.py`'s `basic_db_raw`/`wal_db` fixtures, both real files
    built via genuine `sqlite3` calls, never hand-encoded bytes) — 18
    tests, all passing: `_cell_local_payload_size`'s overflow-threshold
    boundary math; `build_page_map`/`locate_offset`/`identify_structure`
    against a fixture reproducing both real bugs found this session
    (a `WITHOUT ROWID` table misattributed by page-shape alone; the
    `sqlite_master`-exclusion scope gap in `locate_offset`'s cached
    path), plus cached-vs-fresh equivalence and rowid-alias substitution;
    `locate_wal_offset`/`identify_wal_structure` recovering a genuinely
    deleted row from an un-checkpointed WAL fixture built with the same
    "blocking reader" trick verified live against LINE's real WAL
    earlier this session. One real fixture bug caught before trusting
    it: the WAL fixture needed an explicit `PRAGMA wal_checkpoint(TRUNCATE)`
    before the blocking reader attaches, or the base file never gets a
    chance to checkpoint at all and comes back with no schema.

    **Still open, not yet covered**: `chrome_tabs.py`'s three parse
    functions (this item's own original second target — small real
    files already verified byte-for-byte this session, but not yet
    frozen as committed fixtures) and every other module this project's
    manual verification has already checked by hand but never pinned
    down as an automated regression (the record_source/hex-citation
    fixes, the parser-family gap-sweep additions, etc.) — this is a
    first cut establishing the harness and covering today's own new
    code, not a claim of broad coverage.
    *Source: Crush (`crush/tests/`), mf-scan (`tests/`), with ALEAPP as an
    explicit counter-finding.*

20. **[DONE — creation-time picker + indicator, 2026-09-13; ProcessDialog/
    manual-rescan sharing still open, see below] Three-tier header-scan
    scoping, plus transparency + a confirm-gate for the expensive tier.**
    Original behavior, confirmed directly
    from `_collect_header_candidates` (ffs-explorer.py) before any of
    this was designed: `CaseSettingsDialog`'s single checkbox ("Scan
    unknown file headers for precise type detection," unchecked by
    default) already scopes to `ffs_adapter.scan_folders() AND
    _get_file_type(...) == 'Other'` — never the whole archive, and never
    a file that already has a recognized extension, even to check it for
    a mismatch. Runs as a non-blocking background pass (the tree is
    already populated and browsable before it starts, per
    `ZipMetadataWorker`'s own step order) — "takes a while" today means
    "the full picture isn't final yet," not "the app is frozen."

    Replacing the single checkbox with three explicit, named tiers so the
    examiner picks a real cost/value tradeoff instead of an opaque
    on/off switch. **Per direct follow-up instruction, there is
    deliberately NO "don't scan" option any more** — `CaseSettingsDialog`
    now offers exactly Tier 1/2/3, Tier 1 pre-selected, so unknown-
    extension files always get typed for every new case, with no way to
    skip that floor entirely:
    1. **Unknown-extension files only, within `scan_folders()`** — the
       original behavior, unchanged, now the mandatory floor rather than
       an opt-in.
    2. **Every file, within `scan_folders()`** — also re-checks files
       that already declare a recognized extension, catching a
       DELIBERATELY mislabeled file (the real gap tier 1 can't see) —
       still cost-bounded to the same curated app/user-writable areas,
       never OS/system trees a suspect has no ability to write into
       anyway. A strict superset of tier 1's own coverage — a live
       coverage-summary label under the picker states this in plain
       words for whichever tier is currently selected.
    3. **Every file, everywhere — no folder restriction.** The
       exhaustive backstop, not a routine default: `scan_folders()` is a
       curated allowlist maintained by hand, and there's no guarantee
       it's complete for every case — tier 3 exists specifically for
       "I don't trust our own boundary for this case," not as "more
       thorough, so pick it if you're not sure." Selecting it raises the
       shared `_confirm_tier3` gate (see below); declining reverts the
       selection and leaves the underlying dialog OPEN (never closes/
       cancels it), so the examiner can immediately pick a different
       tier rather than having to reopen the whole dialog from scratch.

    **`_confirm_tier3` — one shared, deliberately hard-to-click-through
    confirm gate, used by BOTH pickers** (`CaseSettingsDialog`'s own and
    `ProcessDialog`'s upgrade picker, below) — per direct follow-up
    instruction to make sure there's "enough in the dialog" that
    choosing Tier 3 is a deliberate act, not a reflexive click. Uses
    custom button text (**"Yes, Scan Every File"** / **"No, Use a
    Different Tier"**), not a generic Yes/No pair — a generic pair is far
    easier to click through without reading — with the declining button
    as the dialog's default (Enter/Return activates it, not the
    expensive choice), and states the real cost/benefit tradeoff in the
    body text (Tier 2 already covers every area a suspect could actually
    reach; Tier 3 only helps if there's a specific, case-related reason
    to suspect something hidden outside the normal app/user-accessible
    areas). One implementation, not two independently-worded copies that
    could drift.

    **Transparency requirement, per direct instruction**: a "Show scan
    locations" button next to the tier picker reveals the REAL paths
    `scan_folders()` will use for THIS archive's own detected format —
    iOS-only paths for an iOS extraction, Android-only for Android/
    GrayKey — never a generic combined list, so the examiner can see and
    judge the actual boundary rather than trust a description of it.
    Real sequencing wrinkle found while designing this, not glossed
    over: `CaseSettingsDialog` runs BEFORE format detection
    (`FfsAdapter.detect()` happens later, inside `ZipMetadataWorker`) —
    but `detect()` itself only ever needs the zip's own central-directory
    namelist (`z.namelist()`), a metadata-only read, never a data read —
    so the button's own click handler can cheaply do its own on-demand
    `zipfile.ZipFile(zip_path).namelist()` → `FfsAdapter.detect()` →
    `scan_folders()`, off the GUI thread (a small worker, same
    "never block on a zip touch" convention as everywhere else in this
    project), rather than needing detection to have already happened
    before the dialog can offer this at all. `ProcessDialog`'s own
    pre-existing "Scanned Folders…" button reuses the identical
    extracted `_show_scan_folders_dialog` — the format is already known
    there (no on-demand detection needed), so it's a trivial call.

    **A real candidate considered and retracted while designing this**:
    `mobile/Media/` (`PhotoData/`, `DCIM/`) was floated as a
    `scan_folders()` gap — a real, populated, high-volume location
    genuinely outside the current list — but checked directly rather
    than assumed: iOS Photos content only ever arrives via
    `PHPhotoLibrary`-style APIs (Camera, Photos import, "Save Image"
    from Safari/Messages) that validate the data actually decodes as a
    real image/video before creating an asset, and DCIM's own filenames
    are OS/app-assigned (`IMG_xxxx.JPG/MOV/HEIC`), never user-labelable
    — a Word doc renamed to `.jpg` can't land there through any normal
    (non-jailbroken) iOS path the way it could in a general-purpose
    Downloads-style folder. Not added to `scan_folders()` on this basis;
    revisit only with a real, case-specific reason to suspect a
    jailbroken device's direct-filesystem-write bypassing this.

    **`ProcessDialog` — upgrade-only, per direct follow-up instruction:
    "if tier 2 has been selected then all you can do is upgrade to tier
    3... if tier 1 then can change to 2 or 3... there is no going
    back."** The old single "Scan file headers" checkbox is gone;
    `ProcessDialog` now reads whichever tier is already persisted for
    this case (`_load_current_header_tier`, 0 if genuinely unknown — a
    legacy case predating this feature) and offers ONLY the strictly-
    higher tiers as upgrade choices — already at Tier 2, only Tier 3 is
    offered; already at Tier 3, nothing is offered at all ("already at
    the maximum tier" is stated directly). `_rebuild_tier_upgrade_ui`
    rebuilds this set on `_browse_case` (a different case folder can
    genuinely be at a different tier) and again after a scan finishes
    (`_finish_operations`), so the picker always reflects reality rather
    than a stale snapshot from dialog-open time.

    **A real Qt quirk found and worked around while building this,
    verified by direct testing, not assumed**: the ORIGINAL plan used
    the same `QRadioButton`/`QButtonGroup` shape as `CaseSettingsDialog`'s
    own picker — but `ProcessDialog` genuinely needs a "nothing chosen
    yet" state (`CaseSettingsDialog`'s picker never does, since Tier 1
    is always pre-selected there), and an exclusive-group radio button
    CANNOT be reliably programmatically unchecked back to "nothing
    selected" from within its own click handler: `QButtonGroup.
    checkedId()` kept reporting the just-clicked id even after
    `setChecked(False)` (with `setAutoExclusive(False)` toggled around
    it, and even deferred via `QTimer.singleShot`) — confirmed
    reproducible in complete isolation, outside this dialog entirely,
    before concluding it wasn't a mistake in the surrounding code.
    Switched to plain `QCheckBox`es with hand-rolled mutual exclusivity
    (`_on_process_tier_toggled` unchecks any other currently-checked box)
    instead — confirmed by the same isolation test that a checkbox's own
    `setChecked(False)` from inside its own `toggled` handler behaves
    exactly as expected, no quirk. `CaseSettingsDialog`'s own picker was
    NOT changed — its revert always targets a real previous tier (never
    "nothing"), which never hit this failure mode.

    Tier 3 uses the same shared `_confirm_tier3` gate as
    `CaseSettingsDialog`. Completing an upgrade persists the new tier
    directly (`ProcessDialog` already owns the `case_dir`/`case_settings`
    connection at that point) and emits a new, narrowly-scoped
    `header_scan_tier_done(int)` signal — deliberately NOT reusing the
    existing `header_scan_done(dict)` signal for this, since that one is
    ALSO fired for two unrelated events (nested-archive type updates,
    gzip-content detection) that are not a header-scan tier run at all;
    guessing a tier from those would have mislabeled the banner. The main
    window's own connection just refreshes the blue banner on this
    signal — no second persist, `ProcessDialog` already did it.

    **The nested-archive checkbox no longer force-ticks the header
    scan**, per direct instruction: *"now that we always do option 1 we
    do not need to worry about doing scan file headers before doing the
    zip scan... no need to force a tick when find archive is selected."*
    Since every case now gets at least Tier 1 automatically at creation,
    the old `_sync_header_checkbox`'s "extraction needs a completed scan
    first — force it on" branch no longer has a real gap to guard
    against — removed outright, along with `_on_nested_toggled` (which
    only ever called it). The checkbox's own tooltip was reworded from
    "Requires a completed header scan..." to state plainly that this is
    already covered, not a precondition the examiner needs to manage.

    A new blue banner (`_header_scan_banner`, `dialog_helpers.
    ACTIVE_COLOR` — deliberately a different colour from the timestamp
    banner's own orange so the two independent settings are never
    confused for one at a glance) states the active tier explicitly
    above every tab, mirroring `_refresh_timestamp_mode_indicator`'s own
    "never silently omit an active default" convention. `_HEADER_SCAN_
    TIER_LABELS` is the single shared source of truth for tier wording —
    `CaseSettingsDialog`'s picker, `ProcessDialog`'s upgrade picker, and
    the banner's own text all read from it, so the three surfaces can
    never describe the same tier differently.

    Verified directly, not just compiled: the tier predicate against a
    synthetic five-file layout produced the exact expected count at
    every tier; `CaseSettingsDialog`'s tier-3 confirm/revert (now via
    `_confirm_tier3`, mocked both ways) still reverts correctly with the
    "off" option gone and Tier 1 as the new default; `_DetectFormatWorker`
    was run against a real synthetic Cellebrite-shaped zip and correctly
    returned the real iOS `scan_folders()` list; the `case_settings`
    round-trip was confirmed against a real `caseresults.db`.
    `ProcessDialog`'s own upgrade-only logic was verified across all four
    starting states (no scan yet, Tier 1, Tier 2, Tier 3) — each offers
    exactly the strictly-higher tiers and no others; the checkbox-based
    tier-3 decline/accept, the manual-exclusivity switch between two
    different upgrade choices, and a full simulated run through
    `_on_header_done` all confirmed correct — including catching and
    fixing a real bug during this pass: the tier-persistence write was
    initially nested inside the `run_id is not None` block, so a run
    whose `start_run_log` call had failed (an unrelated, separate
    bookkeeping concern) would silently skip persisting the tier at all;
    moved out to its own independent condition (`not cancelled and
    pending_upgrade_tier > 0`) and re-verified. **Not yet click-tested in
    the live running GUI** (the banner's own on-screen rendering, and
    both dialogs' real visual layout) — everything short of that step
    was checked against real code paths, not simulated in isolation.
    *Source: user's own direct pushback on wasted scan time vs. missed
    coverage, and direct follow-up instructions on the upgrade-only
    behavior and the nested-archive force-tick removal, 2026-09-13 — not
    from any of the four sibling tools.*

21. **[DONE, 2026-09-13] Tier-3-unlocked, whole-case embedded-archive
    review, closing a real keyword-search blind spot.** Two real gaps
    confirmed directly, not
    assumed, while discussing why a keyword search might miss content
    inside an embedded archive:

    1. **An unextracted nested archive's own content is genuinely
       invisible to search today.** `KeywordSearchWorker`
       (`app/keyword_search.py`) searches the main archive's own raw
       entry bytes; if a nested zip's INTERNAL files are DEFLATE-
       compressed (the normal case for an ordinary zip — unlike this
       project's own FFS main archives, confirmed elsewhere to be 100%
       STORED), the plaintext simply never appears anywhere in the outer
       entry's raw bytes to match against. `NestedArchiveSearchWorker`
       only ever searches ALREADY-extracted nested archives (the
       repacked copies under `case_dir/nested_archives/`) — a nested
       archive nobody has extracted yet is invisible to BOTH search
       workers, not degraded, actually invisible.
    2. **Running Tier 3 doesn't currently fix this, even though it
       feels like it should.** `_discover_all_archives` (backs "Find and
       select archives for extraction" / `ArchiveSelectionDialog`) is
       hard-scoped to `ffs_adapter.archive_discovery_folders()`
       (`if not ui_path.startswith(scan_roots): continue`) — checked
       BEFORE ever consulting `header_type_overrides`, so an embedded
       archive a Tier 3 scan already correctly classified as `'Archive'`
       OUTSIDE that folder list is silently filtered out of the
       extraction picker regardless. Paying Tier 3's full cost currently
       buys type-recognition completeness but NOT extraction-picker
       completeness — a real, previously-unnoticed mismatch between what
       Tier 3 actually knows and what the UI built on top of it surfaces.

    **Planned fix**: `_discover_all_archives` gains an `unscoped: bool`
    parameter — when true, skips the `archive_discovery_folders()`
    filter entirely and considers every path in `ui_metadata`, relying
    purely on `header_type_overrides` (already computed, no new I/O) to
    tell real archives from everything else. `_show_archive_selection`
    computes whether this case's EFFECTIVE tier (accounting for an
    upgrade that just completed in the same run, not only what was
    already persisted before this run started) is `>= 3`, and passes
    `unscoped=True` only then — Tier 1/2 keep today's exact bounded
    behavior unchanged. No new confirmation step: reaching Tier 3 at all
    already went through `_confirm_tier3`'s own deliberate gate, so
    unlocking the wider review on top of that isn't a second decision to
    re-confirm — it's the natural payoff of a choice already made
    carefully.

    **The "default is good enough most of the time" framing, per direct
    instruction, grounded in this project's own already-verified finding
    rather than a vague reassurance**: `app_intelligence.py`'s own
    `embedded_archives` work (CLAUDE.md) already checked this directly
    against three real cases — every embedded archive found there turned
    out to be app-internal SDK/telemetry cache (WhatsApp bandwidth
    models, Instagram ML models, Google Play delivery cache, LINE
    stickers), NOT a confirmed case of real user-facing evidence hidden
    this way. The status text shown when this wider review opens should
    say this plainly — extracting everything before a routine search is
    usually unnecessary, and this path exists for a case where there's a
    specific, case-related reason to think otherwise, not as a "do this
    to be thorough" nudge that would undercut the whole point of the
    tiered design.
    **Verified directly, not just compiled**: `_discover_all_archives`
    with `unscoped=True` correctly found a synthetic archive placed
    OUTSIDE `archive_discovery_folders()` (a real gap the pre-existing
    scoped call silently missed, confirmed side-by-side against the same
    input); `unscoped=False` correctly kept excluding it, confirming no
    regression to Tier 1/2's existing bounded behavior. `_show_archive_
    selection`'s effective-tier computation was verified across all
    three real scenarios that matter: already persisted at Tier 3 (no
    new scan this run) → unscoped; persisted at Tier 1 but upgraded to
    Tier 3 in this same run → unscoped (the case that needed the
    "effective," not just "already-persisted," tier check); persisted at
    Tier 2 with no upgrade → stays scoped. A real Qt/PySide6 test-harness
    gotcha was hit and worked around while verifying this, not in the
    shipped feature itself: driving `_show_archive_selection` with the
    real `ArchiveSelectionDialog` under an offscreen platform first hung
    (a real modal blocking forever with nothing to click it — the same
    class of gotcha this project has already documented once before for
    a different dialog) and then segfaulted when only `.exec()` was
    patched on the real class; resolved by substituting a minimal dummy
    stand-in class for the dialog entirely during the test, matching this
    project's own established practice of watching for exactly this
    failure mode in headless verification scripts. **Not yet click-
    tested in the live running GUI** (the real `ArchiveSelectionDialog`'s
    own on-screen behavior, and the status message's actual visibility)
    — everything short of that was checked against real code paths.

    **A real bug found the same day via direct user report, fixed
    immediately: the tier used to gate `unscoped` was the wrong one.**
    The user reported that after running Tier 3, the archive list still
    only showed items inside app/user areas. Root cause, confirmed
    directly rather than assumed: `header_scan_tier` (case_settings) is
    written the MOMENT a tier is chosen — deliberately, at
    `_get_or_ask_case_dir` time, so the blue banner can show the
    examiner's choice immediately even while a large scan is still
    running (see item 20's own banner work). But `ProcessDialog`'s
    `_current_tier` (used both for the available-upgrades list AND this
    item's own `unscoped` gate) was reading that SAME optimistic value —
    so opening "Find and select archives" (or `ProcessDialog` at all)
    WHILE a just-requested Tier 3 scan was still running in the
    background (the creation-time scan has no cancel path and can take a
    real, non-trivial amount of time on a large archive — confirmed the
    app remains fully interactive during it, per item 20's own "non-
    blocking pass" note) would trust incomplete `header_type_overrides`
    data: anything outside `scan_folders()` the scan simply hadn't
    reached yet still read as `'Other'` and was silently excluded,
    exactly matching what was reported.

    Fixed by splitting one key into two: `header_scan_tier` stays the
    optimistic "requested" value (banner text, unchanged); a NEW
    `header_scan_complete_tier` is written only at the point a scan
    GENUINELY finishes (`_on_header_scan_done` for the creation-time
    path — confirmed its own header-scan call has no cancel/interrupt
    branch, so reaching that signal always means real completion;
    `ProcessDialog._on_header_done`'s existing `not cancelled` guard for
    its own upgrade path). `_load_current_header_tier` (gating both
    available upgrades and this item's `unscoped` decision) now reads
    ONLY the complete-tier key. Three more honesty additions so the gap
    is never silent while it exists: the blue banner appends "— scan in
    progress…" whenever requested > complete; `ProcessDialog`'s own
    status label appends a warning naming the in-progress tier; and
    `_show_archive_selection`'s status text does the same when discovery
    ends up scoped specifically because of this, rather than looking
    like "no archives exist outside app/user areas."

    Verified directly by reproducing the exact reported scenario, not
    just reasoning about it: a case with `header_scan_tier='3'` but no
    `header_scan_complete_tier` set (simulating the scan still running)
    correctly reports `_current_tier=0`, still offers all three upgrade
    tiers, shows the in-progress warning in both `ProcessDialog`'s status
    label and `_show_archive_selection`'s own status text at the moment
    the archive dialog would open, and correctly uses SCOPED (not
    unscoped) discovery; setting `header_scan_complete_tier='3'`
    afterward (simulating genuine completion) immediately flips all of
    that to the fully-unlocked state. Full regression suite from this
    item and item 20 re-run afterward with zero failures.

    **A per-path "Preview files to scan…" button was tried the same day
    and explicitly walked back — recorded here so it isn't silently
    re-tried later.** First built as a separate button opening a popup
    listing every candidate `ui_path` for the selected tier. Direct
    follow-up feedback corrected two things: (1) this made Tier 2/3
    behave differently from Tier 1, which never showed a path list at
    all — Tier 1's own "what's covered" signal has always just been a
    plain COUNT (`_refresh_stats`'s "Files this upgrade will scan: N"
    line, pre-existing) plus a live countdown once the scan actually runs
    (`_on_header_progress`'s "Scanning headers: N files remaining…",
    also pre-existing) — a full path listing was new surface Tier 1 never
    had, not parity with it; (2) the intended interaction was the list/
    count appearing automatically and directly as part of the dialog, not
    behind an extra button opening a separate popup. Reverted entirely —
    `_list_header_candidates`, the button, and the popup dialog were all
    removed — back to the exact same count-only display every tier
    already shared (`_count_header_candidates`, unchanged).

    Separately confirmed, not assumed, in response to the same
    conversation: the archive-selection dialog was ALREADY only ever
    opened after a scan's genuine completion (`_show_archive_selection`
    is only called from `_on_header_done`, itself only reached once the
    scan worker's `done` signal fires) — nothing needed to change there;
    what looked like "archives shown before the scan finished" earlier
    was the tier-completion race described above, now fixed.

    **A real, separate correctness gap found and closed while addressing
    the same feedback**: the user asked for the dialog to be
    non-dismissible while a scan runs, "because I prefer it to be done
    before they interact with the rest of the app." Checked directly
    before building anything: `ProcessDialog.accept()`/`.reject()`/
    `.closeEvent()` already all refused to close while `_is_scanning()`
    — a pre-existing guard, so no crash/orphaned-worker risk actually
    existed. What was genuinely missing was the VISIBLE half of the same
    project convention already established in `ArtifactRunnerDialog`/
    `AISummaryDialog` (`app/artifact_viewer.py`): disabling the Close
    button itself during a run, not just silently refusing the click.
    Added `self._close_btn.setEnabled(False)` alongside the existing
    `self._run_btn.setEnabled(False)` in `_run_operations`, re-enabled
    together in `_finish_operations` — matching that exact precedent
    rather than inventing a new pattern.

    Verified directly: reverting to count-only confirmed the stats line
    still reads "Files this upgrade will scan: N" for whichever tier is
    checked, with no path-list widgets left anywhere in `ProcessDialog`;
    the close-button/dialog-guard behavior was confirmed with a mocked
    running worker — `_close_btn` disabled, `.accept()` refusing to
    close, `.closeEvent()` ignoring the close request, all three
    correctly reverting to normal once the worker stops running. Full
    regression suite from earlier in this item re-run with zero failures.

    **The completion message's own count was misleading for Tier 2/3,
    fixed the same day per direct instruction.** Both report a raw
    `len(results)` — every file the scan managed to classify. Fine for
    Tier 1, where every candidate is already `'Other'`-typed by
    construction, so every result genuinely IS new information. Wrong
    for Tier 2/3, which re-check every file regardless of existing type
    — most of `results` on a real archive would just be ordinary files
    (a real `.jpg`, a real `.zip`) whose header correctly confirms what
    the extension already said, drowning out the two things actually
    worth reporting: a file that had NO type before and now has one, and
    a file whose real content DISAGREES with its extension — the actual
    point of running Tier 2/3 at all.

    New `_classify_header_scan_results(results)` splits the raw dict into
    `(newly_identified, mismatch_found)` and is the only thing both
    completion messages (`ZipMetadataWorker.run()`'s creation-time one and
    `ProcessDialog._on_header_done`'s own) now report. A real taxonomy
    trap was found and fixed BEFORE this ever ran against real data, not
    after: a correctly-named `.gz`/`.bz2`/`.xz` file is extension-
    classified as `'Archive'` (`ARCHIVE_EXTENSIONS`) but its real header
    content is `'Compressed'` (`header_scan.py`'s own signature table) —
    same broad kind of file, two different label vocabularies, NOT a
    mislabeled file. Naively comparing the two strings would have flagged
    every correctly-named compressed file in the archive as a false
    "mismatch," the exact opposite of what this fix exists to prevent.
    Reuses the already-established `_EXTRACTABLE_TYPES = {'Archive',
    'Compressed'}` equivalence (previously only used by `_is_extractable`)
    rather than inventing a second grouping for the same real-world fact.

    Verified directly against a realistic mixed batch: two genuinely
    extensionless files (correctly counted as newly identified), a real
    disguised file (`.jpg` extension, `'Database'` real content — the
    kind of case Tier 2/3 exists to catch, correctly counted as a
    mismatch), a real `.jpg` whose header confirms `'Picture'` (correctly
    NOT counted), and both a `.gz` and a `.zip` whose headers confirm
    their own extension-implied category (both correctly NOT counted,
    confirming the taxonomy-equivalence fix actually holds). End-to-end
    message wiring re-verified through `ProcessDialog._on_header_done`
    itself, producing the real user-facing string: "Header scan done — 1
    newly identified, 1 mismatched extension found, from 3 candidates."
    Full regression suite re-run afterward with zero failures.

    **A real, CONFIRMED-AGAINST-LIVE-DATA miscategorization found and
    fixed the same day, once Tier 3's unscoped discovery made it
    reachable at all.** `ArchiveSelectionDialog._build_tree_android`
    only ever recognized two buckets — `AndroidMedia` (anything under
    `data/media/`) and everything else lumped into "App Data," grouped
    by `_android_package(ui_path)` or, when that returned nothing, a
    literal `'Unknown'` sub-group. That fallback reads as "some app we
    couldn't identify," but the real situation for anything genuinely
    outside `data/data/<package>/` — reachable at all only via Tier 3's
    own unscoped discovery, since the scoped tiers/discovery never look
    outside app/user areas in the first place — is "not inside any app's
    data folder at all," a materially different and potentially more
    interesting fact to bury under a misleading label. The user directly
    confirmed this "Unknown" bucket was real and populated on their own
    live, currently-open case before this fix was built — not fixed on
    a hypothetical.

    Fixed by giving `_build_tree_android` a genuine third bucket,
    `other_archives` — anything that's neither `AndroidMedia` nor
    resolves to a real package via `_android_package` now renders as its
    own top-level "Other" group (reusing the existing `_make_app_node`
    exactly as the "User Storage" volume nodes already do), never folded
    into "App Data" at all. `_classify_archive`'s own category strings
    were deliberately left unchanged — this is purely a display-grouping
    fix in `ArchiveSelectionDialog`, not a change to what Tier 3 detects
    or how it's classified upstream.

    Verified directly: a realistic mixed batch (one real app-data
    archive, one real media archive, and two archives on paths outside
    both — e.g. `system/`, `cache/`) correctly split into three separate
    top-level groups with the right counts and children, confirmed by
    walking the actual rendered `QTreeWidget`; a regression case with
    only genuinely-categorizable archives confirmed no spurious empty
    "Other" group appears when there's nothing that belongs in it.
    (Separately: tried to verify this against the user's own live case
    directly via the ffs-explorer MCP connection before making any
    change, per this project's own "verify against real data" discipline
    — the connection wasn't reachable this session, app process running
    but its MCP server not currently accepting connections — so this
    relied on the user's own direct confirmation instead, not fabricated
    or assumed.)
    *Source: user's own direct observation that unextracted archives
    aren't searched properly, refined through discussion into tying the
    fix to Tier 3; then direct follow-up correcting the file-list UX back
    to Tier 1's own simple count-only style and requesting the dialog
    stay non-dismissible during a scan; then a completion-count follow-up
    for Tier 2/3; then this same-day follow-up, confirmed against the
    user's own live case, on the "Unknown" Android-grouping
    miscategorization, 2026-09-13 — not from any of the four sibling
    tools.*

    **The identical gap confirmed and fixed on iOS the same day, per
    direct follow-up asking whether the same three-area structure
    applies there.** `ArchiveSelectionDialog._build_tree_ios` has its own
    equivalent fallback: anything that isn't SMS/Mail/Files resolves via
    `_bundle_id_for_path(ui_path, guid_to_bundle)`, and previously fell
    back to a literal `'Unknown'` bucket when that returned nothing —
    which then nested under "Third-Party Apps" (since `'Unknown'`
    doesn't start with `com.apple.`), implying an unidentified THIRD-
    PARTY APP's own data rather than the real situation: not inside any
    app container at all (`mobile/Library/Caches/`, `mobile/Media/`,
    `private/var/db/`, `mobile/Library/Biome/`, or anywhere else Tier
    3's unscoped discovery can reach). Fixed the same way: an `other_
    archives` list catches anything with no resolvable bundle id, and
    renders as its own top-level "Other" group instead of being folded
    into either app bucket. `_bundle_id_for_path` itself needed no
    change — confirmed it already returns `''` specifically when a path
    has no UUID-shaped container segment at all (as opposed to a UUID
    that's merely unmapped, which it already returns the raw UUID for,
    correctly distinct from "no container present").

    Verified directly: a synthetic real app container (a genuine UUID-
    shaped path segment, mapped via `guid_to_bundle`), a real SMS
    attachment, and two paths with no container segment at all
    (`mobile/Library/Caches/…`, `private/var/db/…`) correctly split into
    "Third-Party Apps," "SMS Attachments," and "Other" respectively — the
    real bug an earlier version of this test caught first, before fixing
    the test itself: a UUID-shaped test path is required to exercise the
    "resolves to a real container" branch at all, since `_bundle_id_for_
    path` matches on a real UUID pattern, not an arbitrary string. A
    regression case with only genuinely-categorizable archives confirmed
    no spurious "Other" group appears on iOS either.
    *Source: user's own direct follow-up question asking whether the
    Android fix above also applies to iOS, 2026-09-13 — not from any of
    the four sibling tools.*

    **Timestamp-mode and header-scan-tier banners merged onto one row,
    same day, per direct instruction not to waste vertical screen
    space.** Previously two full-width stacked `QLabel`s (one per
    `layout.addWidget` call in `FastZipBrowser.__init__`). `_setup_
    timestamp_banner` (`app/timestamp_display.py`) changed from adding
    itself directly to a passed-in layout to just building and returning
    the widget — the only caller now places it in a shared `QHBoxLayout`
    alongside the header-scan banner, with a small `"|"` separator label
    between them, rather than each getting its own line. Colour remains
    the only thing distinguishing the two (timestamp: orange; header
    scan: `dialog_helpers.ACTIVE_COLOR` blue) — unchanged from before,
    still enough to tell them apart at a glance since they now sit side
    by side rather than stacked. Both banners' own refresh methods
    (`_refresh_timestamp_mode_indicator`/`_refresh_header_scan_indicator`)
    needed no change at all — they only ever call `setText`/`setVisible`
    on the label objects directly, unaffected by which layout contains
    them.

    Verified directly: built the two banner widgets via the real
    `TimestampDisplayMixin._setup_timestamp_banner()` and combined them
    in a `QHBoxLayout` the same way `FastZipBrowser.__init__` now does,
    confirmed both share the same parent row and remain independently
    settable/visible (setting one's text/visibility has no effect on the
    other) — not yet click-tested in the live running GUI itself for the
    actual pixel layout, but the underlying widget wiring is confirmed
    correct.
    *Source: user's own direct instruction on screen-space efficiency,
    2026-09-13 — not from any of the four sibling tools.*

22. **[NOT STARTED, 2026-09-14] mmap + threaded bulk reads for the main
    archive's data path — a genuine further speedup for many-files-at-once
    operations (Tier 2/3 header scans, full-archive keyword search, large
    exports), not the single-file case (one hex click, one SQL-record
    interpret), which is already fast enough to be imperceptible.**
    Prompted by a direct user question ("is there any way we can be
    faster?") after the project-wide zipfile-elimination sweep (item 1,
    above) was itself verified against real archives. Measured directly
    against the real IOS17 JoshHickman archive (34GB, 20,000 real STORED
    entries sampled), not estimated:

    | Method | Time | Throughput |
    |---|---|---|
    | Current (`seek()`+`read()`, single thread) | 1.52s (cold) / 0.28s (warm page cache) | 13,167 – 72,603 files/sec |
    | `mmap`, single thread | 0.20s | 101,545 files/sec |
    | `mmap` + 4 threads combined | **0.029s** | **678,046 files/sec** |

    Two independent, stacked wins: (1) `mmap` turns a read into a plain
    memory-address slice instead of a `seek()`+`read()` syscall pair each
    with its own context-switch cost — roughly a 7-10x win alone at bulk
    scale; (2) these are blocking I/O calls, which release Python's GIL,
    so a `ThreadPoolExecutor` genuinely parallelizes across cores here
    (unlike CPU-bound work) — re-verified the plain single-thread baseline
    a second time to rule out "the gap is just a warmer page cache," not
    just threading's own contribution.

    Real trade-off, not a drop-in swap: a shared `mmap` object needs real
    lifecycle management across worker threads (opened once per archive
    session, kept alive, closed on case-close — not per-call), the whole
    read path through it needs a thread-safety pass, and `mmap` behaves
    slightly differently on Windows (this project ships a frozen Windows
    exe per CLAUDE.md's Build/CI section) — worth confirming there before
    trusting it, the same caution already applied to other platform-
    sensitive additions (e.g. QtWebEngine's own frozen-build gap, still
    flagged unverified in CLAUDE.md's webpage-kind entry).
    *Source: direct user question, 2026-09-14, prompted by this session's
    own real-archive verification of item 1 — not from any of the four
    sibling tools.*

23. **[DONE, 2026-09-14] Media Browser: double-click a thumbnail to open
    the full-size image/video viewer, follows selection while open.**
    Direct request: "can you make it that a user can double click on a
    file and the video/full image opens and then if you move through the
    media files you can see the new file in the viewer." The underlying
    dialog (`MediaFullViewDialog`) already existed — built 2026-09-02 for
    Artifact Report `media_fields` columns, including the exact
    "follows selection while open" behavior this request asked for — so
    this is a second real call site reusing it, not a new feature built
    from scratch.

    **A real architectural fix was needed first, not just a new call
    site**: `MediaFullViewDialog` lived in `artifact_media.py`, which
    already imports `sniff_media_kind` FROM `media_viewer.py` — so having
    `media_viewer.py` import `MediaFullViewDialog` back FROM
    `artifact_media.py` would have been a genuine circular import (fragile
    even where Python's partial-module-initialization might happen to
    paper over it, not something to rely on). Fixed by relocating the
    whole class to `media_viewer.py` instead — the more fundamental module
    or the two, with `artifact_media.py`/`artifact_viewer.py` updated to
    import it from its new home. Confirmed nothing else in
    `artifact_media.py` referenced it internally (only the moved class's
    own recursive `_clear_layout` self-call and a docstring mention)
    before moving it, and swept both files' own import lists afterward —
    `shutil`/`tempfile`/`QDialog`/`QHBoxLayout`/`QPushButton`/`QSlider`/
    `QTextEdit`/`QFontDatabase`/`note_label`/`QLabel`/`QScrollArea`/
    `QVBoxLayout` were all genuinely unused in `artifact_media.py` once
    the class left (confirmed via a grep-count pass, not assumed), removed
    rather than left as dead imports.

    `ClickableThumb` (the existing thumbnail-container widget) gained a
    `doubleClicked` signal alongside its existing `clicked` one — Qt
    delivers a `mousePressEvent` for both halves of a double-click, so
    `clicked` (ordinary selection) always fires first, `doubleClicked`
    (open the viewer) second, never the reverse. `_on_thumb_double_clicked`
    mirrors `artifact_viewer.py`'s own `_on_art_report_double_clicked`
    exactly: non-modal `.show()`, tracked on `self._media_full_dialog`, a
    second double-click while one's already open reuses it
    (`load_content` + raise) rather than stacking a new window.
    `_on_thumb_clicked` (the existing single-click selection handler)
    gained one new line, `_media_sync_open_dialog(ui_path)` — the actual
    answer to "move through the media files, see the new file in the
    viewer": if the dialog is currently open, a plain click on a different
    thumbnail swaps its content to follow, exactly mirroring
    `artifact_viewer.py`'s own `_art_sync_open_media_dialog`. Byte reads
    go through the existing shared `self._read_zip_bytes` (`hex_viewer.py`,
    cross-mixin — `MediaViewerMixin` and `HexViewerMixin` are both mixed
    into the same `FastZipBrowser`), the same already-sanctioned
    `.zcd`/`ZipEntry` path every other reader in this project uses — no
    new byte-reading logic needed.

    Verified end-to-end against the real IOS17 JoshHickman archive, not
    just compiled: driving the actual `FastZipBrowser` in-process (same
    technique this project's other sessions have used for headless GUI
    verification) — double-clicking a real `IMG_0001.HEIC` opened the
    dialog with the correct title and visibility; single-clicking a
    DIFFERENT real thumbnail (`IMG_0001.MOV`, a real HEVC video — Qt's own
    QMediaPlayer log confirmed it actually started decoding/playing,
    proving both this feature AND the underlying video pipeline work
    together end-to-end) while the dialog stayed open correctly swapped
    its content and title to the new file; closing the dialog correctly
    cleared `self._media_full_dialog` back to `None`. All touched files
    (`media_viewer.py`, `artifact_media.py`, `artifact_viewer.py`,
    `ffs-explorer.py`) compile clean.
    *Source: direct user request, 2026-09-14 — not from any of the four
    sibling tools; the reused `MediaFullViewDialog`/"follows selection"
    pattern itself was originally built for `artifact_viewer.py`'s own
    Report table, also per direct user request, 2026-09-02.*

24. **[FIXED, 2026-09-14] Regression: EVERY artifact parser run (including
    the Photos.sqlite quick-process offer, AND the main "Run Artifact
    Parsers" dialog — both share `ArtifactRunnerWorker`) hung forever,
    silently.** Reported directly: "went to the DCIM folder on an iPhone
    and it asked if I wanted to process the photo.sql. I said yes but it
    is not completing."

    Root cause, found by reading `ArtifactRunnerWorker.run()` directly
    rather than guessing: this session's own earlier zipfile-elimination
    sweep (item 1, above) replaced this method's `zip_obj = zipfile.
    ZipFile(...)` with `zip_obj = CachedZipView(...)`, but missed that its
    own `finally:` block still called `zip_obj.close()` — a method
    `CachedZipView` deliberately doesn't have (it holds no real handle of
    its own; every other site touched by that same sweep already got this
    right, e.g. `ffs-explorer.py`'s own `_zip_handle` cleanup, which
    correctly guards with `isinstance(..., zipfile.ZipFile)` first — this
    was the one site that slipped through). The resulting `AttributeError`
    fired inside `finally`, AFTER the method's own `except Exception:`
    block had already run and handled — so nothing caught it; it
    propagated straight out of `run()` uncaught. A QThread's own unhandled
    exception doesn't crash the app or show any error — it just silently
    ends the thread — meaning `self.done.emit()`, two lines below the
    `finally` block, was NEVER reached, on literally every single parser
    run since that fix landed. Every caller waiting on `done` (the Photos
    quick-process offer's non-cancelable `QProgressDialog`, and the main
    "Run Artifact Parsers" dialog) hung forever with zero error shown —
    exactly matching the user's report, and a genuinely bigger blast
    radius than just the one feature they happened to hit first.

    Fixed by removing the stray `zip_obj.close()` call (matching the
    pattern already correct everywhere else this session's sweep touched).
    Verified against real data, not just reasoned through: drove the real
    `FastZipBrowser` in-process against the real IOS17 JoshHickman archive,
    constructed the exact same `ArtifactRunnerWorker` the Photos-quick-
    process offer uses, and confirmed `done` now fires (6.3s), with the
    real log output showing `567 rows written` — matching this exact
    case's own already-documented real Photos Metadata row count exactly,
    confirming this is a correctness fix, not just "stops hanging."
    *Source: direct user bug report, 2026-09-14 — the bug itself was
    self-inflicted by this session's own earlier work (item 1), not from
    any of the four sibling tools.*

25. **[FIXED, 2026-09-15] Real beach-ball on case load — found and fixed
    the actual cause, not the one guessed at.** Direct report: "is there
    no way to stop the spinning ball when loading a ffs... it seems to
    relate to loading the massive tree into the gui... is there no way to
    have this as a background thread?" Investigated directly rather than
    assumed: the folder tree itself was already correctly architected
    (lazy `QTreeView`/`QStandardItemModel`, only top-level children built
    at load time, chunked via `QTimer.singleShot` frame-budget batching in
    `_populate_tree_children_batched`) — not the real cause at all.

    Profiled a real reopen of the 830,298-entry IOS17 JoshHickman archive
    with cProfile and found the actual bottleneck: `on_metadata_ready`
    (the main-thread slot that fires the instant metadata arrives) was
    blocking for **4.1–4.4 seconds**, 90% of it one call —
    `zip_cd_cache.load()` re-parsing the ENTIRE central directory through
    `zipfile.ZipFile()` from scratch, synchronously, on the GUI thread.

    User asked the sharper follow-up directly: "do we not already have the
    data as part of the cache we have created using the sidecar" — correct,
    and it exposed a real, systemic gap: the `.zcd` sidecar already avoids
    re-fetching the central directory over the network, but `load()` had
    **zero memoization of its own PARSED result** — every one of the 17+
    independent call sites across this project (every keyword search,
    every "Interpret as SQL Record" click, every media thumbnail load with
    a fresh case_dir, every nested-archive extraction, every single-file
    scan, ...) re-paid this same ~1.6–3.9s parse from scratch, every time,
    for the life of a session.

    Two fixes, both verified against real data:
    1. **`zip_cd_cache.load()` now memoizes its own parsed `ZipInfo` list
       in-memory**, keyed by the real `.zcd` file path (never bare
       `zip_path` — the same archive can legitimately be open under two
       different case_dirs in one session, each with its own `.zcd`, and
       keying by zip_path alone would silently cross-contaminate them),
       invalidated by the `.zcd` file's own `(mtime, size)` so a rebuilt
       cache is never served stale. Verified directly: 1st call 1.6s,
       2nd/3rd calls 0.0000s (same object returned); after touching the
       real `.zcd` file's mtime, correctly re-parsed a fresh object
       rather than serving stale data.
    2. **`ffs-explorer.py`'s own `on_metadata_ready` no longer builds
       `self._zip_handle` synchronously** — submitted to the existing
       `_BG_POOL` background thread pool instead (via a new module-level
       `_build_cached_zip_view` helper), reusing `_get_zip_handle()`'s
       own ALREADY-EXISTING `self._zip_open_future` lazy-fallback
       mechanism (a dormant code path from before an earlier session's
       zipfile-elimination sweep removed its previous raw-zipfile use —
       confirmed nothing else in the codebase reads `self._zip_handle`
       directly, bypassing that getter, before reusing it).

    Verified end-to-end against real data, not assumed: `on_metadata_ready`
    itself dropped from 4.1–4.4s to a consistent ~1.6–1.9s across three
    fresh-process runs; `_get_zip_handle()` called after load correctly
    returned a working, fully-populated handle (`.getinfo()` succeeded on
    a real file) with the background future already resolved. Sub-step
    timing then isolated exactly what's LEFT: `_art_select_and_show_apps`
    (the Apps-tree-node default-view population, a supposedly-fast
    "cache hit" path per its own existing design) now accounts for ~90%
    of the remaining time (0.6–1.0s) — a separate, smaller, well-scoped
    follow-up, not yet addressed; every other sub-step measured
    (`_detect_android_user_data`, `_detect_time_columns`,
    `_inject_nested_archives`, `reload_tree_entirely`,
    `_refresh_artifact_tab`, `_start_case_meta_load`) is sub-30ms and not
    worth chasing further.
    *Source: direct user report and direct user follow-up question,
    2026-09-15 — found and fixed via this session's own direct profiling,
    not from any of the four sibling tools.*

26. **[DONE, 2026-09-15] Converted the hardcoded "Apps" tree node into two
    normal, manually-run parser scripts — a real architectural change,
    not just a move.** Direct request: "can we move the app table to be
    an artifact script instead of where it is just now... this will
    require the tree to be change to not have apps as the top root
    note... also make an android and ios specific artifact script for the
    application report, since they have different data... run from that
    dialog and not when the case is loaded." Confirmed with the user
    before implementing that per-app GROUPING (Chrome's sub-reports
    nesting under one "Chrome" node) should stay — only the fixed "Apps"
    PARENT container that used to wrap every report goes away.

    Real blockers found via direct research before writing any code, not
    assumed away: no existing parser API supported whole-device scope (every
    parser is scoped to one app's own `files`), and `mcp_server.CaseContext`
    (which already has the exact shape `app_intelligence.scan_apps()`
    needs) turned out to be a LIVE bridge from the GUI's own in-memory
    state, not something a headless parser script could construct itself
    — a real correction to an initial hope that it could.

    **New `device_wide` parser capability** (`artifact_runner.py`): a
    parser declares `device_wide = True` instead of `app_path`/`files`;
    `run_artifact()` builds a `paths['_case_context']` (a real
    `mcp_server.CaseContext`) loaded fresh from the case's own persisted
    load-snapshot (`ffs_metadata.load_snapshot_from_case`, a new function
    extracting the exact same read `_try_load_from_snapshot` already did
    for instant re-opens) plus `folder_sizes`/`guid_to_bundle` from
    `casecache.db` — never the live GUI window object, keeping a
    device-wide parser exactly as headless/testable as every other one.
    `raw_content_enabled=True` always (a direct, examiner-triggered run
    against their own already-open case, same reasoning the GUI's former
    Apps view already used, no AI-consent boundary to protect).

    Two real, non-obvious bugs were caught by direct testing before this
    shipped, not assumed correct from the design alone:
    - `_make_zip_byte_reader`'s reserved `_read_zip_bytes` key takes a
      PHYSICAL zip entry name, but `CaseContext.read_bytes` (per its own
      docstring and `scan_apps()`'s real usage) is called with a UI_PATH
      that the callee must resolve itself — passing the physical-path
      reader straight through would have silently failed every single
      read. Fixed with a wrapper doing `adapter.resolve(ui_path)` first.
    - The very first real test returned 0 apps. Root cause: `scan_apps()`
      resolves every GUID-named container to its real app via
      `container_bundle_id(child, guid_map)` — an empty/wrong
      `guid_to_bundle` silently makes every container resolve to
      nothing, not an error. Fixed by having `_build_case_context` load
      `guid_to_bundle` fresh from `casecache.db` itself
      (`db_utils.load_guid_bundle_map`) rather than trusting the
      parameter threaded through from the caller — makes the whole
      context construction self-sufficient from `case_dir` alone, not
      dependent on every future caller getting this right.

    **Two new parser scripts**, `artifacts/ios/app_report.py` and
    `artifacts/android/app_report.py` — thin wrappers matching every
    other parser's shape, calling `app_intelligence.scan_apps(ctx)` then
    two new Qt-free functions moved from what used to be
    `artifact_viewer.py`'s own GUI-only flattening
    (`build_app_registry_lookup`/`flatten_row` in `app_intelligence.py`,
    ported behavior-for-behavior, just producing a normal snake_case row
    dict instead of the old GUI's positional tuple + Title-Case header
    list). The iOS parser's own real, useful documentation about the four
    timestamp columns' meaning and limits (fabricated far-future mtimes
    from third-party disk-cache libraries, etc.) — previously a special
    hand-authored "Application Report Notes" tree page — was preserved by
    moving it into the parser's own `description`, which every report
    already shows via the standard "Report Notes/Warning" page, no
    special-casing needed.

    **New `byte_fields` declarative convention** (`_art_show_report`,
    matching the existing `timestamp_fields`/`media_fields` shape) —
    generalizes what used to be a one-off manual `set_byte_columns(["Total
    Size"])` call hardcoded to the old Apps table into something any
    report can declare, for a raw byte-count column that should MB-format
    at display time while staying numerically sortable in storage.

    **Tree restructuring**: `_refresh_artifact_tab`'s build loop now
    appends standalone reports and app-grouped report nodes directly to
    the tree root instead of to a fixed "Apps" parent (which no longer
    exists) — per-app grouping itself (`app_group_label`) is completely
    unchanged. `ffs-explorer.py`'s `on_metadata_ready` no longer force-
    selects anything at case load — a freshly opened case now starts on
    the blank "Select a Report or Script..." placeholder, exactly
    answering the literal request ("run from that dialog and not when the
    case is loaded"). Confirmed via direct research (not assumed) that
    the "Jump to this row in the report" feature's own tree search
    already used a generic depth-first role search with no position-0
    dependency, and that MCP's `list_apps` tool is fully independent of
    this GUI tree (builds its own CaseContext, shares only the
    `casecache.db` cache table as a pure cache) — neither needed any
    change.

    A large amount of now-genuinely-dead code was removed outright rather
    than left as unused: `AppIntelligenceWorker` (the GUI's own
    QThread wrapper — MCP's `list_apps` never used it, it has its own
    complete cache read/write cycle), `_art_show_apps`/
    `_populate_apps_table`/`_art_select_and_show_apps`/
    `_retire_art_apps_worker`/`_on_art_apps_scan_done`, the entire
    Category-checklist + Date-range filter UI (`_art_category_filter_*`/
    `_art_date_filter_*`/`_apply_apps_combined_filter` and ~9 related
    methods) which was Apps-table-only special UI, not a generic report
    feature — this is a real, acknowledged loss of a previously-requested
    convenience feature (triage-by-category on a ~1000-row table), openly
    flagged to the user rather than silently dropped; picking it back up
    as a new declarative convention (if missed) would be a separate,
    future ask.

    **Verified end-to-end against real data at every layer, not assumed
    correct from the design alone**: `_build_case_context` tested
    directly (830,291 ui_metadata entries, 179,273 folder_map entries,
    correct real HEIC bytes read via the ui_path-resolving wrapper);
    `scan_apps()`+flattening tested directly (1,096 real apps on the
    IOS17 JoshHickman case, including correct real WhatsApp/Instagram/
    TikTok rows with real container paths, plugins, sizes, timestamps);
    the Android script tested against a real Android archive (197 real
    apps, e.g. a real 1.86GB WeChat container). Then the FULL real
    pipeline end-to-end, driving the actual `FastZipBrowser` in-process:
    a freshly loaded case correctly shows no "Apps" node anywhere and
    stays on the blank placeholder (not auto-populating); running the new
    parser via the real `ArtifactRunnerWorker` (the exact same class the
    "Run Artifact Parsers" dialog uses) wrote 1,096 real rows to
    `caseresults.db`; the refreshed tree correctly shows "App Report" as
    a normal standalone top-level item; `_art_show_report('app_report')`
    correctly displayed all 1,096 rows via the standard DB-mode path with
    `byte_fields` MB-formatting working ("564.08 MB" for a real value).
    Separately confirmed on a real Android case that app-grouped reports
    (Chrome's 3 sub-reports under one "Chrome" node) still nest correctly
    — the one thing explicitly required to be preserved.
    *Source: direct user request, 2026-09-15 — not from any of the four
    sibling tools.*

27. **[DONE, 2026-09-15] `app_report` now flags itself stale when a
    DIFFERENT parser is added/updated, reusing the existing "newer
    parser version available" banner.** Direct follow-up to item 26:
    "can we just make it like when a parser is updated that information
    is at the top of the parser and it can be rerun — can we also have
    that for the app report, i.e. if a new artifact script is added the
    script can see that and it ask the user if they want to rerun." The
    existing banner mechanism (`parser_versions.py`/
    `_update_art_version_banner`) only ever detects a script's OWN
    content changing — it has no way to notice that a DIFFERENT script
    was added or updated, which is exactly the case that makes
    `app_report`'s own `has_parser`/`score`/`category` columns stale
    (they're derived from which OTHER parsers exist at scan time, per
    `app_intelligence.resolve_parser_coverage`).

    New `parser_versions.get_coverage_fingerprint(platform)` — a stable
    blake2b-derived fingerprint over every known parser's own version for
    that platform, changing whenever any parser's content changes, a new
    one is added, or one is removed. Self-caught bug before any testing:
    the first draft used `int.from_bytes(digest, "big")` (unsigned) —
    corrected to `signed=True`, since `run_log`'s `coverage_fingerprint`
    column is SQLite's signed-64-bit INTEGER storage class, and an
    unsigned read of a full 8-byte digest can exceed `2**63-1`
    (`sqlite3`'s own INTEGER binding raises `OverflowError` outright
    rather than truncating).

    `run_log` gained a `coverage_fingerprint INTEGER` column (same
    `ALTER TABLE ... ADD COLUMN` + `except sqlite3.OperationalError: pass`
    migration pattern as `parser_version`/`completed_at`), threaded
    through `start_run_log`/`load_last_run`/`load_run_history`.
    `ArtifactRunnerWorker.run()` computes and records it, but ONLY for a
    `device_wide` module (`getattr(module, 'device_wide', False)`) —
    every other parser's `coverage_fingerprint` stays `NULL`, since only
    a device-wide parser's output actually depends on the full parser
    set. Relies on the same precondition `get_current_version`'s own
    callers already rely on — `list_artifacts(platform)`/`load_artifacts`
    must have already run this session (it calls `check_version()` for
    every script it loads) so the store reflects every parser's current
    version; true by construction here, since `ArtifactRunnerDialog`
    already calls `load_artifacts` to build its own selection list before
    a worker can run at all.

    `_update_art_version_banner` (`artifact_viewer.py`) restructured
    (not just extended) to check both signals in one place: the existing
    own-script-version comparison first (unchanged behavior/wording for
    every parser), falling through — only for a `device_wide` module
    whose `used_fingerprint` was actually recorded — to a coverage-
    fingerprint comparison with its own, differently-worded banner text
    ("A parser script has been added or updated since this report last
    ran — has_parser/score/category may be out of date."), since the
    reason is genuinely different (a DIFFERENT parser changed, not this
    one) and conflating the two messages would mislead about what
    actually needs re-running. Reuses the SAME banner widget and Update
    button (`_on_art_update_parser_version`) with zero changes needed
    there — clicking Update re-runs `app_report` via the same
    `ArtifactRunnerWorker` path either way, which then records a fresh,
    current `coverage_fingerprint`, clearing the banner.

    Verified end-to-end against the real IOS17 JoshHickman case DB
    (`case_data/IOS17JoshHickman`), not just compiled — reproduced the
    exact `_update_art_version_banner` decision logic standalone against
    three real inserted `run_log` rows: (1) matching parser version, a
    coverage fingerprint deliberately offset from the current real one →
    correctly decided "coverage stale"; (2) matching version, matching
    real fingerprint → correctly decided "fp matches", no banner; (3) a
    non-`device_wide` parser (`whatsapp`) with `coverage_fingerprint=NULL`
    and a matching own version → correctly decided "not device_wide", no
    banner, confirming zero effect on every ordinary parser. All three
    test rows removed from the real case DB afterward — this was a
    verification harness, not left as test pollution. Not yet exercised
    through the actual running GUI (no way to drive this session's own
    PySide6 window) — the underlying decision logic and DB round-trip are
    both confirmed correct against real data; the click-through itself
    (open App Report, add a var/change a sibling parser, see the banner,
    click Update, see it clear) is the one remaining unverified step.
    *Source: direct user request, 2026-09-15.*

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
