# FFS Explorer — Ideas

Ideas worth building that aren't scheduled yet. Each one says why, what we
already know, a sketch, and what's still open. Nothing here is a decision:
once an idea is agreed and built, its decisions move to `ASSUMPTIONS.md`.
(`TODO.md` is a separate list, drawn from the sibling-project reviews.)

---

## 1. Warn when a parser reads a file that has an earlier capture

**Built 2026-10-21** (`artifact_viewer._earlier_capture_warnings`); the
decision is recorded in ASSUMPTIONS.md A-72. Kept here until idea 2 can
set how loud the warning is.

**Why.** GrayKey can store a path twice (ASSUMPTIONS.md A-72). Parsers read
only the latest copy. The examiner should know when an earlier copy of a
file a parser used exists, and decide whether to look at it.

**Direction (owner, 2026-10-20):** a warning, not an automatic second parser
run. Re-running parsers on earlier captures would double the results to
review, and in the one real case checked the earlier copy held nothing new
(see idea 2).

**Sketch.**
- After a parser runs, check each file it read (database, `-wal`, `-shm`,
  `-journal`) for a "capture" value in the metadata.
- If any has an earlier capture, write a warning line to the **parser run
  log** (the log pane of the Run Artifact Parsers dialog, fed by the
  runner worker's `log` signal), e.g. "WARNING: an earlier capture of
  `Envelope Index-wal` exists (capture 1 of 2). You may wish to review it
  in the SQLite viewer."
- Once idea 2 exists, use its verdict to say how much it matters (see the
  verdicts there), and stay quiet when the copies are identical.

**Decided (owner, 2026-10-20):** the warning goes in the parser run log
only, not in the Artifact Viewer report.

---

## 2. Compare captures: is the earlier copy worth reviewing?

**Why.** When a database was captured twice, the examiner needs to know
whether the earlier copy holds anything the later one doesn't. Checked by
hand for IOS18's Mail `Envelope Index` on 2026-10-20; this idea automates
those steps.

**What the manual check found (IOS18, Mail `Envelope Index`):**
- The database files were identical (same SHA-256). Only the WAL differed.
- Capture 2's WAL was capture 1's WAL byte for byte, plus 3 frames (one
  transaction changing pages 4, 133 and 134). Same salts, same checkpoint
  number: the log was never reset.
- Row-by-row comparison of all 42 tables: 2 differed. `mailboxes` row 1,
  `sync_info.lastSyncDate` moved 54 s (2024-11-20 20:07:15 → 20:08:09
  UTC), and `properties.WriteTransactionGeneration` went from 2770 to
  2771. Mail synced during acquisition; no message data changed.
- Verdict: capture 1 adds nothing. Even deleted-data recovery from it is
  covered, because capture 2's WAL contains all of capture 1's frames.
- On the same archive, 84 of the other 85 repeated files had identical
  hashes in both copies.

**Sketch: the checks, cheapest first, stopping at the first that decides.**

| Check | How | Verdict if it decides |
|---|---|---|
| Same content | Compare the recorded SHA-256 (GrayKey, A-73) or CRC + size. No reads. | **Identical**: nothing to review |
| Not a database | No SQLite header or sidecar | **Different content**: review it |
| WAL grew | Same database bytes, same WAL salts and checkpoint number, and the earlier WAL is a byte prefix of the later one | **Later copy contains it**: earlier copy adds nothing, even for deleted-data recovery |
| WAL reset | Salts or checkpoint number changed between the two WALs | **Earlier copy may hold unique data**: its WAL frames may have been checkpointed and overwritten. Review it, and carve its WAL |
| Database changed | Database bytes differ | **Earlier copy may hold unique data**: free pages and freelist content can differ |
| Row comparison | Open both states from scratch copies and compare every table by rowid (as done by hand) | Lists added, removed and changed rows per table, so the examiner sees exactly what differs |

- Show the result as a short report beside the files (the "Capture" column
  could show the verdict), and feed it into idea 1's warning.
- Only the "WAL reset" and "database changed" verdicts need the examiner;
  the others can be shown quietly.

**Rules it must follow.**
- Copy the raw bytes out first; only ever open copies in a temp folder.
  Opening a database in SQLite can checkpoint its WAL and destroy deleted
  data (project rule on SQLite evidence).
- Pair each database with its own capture's sidecars (A-72), never mix.
- The row comparison is only as complete as SQLite's view. Deleted records
  in free pages need the carving tools as well.

**Open.**
- Run automatically (in Process Case, GrayKey archives only) or on demand
  (right-click "Compare captures")?
- How to show a changed row compactly when a table has many columns.
- Whether the same checks should compare an FFS file with the same file in
  another extraction or backup of the device (e.g. IOS17's separate
  `Backup/` zip).

---

## 3. Don't let hidden columns hide data

**Why.** A case can produce 30 columns in one view (photo, EXIF, hashes,
capture...). Examiners hide columns to cope, and a hidden column with values
in it looks exactly like no data. Today the Columns button shows only how many
are hidden ("Columns ⊘3") and their names, not whether they hold anything.

**Built 2026-10-21 (owner request):** columns with no value for any file in
the current view are hidden automatically, the Columns button says how many
("Columns · 6 empty hidden"), the dialog labels them "(empty here)", and
they're never written into a saved folder layout. Columns with data are
never hidden automatically. The other options below are still parked.

**How columns appear today (from the code).** A column is shown if any file
in the view has the data (photo columns, UUID), or if the case has the data
at all (EXIF, Linked Report, hashes, Capture, Inode). So mixing one DCIM photo
with 100 other files keeps the photo columns, blank for the 100. The
"App" column is always shown; "UUID" only in a folder view of app
containers, never in Selected Files.

**Options, most useful first.**
- In the Columns dialog, show how many files in the current view have a
  value per column ("GPS — 37 of 120 files").
- When a hidden column has values in the current view, the Columns button
  turns to a warning colour, "Columns ⊘2 · data hidden", with a tooltip of
  counts. This also catches saved per-folder layouts hiding data later.
- Default order (Name, Type, Size, Modified, Path first), related columns
  grouped, one-click show/hide per group.

**Check first.** What export and reports do with hidden columns. Hidden must
never mean dropped from an export without asking.

---

## 4. Link every artifact report to the media files it mentions

**Decided (owner, 2026-10-21):** all artifact reports should link to the
files they reference through the File Browser's "Linked Report" column
(ASSUMPTIONS.md A-41), not just iOS SMS, where it was built and checked.

**Where it stands.** Linking is generic: after a parser runs, every row
with a path in a declared `media_fields` column is linked
(`artifact_viewer._register_attachment_links`). So:

| Parsers | `media_fields` | Links checked on real data |
|---|---|---|
| iOS `sms_messages` | yes | yes (one run, 2026-10-12) |
| Android `whatsapp` (message thumbnails inside msgstore.db), `chrome_favicons` (icons inside Favicons), via `embedded_media_path` | yes | yes (A15 CTF25, 2026-10-23: 2,575/2,575 thumbnails, 53/53 icons) |
| iOS `whatsapp`, `photos_metadata`; Android `google_messages`, `whatsapp` (attachment files), `chrome_cache_pages`, `chrome_cache_media`, `chrome_offline_pages` | yes | no |
| The other 30 parsers (iOS 4, Android 26) | none | n/a |

**Work.**
1. Run each of the 8 unchecked parsers on a real case that has the app;
   confirm linked files show the right report row (row id names the same
   record). Fix any that don't.
2. Go through the 30 without `media_fields`: for each, check whether its
   rows name files in the archive (attachments, voice notes, avatars,
   downloads, cached pages). Where they do, build the full ui_path in
   `run()` and declare `media_fields`, checking the database column
   against the real schema first (WRITING_ARTIFACT_PARSERS.md).
3. Mark each parser's status here (or in VERIFICATION_STATUS.md) as it's
   done.
