# FFS Explorer — Ideas

Ideas worth building that aren't scheduled yet. Each one says why, what we
already know, a sketch, and what's still open. Nothing here is a decision:
once an idea is agreed and built, its decisions move to `ASSUMPTIONS.md`.
(`TODO.md` is a separate list, drawn from the sibling-project reviews.)

---

## 1. Warn when a parser reads a file that has an earlier capture

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
- If any has an earlier capture, show a warning with the parser's results,
  e.g. "An earlier capture of `Envelope Index-wal` exists (capture 1 of 2).
  You may wish to review it in the SQLite viewer." Clicking the file name
  jumps to it in the File Browser.
- Once idea 2 exists, use its verdict to say how much it matters (see the
  verdicts there), and stay quiet when the copies are identical.

**Open.** Where the warning lives: the Artifact Viewer report header, the
parser run log, or both.

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
