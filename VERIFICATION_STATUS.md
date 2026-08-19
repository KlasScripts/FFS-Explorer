# Human Verification Status

This codebase has been substantially AI-written. "It runs" and "it passed
a quick check" are not the same claim as "a human read this and confirmed
it does what it's claimed to do" — this file tracks the second, separately
and honestly, one section at a time.

**Nothing here is verified until you say so.** Every row starts 🔴. A row
only moves to 🟢 after you and Claude have actually gone through it
together — Claude explains what a section does and why, in as much depth
as you ask for, you push back on anything that doesn't add up, and it gets
fixed if it doesn't. "Looks fine" is not a note; the note should say what
you actually checked.

## How to use this

1. Pick a row (start anywhere — smaller/self-contained files are an easier
   first pass than `ffs-explorer.py`'s big sections).
2. Say "let's go through `X`" and Claude walks it, section by section if
   it's large, in a live conversation — not just a wall of text dumped at
   once.
3. Ask questions until you're actually confident. Anything that turns out
   wrong or unclear gets fixed or clarified before the row moves to 🟢.
4. Update the row: 🟢, today's date, and a real one-line note (what was
   specifically checked/confirmed, not just "reviewed").
5. **From here on, before editing anything, Claude checks this file.** If
   the change touches a 🟢 row, it says so up front — "this touches
   `_toggle_mcp_server`, which you verified on 2026-08-19" — before making
   the change, so you can decide whether to re-review after. A 🔴/🟡 row
   gets no such warning, since there's nothing yet to protect.
6. A verified row that gets a real functional change should generally
   drop back to 🟡 until re-checked — a verified *old* version of the code
   says nothing about a *new* version of it.

Status: 🔴 Not reviewed · 🟡 In progress / partially reviewed · 🟢 Verified

## Artifact parsers (`artifacts/ios/`, `artifacts/android/`) — highest value to verify first

These directly produce what an examiner cites as evidence. A bug here is
the most consequential kind in this whole codebase.

| Script | Status | Date | Note |
|---|---|---|---|
| `ios/whatsapp.py` | 🔴 | | |
| `ios/sms_messages.py` | 🔴 | | |
| `ios/photos_metadata.py` | 🔴 | | |
| `android/whatsapp.py` | 🔴 | | |
| `android/google_messages.py` | 🔴 | | |
| `android/google_messages_deleted_conversations.py` | 🔴 | | |
| `android/viber.py` | 🔴 | | |
| `android/groupme.py` | 🔴 | | |
| `android/burner.py` | 🔴 | | |
| `android/burner_messageentity.py` | 🔴 | | |
| `android/line.py` | 🔴 | | |
| `android/line_fts.py` | 🔴 | | |

## `app/` modules

Same grouping as CLAUDE.md's module table.

| Module | Status | Date | Note |
|---|---|---|---|
| `adapters/ffs.py` | 🔴 | | |
| `adapters/graykey.py` | 🔴 | | |
| `ffs_metadata.py` | 🔴 | | |
| `db_utils.py` | 🔴 | | |
| `zip_entry.py` / `zip_reader.py` | 🔴 | | |
| `zip_cd_cache.py` | 🔴 | | |
| `header_scan.py` | 🔴 | | |
| `dialog_helpers.py` | 🔴 | | New 2026-08-19 — pure Qt widget-construction helpers (button row, note/error labels, warning/error colors), no case logic. Used by `timestamp_display.py` and swept into `ffs-explorer.py`/`segb_viewer.py`/`artifact_viewer.py` at their genuinely-matching call sites (same button order, same 2-button shape) — sites with a 3rd button, different order, or deferred wiring were deliberately left as their own hand-built rows rather than force-fit |
| `timestamp_display.py` | 🔴 | | Extracted from `ffs-explorer.py` 2026-08-19 (`format_ts`, the mode banner, the Timestamp Display dialog); regression-tested against the pre-extraction behavior (UTC/handset/acquisition/manual formatting incl. a DST winter-vs-summer check, mode text, banner show/hide, full dialog construction against a real case). Further simplified same day using `dialog_helpers` (button row + the acquisition/manual radio+combo duplication factored into one local helper) — regression suite re-run and still passes after that change too. Behavioral equivalence checked by Claude; not yet the same thing as human-verified |
| `device_timezone.py` | 🔴 | | |
| `keyword_search.py` | 🔴 | | |
| `hex_viewer.py` | 🔴 | | |
| `media_viewer.py` | 🔴 | | |
| `sqlite_viewer.py` | 🔴 | | |
| `segb_viewer.py` | 🔴 | | |
| `segb_schemas.py` | 🔴 | | |
| `artifact_runner.py` | 🔴 | | |
| `artifact_db.py` | 🔴 | | |
| `artifact_viewer.py` | 🔴 | | |
| `sqlite_carve.py` | 🔴 | | |
| `artifact_media.py` | 🔴 | | |
| `research_store.py` | 🔴 | | |
| `validation_store.py` / `parser_validation.py` | 🔴 | | |
| `mcp_server.py` | 🔴 | | |
| `mcp_control.py` | 🔴 | | |
| `highlight_delegate.py` | 🔴 | | |

## `ffs-explorer.py` (by section — see CLAUDE.md's section map for what's in each)

| Section | Status | Date | Note |
|---|---|---|---|
| Module-level helpers (prefs, archive formatting, device-info readers, photo flags) | 🔴 | | |
| `ExtractorWorker` | 🔴 | | |
| `ZipMetadataWorker` | 🔴 | | |
| `FileTableModel` + filter proxy | 🔴 | | |
| `ExportProgressDialog` | 🔴 | | |
| Settings/preferences dialogs, small scan workers, integrity check | 🔴 | | |
| `ProcessDialog` | 🔴 | | |
| `ArchiveSelectionDialog` | 🔴 | | |
| `FastZipBrowser.__init__` + column config | 🔴 | | |
| `FastZipBrowser` filtering | 🔴 | | |
| `FastZipBrowser` preview dispatch + tree/table/export | 🔴 | | |
| `FastZipBrowser` jump menu / Android detection | 🔴 | | |
| `FastZipBrowser` loading pipeline (`start_loading`→`on_metadata_ready`) + AI-access toggle | 🔴 | | Timezone detection/dialog moved to `timestamp_display.py` (own row above) 2026-08-19 |
| `FastZipBrowser` folder view refresh / tree population / bookmarks | 🔴 | | |
| `FastZipBrowser` research-status dialog | 🔴 | | |
| `FastZipBrowser` lazy tree expansion, recents, worker lifecycle, `closeEvent` | 🔴 | | |
| Module level: Qt message handler, `__main__` | 🔴 | | |
