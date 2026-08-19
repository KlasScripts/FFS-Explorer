# Writing Your Own Artifact Parser

This is a how-to for examiners who want FFS Explorer to parse an app it
doesn't support yet. It assumes no Python experience beyond reading the
examples below — the format is deliberately small.

## What a parser actually is

A parser is one `.py` file dropped into `artifacts/ios/` or
`artifacts/android/`. It declares which files it needs from the app's own
folder, and a `run()` function that turns those files into rows. FFS
Explorer extracts the files, calls `run()`, and shows the result as a
Report in the Artifacts tab — nothing more.

A parser never touches the archive itself (read-only evidence, always),
never imports anything from this project's `app/` code, and never talks to
the network. It only reads the file paths it's handed and returns
`list[dict]`. That's what "self-contained" means here: you could copy the
one `.py` file to someone else's copy of this app and it would work.

## The fast way: ask the app to draft one

FFS Explorer has a built-in AI-access feature (Tools → Enable AI Access)
that lets an AI assistant read the case's file structure and database
schemas — never raw evidence content unless you separately opt in — and
draft a parser for you. With raw-content access enabled, ask it to
`build_artifact_parser` for the app you want; it will:

1. Find the app's container and its database file(s).
2. Read the schema and sample real rows (never a full dump).
3. Draft a `.py` file matching the format below, as a code block for you
   to review.

**It never installs or runs the draft itself.** You save the file into
`artifacts/ios/` or `artifacts/android/` yourself, after reading it. Treat
an AI-drafted parser exactly like one written by a colleague — verify it
against real data before trusting its output (see Validating below).

The rest of this document explains the format directly, for writing one by
hand or reviewing a drafted one.

## The format

```python
name = "My App Messages"

# Android: the app's private data folder.
app_path = "data/data/com.example.myapp"

# iOS third-party apps sharing data via an App Group use app_group instead
# — the container is a random GUID per install, resolved automatically.
# Use app_path OR app_group, never both.
# app_group = "group.com.example.myapp.shared"

files = {
    "main_db": "databases/messages.db",
}
optional_files = {
    "main_db_wal": "databases/messages.db-wal",
    "main_db_shm": "databases/messages.db-shm",
}

def run(paths):
    import sqlite3
    conn = sqlite3.connect(paths["main_db"])
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT id, sender, body, sent_at FROM messages").fetchall()
    conn.close()

    return [
        {
            "sender": r["sender"],
            "message": r["body"],
            "timestamp": r["sent_at"],
            "raw_message_id": r["id"],
        }
        for r in rows
    ]
```

That's a complete, working parser. Everything else below is optional
polish that makes the report better — add it when it applies, skip what
doesn't.

### `files` / `optional_files`

`files` are required — if one is missing from the archive, the parser
doesn't run and FFS Explorer says why. `optional_files` are extracted when
present but never block the run — always list a database's `-wal`/`-shm`
sidecars here, since a database in WAL mode can have its newest rows only
in the `-wal` file (never in the main file).

### `run(paths)`

`paths` is `{key: extracted_file_path}` for everything you declared, plus
one reserved key you didn't: `paths['_app_base_ui_path']` — the app's
container path inside the archive, for when a database column stores a
path to some *other* file (an attachment, a photo) that you want to
reference rather than extract yourself. See `media_fields` below.

`run()` must return `list[dict]`. Every dict in the list should have the
same keys — different key sets between rows means the Report table gets
ragged columns. Always raw values, never a Python string you built with an
f-string — that's what `timestamp_fields` and `media_fields` (below) are
for; the display layer formats them at view time, so the same raw data
still works correctly however the examiner has timestamps configured.

### `description` (recommended)

A one-paragraph string explaining where the data comes from, what's
verified vs. inferred, and any known gap. Shows in the Report's "Notes"
page. Write it as if a different examiner has to trust your report without
reading your code — because that's exactly the situation.

### `timestamp_fields` (recommended if there's a timestamp)

```python
timestamp_fields = {"timestamp": "s"}
```

`{field_name: unit_code}` — unit codes are `"s"` (Unix seconds), `"ms"`
(Unix milliseconds), `"cocoa_s"` (Cocoa/Mac epoch seconds — iOS databases),
or `"cocoa_ns"` (Cocoa epoch nanoseconds — used by iOS's `message` table
specifically). The Report formats the raw value per the case's UTC /
handset / acquisition / manual timestamp-display setting — never bake a
formatted string into `run()`'s output. **Never use SQLite's `'localtime'`
modifier or a bare `datetime.fromtimestamp(x)`** — both silently convert
using the *analysis machine's* timezone, not the evidence's, and can look
correct by pure coincidence depending on time of year. This has been a
real, shipped bug more than once in this project.

### `media_fields` (if the app has photos/video/attachments)

```python
media_fields = ["attachment_path"]
```

Names the output field(s) holding a full archive **ui_path** (not a raw
database value, not a filesystem path) to a media file. The Report table
then shows a thumbnail there, with double-click opening it full-size or
playing it. Build the ui_path in `run()`:

```python
attachment_path = f"{paths['_app_base_ui_path']}/{row['media_column']}"
```

**Check what the database column actually points at before assuming it's
a usable local path** — some apps store a remote URL (nothing local to
show), or a `content://` URI (meaningless outside a live device). Both are
real, common cases — WhatsApp, Google Messages and GroupMe all needed
different handling here on this project. And the path segment the app
gives you is sometimes not the *complete* relative path — WhatsApp iOS's
database column is missing a `Message/` folder segment that has to be
added by hand. **Verify the constructed path actually resolves to a real
file in the archive before trusting it** — use `find_paths` (via the MCP
tools, or the file browser's search) on a real example filename first.

### `recoverable_tables` (if you've checked for deleted content)

```python
recoverable_tables = ["messages"]
```

Declares a table to also carve for deleted rows (freeblocks, freed pages,
WAL history). No recovery code belongs in the parser — this declaration is
the entire contribution; the shared carving pipeline does the work and
appends whatever it finds to your `run()` output. Only add this once
you've actually checked (a real negative — "no WAL, checked, found
nothing" — is still worth declaring, so a future re-check on different
data finds it automatically instead of needing another one-off
investigation).

## Validating what you wrote

Every parser here was built against one specific test image (a known
device + app version) and its output cross-checked against documented
ground truth before being trusted. Do the same for yours:

1. **Run it against a real archive and read the actual rows** — not just
   "the code compiles." Check a join didn't silently drop rows (compare
   `SELECT COUNT(*)` on the raw table against your output count), and that
   an unresolved foreign key shows up as `"[no chat record — raw
   chat_id=...]"` rather than a blank field or a dropped row.
2. **Record a validation baseline** (Artifacts tab → your parser →
   Validation → "Record This Case as Validation Baseline") **only** against
   a case you know is the GTD/known-version image you built the parser
   against — never against real casework. This snapshots the database
   schema and the app folder's structure, so a *future* run against a
   different app version will show you exactly what changed (a new table,
   a renamed column, a new database file), instead of the parser silently
   going stale.
3. Grep your own diff for `fromtimestamp(` and `'localtime'` before
   calling it done — see the timestamp warning above.

## Where the real detail lives

This document is the quick-start. `CLAUDE.md`'s Conventions section is the
canonical reference for every convention mentioned above (and a few more:
`recovery_field_notes`, `warning`, the raw-id-preservation rule) with the
full reasoning and real examples behind each one — read it before writing
anything nontrivial, and definitely before disagreeing with something
above.
